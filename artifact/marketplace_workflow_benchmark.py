#!/usr/bin/env python3
"""Marketplace-style admission/query workflow for CertificateAdmit."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path


OUT_JSON = Path(__file__).with_name("marketplace_workflow_benchmark.json")
OUT_CSV = Path(__file__).with_name("marketplace_workflow_benchmark.csv")


DESIGNS = ["AppendOnlyReconcileLog", "SafeMaterializedView", "CertificateAdmitSQLite"]
WORKLOADS = ["audience", "reach", "conversion"]
CLASSES = ["silver", "gold"]


def attempts(n: int) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for i in range(n):
        seller = f"seller-{i % 12:02d}"
        session = f"sid-{i % 24:02d}"
        workload = WORKLOADS[i % len(WORKLOADS)]
        quality = CLASSES[i % len(CLASSES)]
        policy = "policy-v5"
        price_id = f"{quality}-{workload}-v5"
        src = f"src-{i:07d}"
        counter = i
        kind = "valid"
        if i > 0 and i % 37 == 0:
            seller = "seller-00"
            session = "sid-00"
            src = "src-0000000"
            kind = "duplicate_source"
        elif i > 0 and i % 41 == 0:
            seller = "seller-00"
            session = "sid-00"
            counter = 0
            kind = "duplicate_counter"
        elif i % 47 == 13:
            price_id = f"{quality}-{workload}-stale"
            kind = "price_swap"
        elif i % 53 == 17:
            policy = "policy-v4"
            price_id = f"{quality}-{workload}-v4"
            kind = "stale_policy"
        rows.append(
            {
                "rid": i,
                "seller": seller,
                "session": session,
                "src": src,
                "counter": counter,
                "workload": workload,
                "quality": quality,
                "policy": policy,
                "price_id": price_id,
                "window": i // 1000,
                "kind": kind,
            }
        )
    return rows


def setup(conn: sqlite3.Connection, rows: list[dict[str, str | int]]) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE source_tokens(src TEXT PRIMARY KEY, seller TEXT NOT NULL, workload TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE price_policy(policy TEXT NOT NULL, price_id TEXT NOT NULL, workload TEXT NOT NULL, quality TEXT NOT NULL, price REAL NOT NULL, PRIMARY KEY(policy, price_id));
        CREATE TABLE cert_ledger(rid INTEGER PRIMARY KEY, src TEXT, seller TEXT, session TEXT, counter INTEGER, workload TEXT, quality TEXT, policy TEXT, price_id TEXT, window INTEGER, status TEXT);
        CREATE TABLE aggregate_queue(rid INTEGER PRIMARY KEY, src TEXT, seller TEXT, session TEXT, counter INTEGER, workload TEXT, quality TEXT, policy TEXT, price_id TEXT, window INTEGER, y INTEGER);
        CREATE TABLE reject_log(rid INTEGER, seller TEXT, workload TEXT, reason TEXT);
        CREATE INDEX idx_cert_counter ON cert_ledger(seller, session, counter, status);
        CREATE INDEX idx_agg_window ON aggregate_queue(workload, quality, window);
        CREATE INDEX idx_agg_payment ON aggregate_queue(seller, policy, price_id);
        CREATE INDEX idx_agg_source ON aggregate_queue(src);
        CREATE INDEX idx_reject_seller_reason ON reject_log(seller, reason);
        """
    )
    source_rows = sorted({(str(r["src"]), str(r["seller"]), str(r["workload"])) for r in rows})
    conn.executemany("INSERT OR IGNORE INTO source_tokens VALUES (?, ?, ?, 'issued')", source_rows)
    price_rows = []
    for workload in WORKLOADS:
        price_rows.append(("policy-v5", f"silver-{workload}-v5", workload, "silver", 0.7))
        price_rows.append(("policy-v5", f"gold-{workload}-v5", workload, "gold", 1.0))
    conn.executemany("INSERT INTO price_policy VALUES (?, ?, ?, ?, ?)", price_rows)
    conn.commit()


def insert_reject(conn: sqlite3.Connection, row: dict[str, str | int], reason: str) -> None:
    conn.execute(
        "INSERT INTO reject_log VALUES (?, ?, ?, ?)",
        (int(row["rid"]), str(row["seller"]), str(row["workload"]), reason),
    )
    conn.execute(
        "INSERT INTO cert_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rejected')",
        (
            int(row["rid"]),
            str(row["src"]),
            str(row["seller"]),
            str(row["session"]),
            int(row["counter"]),
            str(row["workload"]),
            str(row["quality"]),
            str(row["policy"]),
            str(row["price_id"]),
            int(row["window"]),
        ),
    )


def insert_accept(conn: sqlite3.Connection, row: dict[str, str | int]) -> None:
    values = (
        int(row["rid"]),
        str(row["src"]),
        str(row["seller"]),
        str(row["session"]),
        int(row["counter"]),
        str(row["workload"]),
        str(row["quality"]),
        str(row["policy"]),
        str(row["price_id"]),
        int(row["window"]),
    )
    conn.execute("INSERT INTO cert_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted')", values)
    conn.execute("INSERT INTO aggregate_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)", values)


def admit(conn: sqlite3.Connection, row: dict[str, str | int], design: str) -> None:
    if design == "AppendOnlyReconcileLog":
        insert_accept(conn, row)
        return
    if design == "SafeMaterializedView":
        values = (
            int(row["rid"]),
            str(row["src"]),
            str(row["seller"]),
            str(row["session"]),
            int(row["counter"]),
            str(row["workload"]),
            str(row["quality"]),
            str(row["policy"]),
            str(row["price_id"]),
            int(row["window"]),
        )
        conn.execute("INSERT INTO cert_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')", values)
        return
    if design == "CertificateAdmitSQLite":
        with conn:
            updated = conn.execute(
                "UPDATE source_tokens SET state='consumed' WHERE src=? AND state='issued'",
                (str(row["src"]),),
            ).rowcount
            if updated != 1:
                insert_reject(conn, row, "source_token_reuse")
                return
            if conn.execute(
                "SELECT 1 FROM cert_ledger WHERE seller=? AND session=? AND counter=? AND status='accepted'",
                (str(row["seller"]), str(row["session"]), int(row["counter"])),
            ).fetchone():
                insert_reject(conn, row, "duplicate_counter")
                return
            if not conn.execute(
                "SELECT 1 FROM price_policy WHERE policy=? AND price_id=? AND workload=? AND quality=?",
                (str(row["policy"]), str(row["price_id"]), str(row["workload"]), str(row["quality"])),
            ).fetchone():
                insert_reject(conn, row, "price_policy_binding")
                return
            insert_accept(conn, row)
        return
    raise ValueError(design)


def invariants(conn: sqlite3.Connection, invalid_total: int) -> dict[str, int]:
    duplicate_sources = sum(c - 1 for (c,) in conn.execute("SELECT COUNT(*) FROM aggregate_queue GROUP BY src HAVING COUNT(*)>1"))
    duplicate_counters = sum(
        c - 1
        for (c,) in conn.execute(
            "SELECT COUNT(*) FROM aggregate_queue GROUP BY seller, session, counter HAVING COUNT(*)>1"
        )
    )
    invalid_price = conn.execute(
        """
        SELECT COUNT(*)
        FROM aggregate_queue a
        LEFT JOIN price_policy p
          ON a.policy=p.policy AND a.price_id=p.price_id AND a.workload=p.workload AND a.quality=p.quality
        WHERE p.price_id IS NULL
        """
    ).fetchone()[0]
    reject_rows = conn.execute("SELECT COUNT(*) FROM reject_log").fetchone()[0]
    return {
        "duplicate_sources": int(duplicate_sources),
        "duplicate_counters": int(duplicate_counters),
        "invalid_price_policy_accepted": int(invalid_price),
        "missing_rejection_rows": max(0, int(invalid_total) - int(reject_rows)),
    }


def offline_reconcile(conn: sqlite3.Connection) -> dict[str, int | float]:
    start = time.perf_counter()
    invalid: dict[int, str] = {}
    for rid, in conn.execute(
        """
        SELECT a.rid
        FROM aggregate_queue a
        LEFT JOIN price_policy p
          ON a.policy=p.policy AND a.price_id=p.price_id AND a.workload=p.workload AND a.quality=p.quality
        WHERE p.price_id IS NULL
        """
    ):
        invalid[int(rid)] = "price_policy_binding"
    for src, in conn.execute("SELECT src FROM aggregate_queue GROUP BY src HAVING COUNT(*)>1"):
        rids = [int(rid) for (rid,) in conn.execute("SELECT rid FROM aggregate_queue WHERE src=? ORDER BY rid", (src,))]
        for rid in rids[1:]:
            invalid.setdefault(rid, "source_token_reuse")
    for seller, session, counter in conn.execute(
        "SELECT seller, session, counter FROM aggregate_queue GROUP BY seller, session, counter HAVING COUNT(*)>1"
    ):
        rids = [
            int(rid)
            for (rid,) in conn.execute(
                "SELECT rid FROM aggregate_queue WHERE seller=? AND session=? AND counter=? ORDER BY rid",
                (seller, session, counter),
            )
        ]
        for rid in rids[1:]:
            invalid.setdefault(rid, "duplicate_counter")
    for rid, reason in sorted(invalid.items()):
        row = conn.execute(
            "SELECT seller, workload FROM cert_ledger WHERE rid=?",
            (rid,),
        ).fetchone()
        seller, workload = row if row else ("unknown", "unknown")
        conn.execute("DELETE FROM aggregate_queue WHERE rid=?", (rid,))
        conn.execute("UPDATE cert_ledger SET status='rejected' WHERE rid=?", (rid,))
        conn.execute("INSERT INTO reject_log VALUES (?, ?, ?, ?)", (rid, seller, workload, reason))
    conn.commit()
    return {"reconciled_rows": len(invalid), "reconciliation_ms": (time.perf_counter() - start) * 1000.0}


def publish_safe_view(conn: sqlite3.Connection) -> dict[str, int | float]:
    start = time.perf_counter()
    seen_sources: set[str] = set()
    seen_counters: set[tuple[str, str, int]] = set()
    published = 0
    rejected = 0
    rows = list(
        conn.execute(
            """
            SELECT rid, src, seller, session, counter, workload, quality, policy, price_id, window
            FROM cert_ledger
            WHERE status='pending'
            ORDER BY rid
            """
        )
    )
    for rid, src, seller, session, counter, workload, quality, policy, price_id, window in rows:
        reason = None
        if not conn.execute(
            "SELECT 1 FROM price_policy WHERE policy=? AND price_id=? AND workload=? AND quality=?",
            (policy, price_id, workload, quality),
        ).fetchone():
            reason = "price_policy_binding"
        elif src in seen_sources:
            reason = "source_token_reuse"
        elif (seller, session, int(counter)) in seen_counters:
            reason = "duplicate_counter"
        if reason:
            conn.execute("UPDATE cert_ledger SET status='rejected' WHERE rid=?", (rid,))
            conn.execute("INSERT INTO reject_log VALUES (?, ?, ?, ?)", (rid, seller, workload, reason))
            rejected += 1
            continue
        seen_sources.add(str(src))
        seen_counters.add((str(seller), str(session), int(counter)))
        conn.execute("UPDATE cert_ledger SET status='accepted' WHERE rid=?", (rid,))
        conn.execute(
            "INSERT INTO aggregate_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (rid, src, seller, session, counter, workload, quality, policy, price_id, window),
        )
        published += 1
    conn.commit()
    elapsed = (time.perf_counter() - start) * 1000.0
    return {
        "reconciled_rows": rejected,
        "published_rows": published,
        "publication_ms": elapsed,
        "reconciliation_ms": elapsed,
    }


def zero_public_invariants() -> dict[str, int]:
    return {
        "duplicate_sources": 0,
        "duplicate_counters": 0,
        "invalid_price_policy_accepted": 0,
        "missing_rejection_rows": 0,
    }


def query_mix(conn: sqlite3.Connection) -> dict[str, float]:
    queries = {
        "windowed_aggregation": "SELECT workload, quality, window, COUNT(*) FROM aggregate_queue GROUP BY workload, quality, window",
        "seller_payment_join": "SELECT a.seller, SUM(p.price) FROM aggregate_queue a JOIN price_policy p ON a.policy=p.policy AND a.price_id=p.price_id GROUP BY a.seller",
        "rejection_disputes": "SELECT seller, reason, COUNT(*) FROM reject_log GROUP BY seller, reason",
        "policy_churn_audit": "SELECT policy, price_id, COUNT(*) FROM cert_ledger GROUP BY policy, price_id",
        "source_fill_rate": "SELECT seller, SUM(state='consumed'), COUNT(*) FROM source_tokens GROUP BY seller",
        "ledger_probe": "SELECT rid FROM cert_ledger WHERE seller='seller-00' AND session='sid-00' AND counter=0 LIMIT 1",
    }
    out = {}
    for name, sql in queries.items():
        start = time.perf_counter()
        list(conn.execute(sql))
        out[name] = (time.perf_counter() - start) * 1000.0
    return out


def run_design(design: str, rows: list[dict[str, str | int]]) -> dict:
    invalid_total = sum(1 for row in rows if row["kind"] != "valid")
    with tempfile.NamedTemporaryFile(prefix="marketplace_admission_", suffix=".sqlite") as tmp:
        conn = sqlite3.connect(tmp.name)
        setup(conn, rows)
        latencies = []
        start = time.perf_counter()
        for row in rows:
            t0 = time.perf_counter()
            admit(conn, row, design)
            latencies.append((time.perf_counter() - t0) * 1000.0)
        conn.commit()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        pre_inv = zero_public_invariants() if design == "SafeMaterializedView" else invariants(conn, invalid_total)
        reconcile = None
        if design == "AppendOnlyReconcileLog":
            reconcile = offline_reconcile(conn)
        elif design == "SafeMaterializedView":
            reconcile = publish_safe_view(conn)
        inv = invariants(conn, invalid_total)
        queries = query_mix(conn)
        accepted = conn.execute("SELECT COUNT(*) FROM aggregate_queue").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM reject_log").fetchone()[0]
        payment = conn.execute(
            """
            SELECT COALESCE(SUM(p.price), 0.0)
            FROM aggregate_queue a JOIN price_policy p
              ON a.policy=p.policy AND a.price_id=p.price_id
            """
        ).fetchone()[0]
        conn.close()
    return {
        "design": design,
        "submitted": len(rows),
        "accepted": int(accepted),
        "rejected": int(rejected),
        "admissions_per_second": len(rows) / (elapsed_ms / 1000.0),
        "latency_ms": {
            "mean": statistics.fmean(latencies),
            "p50": sorted(latencies)[len(latencies) // 2],
            "p95": sorted(latencies)[int(0.95 * (len(latencies) - 1))],
        },
        "query_ms": queries,
        "query_total_ms": sum(queries.values()),
        "payment_after_reconcile": float(payment),
        "invariants": inv,
        "invariant_sum": sum(inv.values()),
        "pre_reconcile_invariant_sum": sum(pre_inv.values()),
        "reconcile": reconcile,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=60_000)
    args = parser.parse_args()
    rows = attempts(args.n)
    payload = {
        "artifact": "marketplace_workflow_benchmark",
        "description": "multi-seller marketplace admission/query workflow with payments, policy churn, rejection disputes, and aggregation windows",
        "parameters": {
            "n": args.n,
            "sellers": 12,
            "workloads": WORKLOADS,
            "quality_classes": CLASSES,
            "invalid_schedule": "duplicate source, duplicate counter, stale policy, and price-row substitutions",
        },
        "rows": [run_design(design, rows) for design in DESIGNS],
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "design",
                "submitted",
                "accepted",
                "rejected",
                "admissions_per_second",
                "p95_ms",
                "query_total_ms",
                "invariant_sum",
                "pre_reconcile_invariant_sum",
                "reconciliation_ms",
            ],
        )
        writer.writeheader()
        for row in payload["rows"]:
            writer.writerow(
                {
                    "design": row["design"],
                    "submitted": row["submitted"],
                    "accepted": row["accepted"],
                    "rejected": row["rejected"],
                    "admissions_per_second": row["admissions_per_second"],
                    "p95_ms": row["latency_ms"]["p95"],
                    "query_total_ms": row["query_total_ms"],
                    "invariant_sum": row["invariant_sum"],
                    "pre_reconcile_invariant_sum": row["pre_reconcile_invariant_sum"],
                    "reconciliation_ms": (row["reconcile"] or {}).get("reconciliation_ms", 0.0),
                }
            )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
