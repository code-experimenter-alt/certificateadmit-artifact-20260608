#!/usr/bin/env python3
"""Minimal-design ablation for certificate admission.

This deterministic table compares the full certificate-admission primitive
against simpler alternatives readers may naturally suggest. It records which
database invariants each design enforces and which attacks remain possible.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


OUT_CSV = Path(__file__).with_name("minimal_design_ablation.csv")
OUT_JSON = Path(__file__).with_name("minimal_design_ablation.json")

INVARIANTS = [
    "executable_policy_binding",
    "report_commitment",
    "source_exactly_once",
    "freshness_counter_nonce",
    "price_row_binding",
    "rejection_audit",
    "accepted_only_queue",
    "residual_policy_binding",
]


ROWS = [
    {
        "design": "NoVerify metadata",
        "executable_policy_binding": 0,
        "report_commitment": 0,
        "source_exactly_once": 0,
        "freshness_counter_nonce": 0,
        "price_row_binding": 0,
        "rejection_audit": 0,
        "accepted_only_queue": 0,
        "residual_policy_binding": 0,
        "attacks_left": "forgery; downgrade; replay/fork; source double spend; price swap; opaque drops",
    },
    {
        "design": "Attestation blob only",
        "executable_policy_binding": 1,
        "report_commitment": 1,
        "source_exactly_once": 0,
        "freshness_counter_nonce": 0,
        "price_row_binding": 0,
        "rejection_audit": 0,
        "accepted_only_queue": 0,
        "residual_policy_binding": 0,
        "attacks_left": "replay/fork; source double spend; price swap; missing rejection provenance",
    },
    {
        "design": "Append-only certificate log",
        "executable_policy_binding": 1,
        "report_commitment": 1,
        "source_exactly_once": 0,
        "freshness_counter_nonce": 0,
        "price_row_binding": 1,
        "rejection_audit": 1,
        "accepted_only_queue": 0,
        "residual_policy_binding": 0,
        "attacks_left": "duplicate source/counter races; accepted-only race; residual hidden premium",
    },
    {
        "design": "TEE class assertion only",
        "executable_policy_binding": 1,
        "report_commitment": 1,
        "source_exactly_once": 0,
        "freshness_counter_nonce": 1,
        "price_row_binding": 0,
        "rejection_audit": 0,
        "accepted_only_queue": 0,
        "residual_policy_binding": 0,
        "attacks_left": "source double spend; price substitution; opaque drops; within-class premium",
    },
    {
        "design": "Source-token gate only",
        "executable_policy_binding": 0,
        "report_commitment": 0,
        "source_exactly_once": 1,
        "freshness_counter_nonce": 0,
        "price_row_binding": 0,
        "rejection_audit": 1,
        "accepted_only_queue": 1,
        "residual_policy_binding": 0,
        "attacks_left": "forged randomizer; report mutation; stale policy; price swap; replay",
    },
    {
        "design": "Attested transactional outbox",
        "executable_policy_binding": 1,
        "report_commitment": 1,
        "source_exactly_once": 1,
        "freshness_counter_nonce": 0,
        "price_row_binding": 1,
        "rejection_audit": 1,
        "accepted_only_queue": 1,
        "residual_policy_binding": 0,
        "attacks_left": "replay/fork without separate counter/nonce uniqueness; within-class premium without residual policy",
    },
    {
        "design": "Full attested outbox / CertificateAdmit",
        "executable_policy_binding": 1,
        "report_commitment": 1,
        "source_exactly_once": 1,
        "freshness_counter_nonce": 1,
        "price_row_binding": 1,
        "rejection_audit": 1,
        "accepted_only_queue": 1,
        "residual_policy_binding": 1,
        "attacks_left": "none under stated attestation, source, KMS, and ledger assumptions",
    },
]


def with_scores(row: dict[str, object]) -> dict[str, object]:
    covered = sum(int(row[name]) for name in INVARIANTS)
    out = dict(row)
    out["covered_invariants"] = covered
    out["total_invariants"] = len(INVARIANTS)
    return out


def main() -> None:
    rows = [with_scores(row) for row in ROWS]
    fieldnames = ["design", *INVARIANTS, "covered_invariants", "total_invariants", "attacks_left"]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "artifact": "minimal_design_ablation",
        "description": "comparison against simpler attestation/log/source-token designs",
        "invariants": INVARIANTS,
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
