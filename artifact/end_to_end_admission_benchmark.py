#!/usr/bin/env python3
"""End-to-end certificate admission benchmark.

This script exercises the verifier as a transactional data-management
component rather than as a static query table. It uses only SQLite and the
Python standard library so that readers can rerun it locally. The benchmark
models source-token consumption, terminal/freshness-key insertion, price-row binding,
rejection auditing, accepted-only aggregation, concurrent duplicate races, and
crash/recovery states.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import sqlite3
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


OUT_JSON = Path(__file__).with_name("end_to_end_admission_benchmark.json")
OUT_CSV = Path(__file__).with_name("end_to_end_admission_benchmark.csv")

CURRENT_POLICY = "policy-v5"
PRICE_QUALITY = {
    "price-silver-v5": "silver",
    "price-gold-v5": "gold",
}


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 30000;

CREATE TABLE price_table (
  price_id TEXT PRIMARY KEY,
  quality_class TEXT NOT NULL,
  unit_price REAL NOT NULL
);

CREATE TABLE source_tokens (
  source TEXT PRIMARY KEY,
  seller TEXT NOT NULL,
  session TEXT NOT NULL,
  workload TEXT NOT NULL,
  status TEXT NOT NULL,
  consumed_by TEXT,
  updated_at REAL NOT NULL
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
  accepted_at REAL NOT NULL,
  aggregated INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(rid, nonce),
  UNIQUE(source),
  UNIQUE(seller, session, counter),
  UNIQUE(seller, session, nonce)
);

CREATE INDEX idx_cert_status_policy_workload ON certificates(policy, workload, quality_class);
CREATE INDEX idx_cert_price ON certificates(price_id);
CREATE INDEX idx_cert_aggregated ON certificates(aggregated);

CREATE TABLE rejections (
  rid TEXT NOT NULL,
  nonce TEXT NOT NULL,
  reason TEXT NOT NULL,
  source TEXT NOT NULL,
  seller TEXT NOT NULL,
  session TEXT NOT NULL,
  counter INTEGER NOT NULL,
  rejected_at REAL NOT NULL,
  PRIMARY KEY(rid, nonce)
);

CREATE INDEX idx_reject_reason ON rejections(reason);

CREATE TABLE aggregate_queue (
  rid TEXT NOT NULL,
  nonce TEXT NOT NULL,
  workload TEXT NOT NULL,
  quality_class TEXT NOT NULL,
  price_id TEXT NOT NULL,
  y INTEGER NOT NULL,
  processed INTEGER NOT NULL DEFAULT 0,
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
  deadline REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(rid, nonce)
);
"""


@dataclass(frozen=True)
class Attempt:
    rid: str
    seller: str
    session: str
    source: str
    workload: str
    algorithm: str
    policy: str
    price_id: str
    quality_class: str
    counter: int
    nonce: str
    expected_commitment: str
    submitted_commitment: str
    residual_bucket: str
    y: int


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: Path, n_sources: int) -> None:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO price_table VALUES (?, ?, ?)",
        [("price-silver-v5", "silver", 12.0), ("price-gold-v5", "gold", 20.0)],
    )
    now = time.time()
    conn.executemany(
        "INSERT INTO source_tokens VALUES (?, ?, ?, ?, 'issued', NULL, ?)",
        (
            (
                f"src-{i:08d}",
                f"seller-{i % 128:03d}",
                f"session-{i // 1000:05d}",
                ["reach", "conversion", "audience", "lift"][i % 4],
                now,
            )
            for i in range(n_sources)
        ),
    )
    conn.execute(
        "INSERT INTO source_tokens VALUES ('src-preconsumed', 'seller-reserved', 'session-reserved', 'race', 'accepted', 'reserved', ?)",
        (now,),
    )
    conn.commit()
    conn.close()


def make_attempts(n: int, fraud_rate: float, duplicate_rate: float) -> list[Attempt]:
    fraud_step = max(1, int(round(1.0 / fraud_rate))) if fraud_rate > 0 else n + 1
    duplicate_step = max(1, int(round(1.0 / duplicate_rate))) if duplicate_rate > 0 else n + 1
    workloads = ["reach", "conversion", "audience", "lift"]
    out: list[Attempt] = []
    for i in range(n):
        seller = f"seller-{i % 128:03d}"
        session = f"session-{i // 1000:05d}"
        source = f"src-{i:08d}"
        counter = i
        price_id = "price-gold-v5" if i % 2 else "price-silver-v5"
        quality = PRICE_QUALITY[price_id]
        policy = CURRENT_POLICY
        commitment = f"commit-{i:08d}"
        submitted = commitment

        if i > 0 and i % duplicate_step == 0:
            if (i // duplicate_step) % 2:
                source = "src-preconsumed"
            else:
                seller = "seller-000"
                session = "session-00000"
                counter = 0
        elif i > 0 and i % fraud_step == 0:
            kind = (i // fraud_step) % 4
            if kind == 0:
                policy = "policy-v4"
            elif kind == 1:
                price_id = "price-gold-v5"
                quality = "silver"
            elif kind == 2:
                submitted = f"mutated-{i:08d}"
            else:
                source = f"missing-src-{i:08d}"

        out.append(
            Attempt(
                rid=f"rid-{i:08d}",
                seller=seller,
                session=session,
                source=source,
                workload=workloads[i % len(workloads)],
                algorithm="OUE" if i % 3 else "BRR",
                policy=policy,
                price_id=price_id,
                quality_class=quality,
                counter=counter,
                nonce=f"nonce-{i:08d}",
                expected_commitment=commitment,
                submitted_commitment=submitted,
                residual_bucket="deposit" if i % 5 else "audit",
                y=i % 2,
            )
        )
    return out


def reject(conn: sqlite3.Connection, attempt: Attempt, reason: str) -> str:
    conn.execute(
        """
        INSERT OR REPLACE INTO rejections
        (rid, nonce, reason, source, seller, session, counter, rejected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (attempt.rid, attempt.nonce, reason, attempt.source, attempt.seller, attempt.session, attempt.counter, time.time()),
    )
    return reason


def classify_integrity_error(exc: sqlite3.IntegrityError) -> str:
    message = str(exc).lower()
    if "seller" in message and "counter" in message:
        return "duplicate_counter"
    if "seller" in message and "nonce" in message:
        return "nonce_reuse"
    if "source" in message:
        return "source_token_reuse"
    return "ledger_key_conflict"


def admit_one(conn: sqlite3.Connection, attempt: Attempt) -> str:
    if attempt.policy != CURRENT_POLICY:
        return reject(conn, attempt, "stale_policy")
    if PRICE_QUALITY.get(attempt.price_id) != attempt.quality_class:
        return reject(conn, attempt, "price_row_binding")
    if attempt.submitted_commitment != attempt.expected_commitment:
        return reject(conn, attempt, "report_commitment")

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE source_tokens
            SET status = 'consuming', consumed_by = ?, updated_at = ?
            WHERE source = ? AND status = 'issued'
            """,
            (attempt.rid, time.time(), attempt.source),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return reject(conn, attempt, "source_token_reuse")

        conn.execute(
            """
            INSERT INTO certificates
            (rid, seller, session, source, workload, algorithm, policy, price_id,
             quality_class, counter, nonce, commitment, residual_bucket, accepted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "INSERT INTO aggregate_queue VALUES (?, ?, ?, ?, ?, ?, 0)",
            (attempt.rid, attempt.nonce, attempt.workload, attempt.quality_class, attempt.price_id, attempt.y),
        )
        conn.execute(
            "UPDATE source_tokens SET status = 'accepted', updated_at = ? WHERE source = ?",
            (time.time(), attempt.source),
        )
        conn.execute("COMMIT")
        return "accepted"
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        return reject(conn, attempt, classify_integrity_error(exc))
    except sqlite3.OperationalError:
        conn.execute("ROLLBACK")
        return reject(conn, attempt, "ledger_busy")


def worker_run(db_path: Path, rows: list[Attempt]) -> dict[str, object]:
    conn = connect(db_path)
    reasons: dict[str, int] = {}
    latencies: list[float] = []
    for attempt in rows:
        start = time.perf_counter()
        result = admit_one(conn, attempt)
        latencies.append((time.perf_counter() - start) * 1000.0)
        reasons[result] = reasons.get(result, 0) + 1
    conn.close()
    return {"reasons": reasons, "latencies_ms": latencies}


def run_aggregation(conn: sqlite3.Connection) -> float:
    start = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(
        """
        SELECT workload, quality_class, price_id, COUNT(*) AS reports, SUM(y) AS sum_y
        FROM aggregate_queue
        WHERE processed = 0
        GROUP BY workload, quality_class, price_id
        """
    ).fetchall()
    for workload, quality, price_id, reports, sum_y in rows:
        conn.execute(
            """
            INSERT INTO aggregates VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workload, quality_class, price_id)
            DO UPDATE SET
              reports = reports + excluded.reports,
              sum_y = sum_y + excluded.sum_y
            """,
            (workload, quality, price_id, reports, sum_y or 0),
        )
    conn.execute("UPDATE aggregate_queue SET processed = 1 WHERE processed = 0")
    conn.execute("UPDATE certificates SET aggregated = 1 WHERE aggregated = 0")
    conn.execute("COMMIT")
    return (time.perf_counter() - start) * 1000.0


def invariant_checks(conn: sqlite3.Connection) -> dict[str, int]:
    checks = {
        "duplicate_sources": """
            SELECT COALESCE(SUM(cnt - 1), 0)
            FROM (SELECT source, COUNT(*) AS cnt FROM certificates GROUP BY source HAVING cnt > 1)
        """,
        "duplicate_counters": """
            SELECT COALESCE(SUM(cnt - 1), 0)
            FROM (
              SELECT seller, session, counter, COUNT(*) AS cnt
              FROM certificates
              GROUP BY seller, session, counter
              HAVING cnt > 1
            )
        """,
        "duplicate_nonces": """
            SELECT COALESCE(SUM(cnt - 1), 0)
            FROM (
              SELECT seller, session, nonce, COUNT(*) AS cnt
              FROM certificates
              GROUP BY seller, session, nonce
              HAVING cnt > 1
            )
        """,
        "unaggregated_accepted": "SELECT COUNT(*) FROM certificates WHERE aggregated = 0",
        "accepted_without_consumed_token": """
            SELECT COUNT(*)
            FROM certificates c
            LEFT JOIN source_tokens s ON c.source = s.source
            WHERE s.status != 'accepted'
        """,
    }
    return {name: int(conn.execute(sql).fetchone()[0]) for name, sql in checks.items()}


def run_benchmark_once(n: int, workers: int, fraud_rate: float, duplicate_rate: float) -> dict[str, object]:
    tmpdir = Path(tempfile.mkdtemp(prefix=f"e2e_admission_w{workers}_"))
    db_path = tmpdir / "admission.sqlite"
    init_db(db_path, n)
    attempts = make_attempts(n, fraud_rate, duplicate_rate)
    chunks = [attempts[i::workers] for i in range(workers)]

    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(lambda chunk: worker_run(db_path, chunk), chunks))
    elapsed = time.perf_counter() - start

    reasons: dict[str, int] = {}
    latencies: list[float] = []
    for part in parts:
        for reason, count in part["reasons"].items():
            reasons[reason] = reasons.get(reason, 0) + int(count)
        latencies.extend(part["latencies_ms"])

    conn = connect(db_path)
    aggregation_ms = run_aggregation(conn)
    invariants = invariant_checks(conn)
    accepted = int(conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0])
    rejected = int(conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0])
    price_join_ms, accepted_only_ms = query_latencies(conn)
    conn.close()

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
        "db_path": str(db_path),
    }


def query_latencies(conn: sqlite3.Connection) -> tuple[float, float]:
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
        WHERE policy = ?
        GROUP BY policy, workload, quality_class
        """,
        (CURRENT_POLICY,),
    ).fetchall()
    accepted_ms = (time.perf_counter() - start) * 1000.0
    return price_ms, accepted_ms


def run_race(source_race: bool, workers: int) -> dict[str, object]:
    tmpdir = Path(tempfile.mkdtemp(prefix="e2e_race_"))
    db_path = tmpdir / "race.sqlite"
    init_db(db_path, workers + 1)
    attempts: list[Attempt] = []
    for i in range(workers):
        source = "src-00000000" if source_race else f"src-{i:08d}"
        counter = i if source_race else 0
        attempts.append(
            Attempt(
                rid=f"race-{i:03d}",
                seller="seller-000",
                session="session-00000",
                source=source,
                workload="reach",
                algorithm="BRR",
                policy=CURRENT_POLICY,
                price_id="price-gold-v5",
                quality_class="gold",
                counter=counter,
                nonce=f"race-nonce-{i:03d}",
                expected_commitment=f"race-commit-{i:03d}",
                submitted_commitment=f"race-commit-{i:03d}",
                residual_bucket="deposit",
                y=i % 2,
            )
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(lambda row: worker_run(db_path, [row]), attempts))
    conn = connect(db_path)
    aggregation_ms = run_aggregation(conn)
    accepted = int(conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0])
    rejected_by_reason = dict(conn.execute("SELECT reason, COUNT(*) FROM rejections GROUP BY reason").fetchall())
    invariants = invariant_checks(conn)
    conn.close()
    return {
        "scenario": "source_token_race" if source_race else "counter_race",
        "workers": workers,
        "accepted": accepted,
        "aggregation_ms": aggregation_ms,
        "rejected_by_reason": rejected_by_reason,
        "invariants": invariants,
    }


def run_crash_recovery() -> dict[str, object]:
    tmpdir = Path(tempfile.mkdtemp(prefix="e2e_crash_"))
    db_path = tmpdir / "crash.sqlite"
    init_db(db_path, 8)
    conn = connect(db_path)
    now = time.time()
    conn.executemany(
        "INSERT INTO precommits VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("crash-before-release", "crash-eta-0", "src-00000000", "precommitted", now - 30.0, now - 30.0),
            ("crash-after-release", "crash-eta-1", "src-00000001", "released", now - 30.0, now - 20.0),
        ],
    )
    conn.execute(
        """
        INSERT INTO certificates
        (rid, seller, session, source, workload, algorithm, policy, price_id,
         quality_class, counter, nonce, commitment, residual_bucket, accepted_at, aggregated)
        VALUES ('accepted-before-aggregation', 'seller-002', 'session-00000',
                'src-00000002', 'reach', 'BRR', ?, 'price-gold-v5', 'gold',
                2, 'nonce-crash', 'commit-crash', 'deposit', ?, 0)
        """,
        (CURRENT_POLICY, now),
    )
    conn.execute(
        "UPDATE source_tokens SET status = 'accepted', consumed_by = 'accepted-before-aggregation' WHERE source = 'src-00000002'"
    )
    conn.execute(
        "INSERT INTO aggregate_queue VALUES ('accepted-before-aggregation', 'nonce-crash', 'reach', 'gold', 'price-gold-v5', 1, 0)"
    )
    conn.commit()

    start = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    expired = conn.execute(
        """
        SELECT rid, nonce, source
        FROM precommits
        WHERE state IN ('precommitted', 'released') AND deadline < ?
        """,
        (now,),
    ).fetchall()
    for rid, nonce, source in expired:
        conn.execute("UPDATE precommits SET state = 'missing', updated_at = ? WHERE rid = ? AND nonce = ?", (now, rid, nonce))
        conn.execute("UPDATE source_tokens SET status = 'missing', consumed_by = ?, updated_at = ? WHERE source = ?", (rid, now, source))
    conn.execute("COMMIT")
    recovery_mark_missing_ms = (time.perf_counter() - start) * 1000.0
    aggregation_ms = run_aggregation(conn)

    # Rerun aggregation to verify idempotence.
    second_aggregation_ms = run_aggregation(conn)
    missing = int(conn.execute("SELECT COUNT(*) FROM precommits WHERE state = 'missing'").fetchone()[0])
    unaggregated = int(conn.execute("SELECT COUNT(*) FROM certificates WHERE aggregated = 0").fetchone()[0])
    aggregate_reports = int(conn.execute("SELECT COALESCE(SUM(reports), 0) FROM aggregates").fetchone()[0])
    conn.close()
    return {
        "scenario": "crash_recovery",
        "missing_events": missing,
        "recovery_mark_missing_ms": recovery_mark_missing_ms,
        "first_aggregation_ms": aggregation_ms,
        "second_aggregation_ms": second_aggregation_ms,
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
                    "kind": "admission",
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
                    "aggregation_ms": "",
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


def parse_workers(values: Iterable[str]) -> list[int]:
    workers: list[int] = []
    for value in values:
        for part in value.replace(",", " ").split():
            workers.append(int(part))
    return workers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100_000, help="submitted attempts per worker-count run")
    parser.add_argument("--workers", nargs="+", default=["1", "4", "16", "32"], help="worker counts, space or comma separated")
    parser.add_argument("--fraud-rate", type=float, default=0.10)
    parser.add_argument("--duplicate-rate", type=float, default=0.05)
    args = parser.parse_args()

    worker_counts = parse_workers(args.workers)
    benchmarks = [
        run_benchmark_once(args.n, workers, args.fraud_rate, args.duplicate_rate)
        for workers in worker_counts
    ]
    race_workers = max(worker_counts) if worker_counts else 32
    results: dict[str, object] = {
        "parameters": {
            "n": args.n,
            "workers": worker_counts,
            "fraud_rate": args.fraud_rate,
            "duplicate_rate": args.duplicate_rate,
            "db": "sqlite-wal",
            "policy": CURRENT_POLICY,
        },
        "admission_benchmarks": benchmarks,
        "race_tests": [
            run_race(source_race=True, workers=race_workers),
            run_race(source_race=False, workers=race_workers),
        ],
        "crash_recovery": run_crash_recovery(),
    }
    OUT_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    write_csv(results)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
