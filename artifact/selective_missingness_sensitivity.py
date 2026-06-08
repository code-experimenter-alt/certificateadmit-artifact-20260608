#!/usr/bin/env python3
"""Sensitivity table for selective missingness after source-token consumption.

CertificateAdmit does not prove missing-at-random behavior. This deterministic
artifact quantifies what the verifier can observe: a consumed-but-missing rate
and the resulting decision under simple fill-rate SLAs. The table is deliberately
not a privacy proof; it is a source-contract stress test.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


OUT_CSV = Path(__file__).with_name("selective_missingness_sensitivity.csv")
OUT_JSON = Path(__file__).with_name("selective_missingness_sensitivity.json")
OUT_SUMMARY_CSV = Path(__file__).with_name("selective_missingness_mitigation_summary.csv")
OUT_SUMMARY_JSON = Path(__file__).with_name("selective_missingness_mitigation_summary.json")


def status(missing_rate: float, sla_warn: float, sla_reject: float) -> str:
    if missing_rate > sla_reject:
        return "reject_cohort"
    if missing_rate > sla_warn:
        return "reprice_or_audit"
    return "accept_under_sla"


def main() -> None:
    n = 100_000
    true_positive_rate = 0.05
    positives = n * true_positive_rate
    negatives = n - positives
    policies = [
        ("baseline_5_10", 0.05, 0.10),
        ("strict_2_5", 0.02, 0.05),
        ("strict_1_3", 0.01, 0.03),
    ]
    rows = []
    summary_rows = []
    for policy_name, sla_warn, sla_reject in policies:
        policy_rows = []
        for pos_drop in [0.0, 0.05, 0.10, 0.20, 0.35, 0.50]:
            for neg_drop in [0.0, 0.02, 0.05, 0.10]:
                missing_pos = positives * pos_drop
                missing_neg = negatives * neg_drop
                accepted_pos = positives - missing_pos
                accepted_neg = negatives - missing_neg
                accepted = accepted_pos + accepted_neg
                missing = missing_pos + missing_neg
                missing_rate = missing / n
                observed_rate = accepted_pos / accepted if accepted else 0.0
                row = {
                    "policy": policy_name,
                    "n": n,
                    "true_positive_rate": true_positive_rate,
                    "sla_warn": sla_warn,
                    "sla_reject": sla_reject,
                    "positive_drop_rate": pos_drop,
                    "negative_drop_rate": neg_drop,
                    "missing_rate": missing_rate,
                    "observed_positive_rate_if_conditioned": observed_rate,
                    "absolute_bias_if_conditioned": observed_rate - true_positive_rate,
                    "source_contract_status": status(missing_rate, sla_warn, sla_reject),
                }
                rows.append(row)
                policy_rows.append(row)
        accepted_rows = [
            row for row in policy_rows if row["source_contract_status"] == "accept_under_sla"
        ]
        flagged_rows = [
            row for row in policy_rows if row["source_contract_status"] != "accept_under_sla"
        ]
        worst = max(
            accepted_rows,
            key=lambda row: abs(row["absolute_bias_if_conditioned"]),
        )
        summary_rows.append(
            {
                "policy": policy_name,
                "sla_warn": sla_warn,
                "sla_reject": sla_reject,
                "accepted_cells": len(accepted_rows),
                "flagged_or_rejected_cells": len(flagged_rows),
                "max_abs_bias_accepted": abs(worst["absolute_bias_if_conditioned"]),
                "worst_accepted_positive_drop": worst["positive_drop_rate"],
                "worst_accepted_negative_drop": worst["negative_drop_rate"],
                "worst_accepted_missing_rate": worst["missing_rate"],
            }
        )
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with OUT_SUMMARY_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    payload = {
        "artifact": "selective_missingness_sensitivity",
        "description": "source/fill-rate sensitivity for consumed-but-missing reports; not a missing-at-random proof",
        "parameters": {
            "n": n,
            "true_positive_rate": true_positive_rate,
            "policies": [
                {"policy": name, "sla_warn": warn, "sla_reject": reject}
                for name, warn, reject in policies
            ],
        },
        "summary": summary_rows,
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    OUT_SUMMARY_JSON.write_text(
        json.dumps(
            {
                "artifact": "selective_missingness_mitigation_summary",
                "description": "how stricter fill-rate policies reduce worst accepted selective-missingness bias",
                "summary": summary_rows,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
