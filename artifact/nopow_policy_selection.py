#!/usr/bin/env python3
"""No-PoW residual-control sensitivity for sealed-class admission.

The script enumerates the same kind of budget-overclaim cells used in the paper
and compares deposit/audit-first policies against large-premium rejection. It is
not a hardware benchmark; it is a transparent policy-selection sanity check.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path


EPS = [0.2, 0.5, 1.0, 2.0, 4.0, 8.0]
NS = [10_000, 100_000, 1_000_000]
DOMAINS = [2, 32, 1024]
BASE_NITRO_P95_MS = 18.9
AUDIT_LEDGER_OVERHEAD_MS = 2.4
DEPOSIT_AUDIT_CAP = 80.0
OUT_CSV = Path(__file__).with_name("nopow_policy_selection.csv")
OUT_JSON = Path(__file__).with_name("nopow_policy_selection.json")


def sigma(epsilon: float, domain: int, n: int) -> float:
    return math.sqrt(domain / n) * (math.exp(epsilon) + domain - 2.0) / (math.exp(epsilon) - 1.0)


def value(epsilon: float, domain: int, n: int) -> float:
    return 100.0 * math.exp(-0.75 * sigma(epsilon, domain, n))


def premium_cells() -> list[dict[str, float]]:
    cells = []
    for n in NS:
        for domain in DOMAINS:
            for i, eps_h in enumerate(EPS):
                for eps_c in EPS[i + 1 :]:
                    premium = max(0.0, value(eps_c, domain, n) - value(eps_h, domain, n))
                    cells.append(
                        {
                            "n": n,
                            "domain": domain,
                            "eps_h": eps_h,
                            "eps_c": eps_c,
                            "premium": premium,
                        }
                    )
    return cells


def summarize(cells: list[dict[str, float]], reject_large: bool) -> dict[str, float | str | bool]:
    covered = 0
    rejected = 0
    unsafe = 0
    for cell in cells:
        premium = float(cell["premium"])
        if premium <= DEPOSIT_AUDIT_CAP:
            covered += 1
        elif reject_large:
            covered += 1
            rejected += 1
        else:
            unsafe += 1
    total = len(cells)
    return {
        "policy": "DepositAuditReject" if reject_large else "DepositAuditNoPoW",
        "uses_pow": False,
        "cells": total,
        "ic_coverage": covered / total,
        "accepted_cell_fraction": (total - rejected) / total,
        "rejected_cell_fraction": rejected / total,
        "unsafe_cell_fraction": unsafe / total,
        "p95_latency_ms": BASE_NITRO_P95_MS + AUDIT_LEDGER_OVERHEAD_MS,
        "max_deposit_audit_cost": DEPOSIT_AUDIT_CAP,
    }


def main() -> None:
    cells = premium_cells()
    rows = [summarize(cells, reject_large=False), summarize(cells, reject_large=True)]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "parameters": {
            "eps": EPS,
            "n": NS,
            "domains": DOMAINS,
            "cell_count": len(cells),
            "base_nitro_p95_ms": BASE_NITRO_P95_MS,
            "audit_ledger_overhead_ms": AUDIT_LEDGER_OVERHEAD_MS,
            "deposit_audit_cap": DEPOSIT_AUDIT_CAP,
            "value_model": "100*exp(-0.75*sigma), sigma=sqrt(domain/n)*(exp(eps)+domain-2)/(exp(eps)-1)",
        },
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
