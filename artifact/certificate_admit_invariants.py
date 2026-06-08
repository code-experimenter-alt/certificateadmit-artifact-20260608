#!/usr/bin/env python3
"""Emit the visible-state invariants for CertificateAdmit.

This is a compact machine-readable contract sheet, not a model checker.  The
JSON/CSV/TLA-style outputs name the accepted/rejected/missing lifecycle
invariants and contract-equivalence predicates that the executable artifacts
test with SQL or KV stores.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
OUT_CSV = BASE / "certificate_admit_invariants.csv"
OUT_JSON = BASE / "certificate_admit_invariants.json"
OUT_TLA = BASE / "certificate_admit_invariants.tla"
OUT_EQ_CSV = BASE / "certificate_admit_contract_equivalence.csv"


INVARIANTS = [
    {
        "id": "I0_durable_receipt",
        "state": "AttemptInbox, CertLedger, RejectLog",
        "condition": "Every visible accepted, rejected, or consumed-missing outcome is linked to a durable receive record.",
        "violation_query": "abstract predicate: terminal or reject row has no prior AttemptInbox receive id",
        "covered_by": "durable-receipt contract, malformed/crash recovery row",
    },
    {
        "id": "I1_source_once",
        "state": "SourceToken, AggregateQueue",
        "condition": "Each source token appears in at most one accepted queue row.",
        "violation_query": "SELECT src FROM AggregateQueue GROUP BY src HAVING COUNT(*)>1;",
        "covered_by": "source-token race, duplicate-source ablation",
    },
    {
        "id": "I2_separate_freshness_keys",
        "state": "CertLedger",
        "condition": "No two accepted rows share a terminal, counter, or nonce freshness key.",
        "violation_query": "GROUP BY rid,nonce OR seller,session,ctr OR seller,session,nonce HAVING COUNT(*)>1",
        "covered_by": "counter race, replay/fork mutation tests",
    },
    {
        "id": "I3_policy_price_bound",
        "state": "CertLedger, PricePolicy",
        "condition": "Every accepted row uses an allowed policy and price row checked at admission.",
        "violation_query": "SELECT accepted.* FROM accepted LEFT JOIN PricePolicy USING(policy,priceID) WHERE PricePolicy.priceID IS NULL;",
        "covered_by": "price-substitution mutation, policy churn workload",
    },
    {
        "id": "I4_commitment_bound",
        "state": "CertLedger",
        "condition": "The accepted report commitment recomputes from the submitted report and certificate fields.",
        "violation_query": "abstract predicate: Hash(y,u,seller,src,sid,W) != commitment",
        "covered_by": "commitment mutation test",
    },
    {
        "id": "I5_reject_no_queue",
        "state": "RejectLog, AggregateQueue",
        "condition": "A deterministic rejection has a queryable reason and no aggregate queue row.",
        "violation_query": "SELECT rid,nonce FROM RejectLog INTERSECT SELECT rid,nonce FROM AggregateQueue;",
        "covered_by": "rejection audit, invalid schedule workload",
    },
    {
        "id": "I6_missing_no_queue",
        "state": "CertLedger, AggregateQueue",
        "condition": "Expired precommit/released rows become consumed-missing and never enter the queue.",
        "violation_query": "SELECT rid,nonce FROM CertLedger WHERE status='consumed_missing' INTERSECT SELECT rid,nonce FROM AggregateQueue;",
        "covered_by": "crash-before-release, crash-after-release recovery",
    },
    {
        "id": "I7_queue_accepted_only",
        "state": "CertLedger, AggregateQueue",
        "condition": "Every aggregate queue row has an accepted certificate row with the same terminal key.",
        "violation_query": "SELECT q.rid,q.nonce FROM AggregateQueue q LEFT JOIN CertLedger c ON q.rid=c.rid AND q.nonce=c.nonce AND c.status='accepted' WHERE c.rid IS NULL;",
        "covered_by": "accepted-only queue invariant",
    },
    {
        "id": "I8_disclosure_mode",
        "state": "CertLedger, policy",
        "condition": "Exact/private-exact rows satisfy the realized-budget predicate; sealed rows satisfy class, price-row, and residual-evidence predicates.",
        "violation_query": "abstract predicate over mode, q, realized budget, class map, residual evidence",
        "covered_by": "exact mismatch, class mismatch, residual mismatch tests",
    },
]


CONTRACT_EQUIVALENCE = [
    {
        "id": "P0_durable_receipt",
        "type": "predicate",
        "condition": "The verifier durably records receive id and payload hash before parsing, verification, or rejection.",
        "implementation_hook": "AttemptInbox insert or equivalent inbox key before host-code validation",
    },
    {
        "id": "P1_attested_execution_commitment",
        "type": "predicate",
        "condition": "The report commitment and certificate fields are bound to an allowed attested execution identity.",
        "implementation_hook": "attestation verifier, PCR/algorithm allow-list, commitment recomputation",
    },
    {
        "id": "P2_source_token_consumption",
        "type": "predicate",
        "condition": "An issued source token is conditionally consumed at most once for an accepted report.",
        "implementation_hook": "SourceToken compare/update or serializable row update",
    },
    {
        "id": "P3_separate_uniqueness",
        "type": "predicate",
        "condition": "Terminal, source, counter, and nonce keys are separately unique before acceptance.",
        "implementation_hook": "PK(rid,nonce), unique source, unique seller/session/counter, unique seller/session/nonce",
    },
    {
        "id": "P4_policy_price_join",
        "type": "predicate",
        "condition": "The accepted row joins the attested policy to an allowed price row before visibility.",
        "implementation_hook": "transaction-time PricePolicy join or equivalent conditional write",
    },
    {
        "id": "P5_disclosure_rule",
        "type": "predicate",
        "condition": "Exact/private-exact/sealed disclosure state satisfies the selected market policy.",
        "implementation_hook": "exact budget check, confidential-verifier check, or class-map check",
    },
    {
        "id": "P6_residual_evidence",
        "type": "predicate",
        "condition": "Sealed-class rows include required deposit, audit, rejection, deadline, or optional rate-limit evidence.",
        "implementation_hook": "residual-policy table and evidence predicate",
    },
    {
        "id": "P7_idempotent_recovery",
        "type": "predicate",
        "condition": "Retries and crashes cannot expose partial accepted rows or duplicate queue entries.",
        "implementation_hook": "savepoints, idempotent rejection keys, consumed-missing recovery",
    },
    {
        "id": "V1_accepted_only_visibility",
        "type": "visibility",
        "condition": "Estimators, payment joins, and buyer queries see only rows that passed every admission predicate.",
        "implementation_hook": "AggregateQueue insert after all checks or validated materialized view",
    },
    {
        "id": "V2_queryable_rejections",
        "type": "visibility",
        "condition": "Every malformed durable receive or parsed deterministic admission failure has a queryable rejection reason and no queue entry.",
        "implementation_hook": "RejectLog row keyed by terminal key for parsed failures or receive id for malformed attempts",
    },
    {
        "id": "V3_consumed_missing_accounting",
        "type": "visibility",
        "condition": "Expired precommit or release attempts are counted for fill-rate and never enter aggregation.",
        "implementation_hook": "consumed_missing lifecycle state and fill-rate denominator",
    },
]


def tla_sheet() -> str:
    return """---- MODULE CertificateAdmitInvariants ----
EXTENDS FiniteSets, Sequences

\\* TLA-style invariant skeleton for the visible-state contract.
\\* CommitmentValid and DisclosureValid are abstract predicates implemented by
\\* the executable SQL/KV verifier tests.

CONSTANTS AttemptInbox, CertLedger, PricePolicy, RejectLog, AggregateQueue,
          ReceiptLinked, CommitmentValid, DisclosureValid

TerminalKey(c) == <<c.rid, c.nonce>>
Accepted == {c \\in CertLedger : c.status = "accepted"}
QueuedKeys == {TerminalKey(q) : q \\in AggregateQueue}
CounterKey(c) == <<c.seller, c.session, c.ctr>>
NonceKey(c) == <<c.seller, c.session, c.nonce>>

I0_durable_receipt ==
    /\\ \\A c \\in CertLedger : ReceiptLinked[c]
    /\\ \\A r \\in RejectLog : ReceiptLinked[r]

I1_source_once ==
    \\A s \\in {q.src : q \\in AggregateQueue} :
        Cardinality({q \\in AggregateQueue : q.src = s}) <= 1

I2_separate_freshness_keys ==
    \\A c1 \\in Accepted :
    \\A c2 \\in Accepted :
        /\\ (TerminalKey(c1) = TerminalKey(c2) => c1 = c2)
        /\\ (CounterKey(c1) = CounterKey(c2) => c1 = c2)
        /\\ (NonceKey(c1) = NonceKey(c2) => c1 = c2)

I3_policy_price_bound ==
    \\A c \\in Accepted :
        \\E p \\in PricePolicy : p.policy = c.policy /\\ p.priceID = c.priceID

I4_commitment_bound ==
    \\A c \\in Accepted : CommitmentValid[c]

I5_reject_no_queue ==
    \\A r \\in RejectLog : TerminalKey(r) \\notin QueuedKeys

I6_missing_no_queue ==
    \\A c \\in CertLedger :
        c.status = "consumed_missing" => TerminalKey(c) \\notin QueuedKeys

I7_queue_accepted_only ==
    \\A q \\in AggregateQueue :
        \\E c \\in Accepted : TerminalKey(c) = TerminalKey(q)

I8_disclosure_mode ==
    \\A c \\in Accepted : DisclosureValid[c]

CertificateAdmitInvariant ==
    /\\ I0_durable_receipt
    /\\ I1_source_once
    /\\ I2_separate_freshness_keys
    /\\ I3_policy_price_bound
    /\\ I4_commitment_bound
    /\\ I5_reject_no_queue
    /\\ I6_missing_no_queue
    /\\ I7_queue_accepted_only
    /\\ I8_disclosure_mode

====
"""


def main() -> None:
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(INVARIANTS[0].keys()))
        writer.writeheader()
        writer.writerows(INVARIANTS)
    with OUT_EQ_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CONTRACT_EQUIVALENCE[0].keys()))
        writer.writeheader()
        writer.writerows(CONTRACT_EQUIVALENCE)
    payload = {
        "artifact": "certificate_admit_invariants",
        "description": "visible-state lifecycle invariants and contract-equivalence conditions for CertificateAdmit",
        "contract_equivalence_conditions": CONTRACT_EQUIVALENCE,
        "invariants": INVARIANTS,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    OUT_TLA.write_text(tla_sheet(), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
