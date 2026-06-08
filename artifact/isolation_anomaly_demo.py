#!/usr/bin/env python3
"""Isolation anomaly demo for certificate admission.

This optional PostgreSQL artifact shows why certificate admission is specified
as an atomic serializable/linearizable admission contract rather than a naive
read-check-write workflow. The weak workflow intentionally omits the source
uniqueness constraint on accepted rows and separates the source-token read from
the write. Under READ COMMITTED, two concurrent admissions can both observe an
issued token and both accept. Under SERIALIZABLE, PostgreSQL aborts one branch.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from pathlib import Path

from psycopg import errors

from postgres_admission_benchmark import connect, temporary_postgres


OUT_JSON = Path(__file__).with_name("isolation_anomaly_demo.json")
OUT_CSV = Path(__file__).with_name("isolation_anomaly_demo.csv")


SCHEMA = """
DROP TABLE IF EXISTS weak_accepts, weak_source_tokens;

CREATE TABLE weak_source_tokens (
  source TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  updated_by TEXT,
  updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE weak_accepts (
  rid TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  worker TEXT NOT NULL,
  accepted_at DOUBLE PRECISION NOT NULL
);
"""


def setup(dsn: dict[str, str]) -> None:
    with connect(dsn) as conn:
        conn.execute(SCHEMA)
        conn.execute(
            "INSERT INTO weak_source_tokens VALUES ('src-shared', 'issued', NULL, %s)",
            (time.time(),),
        )
        conn.commit()


def naive_admit(dsn: dict[str, str], isolation: str, rid: str, barrier: threading.Barrier) -> str:
    """Run one intentionally weak admission branch."""
    with connect(dsn) as conn:
        try:
            conn.execute(f"BEGIN ISOLATION LEVEL {isolation}")
            status = conn.execute(
                "SELECT status FROM weak_source_tokens WHERE source = 'src-shared'"
            ).fetchone()[0]
            barrier.wait(timeout=5.0)
            if status != "issued":
                conn.rollback()
                return "rejected_not_issued"
            time.sleep(0.05)
            conn.execute(
                "INSERT INTO weak_accepts VALUES (%s, 'src-shared', %s, %s)",
                (rid, rid, time.time()),
            )
            conn.execute(
                "UPDATE weak_source_tokens SET status = 'accepted', updated_by = %s, updated_at = %s WHERE source = 'src-shared'",
                (rid, time.time()),
            )
            conn.commit()
            return "accepted"
        except errors.SerializationFailure:
            conn.rollback()
            return "serialization_failure"
        except Exception as exc:
            conn.rollback()
            return f"error:{exc.__class__.__name__}"


def count_state(dsn: dict[str, str]) -> dict[str, int | str]:
    with connect(dsn) as conn:
        accepted = int(conn.execute("SELECT COUNT(*) FROM weak_accepts").fetchone()[0])
        duplicate_sources = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(cnt - 1), 0)
                FROM (
                  SELECT source, COUNT(*) AS cnt
                  FROM weak_accepts
                  GROUP BY source
                  HAVING COUNT(*) > 1
                ) t
                """
            ).fetchone()[0]
        )
        final_status = conn.execute(
            "SELECT status FROM weak_source_tokens WHERE source = 'src-shared'"
        ).fetchone()[0]
    return {
        "accepted_rows": accepted,
        "duplicate_accepted_sources": duplicate_sources,
        "final_token_status": final_status,
    }


def run_isolation_case(dsn: dict[str, str], isolation: str) -> dict[str, object]:
    setup(dsn)
    barrier = threading.Barrier(2)
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda rid=rid: results.append(naive_admit(dsn, isolation, rid, barrier)))
        for rid in ("weak-a", "weak-b")
    ]
    start = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    state = count_state(dsn)
    return {
        "scenario": "naive_read_check_write",
        "isolation": isolation,
        "worker_results": sorted(results),
        "elapsed_ms": elapsed_ms,
        **state,
    }


def write_csv(rows: list[dict[str, object]]) -> None:
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "scenario",
                "isolation",
                "accepted_rows",
                "duplicate_accepted_sources",
                "serialization_failures",
                "final_token_status",
                "elapsed_ms",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "scenario": row["scenario"],
                    "isolation": row["isolation"],
                    "accepted_rows": row["accepted_rows"],
                    "duplicate_accepted_sources": row["duplicate_accepted_sources"],
                    "serialization_failures": row["worker_results"].count("serialization_failure"),
                    "final_token_status": row["final_token_status"],
                    "elapsed_ms": f"{row['elapsed_ms']:.4f}",
                }
            )


def main() -> None:
    with temporary_postgres() as dsn:
        rows = [
            run_isolation_case(dsn, "READ COMMITTED"),
            run_isolation_case(dsn, "SERIALIZABLE"),
        ]
    payload = {
        "artifact": "isolation_anomaly_demo",
        "description": "weak read-check-write source-token admission under READ COMMITTED versus SERIALIZABLE",
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(rows)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
