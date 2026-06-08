#!/usr/bin/env python3
"""Compare CertificateAdmit with simpler admission/database designs."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path


OUT_JSON = Path(__file__).with_name("admission_design_comparison.json")
OUT_CSV = Path(__file__).with_name("admission_design_comparison.csv")


DESIGNS = [
    "MetadataOnly",
    "AttestationBlobOnly",
    "AppendOnlyCertLog",
    "AppendOnlyReconcileLog",
    "SafeMaterializedView",
    "SourceTokenGate",
    "TransactionalOutbox",
    "FullTransactionalOutbox",
    "CertificateAdmitSQLite",
]


def attempts(n: int) -> list[dict[str, str | int]]:
    rows = []
    for i in range(n):
        src = f"src-{i:06d}"
        counter = i
        price_id = "price-gold-v5"
        kind = "valid"
        if i > 0 and i % 20 == 0:
            src = "src-000000"
            kind = "duplicate_source"
        elif i > 0 and i % 20 == 10:
            counter = 0
            kind = "duplicate_counter"
        elif i % 40 == 7:
            price_id = "price-stale-v1"
            kind = "price_swap"
        rows.append({"rid": i, "src": src, "counter": counter, "price_id": price_id, "kind": kind})
    return rows


def setup(conn: sqlite3.Connection, rows: list[dict[str, str | int]]) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE source_tokens(src TEXT PRIMARY KEY, state TEXT NOT NULL);
        CREATE TABLE price_policy(price_id TEXT PRIMARY KEY, policy TEXT NOT NULL, price REAL NOT NULL);
        CREATE TABLE cert_log(rid INTEGER, src TEXT, seller TEXT, session TEXT, counter INTEGER, price_id TEXT, status TEXT);
        CREATE TABLE aggregate_queue(rid INTEGER, src TEXT, seller TEXT, session TEXT, counter INTEGER, price_id TEXT, y INTEGER);
        CREATE TABLE reject_log(rid INTEGER, reason TEXT);
        CREATE INDEX idx_cert_rid ON cert_log(rid);
        CREATE INDEX idx_cert_counter ON cert_log(seller, session, counter, status);
        CREATE INDEX idx_agg_policy ON aggregate_queue(price_id);
        CREATE INDEX idx_agg_source ON aggregate_queue(src);
        CREATE INDEX idx_agg_counter ON aggregate_queue(seller, session, counter);
        CREATE INDEX idx_reject_reason ON reject_log(reason);
        """
    )
    unique_sources = sorted({str(r["src"]) for r in rows})
    conn.executemany("INSERT OR IGNORE INTO source_tokens VALUES (?, 'issued')", [(src,) for src in unique_sources])
    conn.execute("INSERT INTO price_policy VALUES ('price-gold-v5', 'policy-v5', 1.0)")
    conn.commit()


def reject(conn: sqlite3.Connection, rid: int, reason: str) -> None:
    conn.execute("INSERT INTO reject_log VALUES (?, ?)", (rid, reason))
    conn.execute("INSERT INTO cert_log VALUES (?, ?, 'seller', 'sid', ?, ?, 'rejected')", (rid, f"src-rej-{rid}", rid, "rejected"))


def admit(conn: sqlite3.Connection, row: dict[str, str | int], design: str) -> None:
    rid = int(row["rid"])
    src = str(row["src"])
    counter = int(row["counter"])
    price_id = str(row["price_id"])
    if design == "MetadataOnly":
        conn.execute("INSERT INTO aggregate_queue VALUES (?, ?, 'seller', 'sid', ?, ?, 1)", (rid, src, counter, price_id))
        return
    if design == "AttestationBlobOnly":
        conn.execute("INSERT INTO cert_log VALUES (?, ?, 'seller', 'sid', ?, ?, 'accepted')", (rid, src, counter, price_id))
        conn.execute("INSERT INTO aggregate_queue VALUES (?, ?, 'seller', 'sid', ?, ?, 1)", (rid, src, counter, price_id))
        return
    if design == "AppendOnlyCertLog":
        if not conn.execute("SELECT 1 FROM price_policy WHERE price_id=?", (price_id,)).fetchone():
            reject(conn, rid, "price_row_binding")
            return
        conn.execute("INSERT INTO cert_log VALUES (?, ?, 'seller', 'sid', ?, ?, 'accepted')", (rid, src, counter, price_id))
        conn.execute("INSERT INTO aggregate_queue VALUES (?, ?, 'seller', 'sid', ?, ?, 1)", (rid, src, counter, price_id))
        return
    if design == "AppendOnlyReconcileLog":
        conn.execute("INSERT INTO cert_log VALUES (?, ?, 'seller', 'sid', ?, ?, 'accepted')", (rid, src, counter, price_id))
        conn.execute("INSERT INTO aggregate_queue VALUES (?, ?, 'seller', 'sid', ?, ?, 1)", (rid, src, counter, price_id))
        return
    if design == "SafeMaterializedView":
        conn.execute("INSERT INTO cert_log VALUES (?, ?, 'seller', 'sid', ?, ?, 'pending')", (rid, src, counter, price_id))
        return
    if design == "SourceTokenGate":
        updated = conn.execute("UPDATE source_tokens SET state='consumed' WHERE src=? AND state='issued'", (src,)).rowcount
        if updated != 1:
            reject(conn, rid, "source_token_reuse")
            return
        conn.execute("INSERT INTO cert_log VALUES (?, ?, 'seller', 'sid', ?, ?, 'accepted')", (rid, src, counter, price_id))
        conn.execute("INSERT INTO aggregate_queue VALUES (?, ?, 'seller', 'sid', ?, ?, 1)", (rid, src, counter, price_id))
        return
    if design == "TransactionalOutbox":
        with conn:
            updated = conn.execute("UPDATE source_tokens SET state='consumed' WHERE src=? AND state='issued'", (src,)).rowcount
            if updated != 1:
                reject(conn, rid, "source_token_reuse")
                return
            if not conn.execute("SELECT 1 FROM price_policy WHERE price_id=?", (price_id,)).fetchone():
                reject(conn, rid, "price_row_binding")
                return
            conn.execute("INSERT INTO cert_log VALUES (?, ?, 'seller', 'sid', ?, ?, 'accepted')", (rid, src, counter, price_id))
            conn.execute("INSERT INTO aggregate_queue VALUES (?, ?, 'seller', 'sid', ?, ?, 1)", (rid, src, counter, price_id))
        return
    if design in {"FullTransactionalOutbox", "CertificateAdmitSQLite"}:
        with conn:
            updated = conn.execute("UPDATE source_tokens SET state='consumed' WHERE src=? AND state='issued'", (src,)).rowcount
            if updated != 1:
                reject(conn, rid, "source_token_reuse")
                return
            if conn.execute("SELECT 1 FROM cert_log WHERE seller='seller' AND session='sid' AND counter=? AND status='accepted'", (counter,)).fetchone():
                reject(conn, rid, "duplicate_counter")
                return
            if not conn.execute("SELECT 1 FROM price_policy WHERE price_id=?", (price_id,)).fetchone():
                reject(conn, rid, "price_row_binding")
                return
            conn.execute("INSERT INTO cert_log VALUES (?, ?, 'seller', 'sid', ?, ?, 'accepted')", (rid, src, counter, price_id))
            conn.execute("INSERT INTO aggregate_queue VALUES (?, ?, 'seller', 'sid', ?, ?, 1)", (rid, src, counter, price_id))
        return
    raise ValueError(design)


def offline_reconcile(conn: sqlite3.Connection) -> dict[str, int | float]:
    """Batch audit for the stronger append-only baseline.

    It repairs the materialized aggregate queue, but only after invalid rows
    have been visible to payment/aggregation queries.
    """
    start = time.perf_counter()
    invalid: dict[int, str] = {}
    for rid, in conn.execute(
        """
        SELECT a.rid
        FROM aggregate_queue a
        LEFT JOIN price_policy p ON a.price_id=p.price_id
        WHERE p.price_id IS NULL
        """
    ):
        invalid[int(rid)] = "price_row_binding"
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
        conn.execute("DELETE FROM aggregate_queue WHERE rid=?", (rid,))
        conn.execute("UPDATE cert_log SET status='rejected' WHERE rid=?", (rid,))
        conn.execute("INSERT INTO reject_log VALUES (?, ?)", (rid, reason))
    conn.commit()
    return {
        "reconciled_rows": len(invalid),
        "reconciliation_ms": (time.perf_counter() - start) * 1000.0,
    }


def publish_safe_view(conn: sqlite3.Connection) -> dict[str, int | float]:
    """Build a materialized accepted view only after validating a watermark.

    This is a strong event-sourced baseline: rows are not query-visible until
    validation completes, so it avoids the unsafe pre-reconciliation window at
    the cost of publication delay.
    """
    start = time.perf_counter()
    seen_sources: set[str] = set()
    seen_counters: set[tuple[str, str, int]] = set()
    published = 0
    rejected = 0
    rows = list(
        conn.execute(
            """
            SELECT rid, src, seller, session, counter, price_id
            FROM cert_log
            WHERE status='pending'
            ORDER BY rid
            """
        )
    )
    for rid, src, seller, session, counter, price_id in rows:
        reason = None
        if not conn.execute("SELECT 1 FROM price_policy WHERE price_id=?", (price_id,)).fetchone():
            reason = "price_row_binding"
        elif src in seen_sources:
            reason = "source_token_reuse"
        elif (seller, session, int(counter)) in seen_counters:
            reason = "duplicate_counter"
        if reason:
            conn.execute("UPDATE cert_log SET status='rejected' WHERE rid=?", (rid,))
            conn.execute("INSERT INTO reject_log VALUES (?, ?)", (rid, reason))
            rejected += 1
            continue
        seen_sources.add(str(src))
        seen_counters.add((str(seller), str(session), int(counter)))
        conn.execute("UPDATE cert_log SET status='accepted' WHERE rid=?", (rid,))
        conn.execute(
            "INSERT INTO aggregate_queue VALUES (?, ?, ?, ?, ?, ?, 1)",
            (rid, src, seller, session, counter, price_id),
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
        "price_swap_accepted": 0,
        "missing_rejection_rows": 0,
    }


def query_ms(conn: sqlite3.Connection) -> dict[str, float]:
    queries = {
        "accepted_by_price": "SELECT price_id, COUNT(*) FROM aggregate_queue GROUP BY price_id",
        "price_join": "SELECT SUM(p.price) FROM aggregate_queue a JOIN price_policy p ON a.price_id=p.price_id",
        "source_audit": "SELECT src, COUNT(*) c FROM aggregate_queue GROUP BY src HAVING c>1",
        "rejection_audit": "SELECT reason, COUNT(*) FROM reject_log GROUP BY reason",
        "ledger_probe": "SELECT rid FROM aggregate_queue WHERE src='src-000001' LIMIT 1",
    }
    out = {}
    for name, sql in queries.items():
        start = time.perf_counter()
        list(conn.execute(sql))
        out[name] = (time.perf_counter() - start) * 1000.0
    return out


def invariants(conn: sqlite3.Connection, invalid_total: int) -> dict[str, int]:
    duplicate_sources = sum(c - 1 for (c,) in conn.execute("SELECT COUNT(*) FROM aggregate_queue GROUP BY src HAVING COUNT(*)>1"))
    duplicate_counters = sum(c - 1 for (c,) in conn.execute("SELECT COUNT(*) FROM aggregate_queue GROUP BY seller, session, counter HAVING COUNT(*)>1"))
    price_swaps = conn.execute("SELECT COUNT(*) FROM aggregate_queue WHERE price_id!='price-gold-v5'").fetchone()[0]
    reject_rows = conn.execute("SELECT COUNT(*) FROM reject_log").fetchone()[0]
    return {
        "duplicate_sources": int(duplicate_sources),
        "duplicate_counters": int(duplicate_counters),
        "price_swap_accepted": int(price_swaps),
        "missing_rejection_rows": max(0, int(invalid_total) - int(reject_rows)),
    }


def run_design(design: str, rows: list[dict[str, str | int]]) -> dict:
    invalid_total = sum(1 for r in rows if r["kind"] != "valid")
    with tempfile.NamedTemporaryFile(prefix="admission_design_", suffix=".sqlite") as tmp:
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
        q = query_ms(conn)
        inv = invariants(conn, invalid_total)
        accepted = conn.execute("SELECT COUNT(*) FROM aggregate_queue").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM reject_log").fetchone()[0]
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
        "query_ms": q,
        "query_total_ms": sum(q.values()),
        "invariants": inv,
        "invariant_sum": sum(inv.values()),
        "pre_reconcile_invariant_sum": sum(pre_inv.values()),
        "reconcile": reconcile,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50_000)
    args = parser.parse_args()
    rows = attempts(args.n)
    payload = {
        "artifact": "admission_design_comparison",
        "description": "mixed admission/query workload comparing simpler designs with CertificateAdmit",
        "parameters": {"n": args.n, "invalid_schedule": "5% duplicate source, 5% duplicate counter, 2.5% price-row substitutions"},
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
