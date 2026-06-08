#!/usr/bin/env python3
"""Certificate-store indexing and query benchmark.

This lightweight benchmark materializes synthetic certificate rows in SQLite
with the same admission keys used by the verifier artifact. It measures bulk
insert throughput and representative market queries. It is not a production
database benchmark; its purpose is to make the certificate-as-relational-record
claim reproducible with only the Python standard library.
"""

from __future__ import annotations

import csv
import concurrent.futures
import json
import sqlite3
import tempfile
import time
from pathlib import Path


OUT_JSON = Path(__file__).with_name("certificate_store_benchmark.json")
OUT_CSV = Path(__file__).with_name("certificate_store_benchmark.csv")
N_ROWS = 1_000_000
CONCURRENT_WORKERS = 8
CONCURRENT_PROBES = 10_000


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE certificates (
  rid TEXT NOT NULL,
  seller TEXT NOT NULL,
  session TEXT NOT NULL,
  source TEXT NOT NULL UNIQUE,
  workload TEXT NOT NULL,
  algorithm TEXT NOT NULL,
  policy TEXT NOT NULL,
  price_id TEXT NOT NULL,
  quality_class TEXT NOT NULL,
  counter INTEGER NOT NULL,
  nonce TEXT NOT NULL,
  commitment TEXT NOT NULL,
  residual_bucket TEXT NOT NULL,
  accepted INTEGER NOT NULL,
  PRIMARY KEY(rid, nonce),
  UNIQUE(source),
  UNIQUE(seller, session, counter),
  UNIQUE(seller, session, nonce)
);

CREATE INDEX idx_cert_policy_workload ON certificates(policy, workload, accepted);
CREATE INDEX idx_cert_price ON certificates(price_id);
CREATE INDEX idx_cert_source ON certificates(source);
CREATE INDEX idx_cert_quality ON certificates(quality_class);

CREATE TABLE price_table (
  price_id TEXT PRIMARY KEY,
  quality_class TEXT NOT NULL,
  unit_price REAL NOT NULL
);

CREATE TABLE source_tokens (
  source TEXT PRIMARY KEY,
  seller TEXT NOT NULL,
  cohort TEXT NOT NULL,
  sampling_contract TEXT NOT NULL,
  active INTEGER NOT NULL
);

CREATE TABLE rejections (
  rid TEXT NOT NULL,
  nonce TEXT NOT NULL,
  seller TEXT NOT NULL,
  source TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY(rid, nonce)
);

CREATE INDEX idx_reject_reason ON rejections(reason);
"""


def certificate_rows(n_rows: int):
    workloads = ["reach", "conversion", "audience", "lift"]
    price_ids = ["price-silver-v4", "price-gold-v4"]
    quality = {"price-silver-v4": "silver", "price-gold-v4": "gold"}
    for i in range(n_rows):
        price_id = price_ids[i % len(price_ids)]
        seller = f"seller-{i % 100:03d}"
        session = f"session-{i // 1000:04d}"
        source = f"src-{i:08d}"
        yield (
            f"rid-{i:08d}",
            seller,
            session,
            source,
            workloads[i % len(workloads)],
            "OUE" if i % 3 else "BRR",
            "policy-v4",
            price_id,
            quality[price_id],
            i,
            f"nonce-{i:08d}",
            f"commit-{i:08d}",
            "deposit" if i % 5 else "audit",
            1,
        )


def source_rows(n_rows: int):
    for i in range(n_rows):
        yield (f"src-{i:08d}", f"seller-{i % 100:03d}", f"cohort-{i % 32:02d}", "sampling-contract-2026", 1)


def rejection_rows(n_rows: int):
    reasons = ["pcr_allowlist", "policy_version", "source_token_reuse", "price_row_binding"]
    for i in range(max(1, n_rows // 100)):
        yield (f"rej-{i:08d}", f"rej-nonce-{i:08d}", f"seller-{i % 100:03d}", f"bad-src-{i:08d}", reasons[i % len(reasons)])


def timed_query(conn: sqlite3.Connection, sql: str) -> tuple[float, list[dict[str, object]], list[str]]:
    start = time.perf_counter()
    cur = conn.execute(sql)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()]
    return elapsed_ms, rows, plan


def conflict_checks(conn: sqlite3.Connection) -> dict[str, str]:
    duplicate_source = (
        "rid-conflict-source",
        "seller-999",
        "session-9999",
        "src-00000000",
        "reach",
        "OUE",
        "policy-v4",
        "price-gold-v4",
        "gold",
        N_ROWS + 1,
        "nonce-conflict-source",
        "commit-conflict-source",
        "deposit",
        1,
    )
    duplicate_counter = (
        "rid-conflict-counter",
        "seller-000",
        "session-0000",
        "src-conflict-counter",
        "reach",
        "OUE",
        "policy-v4",
        "price-gold-v4",
        "gold",
        0,
        "nonce-conflict-counter",
        "commit-conflict-counter",
        "deposit",
        1,
    )
    duplicate_nonce = (
        "rid-conflict-nonce",
        "seller-000",
        "session-0000",
        "src-conflict-nonce",
        "reach",
        "OUE",
        "policy-v4",
        "price-gold-v4",
        "gold",
        N_ROWS + 2,
        "nonce-00000000",
        "commit-conflict-nonce",
        "deposit",
        1,
    )
    sql = """
        INSERT INTO certificates
        (rid, seller, session, source, workload, algorithm, policy, price_id,
         quality_class, counter, nonce, commitment, residual_bucket, accepted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    out = {}
    for name, row in [
        ("duplicate_source", duplicate_source),
        ("duplicate_counter", duplicate_counter),
        ("duplicate_nonce", duplicate_nonce),
    ]:
        try:
            conn.execute(sql, row)
            conn.commit()
            out[name] = "accepted_unexpectedly"
        except sqlite3.IntegrityError as exc:
            out[name] = str(exc).split(":")[0]
    return out


def concurrent_ledger_probe(db_path: Path, probes: int, workers: int) -> dict[str, float | int]:
    def worker(offset: int) -> int:
        local = sqlite3.connect(db_path)
        hits = 0
        per_worker = probes // workers
        for j in range(per_worker):
            counter = (offset + j * workers) % N_ROWS
            seller = f"seller-{counter % 100:03d}"
            session = f"session-{counter // 1000:04d}"
            row = local.execute(
                """
                SELECT rid
                FROM certificates
                WHERE seller = ?
                  AND session = ?
                  AND counter = ?
                  AND policy = 'policy-v4'
                """,
                (seller, session, counter),
            ).fetchone()
            hits += 1 if row else 0
        local.close()
        return hits

    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        hits = sum(pool.map(worker, range(workers)))
    elapsed = time.perf_counter() - start
    return {
        "workers": workers,
        "probes": probes,
        "hits": hits,
        "elapsed_ms": elapsed * 1000.0,
        "probes_per_second": probes / elapsed,
    }


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cert_store_bench_")) / "certificate_store.sqlite"
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO price_table VALUES (?, ?, ?)",
        [("price-silver-v4", "silver", 12.0), ("price-gold-v4", "gold", 20.0)],
    )

    start = time.perf_counter()
    with conn:
        conn.executemany("INSERT INTO source_tokens VALUES (?, ?, ?, ?, ?)", source_rows(N_ROWS))
        conn.executemany(
            """
            INSERT INTO certificates
            (rid, seller, session, source, workload, algorithm, policy, price_id,
             quality_class, counter, nonce, commitment, residual_bucket, accepted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            certificate_rows(N_ROWS),
        )
        conn.executemany("INSERT INTO rejections VALUES (?, ?, ?, ?, ?)", rejection_rows(N_ROWS))
    insert_seconds = time.perf_counter() - start

    queries = {
        "accepted_by_policy_workload": """
            SELECT policy, workload, quality_class, COUNT(*) AS reports
            FROM certificates
            WHERE accepted = 1 AND policy = 'policy-v4'
            GROUP BY policy, workload, quality_class
        """,
        "price_join": """
            SELECT c.price_id, SUM(p.unit_price) AS payment, COUNT(*) AS reports
            FROM certificates c
            JOIN price_table p USING (price_id)
            WHERE c.accepted = 1
            GROUP BY c.price_id
        """,
        "source_join_sample": """
            SELECT c.rid, c.seller, s.cohort, s.sampling_contract
            FROM certificates c
            JOIN source_tokens s USING (source)
            WHERE c.source BETWEEN 'src-00001000' AND 'src-00001020'
        """,
        "ledger_probe": """
            SELECT rid, accepted
            FROM certificates
            WHERE source = 'src-00054321'
              AND seller = 'seller-021'
              AND session = 'session-0054'
              AND counter = 54321
              AND policy = 'policy-v4'
              AND price_id = 'price-gold-v4'
        """,
        "rejection_audit": """
            SELECT reason, COUNT(*) AS failures
            FROM rejections
            GROUP BY reason
            ORDER BY reason
        """,
    }

    query_payload = {}
    csv_rows = [
        {
            "metric": "insert_rows_per_second",
            "value": N_ROWS / insert_seconds,
            "rows_returned": N_ROWS,
        }
    ]
    for name, sql in queries.items():
        elapsed_ms, rows, plan = timed_query(conn, sql)
        query_payload[name] = {"elapsed_ms": elapsed_ms, "rows": rows, "plan": plan}
        csv_rows.append({"metric": f"{name}_ms", "value": elapsed_ms, "rows_returned": len(rows)})

    conflicts = conflict_checks(conn)
    for name, value in conflicts.items():
        csv_rows.append({"metric": name, "value": 1.0 if value.startswith("UNIQUE") else 0.0, "rows_returned": 1})

    concurrent_probe = concurrent_ledger_probe(tmp, CONCURRENT_PROBES, CONCURRENT_WORKERS)
    csv_rows.append(
        {
            "metric": "concurrent_ledger_probe_per_second",
            "value": concurrent_probe["probes_per_second"],
            "rows_returned": concurrent_probe["hits"],
        }
    )

    conn.close()
    payload = {
        "rows": N_ROWS,
        "database": "SQLite temporary file",
        "insert_seconds": insert_seconds,
        "insert_rows_per_second": N_ROWS / insert_seconds,
        "conflict_checks": conflicts,
        "concurrent_ledger_probe": concurrent_probe,
        "queries": query_payload,
        "notes": [
            "synthetic certificate rows",
            "separate terminal, source, counter, and nonce uniqueness constraints",
            "standard-library reproducibility benchmark",
            "not a production database benchmark",
        ],
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "value", "rows_returned"])
        writer.writeheader()
        writer.writerows(csv_rows)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
