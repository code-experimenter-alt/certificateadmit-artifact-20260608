#!/usr/bin/env python3
"""Certificate component ablation table.

This script records the verifier-side negative controls as a compact
field-to-attack map. It is intentionally deterministic: the executable
certificate demos test representative failures, while this table states which
admission invariant is lost if a verifier omits a certificate component.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


OUT_CSV = Path(__file__).with_name("component_ablation.csv")
OUT_JSON = Path(__file__).with_name("component_ablation.json")


ROWS = [
    {
        "removed_component": "attestation digest binding",
        "lost_invariant": "accepted report is produced by an allowed oracle image and field tuple",
        "attack_enabled": "forged certificate or unattested field substitution",
        "demo_control": "attestation_chain, attestation_binding, pcr_allowlist",
    },
    {
        "removed_component": "report commitment",
        "lost_invariant": "submitted y is the report committed inside the oracle trace",
        "attack_enabled": "report mutation after certification",
        "demo_control": "report_commitment",
    },
    {
        "removed_component": "source token",
        "lost_invariant": "at most one accepted report per admitted source/cohort slot",
        "attack_enabled": "source double spend or silent resampling",
        "demo_control": "source_admission, source_token_reuse",
    },
    {
        "removed_component": "seller/session/counter key",
        "lost_invariant": "forked oracle branches cannot both advance freshness state",
        "attack_enabled": "fork or rollback replay",
        "demo_control": "stale_or_duplicate_counter",
    },
    {
        "removed_component": "verifier nonce",
        "lost_invariant": "old oracle output cannot satisfy a fresh challenge",
        "attack_enabled": "cross-session replay",
        "demo_control": "nonce_reuse",
    },
    {
        "removed_component": "policy version and PCR allow-list",
        "lost_invariant": "admission uses the current approved randomizer and class map",
        "attack_enabled": "downgraded code or stale policy",
        "demo_control": "policy_version, pcr_allowlist",
    },
    {
        "removed_component": "price-row binding",
        "lost_invariant": "certificate quality signal matches the charged price row",
        "attack_enabled": "price substitution after attestation",
        "demo_control": "price_row_binding, quality_class",
    },
    {
        "removed_component": "residual-control evidence",
        "lost_invariant": "sealed-class hidden premium is priced, audited, deposited, or rejected",
        "attack_enabled": "within-class over-claim with no expected penalty",
        "demo_control": "residual_control",
    },
    {
        "removed_component": "rejection audit row",
        "lost_invariant": "market can query failed admissions and fill-rate exceptions",
        "attack_enabled": "unexplained dropped reports and opaque seller failures",
        "demo_control": "rejected_rows, missing_precommit",
    },
]


def main() -> None:
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ROWS[0].keys()))
        writer.writeheader()
        writer.writerows(ROWS)

    payload = {
        "artifact": "component_ablation",
        "description": "field-level certificate ablation map for admission invariants",
        "rows": ROWS,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
