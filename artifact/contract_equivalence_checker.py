#!/usr/bin/env python3
"""Design-by-predicate checker for the CertificateAdmit contract.

The script is intentionally deterministic: it records whether representative
database/admission designs enforce the P0--P7 predicates and V1--V3 visibility
conditions used in the paper.  It is not a theorem prover; the output is a
machine-readable contract coverage table that complements the executable SQL
and KV admission tests.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
OUT_CSV = BASE / "contract_equivalence_checker.csv"
OUT_JSON = BASE / "contract_equivalence_checker.json"

CONDITIONS = [
    ("P0_durable_receipt", "receive id and payload hash are durable before parsing, verification, or rejection"),
    ("P1_attested_execution_commitment", "report commitment and certificate fields are bound to allowed attested execution"),
    ("P2_source_token_consumption", "issued source token is conditionally consumed at most once"),
    ("P3_separate_uniqueness", "terminal, source, counter, and nonce keys are separately unique"),
    ("P4_policy_price_join", "accepted row joins an allowed policy and price row before visibility"),
    ("P5_disclosure_rule", "exact, private-exact, or restricted-disclosure rule is checked"),
    ("P6_residual_evidence", "restricted-disclosure rows include required residual-control evidence"),
    ("P7_idempotent_recovery", "retries and crashes cannot expose partial accepted rows or duplicate queue entries"),
    ("V1_accepted_only_visibility", "payment, buyer, and estimator queries see only rows that passed admission"),
    ("V2_queryable_rejections", "malformed durable receives and parsed deterministic failures have queryable rejection reasons and no queue entry"),
    ("V3_consumed_missing_accounting", "expired precommit or release attempts are counted and excluded from aggregation"),
]

COND_IDS = [condition_id for condition_id, _ in CONDITIONS]


def row(design: str, covered: set[str], note: str) -> dict[str, object]:
    out: dict[str, object] = {"design": design}
    out.update({condition_id: int(condition_id in covered) for condition_id in COND_IDS})
    out["covered_conditions"] = sum(int(out[condition_id]) for condition_id in COND_IDS)
    out["total_conditions"] = len(COND_IDS)
    out["contract_equivalent"] = int(out["covered_conditions"] == out["total_conditions"])
    out["remaining_gap"] = note
    return out


ALL = set(COND_IDS)

ROWS = [
    row(
        "NoVerify metadata",
        set(),
        "no durable receipt, executable binding, source consumption, freshness, price join, rejection provenance, or accepted-only gate",
    ),
    row(
        "Attestation blob only",
        {"P1_attested_execution_commitment"},
        "attested origin is recorded, but source reuse, replay/fork, price substitution, rejection, and missingness remain outside admission",
    ),
    row(
        "Append-only certificate log",
        {"P1_attested_execution_commitment", "P4_policy_price_join", "V2_queryable_rejections"},
        "audit trail can expose failures, but invalid rows may be visible before uniqueness and lifecycle checks complete",
    ),
    row(
        "Append-only + reconciliation",
        {
            "P1_attested_execution_commitment",
            "P2_source_token_consumption",
            "P3_separate_uniqueness",
            "P4_policy_price_join",
            "V2_queryable_rejections",
        },
        "eventual repair does not satisfy accepted-only visibility or crash/retry lifecycle semantics during the unsafe window",
    ),
    row(
        "Safe materialized view",
        {
            "P0_durable_receipt",
            "P1_attested_execution_commitment",
            "P2_source_token_consumption",
            "P3_separate_uniqueness",
            "P4_policy_price_join",
            "P5_disclosure_rule",
            "P6_residual_evidence",
            "P7_idempotent_recovery",
            "V1_accepted_only_visibility",
            "V2_queryable_rejections",
            "V3_consumed_missing_accounting",
        },
        "contract-equivalent when durable receipt precedes validation and publication waits for a validated watermark with consumed-missing accounting",
    ),
    row(
        "Source-token gate only",
        {"P2_source_token_consumption", "V1_accepted_only_visibility", "V2_queryable_rejections", "V3_consumed_missing_accounting"},
        "prevents duplicate sources but does not bind executable identity, report commitment, counter/nonce freshness, policy price, or disclosure evidence",
    ),
    row(
        "Partial attested outbox",
        {
            "P1_attested_execution_commitment",
            "P2_source_token_consumption",
            "P4_policy_price_join",
            "V1_accepted_only_visibility",
            "V2_queryable_rejections",
        },
        "atomic accepted-event delivery is insufficient without separate counter/nonce uniqueness, disclosure/residual checks, and recovery/missingness lifecycle",
    ),
    row(
        "Full attested outbox",
        ALL,
        "contract-equivalent implementation when the outbox transaction enforces every P/V condition before event publication",
    ),
    row(
        "CertificateAdmit SQL",
        ALL,
        "contract-equivalent serializable SQL implementation",
    ),
    row(
        "Linearizable KV CertificateAdmit",
        ALL,
        "contract-equivalent conditional-write implementation",
    ),
]


def main() -> None:
    fieldnames = ["design", *COND_IDS, "covered_conditions", "total_conditions", "contract_equivalent", "remaining_gap"]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ROWS)
    payload = {
        "artifact": "contract_equivalence_checker",
        "description": "deterministic design-by-predicate coverage table for P0--P7 and V1--V3",
        "conditions": [{"id": condition_id, "meaning": meaning} for condition_id, meaning in CONDITIONS],
        "rows": ROWS,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
