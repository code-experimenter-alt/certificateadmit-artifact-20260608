#!/usr/bin/env python3
"""Public direct-marketing workload sanity check.

Uses the UCI Bank Marketing `bank-full.csv` file to build a small public
workload for the manuscript: job-segment audience counts and top-3 conversion
allocation under binary randomized response.
"""

from __future__ import annotations

import csv
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "bank_marketing" / "bank" / "bank-full.csv"
OUT_CSV = Path(__file__).with_name("public_marketing_workload_summary.csv")
OUT_JSON = Path(__file__).with_name("public_marketing_workload_summary.json")
SEEDS = [2026, 2027, 2028, 2029, 2030]
EPS_VALUES = [0.5, 2.0, 8.0]


def summarize(samples: list[float]) -> dict[str, float]:
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    return {
        "mean": statistics.mean(samples),
        "stdev": stdev,
        "ci95": 1.96 * stdev / math.sqrt(len(samples)) if samples else 0.0,
    }


def load_rows() -> list[dict[str, str]]:
    if not DATA.exists():
        raise SystemExit(f"missing {DATA}; download UCI Bank Marketing data first")
    with DATA.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def rr_count(bits: list[int], eps: float, rng: random.Random) -> float:
    n = len(bits)
    p = math.exp(eps) / (math.exp(eps) + 1.0)
    q = 1.0 / (math.exp(eps) + 1.0)
    observed = 0
    for bit in bits:
        observed += 1 if rng.random() < (p if bit else q) else 0
    estimate = (observed - n * q) / (p - q)
    return max(0.0, min(float(n), estimate))


def evaluate(rows: list[dict[str, str]], eps: float, seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    jobs = sorted({row["job"] for row in rows})
    true_counts = Counter(row["job"] for row in rows)
    true_conversions = Counter(row["job"] for row in rows if row["y"] == "yes")
    rel_errors = []
    est_rates = {}
    true_rates = {}

    for job in jobs:
        audience_bits = [1 if row["job"] == job else 0 for row in rows]
        conversion_bits = [1 if row["job"] == job and row["y"] == "yes" else 0 for row in rows]
        est_count = rr_count(audience_bits, eps, rng)
        est_conv = rr_count(conversion_bits, eps, rng)
        rel_errors.append(abs(est_count - true_counts[job]) / max(1, true_counts[job]))
        est_rates[job] = est_conv / max(1.0, est_count)
        true_rates[job] = true_conversions[job] / max(1, true_counts[job])

    true_top3 = sorted(jobs, key=lambda j: true_rates[j], reverse=True)[:3]
    est_top3 = sorted(jobs, key=lambda j: est_rates[j], reverse=True)[:3]
    true_best = sum(true_rates[j] for j in true_top3) / 3.0
    selected = sum(true_rates[j] for j in est_top3) / 3.0
    return {
        "audience_mean_relative_error": statistics.mean(rel_errors),
        "top3_overlap": len(set(true_top3) & set(est_top3)) / 3.0,
        "top3_conversion_regret": max(0.0, true_best - selected),
    }


def main() -> None:
    rows = load_rows()
    by_eps: dict[float, list[dict[str, float]]] = defaultdict(list)
    for eps in EPS_VALUES:
        for seed in SEEDS:
            by_eps[eps].append(evaluate(rows, eps, seed))

    summary_rows = []
    for eps, values in by_eps.items():
        row = {"epsilon": eps}
        for metric in values[0]:
            stats = summarize([v[metric] for v in values])
            row[f"{metric}_mean"] = stats["mean"]
            row[f"{metric}_stdev"] = stats["stdev"]
            row[f"{metric}_ci95"] = stats["ci95"]
        summary_rows.append(row)

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)

    metadata = {
        "dataset": "UCI Bank Marketing bank-full.csv",
        "rows": len(rows),
        "segments": len({row["job"] for row in rows}),
        "conversions": sum(1 for row in rows if row["y"] == "yes"),
        "seeds": SEEDS,
        "summary": summary_rows,
    }
    OUT_JSON.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
