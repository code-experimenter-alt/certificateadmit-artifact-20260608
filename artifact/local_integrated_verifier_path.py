#!/usr/bin/env python3
"""Local integrated verifier-path benchmark for CertificateAdmit.

This script keeps all components in one locally reproducible path: source-token
state, a lightweight attestation/policy digest check, serializable SQLite
admission, deterministic rejection provenance, accepted-only queueing, and
aggregation. It is not a live AWS Nitro/KMS/replicated-ledger benchmark; the
cloud path remains covered by recorded Nitro components and the deployment
envelope artifact.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

import end_to_end_admission_benchmark as e2e


OUT_JSON = Path(__file__).with_name("local_integrated_verifier_path.json")
OUT_CSV = Path(__file__).with_name("local_integrated_verifier_path.csv")


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def attested_policy_digest(attempt: e2e.Attempt) -> str:
    payload = "|".join(
        [
            attempt.rid,
            attempt.seller,
            attempt.session,
            attempt.source,
            attempt.policy,
            attempt.price_id,
            attempt.quality_class,
            str(attempt.counter),
            attempt.nonce,
            attempt.submitted_commitment,
            attempt.residual_bucket,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def integrated_worker(db_path: Path, rows: list[e2e.Attempt]) -> dict[str, object]:
    conn = e2e.connect(db_path)
    reasons: dict[str, int] = {}
    latencies: list[float] = []
    digest_latencies: list[float] = []
    for attempt in rows:
        start = time.perf_counter()
        digest_start = time.perf_counter()
        digest = attested_policy_digest(attempt)
        digest_latencies.append((time.perf_counter() - digest_start) * 1000.0)
        if len(digest) != 64:
            result = e2e.reject(conn, attempt, "attestation_digest")
        else:
            result = e2e.admit_one(conn, attempt)
        latencies.append((time.perf_counter() - start) * 1000.0)
        reasons[result] = reasons.get(result, 0) + 1
    conn.close()
    return {"reasons": reasons, "latencies_ms": latencies, "digest_latencies_ms": digest_latencies}


def run_once(n: int, workers: int, fraud_rate: float, duplicate_rate: float) -> dict[str, object]:
    tmpdir = Path(tempfile.mkdtemp(prefix=f"local_integrated_w{workers}_"))
    db_path = tmpdir / "integrated.sqlite"
    e2e.init_db(db_path, n)
    attempts = e2e.make_attempts(n, fraud_rate, duplicate_rate)
    chunks = [attempts[i::workers] for i in range(workers)]

    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(lambda chunk: integrated_worker(db_path, chunk), chunks))
    elapsed = time.perf_counter() - start

    reasons: dict[str, int] = {}
    latencies: list[float] = []
    digest_latencies: list[float] = []
    for part in parts:
        for reason, count in part["reasons"].items():
            reasons[reason] = reasons.get(reason, 0) + int(count)
        latencies.extend(part["latencies_ms"])
        digest_latencies.extend(part["digest_latencies_ms"])

    conn = e2e.connect(db_path)
    aggregation_ms = e2e.run_aggregation(conn)
    invariants = e2e.invariant_checks(conn)
    accepted = int(conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0])
    rejected = int(conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0])
    price_join_ms, accepted_only_ms = e2e.query_latencies(conn)
    conn.close()

    return {
        "workers": workers,
        "submitted": n,
        "accepted": accepted,
        "rejected": rejected,
        "reasons": reasons,
        "elapsed_ms": elapsed * 1000.0,
        "reports_per_second": n / elapsed,
        "end_to_end_latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else 0.0,
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
        "attestation_digest_ms": {
            "mean": statistics.fmean(digest_latencies) if digest_latencies else 0.0,
            "p95": percentile(digest_latencies, 95),
        },
        "accepted_only_aggregation_ms": aggregation_ms,
        "accepted_only_query_ms": accepted_only_ms,
        "price_join_query_ms": price_join_ms,
        "invariants": invariants,
    }


def write_outputs(payload: dict[str, object]) -> None:
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "workers",
                "submitted",
                "accepted",
                "rejected",
                "reports_per_second",
                "p50_ms",
                "p95_ms",
                "p99_ms",
                "digest_p95_ms",
                "aggregation_ms",
                "invariant_sum",
            ],
        )
        writer.writeheader()
        for row in payload["admission_benchmarks"]:
            writer.writerow(
                {
                    "workers": row["workers"],
                    "submitted": row["submitted"],
                    "accepted": row["accepted"],
                    "rejected": row["rejected"],
                    "reports_per_second": row["reports_per_second"],
                    "p50_ms": row["end_to_end_latency_ms"]["p50"],
                    "p95_ms": row["end_to_end_latency_ms"]["p95"],
                    "p99_ms": row["end_to_end_latency_ms"]["p99"],
                    "digest_p95_ms": row["attestation_digest_ms"]["p95"],
                    "aggregation_ms": row["accepted_only_aggregation_ms"],
                    "invariant_sum": sum(row["invariants"].values()),
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20_000)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4, 16])
    parser.add_argument("--fraud-rate", type=float, default=0.10)
    parser.add_argument("--duplicate-rate", type=float, default=0.05)
    args = parser.parse_args()

    rows = [run_once(args.n, workers, args.fraud_rate, args.duplicate_rate) for workers in args.workers]
    payload = {
        "artifact": "local_integrated_verifier_path",
        "description": "local integrated source-token, attestation-digest, serializable admission, rejection, queue, and aggregation path",
        "limitations": [
            "local SQLite-WAL verifier path, not live AWS Nitro",
            "KMS/session release is represented as policy input, not a live KMS call",
            "not a replicated-ledger or production-cloud deployment",
        ],
        "parameters": {
            "n": args.n,
            "workers": args.workers,
            "fraud_rate": args.fraud_rate,
            "duplicate_rate": args.duplicate_rate,
            "policy": e2e.CURRENT_POLICY,
        },
        "admission_benchmarks": rows,
        "race_tests": [e2e.run_race(True, max(args.workers)), e2e.run_race(False, max(args.workers))],
        "crash_recovery": e2e.run_crash_recovery(),
    }
    write_outputs(payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
