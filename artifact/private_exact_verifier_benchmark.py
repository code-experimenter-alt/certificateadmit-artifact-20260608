#!/usr/bin/env python3
"""PrivateExact verifier/transcript benchmark.

This local artifact exercises the restricted-disclosure exact verifier path:
the private verifier sees exact budget state, emits a signed decision
transcript, and the public admission layer stores/verifies only the decision,
reason class, verifier PCR, transcript hash, and signature.  It is not a
production clean-room or Nitro verifier; it is a reproducible latency, leakage,
and failure-handling check for the PrivateExact contract surface.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import hmac
import json
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import end_to_end_admission_benchmark as e2e


OUT_JSON = Path(__file__).with_name("private_exact_verifier_benchmark.json")
OUT_CSV = Path(__file__).with_name("private_exact_verifier_benchmark.csv")

VERIFIER_ID = "private-exact-verifier-01"
ALLOWED_PCR = "pcr-private-exact-v1"
STALE_PCR = "pcr-private-exact-v0"
TRANSCRIPT_KEY = b"certificateadmit-private-exact-local-transcript-key"


@dataclass(frozen=True)
class PrivateAttempt:
    attempt: e2e.Attempt
    eps_h: float
    eps_claimed: float
    verifier_pcr: str


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(payload: dict[str, object]) -> str:
    return hmac.new(TRANSCRIPT_KEY, canonical_json(payload), hashlib.sha256).hexdigest()


def exact_state_hash(row: PrivateAttempt) -> str:
    private_payload = {
        "rid": row.attempt.rid,
        "nonce": row.attempt.nonce,
        "source": row.attempt.source,
        "policy": row.attempt.policy,
        "price_id": row.attempt.price_id,
        "commitment": row.attempt.submitted_commitment,
        "eps_h": f"{row.eps_h:.6f}",
        "eps_claimed": f"{row.eps_claimed:.6f}",
    }
    return hashlib.sha256(canonical_json(private_payload)).hexdigest()


def make_private_attempts(n: int, mismatch_rate: float, stale_verifier_rate: float) -> list[PrivateAttempt]:
    mismatch_step = max(1, int(round(1.0 / mismatch_rate))) if mismatch_rate > 0 else n + 1
    stale_step = max(1, int(round(1.0 / stale_verifier_rate))) if stale_verifier_rate > 0 else n + 1
    attempts = e2e.make_attempts(n, fraud_rate=0.0, duplicate_rate=0.0)
    out: list[PrivateAttempt] = []
    for i, attempt in enumerate(attempts):
        eps_claimed = 2.0 if attempt.quality_class == "gold" else 1.0
        eps_h = eps_claimed
        verifier_pcr = ALLOWED_PCR
        if i > 0 and i % mismatch_step == 0:
            eps_h = eps_claimed / 2.0
        if i > 0 and i % stale_step == 0:
            verifier_pcr = STALE_PCR
        out.append(PrivateAttempt(attempt=attempt, eps_h=eps_h, eps_claimed=eps_claimed, verifier_pcr=verifier_pcr))
    return out


def private_verifier_transcript(row: PrivateAttempt) -> dict[str, object]:
    if row.eps_h + 1e-12 < row.eps_claimed:
        decision = "reject"
        reason = "private_exact_mismatch"
    else:
        decision = "accept"
        reason = "exact_budget_ok"

    transcript_hash = exact_state_hash(row)
    public_transcript: dict[str, object] = {
        "verifier_id": VERIFIER_ID,
        "verifier_pcr": row.verifier_pcr,
        "rid": row.attempt.rid,
        "nonce": row.attempt.nonce,
        "policy": row.attempt.policy,
        "price_id": row.attempt.price_id,
        "decision": decision,
        "reason_class": reason,
        "transcript_hash": transcript_hash,
        "commitment": row.attempt.submitted_commitment,
    }
    public_transcript["signature"] = sign_payload(public_transcript)
    return public_transcript


def public_verify_transcript(transcript: dict[str, object]) -> tuple[bool, str]:
    if transcript.get("verifier_id") != VERIFIER_ID:
        return False, "verifier_id"
    if transcript.get("verifier_pcr") != ALLOWED_PCR:
        return False, "verifier_not_allowlisted"
    received = str(transcript.get("signature", ""))
    unsigned = {key: value for key, value in transcript.items() if key != "signature"}
    expected = sign_payload(unsigned)
    if not hmac.compare_digest(received, expected):
        return False, "transcript_signature"
    return True, "verified"


def public_leakage_violations(transcript: dict[str, object]) -> int:
    forbidden = ("eps", "epsilon")
    for key, value in transcript.items():
        lowered_key = str(key).lower()
        lowered_value = str(value).lower()
        if any(token in lowered_key or token in lowered_value for token in forbidden):
            return 1
    return 0


def worker_run(db_path: Path, rows: list[PrivateAttempt]) -> dict[str, object]:
    conn = e2e.connect(db_path)
    reasons: dict[str, int] = {}
    total_latencies: list[float] = []
    private_latencies: list[float] = []
    public_latencies: list[float] = []
    admission_latencies: list[float] = []
    leakage_violations = 0

    for row in rows:
        start = time.perf_counter()
        private_start = time.perf_counter()
        transcript = private_verifier_transcript(row)
        private_latencies.append((time.perf_counter() - private_start) * 1000.0)
        leakage_violations += public_leakage_violations(transcript)

        public_start = time.perf_counter()
        verified, verify_reason = public_verify_transcript(transcript)
        public_latencies.append((time.perf_counter() - public_start) * 1000.0)

        admission_start = time.perf_counter()
        if not verified:
            result = e2e.reject(conn, row.attempt, verify_reason)
        elif transcript["decision"] == "accept":
            result = e2e.admit_one(conn, row.attempt)
        else:
            result = e2e.reject(conn, row.attempt, str(transcript["reason_class"]))
        admission_latencies.append((time.perf_counter() - admission_start) * 1000.0)

        total_latencies.append((time.perf_counter() - start) * 1000.0)
        reasons[result] = reasons.get(result, 0) + 1

    conn.close()
    return {
        "reasons": reasons,
        "total_ms": total_latencies,
        "private_verifier_ms": private_latencies,
        "public_verify_ms": public_latencies,
        "admission_ms": admission_latencies,
        "leakage_violations": leakage_violations,
    }


def merge_parts(parts: list[dict[str, object]]) -> dict[str, object]:
    reasons: dict[str, int] = {}
    merged = {
        "total_ms": [],
        "private_verifier_ms": [],
        "public_verify_ms": [],
        "admission_ms": [],
    }
    leakage_violations = 0
    for part in parts:
        for reason, count in part["reasons"].items():
            reasons[reason] = reasons.get(reason, 0) + int(count)
        for key in merged:
            merged[key].extend(part[key])
        leakage_violations += int(part["leakage_violations"])
    return {"reasons": reasons, "latencies": merged, "leakage_violations": leakage_violations}


def summarize_latencies(values: list[float]) -> dict[str, float]:
    return {
        "mean": statistics.fmean(values) if values else 0.0,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def run_once(n: int, workers: int, mismatch_rate: float, stale_verifier_rate: float) -> dict[str, object]:
    tmpdir = Path(tempfile.mkdtemp(prefix=f"private_exact_w{workers}_"))
    db_path = tmpdir / "private_exact.sqlite"
    e2e.init_db(db_path, n)
    rows = make_private_attempts(n, mismatch_rate, stale_verifier_rate)
    chunks = [rows[i::workers] for i in range(workers)]

    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(lambda chunk: worker_run(db_path, chunk), chunks))
    elapsed = time.perf_counter() - start
    merged = merge_parts(parts)

    conn = e2e.connect(db_path)
    aggregation_ms = e2e.run_aggregation(conn)
    invariants = e2e.invariant_checks(conn)
    accepted = int(conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0])
    rejected = int(conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0])
    price_join_ms, accepted_only_ms = e2e.query_latencies(conn)
    conn.close()

    latency_rows = merged["latencies"]
    return {
        "workers": workers,
        "submitted": n,
        "accepted": accepted,
        "rejected": rejected,
        "reasons": merged["reasons"],
        "elapsed_ms": elapsed * 1000.0,
        "reports_per_second": n / elapsed,
        "total_latency_ms": summarize_latencies(latency_rows["total_ms"]),
        "private_verifier_ms": summarize_latencies(latency_rows["private_verifier_ms"]),
        "public_dispute_verify_ms": summarize_latencies(latency_rows["public_verify_ms"]),
        "admission_ms": summarize_latencies(latency_rows["admission_ms"]),
        "accepted_only_aggregation_ms": aggregation_ms,
        "accepted_only_query_ms": accepted_only_ms,
        "price_join_query_ms": price_join_ms,
        "invariants": invariants,
        "public_leakage_violations": merged["leakage_violations"],
    }


def negative_controls() -> dict[str, object]:
    row = make_private_attempts(2, mismatch_rate=0.0, stale_verifier_rate=0.0)[1]
    transcript = private_verifier_transcript(row)
    tampered = dict(transcript)
    tampered["decision"] = "accept" if transcript["decision"] == "reject" else "reject"
    tampered_ok, tampered_reason = public_verify_transcript(tampered)

    stale = PrivateAttempt(row.attempt, row.eps_h, row.eps_claimed, STALE_PCR)
    stale_transcript = private_verifier_transcript(stale)
    stale_ok, stale_reason = public_verify_transcript(stale_transcript)

    mismatch = PrivateAttempt(row.attempt, row.eps_claimed / 2.0, row.eps_claimed, ALLOWED_PCR)
    mismatch_transcript = private_verifier_transcript(mismatch)

    return {
        "public_transcript_fields": sorted(transcript.keys()),
        "leakage_violations_in_example": public_leakage_violations(transcript),
        "tampered_signature_rejected": not tampered_ok,
        "tampered_signature_reason": tampered_reason,
        "stale_verifier_rejected": not stale_ok,
        "stale_verifier_reason": stale_reason,
        "budget_mismatch_decision": mismatch_transcript["decision"],
        "budget_mismatch_reason": mismatch_transcript["reason_class"],
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
                "total_p95_ms",
                "private_verifier_p95_ms",
                "public_dispute_verify_p95_ms",
                "admission_p95_ms",
                "leakage_violations",
                "invariant_sum",
            ],
        )
        writer.writeheader()
        for row in payload["benchmarks"]:
            writer.writerow(
                {
                    "workers": row["workers"],
                    "submitted": row["submitted"],
                    "accepted": row["accepted"],
                    "rejected": row["rejected"],
                    "reports_per_second": row["reports_per_second"],
                    "total_p95_ms": row["total_latency_ms"]["p95"],
                    "private_verifier_p95_ms": row["private_verifier_ms"]["p95"],
                    "public_dispute_verify_p95_ms": row["public_dispute_verify_ms"]["p95"],
                    "admission_p95_ms": row["admission_ms"]["p95"],
                    "leakage_violations": row["public_leakage_violations"],
                    "invariant_sum": sum(row["invariants"].values()),
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20_000)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4, 16])
    parser.add_argument("--mismatch-rate", type=float, default=0.10)
    parser.add_argument("--stale-verifier-rate", type=float, default=0.005)
    args = parser.parse_args()

    benchmarks = [run_once(args.n, workers, args.mismatch_rate, args.stale_verifier_rate) for workers in args.workers]
    payload = {
        "artifact": "private_exact_verifier_benchmark",
        "description": "local PrivateExact exact-budget verifier transcript, public dispute verification, leakage, and admission benchmark",
        "limitations": [
            "local HMAC transcript signature for reproducibility; production deployment should use an allow-listed public-key verifier or enclave signature",
            "exact budget state is modeled as private verifier input; this is not a clean-room or Nitro run",
            "KMS/session release, verifier governance, and replicated ledger operations are deployment assumptions",
        ],
        "parameters": {
            "n": args.n,
            "workers": args.workers,
            "mismatch_rate": args.mismatch_rate,
            "stale_verifier_rate": args.stale_verifier_rate,
            "allowed_pcr": ALLOWED_PCR,
            "verifier_id": VERIFIER_ID,
        },
        "benchmarks": benchmarks,
        "negative_controls": negative_controls(),
    }
    write_outputs(payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
