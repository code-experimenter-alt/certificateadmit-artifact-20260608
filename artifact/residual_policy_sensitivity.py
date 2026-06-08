#!/usr/bin/env python3
"""Residual-policy sensitivity over value scale, deposit cap, and recovery.

This deterministic artifact extends the no-PoW policy selection table. It keeps
the same transparent premium model but varies the business-value scale, the
deposit/audit cap, and the recoverable fraction of that cap. It is a policy
sensitivity check, not an empirical market-pricing claim.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path


EPS = [0.2, 0.5, 1.0, 2.0, 4.0, 8.0]
NS = [10_000, 100_000, 1_000_000]
DOMAINS = [2, 32, 1024]
VALUE_SCALES = [50.0, 100.0, 200.0]
DEPOSIT_CAPS = [40.0, 80.0, 160.0]
AUDIT_RECOVERY = [0.5, 1.0]
OUT_CSV = Path(__file__).with_name("residual_policy_sensitivity.csv")
OUT_JSON = Path(__file__).with_name("residual_policy_sensitivity.json")


def sigma(epsilon: float, domain: int, n: int) -> float:
    return math.sqrt(domain / n) * (math.exp(epsilon) + domain - 2.0) / (math.exp(epsilon) - 1.0)


def value(epsilon: float, domain: int, n: int, value_scale: float) -> float:
    return value_scale * math.exp(-0.75 * sigma(epsilon, domain, n))


def premium_cells(value_scale: float) -> list[float]:
    cells: list[float] = []
    for n in NS:
        for domain in DOMAINS:
            for i, eps_h in enumerate(EPS):
                for eps_c in EPS[i + 1 :]:
                    premium = max(0.0, value(eps_c, domain, n, value_scale) - value(eps_h, domain, n, value_scale))
                    cells.append(premium)
    return cells


def summarize(value_scale: float, deposit_cap: float, audit_recovery: float) -> dict[str, float | int | str]:
    cells = premium_cells(value_scale)
    effective_cap = deposit_cap * audit_recovery
    covered_by_deposit = sum(1 for premium in cells if premium <= effective_cap)
    total = len(cells)
    rejected_if_strict = total - covered_by_deposit
    unsafe_max_premium = max((premium for premium in cells if premium > effective_cap), default=0.0)
    return {
        "value_scale": value_scale,
        "deposit_cap": deposit_cap,
        "audit_recovery": audit_recovery,
        "effective_cap": effective_cap,
        "cells": total,
        "deposit_only_coverage": covered_by_deposit / total,
        "reject_large_cell_fraction": rejected_if_strict / total,
        "deposit_or_reject_coverage": 1.0,
        "max_uncovered_premium": unsafe_max_premium,
    }


def main() -> None:
    rows = [
        summarize(value_scale, deposit_cap, audit_recovery)
        for value_scale in VALUE_SCALES
        for deposit_cap in DEPOSIT_CAPS
        for audit_recovery in AUDIT_RECOVERY
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "artifact": "residual_policy_sensitivity",
        "description": "no-PoW sealed-class sensitivity over value scale, deposit cap, and audit recovery",
        "parameters": {
            "eps": EPS,
            "n": NS,
            "domains": DOMAINS,
            "value_scales": VALUE_SCALES,
            "deposit_caps": DEPOSIT_CAPS,
            "audit_recovery": AUDIT_RECOVERY,
            "value_model": "B*exp(-0.75*sigma), sigma=sqrt(domain/n)*(exp(eps)+domain-2)/(exp(eps)-1)",
        },
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
