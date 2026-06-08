#!/usr/bin/env python3
"""Write a figure-to-artifact manifest.

The paper ships prebuilt PDF/PNG figures. This script records their hashes and
the reproducible scripts or data products that support each figure. It is an
artifact hygiene check, not a claim that every plotting step is regenerated from
raw measurements in this minimal package.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = Path(__file__).with_name("figure_reproduction_manifest.json")
OUT_CSV = Path(__file__).with_name("figure_reproduction_manifest.csv")


FIGURES = [
    {
        "figure": "figure1.pdf",
        "role": "fraud taxonomy schematic retained as a prebuilt source figure",
        "support": "paper source",
    },
    {
        "figure": "figures/architecture_diagram.pdf",
        "role": "certificate admission architecture schematic",
        "support": "paper source and certificate_demo.py",
    },
    {
        "figure": "figures/disclosure_frontier.pdf",
        "role": "disclosure and deterrence frontier",
        "support": "nopow_policy_selection.py; nopow_policy_selection.csv/json",
    },
    {
        "figure": "figures/fraud_premium_heatmap.pdf",
        "role": "valuation premium grid",
        "support": "paper valuation formulas; supplement premium table",
    },
    {
        "figure": "figures/valuation_curves.pdf",
        "role": "LDP budget/value curves",
        "support": "paper valuation formulas; supplement premium table",
    },
    {
        "figure": "figures/marketing_audience_error.pdf",
        "role": "marketing audience-count error",
        "support": "public_marketing_workload.py; public_marketing_workload_summary.csv/json",
    },
    {
        "figure": "figures/marketing_reach_error.pdf",
        "role": "marketing reach error",
        "support": "public_marketing_workload.py; public_marketing_workload_summary.csv/json",
    },
    {
        "figure": "figures/marketing_conversion_regret.pdf",
        "role": "marketing conversion-allocation regret",
        "support": "public_marketing_workload.py; public_marketing_workload_summary.csv/json",
    },
    {
        "figure": "figures/marketing_disclosure_frontier.pdf",
        "role": "marketing disclosure frontier",
        "support": "public_mechanism_comparison.py; public_mechanism_comparison.csv/json",
    },
    {
        "figure": "figures/nitro_latency.pdf",
        "role": "Nitro latency evidence",
        "support": "supplement tables; full Nitro archive not included in minimal package",
    },
    {
        "figure": "figures/replay_confusion.pdf",
        "role": "verifier mutation checks",
        "support": "certificate_demo.py; certificate_demo_results.json",
    },
    {
        "figure": "figures/ic_coverage.pdf",
        "role": "incentive-coverage summary",
        "support": "nopow_policy_selection.py; nopow_policy_selection.csv/json",
    },
    {
        "figure": "figures/ic_coverage_heatmap.pdf",
        "role": "incentive-coverage heatmap",
        "support": "nopow_policy_selection.py; nopow_policy_selection.csv/json",
    },
    {
        "figure": "figures/market_utility.pdf",
        "role": "market utility curve",
        "support": "public_mechanism_comparison.py; public_mechanism_comparison.csv/json",
    },
    {
        "figure": "figures/market_baselines.pdf",
        "role": "market baseline comparison",
        "support": "public_mechanism_comparison.py; public_mechanism_comparison.csv/json",
    },
    {
        "figure": "figures/robustness_ablation.pdf",
        "role": "robustness sensitivity",
        "support": "nopow_policy_selection.py; supplement sensitivity table",
    },
    {
        "figure": "figures/throughput_cost.pdf",
        "role": "throughput and cost sensitivity",
        "support": "supplement Nitro table; certificate_store_benchmark.py",
    },
    {
        "figure": "figures/attacker_strategy.pdf",
        "role": "adaptive attacker strategy",
        "support": "supplement adaptive-attacker description",
    },
    {
        "figure": "figures/pow_calibration.pdf",
        "role": "optional PoW calibration",
        "support": "supplement optional-PoW table",
    },
    {
        "figure": "figures/pow_deadline_heatmap.pdf",
        "role": "optional PoW deadline heatmap",
        "support": "supplement optional-PoW table",
    },
]


def sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    rows = []
    for item in FIGURES:
        path = ROOT / item["figure"]
        rows.append(
            {
                **item,
                "exists": path.exists(),
                "bytes": path.stat().st_size if path.exists() else 0,
                "sha256": sha256(path),
            }
        )
    OUT_JSON.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["figure", "exists", "bytes", "sha256", "role", "support"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"figures": len(rows), "missing": [r["figure"] for r in rows if not r["exists"]]}, indent=2))


if __name__ == "__main__":
    main()
