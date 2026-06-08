#!/usr/bin/env python3
"""Output-only statistical validation baseline for LDP budget over-claims.

The baseline tests whether a homogeneous batch of reports is statistically
consistent with the claimed epsilon. It uses only output counts and a known
workload prevalence/model, so it does not provide per-report execution identity,
source-token binding, freshness, or price-row binding.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path


OUT_CSV = Path(__file__).with_name("output_stat_baseline.csv")
OUT_JSON = Path(__file__).with_name("output_stat_baseline.json")
FALSE_REJECT = 0.05
Z_95 = 1.6448536269514722
NS = [100, 1_000, 10_000, 100_000]
EPS_PAIRS = [(0.5, 2.0), (1.0, 2.0), (1.5, 2.0), (2.0, 4.0), (3.0, 4.0)]


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def bernoulli_var(p: float) -> float:
    return p * (1.0 - p)


def br_moments(epsilon: float, prevalence: float) -> tuple[float, float]:
    exp_eps = math.exp(epsilon)
    p_true = exp_eps / (exp_eps + 1.0)
    p_false = 1.0 / (exp_eps + 1.0)
    mean_per_report = prevalence * p_true + (1.0 - prevalence) * p_false
    var_per_report = prevalence * bernoulli_var(p_true) + (1.0 - prevalence) * bernoulli_var(p_false)
    return mean_per_report, var_per_report


def oue_moments(epsilon: float, domain: int) -> tuple[float, float]:
    p_true = 0.5
    q = 1.0 / (math.exp(epsilon) + 1.0)
    mean_per_report = p_true + (domain - 1.0) * q
    var_per_report = bernoulli_var(p_true) + (domain - 1.0) * bernoulli_var(q)
    return mean_per_report, var_per_report


def power_for_count_test(
    reports: int,
    mean_claim_per_report: float,
    var_claim_per_report: float,
    mean_hidden_per_report: float,
    var_hidden_per_report: float,
) -> tuple[float, float, float, float, float, float]:
    """Return threshold and detection power for a one-sided high-noise test."""
    mean_claim = reports * mean_claim_per_report
    sd_claim = math.sqrt(max(1e-12, reports * var_claim_per_report))
    threshold = mean_claim + Z_95 * sd_claim
    mean_hidden = reports * mean_hidden_per_report
    sd_hidden = math.sqrt(max(1e-12, reports * var_hidden_per_report))
    power = normal_sf((threshold - mean_hidden) / sd_hidden)
    return threshold, power, mean_claim, sd_claim, mean_hidden, sd_hidden


def rows() -> list[dict[str, float | int | str]]:
    out: list[dict[str, float | int | str]] = []
    for eps_hidden, eps_claim in EPS_PAIRS:
        for n in NS:
            mean_claim_per_report, var_claim_per_report = br_moments(eps_claim, prevalence=0.2)
            mean_hidden_per_report, var_hidden_per_report = br_moments(eps_hidden, prevalence=0.2)
            threshold, power, mean_claim, sd_claim, mean_hidden, sd_hidden = power_for_count_test(
                n,
                mean_claim_per_report,
                var_claim_per_report,
                mean_hidden_per_report,
                var_hidden_per_report,
            )
            out.append(
                {
                    "protocol": "BRR",
                    "domain": 2,
                    "n": n,
                    "eps_hidden": eps_hidden,
                    "eps_claim": eps_claim,
                    "false_reject_rate": FALSE_REJECT,
                    "test_statistic": "number of output ones",
                    "mean_claim": mean_claim,
                    "sd_claim": sd_claim,
                    "mean_hidden": mean_hidden,
                    "sd_hidden": sd_hidden,
                    "threshold": threshold,
                    "detection_power": power,
                    "variance_model": "fixed-composition Poisson-binomial normal approximation",
                    "limitation": "batch-level output test; no execution identity, freshness, source-token, or price-row binding",
                }
            )

            domain = 32
            mean_claim_per_report, var_claim_per_report = oue_moments(eps_claim, domain)
            mean_hidden_per_report, var_hidden_per_report = oue_moments(eps_hidden, domain)
            threshold, power, mean_claim, sd_claim, mean_hidden, sd_hidden = power_for_count_test(
                n,
                mean_claim_per_report,
                var_claim_per_report,
                mean_hidden_per_report,
                var_hidden_per_report,
            )
            out.append(
                {
                    "protocol": "OUE",
                    "domain": domain,
                    "n": n,
                    "eps_hidden": eps_hidden,
                    "eps_claim": eps_claim,
                    "false_reject_rate": FALSE_REJECT,
                    "test_statistic": "total one bits in OUE vectors",
                    "mean_claim": mean_claim,
                    "sd_claim": sd_claim,
                    "mean_hidden": mean_hidden,
                    "sd_hidden": sd_hidden,
                    "threshold": threshold,
                    "detection_power": power,
                    "variance_model": "sum of one true-coordinate Bernoulli and d-1 false-coordinate Bernoulli variances",
                    "limitation": "batch-level output test; assumes known model/prevalence; no admission binding",
                }
            )
    return out


def main() -> None:
    data = rows()
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(data[0].keys()))
        writer.writeheader()
        writer.writerows(data)

    payload = {
        "baseline": "OUTSTAT",
        "description": "one-sided output-count test at fixed honest false-reject rate",
        "false_reject_rate": FALSE_REJECT,
        "assumptions": [
            "homogeneous batch",
            "fixed batch composition or known prevalence/model for the tested workload",
            "normal approximation to a Poisson-binomial output-count statistic",
        ],
        "limitations": [
            "no per-report execution identity",
            "no source-token, freshness, or price-row binding",
            "limited power for small batches or small epsilon gaps",
        ],
        "rows": data,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
