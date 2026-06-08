#!/usr/bin/env python3
"""Optional PostgreSQL admission benchmark.

The main admission artifact is dependency-free SQLite. This optional benchmark
uses PostgreSQL SERIALIZABLE transactions and unique indexes to exercise the
same certificate-admission contract on a production-grade transactional store.
It starts a temporary local PostgreSQL cluster when PostgreSQL binaries are
available, so it does not require a privileged database role.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import shutil
import socket
import statistics
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

try:
    import psycopg
    from psycopg import errors
except Exception as exc:  # pragma: no cover - used for clean optional failure.
    psycopg = None
    errors = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

from end_to_end_admission_benchmark import CURRENT_POLICY, PRICE_QUALITY, Attempt, make_attempts, percentile


OUT_JSON = Path(__file__).with_name("postgres_admission_benchmark.json")
OUT_CSV = Path(__file__).with_name("postgres_admission_benchmark.csv")


POSTGRES_SCHEMA = """
DROP TABLE IF EXISTS aggregate_queue, aggregates, certificates, rejections, source_tokens, price_table, precommits CASCADE;

CREATE TABLE price_table (
  price_id TEXT PRIMARY KEY,
  quality_class TEXT NOT NULL,
  unit_price DOUBLE PRECISION NOT NULL
);

CREATE TABLE source_tokens (
  source TEXT PRIMARY KEY,
  seller TEXT NOT NULL,
  session TEXT NOT NULL,
  workload TEXT NOT NULL,
  status TEXT NOT NULL,
  consumed_by TEXT,
  updated_at DOUBLE PRECISION NOT NULL
);

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
  accepted_at DOUBLE PRECISION NOT NULL,
  aggregated BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY(rid, nonce),
  UNIQUE(source),
  UNIQUE(seller, session, counter),
  UNIQUE(seller, session, nonce)
);

CREATE INDEX idx_pg_cert_status_policy_workload ON certificates(policy, workload, quality_class);
CREATE INDEX idx_pg_cert_price ON certificates(price_id);
CREATE INDEX idx_pg_cert_aggregated ON certificates(aggregated);

CREATE TABLE rejections (
  rid TEXT NOT NULL,
  nonce TEXT NOT NULL,
  reason TEXT NOT NULL,
  source TEXT NOT NULL,
  seller TEXT NOT NULL,
  session TEXT NOT NULL,
  counter INTEGER NOT NULL,
  rejected_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(rid, nonce)
);

CREATE INDEX idx_pg_reject_reason ON rejections(reason);

CREATE TABLE aggregate_queue (
  rid TEXT NOT NULL,
  nonce TEXT NOT NULL,
  workload TEXT NOT NULL,
  quality_class TEXT NOT NULL,
  price_id TEXT NOT NULL,
  y INTEGER NOT NULL,
  processed BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY(rid, nonce)
);

CREATE TABLE aggregates (
  workload TEXT NOT NULL,
  quality_class TEXT NOT NULL,
  price_id TEXT NOT NULL,
  reports INTEGER NOT NULL,
  sum_y INTEGER NOT NULL,
  PRIMARY KEY (workload, quality_class, price_id)
);

CREATE TABLE precommits (
  rid TEXT NOT NULL,
  nonce TEXT NOT NULL,
  source TEXT NOT NULL,
  state TEXT NOT NULL,
  deadline DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(rid, nonce)
);
"""


def find_pg_bin(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    for version in ("18", "17", "16", "15", "14"):
        candidate = Path(f"/usr/lib/postgresql/{version}/bin/{name}")
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"PostgreSQL binary not found: {name}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def temporary_postgres() -> Iterator[dict[str, str]]:
    if IMPORT_ERROR is not None:
        raise RuntimeError(f"psycopg is required for this optional benchmark: {IMPORT_ERROR}")

    initdb = find_pg_bin("initdb")
    pg_ctl = find_pg_bin("pg_ctl")
    tmp = Path(tempfile.mkdtemp(prefix="pg_admission_"))
    data = tmp / "data"
    sockdir = tmp / "socket"
    sockdir.mkdir()
    port = free_port()
    user = os.environ.get("USER", "codex")
    log = tmp / "postgres.log"

    subprocess.run([initdb, "-D", str(data), "-A", "trust", "-U", user], check=True, stdout=subprocess.DEVNULL)
    opts = f"-p {port} -k {sockdir} -F -c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    subprocess.run([pg_ctl, "-D", str(data), "-l", str(log), "-o", opts, "-w", "start"], check=True, stdout=subprocess.DEVNULL)
    try:
        yield {"host": str(sockdir), "port": str(port), "user": user, "dbname": "postgres", "log": str(log)}
    finally:
        subprocess.run([pg_ctl, "-D", str(data), "-m", "fast", "-w", "stop"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect(dsn: dict[str, str]):
    return psycopg.connect(
        dbname=dsn["dbname"],
        user=dsn["user"],
        host=dsn["host"],
        port=dsn["port"],
        autocommit=False,
    )


def setup_db(dsn: dict[str, str], n_sources: int) -> None:
    now = time.time()
    with connect(dsn) as conn:
        conn.execute(POSTGRES_SCHEMA)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO price_table VALUES (%s, %s, %s)",
                [("price-silver-v5", "silver", 12.0), ("price-gold-v5", "gold", 20.0)],
            )
            cur.executemany(
                "INSERT INTO source_tokens VALUES (%s, %s, %s, %s, 'issued', NULL, %s)",
                [
                    (
                        f"src-{i:08d}",
                        f"seller-{i % 128:03d}",
                        f"session-{i // 1000:05d}",
                        ["reach", "conversion", "audience", "lift"][i % 4],
                        now,
                    )
                    for i in range(n_sources)
                ],
            )
            cur.execute(
                "INSERT INTO source_tokens VALUES ('src-preconsumed', 'seller-reserved', 'session-reserved', 'race', 'accepted', 'reserved', %s)",
                (now,),
            )
        conn.commit()


def reject_pg(conn, attempt: Attempt, reason: str) -> str:
    conn.execute(
        """
        INSERT INTO rejections
        (rid, nonce, reason, source, seller, session, counter, rejected_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (rid, nonce) DO UPDATE SET reason = EXCLUDED.reason, rejected_at = EXCLUDED.rejected_at
        """,
        (attempt.rid, attempt.nonce, reason, attempt.source, attempt.seller, attempt.session, attempt.counter, time.time()),
    )
    return reason


def classify_pg_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "seller" in text and "counter" in text:
        return "duplicate_counter"
    if "seller" in text and "nonce" in text:
        return "nonce_reuse"
    if "source" in text:
        return "source_token_reuse"
    return "ledger_key_conflict"


def admit_one_pg(conn, attempt: Attempt) -> str:
    if attempt.policy != CURRENT_POLICY:
        with conn.transaction():
            return reject_pg(conn, attempt, "stale_policy")
    if PRICE_QUALITY.get(attempt.price_id) != attempt.quality_class:
        with conn.transaction():
            return reject_pg(conn, attempt, "price_row_binding")
    if attempt.submitted_commitment != attempt.expected_commitment:
        with conn.transaction():
            return reject_pg(conn, attempt, "report_commitment")

    for retry in range(20):
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                cur = conn.execute(
                    """
                    UPDATE source_tokens
                    SET status = 'consuming', consumed_by = %s, updated_at = %s
                    WHERE source = %s AND status = 'issued'
                    """,
                    (attempt.rid, time.time(), attempt.source),
                )
                if cur.rowcount != 1:
                    return reject_pg(conn, attempt, "source_token_reuse")
                conn.execute(
                    """
                    INSERT INTO certificates
                    (rid, seller, session, source, workload, algorithm, policy, price_id,
                     quality_class, counter, nonce, commitment, residual_bucket, accepted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        attempt.rid,
                        attempt.seller,
                        attempt.session,
                        attempt.source,
                        attempt.workload,
                        attempt.algorithm,
                        attempt.policy,
                        attempt.price_id,
                        attempt.quality_class,
                        attempt.counter,
                        attempt.nonce,
                        attempt.submitted_commitment,
                        attempt.residual_bucket,
                        time.time(),
                    ),
                )
                conn.execute(
                    "INSERT INTO aggregate_queue VALUES (%s, %s, %s, %s, %s, %s, FALSE)",
                    (attempt.rid, attempt.nonce, attempt.workload, attempt.quality_class, attempt.price_id, attempt.y),
                )
                conn.execute("UPDATE source_tokens SET status = 'accepted', updated_at = %s WHERE source = %s", (time.time(), attempt.source))
                return "accepted"
        except (errors.SerializationFailure, errors.DeadlockDetected):
            conn.rollback()
            time.sleep(0.001 * (retry + 1))
        except errors.UniqueViolation as exc:
            conn.rollback()
            with conn.transaction():
                return reject_pg(conn, attempt, classify_pg_error(exc))
    with conn.transaction():
        return reject_pg(conn, attempt, "serialization_failure")


def worker_run(dsn: dict[str, str], rows: list[Attempt]) -> dict[str, object]:
    reasons: dict[str, int] = {}
    latencies: list[float] = []
    with connect(dsn) as conn:
        for attempt in rows:
            start = time.perf_counter()
            result = admit_one_pg(conn, attempt)
            latencies.append((time.perf_counter() - start) * 1000.0)
            reasons[result] = reasons.get(result, 0) + 1
    return {"reasons": reasons, "latencies_ms": latencies}


def run_aggregation_pg(conn) -> float:
    start = time.perf_counter()
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT workload, quality_class, price_id, COUNT(*) AS reports, COALESCE(SUM(y), 0) AS sum_y
            FROM aggregate_queue
            WHERE processed = FALSE
            GROUP BY workload, quality_class, price_id
            """
        ).fetchall()
        for workload, quality, price_id, reports, sum_y in rows:
            conn.execute(
                """
                INSERT INTO aggregates VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(workload, quality_class, price_id)
                DO UPDATE SET
                  reports = aggregates.reports + EXCLUDED.reports,
                  sum_y = aggregates.sum_y + EXCLUDED.sum_y
                """,
                (workload, quality, price_id, reports, sum_y),
            )
        conn.execute("UPDATE aggregate_queue SET processed = TRUE WHERE processed = FALSE")
        conn.execute("UPDATE certificates SET aggregated = TRUE WHERE aggregated = FALSE")
    return (time.perf_counter() - start) * 1000.0


def invariant_checks_pg(conn) -> dict[str, int]:
    checks = {
        "duplicate_sources": """
            SELECT COALESCE(SUM(cnt - 1), 0)
            FROM (SELECT source, COUNT(*) AS cnt FROM certificates GROUP BY source HAVING COUNT(*) > 1) t
        """,
        "duplicate_counters": """
            SELECT COALESCE(SUM(cnt - 1), 0)
            FROM (
              SELECT seller, session, counter, COUNT(*) AS cnt
              FROM certificates
              GROUP BY seller, session, counter
              HAVING COUNT(*) > 1
            ) t
        """,
        "duplicate_nonces": """
            SELECT COALESCE(SUM(cnt - 1), 0)
            FROM (
              SELECT seller, session, nonce, COUNT(*) AS cnt
              FROM certificates
              GROUP BY seller, session, nonce
              HAVING COUNT(*) > 1
            ) t
        """,
        "unaggregated_accepted": "SELECT COUNT(*) FROM certificates WHERE aggregated = FALSE",
        "accepted_without_consumed_token": """
            SELECT COUNT(*)
            FROM certificates c
            LEFT JOIN source_tokens s ON c.source = s.source
            WHERE s.status != 'accepted'
        """,
    }
    return {name: int(conn.execute(sql).fetchone()[0]) for name, sql in checks.items()}


def query_latencies_pg(conn) -> tuple[float, float]:
    start = time.perf_counter()
    conn.execute(
        """
        SELECT c.price_id, SUM(p.unit_price), COUNT(*)
        FROM certificates c JOIN price_table p USING (price_id)
        GROUP BY c.price_id
        """
    ).fetchall()
    price_ms = (time.perf_counter() - start) * 1000.0

    start = time.perf_counter()
    conn.execute(
        """
        SELECT policy, workload, quality_class, COUNT(*)
        FROM certificates
        WHERE policy = %s
        GROUP BY policy, workload, quality_class
        """,
        (CURRENT_POLICY,),
    ).fetchall()
    accepted_ms = (time.perf_counter() - start) * 1000.0
    return price_ms, accepted_ms


def run_benchmark_once(dsn: dict[str, str], n: int, workers: int, fraud_rate: float, duplicate_rate: float) -> dict[str, object]:
    setup_db(dsn, n)
    attempts = make_attempts(n, fraud_rate, duplicate_rate)
    chunks = [attempts[i::workers] for i in range(workers)]
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(lambda chunk: worker_run(dsn, chunk), chunks))
    elapsed = time.perf_counter() - start

    reasons: dict[str, int] = {}
    latencies: list[float] = []
    for part in parts:
        for reason, count in part["reasons"].items():
            reasons[reason] = reasons.get(reason, 0) + int(count)
        latencies.extend(part["latencies_ms"])

    with connect(dsn) as conn:
        aggregation_ms = run_aggregation_pg(conn)
        invariants = invariant_checks_pg(conn)
        accepted = int(conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0])
        rejected = int(conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0])
        price_join_ms, accepted_only_ms = query_latencies_pg(conn)

    return {
        "workers": workers,
        "submitted": n,
        "accepted": accepted,
        "rejected": rejected,
        "reasons": reasons,
        "elapsed_ms": elapsed * 1000.0,
        "admissions_per_second": n / elapsed,
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else 0.0,
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
        "accepted_only_aggregation_ms": aggregation_ms,
        "accepted_only_query_ms": accepted_only_ms,
        "price_join_query_ms": price_join_ms,
        "invariants": invariants,
    }


def run_race(dsn: dict[str, str], source_race: bool, workers: int) -> dict[str, object]:
    setup_db(dsn, workers + 1)
    attempts: list[Attempt] = []
    for i in range(workers):
        attempts.append(
            Attempt(
                rid=f"pg-race-{i:03d}",
                seller="seller-000",
                session="session-00000",
                source="src-00000000" if source_race else f"src-{i:08d}",
                workload="reach",
                algorithm="BRR",
                policy=CURRENT_POLICY,
                price_id="price-gold-v5",
                quality_class="gold",
                counter=i if source_race else 0,
                nonce=f"pg-race-nonce-{i:03d}",
                expected_commitment=f"pg-race-commit-{i:03d}",
                submitted_commitment=f"pg-race-commit-{i:03d}",
                residual_bucket="deposit",
                y=i % 2,
            )
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda row: worker_run(dsn, [row]), attempts))
    with connect(dsn) as conn:
        aggregation_ms = run_aggregation_pg(conn)
        accepted = int(conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0])
        rejected = dict(conn.execute("SELECT reason, COUNT(*) FROM rejections GROUP BY reason").fetchall())
        invariants = invariant_checks_pg(conn)
    return {
        "scenario": "postgres_source_token_race" if source_race else "postgres_counter_race",
        "workers": workers,
        "accepted": accepted,
        "aggregation_ms": aggregation_ms,
        "rejected_by_reason": rejected,
        "invariants": invariants,
    }


def run_crash_recovery(dsn: dict[str, str]) -> dict[str, object]:
    setup_db(dsn, 8)
    now = time.time()
    with connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO precommits VALUES (%s, %s, %s, %s, %s, %s)",
                ("pg-crash-before-release", "pg-crash-eta-0", "src-00000000", "precommitted", now - 30.0, now - 30.0),
            )
            conn.execute(
                "INSERT INTO precommits VALUES (%s, %s, %s, %s, %s, %s)",
                ("pg-crash-after-release", "pg-crash-eta-1", "src-00000001", "released", now - 30.0, now - 20.0),
            )
            conn.execute(
                """
                INSERT INTO certificates
                (rid, seller, session, source, workload, algorithm, policy, price_id,
                 quality_class, counter, nonce, commitment, residual_bucket, accepted_at, aggregated)
                VALUES ('pg-accepted-before-aggregation', 'seller-002', 'session-00000',
                        'src-00000002', 'reach', 'BRR', %s, 'price-gold-v5', 'gold',
                        2, 'pg-nonce-crash', 'pg-commit-crash', 'deposit', %s, FALSE)
                """,
                (CURRENT_POLICY, now),
            )
            conn.execute(
                "UPDATE source_tokens SET status = 'accepted', consumed_by = 'pg-accepted-before-aggregation' WHERE source = 'src-00000002'"
            )
            conn.execute(
                "INSERT INTO aggregate_queue VALUES ('pg-accepted-before-aggregation', 'pg-nonce-crash', 'reach', 'gold', 'price-gold-v5', 1, FALSE)"
            )

        start = time.perf_counter()
        with conn.transaction():
            expired = conn.execute(
                "SELECT rid, nonce, source FROM precommits WHERE state IN ('precommitted', 'released') AND deadline < %s",
                (now,),
            ).fetchall()
            for rid, nonce, source in expired:
                conn.execute("UPDATE precommits SET state = 'missing', updated_at = %s WHERE rid = %s AND nonce = %s", (now, rid, nonce))
                conn.execute("UPDATE source_tokens SET status = 'missing', consumed_by = %s, updated_at = %s WHERE source = %s", (rid, now, source))
        recovery_ms = (time.perf_counter() - start) * 1000.0
        first_agg = run_aggregation_pg(conn)
        second_agg = run_aggregation_pg(conn)
        missing = int(conn.execute("SELECT COUNT(*) FROM precommits WHERE state = 'missing'").fetchone()[0])
        unaggregated = int(conn.execute("SELECT COUNT(*) FROM certificates WHERE aggregated = FALSE").fetchone()[0])
        aggregate_reports = int(conn.execute("SELECT COALESCE(SUM(reports), 0) FROM aggregates").fetchone()[0])
    return {
        "scenario": "postgres_crash_recovery",
        "missing_events": missing,
        "recovery_mark_missing_ms": recovery_ms,
        "first_aggregation_ms": first_agg,
        "second_aggregation_ms": second_agg,
        "unaggregated_accepted": unaggregated,
        "aggregate_reports_after_two_replays": aggregate_reports,
        "invariant_violations": 0 if missing == 2 and unaggregated == 0 and aggregate_reports == 1 else 1,
    }


def write_csv(results: dict[str, object]) -> None:
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "kind",
                "workers",
                "submitted",
                "accepted",
                "rejected",
                "admissions_per_second",
                "p50_ms",
                "p95_ms",
                "p99_ms",
                "aggregation_ms",
                "invariant_violations",
            ],
        )
        writer.writeheader()
        for row in results["admission_benchmarks"]:
            writer.writerow(
                {
                    "kind": "postgres_admission",
                    "workers": row["workers"],
                    "submitted": row["submitted"],
                    "accepted": row["accepted"],
                    "rejected": row["rejected"],
                    "admissions_per_second": f"{row['admissions_per_second']:.2f}",
                    "p50_ms": f"{row['latency_ms']['p50']:.4f}",
                    "p95_ms": f"{row['latency_ms']['p95']:.4f}",
                    "p99_ms": f"{row['latency_ms']['p99']:.4f}",
                    "aggregation_ms": f"{row['accepted_only_aggregation_ms']:.4f}",
                    "invariant_violations": sum(row["invariants"].values()),
                }
            )
        for row in results["race_tests"]:
            writer.writerow(
                {
                    "kind": row["scenario"],
                    "workers": row["workers"],
                    "submitted": row["workers"],
                    "accepted": row["accepted"],
                    "rejected": row["workers"] - row["accepted"],
                    "admissions_per_second": "",
                    "p50_ms": "",
                    "p95_ms": "",
                    "p99_ms": "",
                    "aggregation_ms": f"{row['aggregation_ms']:.4f}",
                    "invariant_violations": sum(row["invariants"].values()),
                }
            )
        crash = results["crash_recovery"]
        writer.writerow(
            {
                "kind": crash["scenario"],
                "workers": "",
                "submitted": "",
                "accepted": crash["aggregate_reports_after_two_replays"],
                "rejected": crash["missing_events"],
                "admissions_per_second": "",
                "p50_ms": "",
                "p95_ms": "",
                "p99_ms": "",
                "aggregation_ms": f"{crash['first_aggregation_ms']:.4f}",
                "invariant_violations": crash["invariant_violations"],
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100_000)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4, 16])
    parser.add_argument("--fraud-rate", type=float, default=0.10)
    parser.add_argument("--duplicate-rate", type=float, default=0.05)
    args = parser.parse_args()

    with temporary_postgres() as dsn:
        results = {
            "parameters": {
                "n": args.n,
                "workers": args.workers,
                "fraud_rate": args.fraud_rate,
                "duplicate_rate": args.duplicate_rate,
                "db": "postgresql-serializable-temp-cluster",
                "policy": CURRENT_POLICY,
            },
            "admission_benchmarks": [
                run_benchmark_once(dsn, args.n, workers, args.fraud_rate, args.duplicate_rate)
                for workers in args.workers
            ],
            "race_tests": [run_race(dsn, True, 32), run_race(dsn, False, 32)],
            "crash_recovery": run_crash_recovery(dsn),
        }
        results["postgres"] = {"dsn_public": {"host": dsn["host"], "port": dsn["port"], "user": dsn["user"]}}

    OUT_JSON.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(results)
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
