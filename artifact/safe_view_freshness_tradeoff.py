#!/usr/bin/env python3
"""Freshness tradeoff for safe materialized-view publication."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from marketplace_workflow_benchmark import admit, attempts, setup


OUT_JSON = Path(__file__).with_name("safe_view_freshness_tradeoff.json")
OUT_CSV = Path(__file__).with_name("safe_view_freshness_tradeoff.csv")


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def publish_pending(conn: sqlite3.Connection) -> dict[str, int | float]:
    start = time.perf_counter()
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
    published = 0
    rejected = 0
    for rid, src, seller, session, counter, workload, quality, policy, price_id, window in rows:
        reason = None
        if not conn.execute(
            "SELECT 1 FROM price_policy WHERE policy=? AND price_id=? AND workload=? AND quality=?",
            (policy, price_id, workload, quality),
        ).fetchone():
            reason = "price_policy_binding"
        elif not conn.execute("SELECT 1 FROM source_tokens WHERE src=? AND state='issued'", (src,)).fetchone():
            reason = "source_token_reuse"
        elif conn.execute(
            "SELECT 1 FROM aggregate_queue WHERE seller=? AND session=? AND counter=?",
            (seller, session, int(counter)),
        ).fetchone():
            reason = "duplicate_counter"
        if reason:
            conn.execute("UPDATE cert_ledger SET status='rejected' WHERE rid=?", (rid,))
            conn.execute("INSERT INTO reject_log VALUES (?, ?, ?, ?)", (rid, seller, workload, reason))
            rejected += 1
            continue
        conn.execute("UPDATE source_tokens SET state='consumed' WHERE src=?", (src,))
        conn.execute("UPDATE cert_ledger SET status='accepted' WHERE rid=?", (rid,))
        conn.execute(
            "INSERT INTO aggregate_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (rid, src, seller, session, counter, workload, quality, policy, price_id, window),
        )
        published += 1
    conn.commit()
    return {
        "batch_rows": len(rows),
        "published_rows": published,
        "rejected_rows": rejected,
        "publication_ms": (time.perf_counter() - start) * 1000.0,
    }


def run_batch_size(
    batch_size: int,
    n: int,
    arrival_rate_per_second: float,
    certificate_p95_ms: float,
) -> dict[str, int | float]:
    rows = attempts(n)
    with tempfile.NamedTemporaryFile(prefix="safe_view_freshness_", suffix=".sqlite") as tmp:
        conn = sqlite3.connect(tmp.name)
        setup(conn, rows)
        conn.execute("CREATE INDEX idx_safe_view_agg_counter ON aggregate_queue(seller, session, counter)")
        stage_latencies = []
        publications = []
        pipeline_start = time.perf_counter()
        for offset in range(0, n, batch_size):
            chunk = rows[offset : offset + batch_size]
            for row in chunk:
                t0 = time.perf_counter()
                admit(conn, row, "SafeMaterializedView")
                stage_latencies.append((time.perf_counter() - t0) * 1000.0)
            conn.commit()
            publications.append(publish_pending(conn))
        pipeline_ms = (time.perf_counter() - pipeline_start) * 1000.0
        staged_row_ms = sum(stage_latencies)
        accepted = conn.execute("SELECT COUNT(*) FROM aggregate_queue").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM reject_log").fetchone()[0]
        conn.close()

    visibility_delays = []
    for publication in publications:
        m = int(publication["batch_rows"])
        publication_ms = float(publication["publication_ms"])
        for position in range(m):
            fill_wait_ms = (m - 1 - position) / arrival_rate_per_second * 1000.0
            visibility_delays.append(fill_wait_ms + publication_ms)
    publication_ms_values = [float(row["publication_ms"]) for row in publications]
    p95_visible = percentile(visibility_delays, 0.95)
    return {
        "batch_size": batch_size,
        "submitted": n,
        "batches": len(publications),
        "accepted": int(accepted),
        "rejected": int(rejected),
        "staging_admissions_per_second": n / (staged_row_ms / 1000.0),
        "published_pipeline_per_second": n / (pipeline_ms / 1000.0),
        "stage_p95_ms": percentile(stage_latencies, 0.95),
        "publication_total_ms": sum(publication_ms_values),
        "publication_mean_ms": statistics.fmean(publication_ms_values),
        "publication_p95_ms": percentile(publication_ms_values, 0.95),
        "visibility_delay_mean_ms": statistics.fmean(visibility_delays),
        "visibility_delay_p95_ms": p95_visible,
        "visibility_delay_max_ms": max(visibility_delays),
        "certificate_admit_reference_p95_ms": certificate_p95_ms,
        "p95_delay_over_certificate_ms": p95_visible - certificate_p95_ms,
        "max_withheld_rows": min(batch_size, n),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=60_000)
    parser.add_argument("--arrival-rate", type=float, default=32_772.0)
    parser.add_argument("--certificate-p95-ms", type=float, default=0.0416)
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[128, 512, 2048, 8192, 60_000])
    args = parser.parse_args()
    payload = {
        "artifact": "safe_view_freshness_tradeoff",
        "description": "safe materialized-view publication latency and query-visible freshness delay",
        "parameters": {
            "n": args.n,
            "arrival_rate_per_second": args.arrival_rate,
            "certificate_admit_reference_p95_ms": args.certificate_p95_ms,
            "delay_model": "records are query-visible only after their batch fills and the validation watermark publishes",
        },
        "rows": [
            run_batch_size(batch_size, args.n, args.arrival_rate, args.certificate_p95_ms)
            for batch_size in args.batch_sizes
        ],
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "batch_size",
                "submitted",
                "batches",
                "accepted",
                "rejected",
                "staging_admissions_per_second",
                "published_pipeline_per_second",
                "stage_p95_ms",
                "publication_total_ms",
                "publication_mean_ms",
                "publication_p95_ms",
                "visibility_delay_mean_ms",
                "visibility_delay_p95_ms",
                "visibility_delay_max_ms",
                "certificate_admit_reference_p95_ms",
                "p95_delay_over_certificate_ms",
                "max_withheld_rows",
            ],
        )
        writer.writeheader()
        for row in payload["rows"]:
            writer.writerow(row)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
