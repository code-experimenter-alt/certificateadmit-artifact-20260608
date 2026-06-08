#!/usr/bin/env python3
"""Public web-session conversion comparison on UCI Online Shoppers.

The experiment uses traffic-source segments and purchase revenue labels as a
small public click/conversion-style workload. It complements the direct
marketing workload with session telemetry fields from an e-commerce site.
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
DATA = ROOT / "data" / "online_shoppers" / "online_shoppers_intention.csv"
OUT_CSV = Path(__file__).with_name("public_websession_workload.csv")
OUT_JSON = Path(__file__).with_name("public_websession_workload.json")
SEEDS = list(range(2026, 2056))
EPS_CLAIM = 2.0
EPS_TRUE_FRAUD = 0.5
FRAUD_FRACTION = 0.7
TOP_K = 3
MIN_SEGMENT_SESSIONS = 200


MECHANISMS = {
    "NoVerify": {"accepted_fraud": FRAUD_FRACTION, "truthful_switch": 0.0, "rejected_fraud": 0.0},
    "TEEOnly": {"accepted_fraud": 0.56, "truthful_switch": 0.0, "rejected_fraud": 0.14},
    "DepositAuditNoPoW": {"accepted_fraud": 0.073, "truthful_switch": 0.554, "rejected_fraud": 0.073},
    "DepositAuditReject": {"accepted_fraud": 0.0, "truthful_switch": 0.627, "rejected_fraud": 0.073},
    "SealedHybrid": {"accepted_fraud": 0.049, "truthful_switch": 0.602, "rejected_fraud": 0.049},
    "DirectAttest": {"accepted_fraud": 0.0, "truthful_switch": FRAUD_FRACTION, "rejected_fraud": 0.0},
    "RejectMismatch": {"accepted_fraud": 0.0, "truthful_switch": 0.0, "rejected_fraud": FRAUD_FRACTION},
}


def load_rows() -> list[dict[str, str]]:
    if not DATA.exists():
        raise SystemExit(f"missing {DATA}; download UCI Online Shoppers CSV")
    with DATA.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def revenue(row: dict[str, str]) -> bool:
    return row["Revenue"].strip().lower() == "true"


def build_segmenter(rows: list[dict[str, str]]):
    traffic_counts = Counter(row["TrafficType"] for row in rows)

    def segment(row: dict[str, str]) -> str:
        traffic = row["TrafficType"]
        if traffic_counts[traffic] >= MIN_SEGMENT_SESSIONS:
            return f"traffic_{traffic}"
        return "traffic_other"

    return segment


def rr_count(bits: list[int], epsilon: float, rng: random.Random) -> float:
    n = len(bits)
    if n == 0:
        return 0.0
    p = math.exp(epsilon) / (math.exp(epsilon) + 1.0)
    q = 1.0 / (math.exp(epsilon) + 1.0)
    observed = 0
    for bit in bits:
        observed += 1 if rng.random() < (p if bit else q) else 0
    return max(0.0, min(float(n), (observed - n * q) / (p - q)))


def split_bits(
    bits: list[int],
    accepted_fraud: float,
    truthful_switch: float,
    rejected_fraud: float,
    rng: random.Random,
) -> tuple[list[int], list[int], list[int]]:
    honest_bits = []
    fraud_bits = []
    switched_bits = []
    for bit in bits:
        draw = rng.random()
        if draw < accepted_fraud:
            fraud_bits.append(bit)
        elif draw < accepted_fraud + truthful_switch:
            switched_bits.append(bit)
        elif draw < accepted_fraud + truthful_switch + rejected_fraud:
            continue
        else:
            honest_bits.append(bit)
    return honest_bits, fraud_bits, switched_bits


def evaluate(rows: list[dict[str, str]], mechanism: str, seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    params = MECHANISMS[mechanism]
    segment = build_segmenter(rows)
    segments = sorted({segment(row) for row in rows})
    true_counts = Counter(segment(row) for row in rows)
    true_conversions = Counter(segment(row) for row in rows if revenue(row))
    true_rates = {seg: true_conversions[seg] / max(1, true_counts[seg]) for seg in segments}
    est_rates = {}
    rel_errors = []
    accepted_total = 0
    accepted_fraud_total = 0

    for seg in segments:
        audience_bits = [1 if segment(row) == seg else 0 for row in rows]
        conversion_bits = [1 if segment(row) == seg and revenue(row) else 0 for row in rows]

        aud_honest, aud_fraud, aud_switched = split_bits(
            audience_bits, params["accepted_fraud"], params["truthful_switch"], params["rejected_fraud"], rng
        )
        conv_honest, conv_fraud, conv_switched = split_bits(
            conversion_bits, params["accepted_fraud"], params["truthful_switch"], params["rejected_fraud"], rng
        )
        est_audience = (
            rr_count(aud_honest, EPS_CLAIM, rng)
            + rr_count(aud_switched, EPS_CLAIM, rng)
            + rr_count(aud_fraud, EPS_TRUE_FRAUD, rng)
        )
        est_conversion = (
            rr_count(conv_honest, EPS_CLAIM, rng)
            + rr_count(conv_switched, EPS_CLAIM, rng)
            + rr_count(conv_fraud, EPS_TRUE_FRAUD, rng)
        )
        accepted_total += len(aud_honest) + len(aud_switched) + len(aud_fraud)
        accepted_fraud_total += len(aud_fraud)
        rel_errors.append(abs(est_audience - true_counts[seg]) / max(1, true_counts[seg]))
        est_rates[seg] = est_conversion / max(1.0, est_audience)

    true_top = sorted(segments, key=lambda seg: true_rates[seg], reverse=True)[:TOP_K]
    est_top = sorted(segments, key=lambda seg: est_rates[seg], reverse=True)[:TOP_K]
    true_best = sum(true_rates[seg] for seg in true_top) / TOP_K
    selected = sum(true_rates[seg] for seg in est_top) / TOP_K
    regret = max(0.0, true_best - selected)
    utility = max(0.0, 20.0 - 180.0 * regret - 8.0 * statistics.mean(rel_errors))
    return {
        "accepted_fraud_rate": accepted_fraud_total / max(1, accepted_total),
        "accepted_volume_fraction": accepted_total / (len(rows) * len(segments)),
        "audience_error": statistics.mean(rel_errors),
        "top3_overlap": len(set(true_top) & set(est_top)) / TOP_K,
        "conversion_regret": regret,
        "buyer_utility": utility,
    }


def summarize(samples: list[float]) -> dict[str, float]:
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    return {
        "mean": statistics.mean(samples),
        "stdev": stdev,
        "ci95": 1.96 * stdev / math.sqrt(len(samples)) if samples else 0.0,
    }


def main() -> None:
    rows = load_rows()
    raw = []
    for mechanism in MECHANISMS:
        for seed in SEEDS:
            row = {"mechanism": mechanism, "seed": seed}
            row.update(evaluate(rows, mechanism, seed))
            raw.append(row)

    summary = []
    by_mech: dict[str, list[dict[str, float]]] = defaultdict(list)
    for row in raw:
        by_mech[row["mechanism"]].append(row)
    for mechanism, values in by_mech.items():
        out = {"mechanism": mechanism, "seeds": len(values)}
        for metric in [
            "accepted_fraud_rate",
            "accepted_volume_fraction",
            "audience_error",
            "top3_overlap",
            "conversion_regret",
            "buyer_utility",
        ]:
            stats = summarize([float(v[metric]) for v in values])
            out[f"{metric}_mean"] = stats["mean"]
            out[f"{metric}_stdev"] = stats["stdev"]
            out[f"{metric}_ci95"] = stats["ci95"]
        summary.append(out)

    fieldnames = list(summary[0].keys())
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary)

    payload = {
        "dataset": "UCI Online Shoppers Purchasing Intention online_shoppers_intention.csv",
        "rows": len(rows),
        "segments": len({build_segmenter(rows)(row) for row in rows}),
        "conversions": sum(1 for row in rows if revenue(row)),
        "segment_field": "TrafficType with rare sources merged",
        "min_segment_sessions": MIN_SEGMENT_SESSIONS,
        "fraud_fraction": FRAUD_FRACTION,
        "eps_claim": EPS_CLAIM,
        "eps_true_fraud": EPS_TRUE_FRAUD,
        "seeds": SEEDS,
        "mechanism_parameters": MECHANISMS,
        "summary": summary,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
