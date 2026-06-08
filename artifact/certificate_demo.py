#!/usr/bin/env python3
"""Minimal certificate-admission artifact for the ICDE LDP budget-integrity paper.

The demo is intentionally small and dependency-free. It models the verifier-side
semantics used in the paper: certificate validity, ledger-backed freshness,
negative controls, and joining accepted certificates with a price table before
aggregation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


POLICY = {
    "version": "policy-v4",
    "allowed_pcr": {"c189c18e", "faa4049a"},
    "admitted_sources": {
        "src-marketing-cohort-7",
        "src-002",
        "src-003",
        "src-005",
        "src-006",
        "src-007",
        "src-008",
        "src-fork-a",
        "src-fork-b",
        "src-reuse",
    },
    "price_rows": {
        "price-silver-v4": {"quality_class": "silver", "unit_price": 12.0},
        "price-gold-v4": {"quality_class": "gold", "unit_price": 20.0},
    },
    "min_deposit": {"silver": 2.0, "gold": 5.0},
}


def digest(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def make_certificate(
    report: int,
    *,
    rid: str = "r-001",
    session: str = "s-001",
    seller: str = "seller-a",
    source: str = "src-marketing-cohort-7",
    workload: str = "reach",
    pcr: str = "c189c18e",
    policy: str = "policy-v4",
    quality_class: str = "gold",
    price_id: str = "price-gold-v4",
    counter: int = 1,
    nonce: str = "nonce-001",
    residual_bucket: str = "deposit",
    salt: str = "salt-001",
) -> dict:
    commitment = digest(report, salt, seller, source, session, workload)
    attested = {
        "algorithm": "OUE",
        "commitment": commitment,
        "source": source,
        "pcr": pcr,
        "policy": policy,
        "price_id": price_id,
        "quality_class": quality_class,
        "counter": counter,
        "nonce": nonce,
        "residual_bucket": residual_bucket,
    }
    return {
        "rid": rid,
        "session": session,
        "seller": seller,
        "source": source,
        "workload": workload,
        "algorithm": "OUE",
        "pcr": pcr,
        "policy": policy,
        "price_id": price_id,
        "quality_class": quality_class,
        "counter": counter,
        "nonce": nonce,
        "salt": salt,
        "commitment": commitment,
        "residual_bucket": residual_bucket,
        "attestation": {"chain_valid": True, "user_data": attested},
    }


class Verifier:
    def __init__(self, policy: dict):
        self.policy = policy
        self.terminal_ledger = set()
        self.counter_ledger = set()
        self.nonces = set()
        self.consumed_sources = set()
        self.accepted = []
        self.rejected = []

    def verify(self, report: int, cert: dict, evidence: dict) -> tuple[bool, str]:
        att = cert.get("attestation", {})
        user_data = att.get("user_data", {})
        expected_commitment = digest(
            report,
            cert.get("salt"),
            cert.get("seller"),
            cert.get("source"),
            cert.get("session"),
            cert.get("workload"),
        )
        price_row = self.policy["price_rows"].get(cert.get("price_id"))

        checks = [
            (att.get("chain_valid") is True, "attestation_chain"),
            (cert.get("commitment") == expected_commitment, "report_commitment"),
            (user_data.get("commitment") == cert.get("commitment"), "attestation_binding"),
            (user_data.get("algorithm") == cert.get("algorithm"), "algorithm_binding"),
            (user_data.get("source") == cert.get("source"), "source_binding"),
            (user_data.get("price_id") == cert.get("price_id"), "price_row_binding"),
            (cert.get("pcr") in self.policy["allowed_pcr"], "pcr_allowlist"),
            (cert.get("policy") == self.policy["version"], "policy_version"),
            (cert.get("source") in self.policy["admitted_sources"], "source_admission"),
            (price_row is not None, "price_row"),
            (price_row is not None and price_row["quality_class"] == cert.get("quality_class"), "quality_class"),
        ]
        for ok, reason in checks:
            if not ok:
                self._reject(cert, reason)
                return False, reason

        terminal_key = (cert["rid"], cert["nonce"])
        if terminal_key in self.terminal_ledger:
            self._reject(cert, "stale_or_duplicate_certificate")
            return False, "stale_or_duplicate_certificate"

        counter_key = (cert["seller"], cert["session"], cert["counter"])
        if counter_key in self.counter_ledger:
            self._reject(cert, "stale_or_duplicate_counter")
            return False, "stale_or_duplicate_counter"

        if cert["source"] in self.consumed_sources:
            self._reject(cert, "source_token_reuse")
            return False, "source_token_reuse"

        nonce_key = (cert["seller"], cert["session"], cert["nonce"])
        if nonce_key in self.nonces:
            self._reject(cert, "nonce_reuse")
            return False, "nonce_reuse"

        min_deposit = self.policy["min_deposit"][cert["quality_class"]]
        if evidence.get("deposit", 0.0) < min_deposit:
            self._reject(cert, "residual_control")
            return False, "residual_control"

        self.terminal_ledger.add(terminal_key)
        self.counter_ledger.add(counter_key)
        self.nonces.add(nonce_key)
        self.consumed_sources.add(cert["source"])
        accepted = copy.deepcopy(cert)
        accepted["price"] = price_row["unit_price"]
        accepted["accepted"] = True
        self.accepted.append(accepted)
        return True, "accepted"

    def _reject(self, cert: dict, reason: str) -> None:
        row = {
            "rid": cert.get("rid"),
            "nonce": cert.get("nonce"),
            "session": cert.get("session"),
            "seller": cert.get("seller"),
            "source": cert.get("source"),
            "counter": cert.get("counter"),
            "accepted": False,
            "reason": reason,
        }
        self.rejected.append(row)


def run_demo() -> dict:
    report = 1
    verifier = Verifier(POLICY)
    valid = make_certificate(report)
    outcomes = {}

    outcomes["valid"] = verifier.verify(report, valid, {"deposit": 5.0})[1]
    outcomes["replay"] = verifier.verify(report, valid, {"deposit": 5.0})[1]

    variants = {
        "pcr_mismatch": {
            "pcr": "badpcr00",
            "source": "src-002",
            "counter": 2,
            "nonce": "nonce-002",
            "rid": "r-002",
        },
        "stale_policy": {
            "policy": "policy-v3",
            "source": "src-003",
            "counter": 3,
            "nonce": "nonce-003",
            "rid": "r-003",
        },
        "source_missing": {"source": "unknown-source", "counter": 4, "nonce": "nonce-004", "rid": "r-004"},
        "nonce_reuse": {"source": "src-005", "counter": 5, "nonce": "nonce-001", "rid": "r-005"},
        "residual_mismatch": {"source": "src-006", "counter": 6, "nonce": "nonce-006", "rid": "r-006"},
    }
    for name, patch in variants.items():
        cert = make_certificate(report, **patch)
        evidence = {"deposit": 0.0 if name == "residual_mismatch" else 5.0}
        outcomes[name] = verifier.verify(report, cert, evidence)[1]

    bad_commit = make_certificate(report, source="src-007", counter=7, nonce="nonce-007", rid="r-007")
    bad_commit["commitment"] = digest("tampered")
    outcomes["commitment_mutation"] = verifier.verify(report, bad_commit, {"deposit": 5.0})[1]

    price_mutation = make_certificate(report, source="src-008", counter=8, nonce="nonce-008", rid="r-008")
    price_mutation["price_id"] = "price-silver-v4"
    outcomes["price_row_substitution"] = verifier.verify(report, price_mutation, {"deposit": 5.0})[1]

    fork_a = make_certificate(
        report, source="src-fork-a", session="s-fork", counter=1, nonce="nonce-fork-a", rid="r-fork-a"
    )
    fork_b = make_certificate(
        report, source="src-fork-b", session="s-fork", counter=1, nonce="nonce-fork-b", rid="r-fork-b"
    )
    outcomes["fork_first"] = verifier.verify(report, fork_a, {"deposit": 5.0})[1]
    outcomes["fork_second"] = verifier.verify(report, fork_b, {"deposit": 5.0})[1]

    reuse_a = make_certificate(report, source="src-reuse", counter=9, nonce="nonce-reuse-a", rid="r-reuse-a")
    reuse_b = make_certificate(report, source="src-reuse", counter=10, nonce="nonce-reuse-b", rid="r-reuse-b")
    outcomes["source_reuse_first"] = verifier.verify(report, reuse_a, {"deposit": 5.0})[1]
    outcomes["source_reuse_second"] = verifier.verify(report, reuse_b, {"deposit": 5.0})[1]

    aggregate = {
        "accepted_reports": len(verifier.accepted),
        "total_payment": sum(row["price"] for row in verifier.accepted),
        "accepted_rows": verifier.accepted,
        "rejected_rows": verifier.rejected,
        "outcomes": outcomes,
    }
    return aggregate


def main() -> None:
    result = run_demo()
    out = Path(__file__).with_name("certificate_demo_results.json")
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["outcomes"], indent=2, sort_keys=True))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
