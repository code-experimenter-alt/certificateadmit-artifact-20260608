#!/usr/bin/env python3
"""Compile the CertificateAdmit contract into store-level artifacts.

This script is intentionally small: it treats CertificateAdmit as a declarative
admission/provenance contract and emits the store checks that an implementation
must enforce. The generated SQL is a skeleton used by the paper artifact to
make the contract reusable across ordinary transactions, outboxes, and
linearizable KV stores.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
OUT_JSON = BASE / "certificate_contract_compiler.json"
OUT_CSV = BASE / "certificate_contract_compiler.csv"
OUT_SQLITE = BASE / "certificate_contract_sqlite.sql"
OUT_POSTGRES = BASE / "certificate_contract_postgres.sql"
OUT_KV = BASE / "certificate_contract_kv_plan.txt"


RELATIONS = [
    {
        "name": "AttemptInbox",
        "schema": "attempt_id TEXT PRIMARY KEY, receive_hash TEXT NOT NULL, state TEXT NOT NULL, received_at REAL NOT NULL",
        "role": "durable receive record before parsing, verification, or rejection",
    },
    {
        "name": "SourceToken",
        "schema": "src TEXT PRIMARY KEY, seller TEXT NOT NULL, session TEXT NOT NULL, workload TEXT NOT NULL, state TEXT NOT NULL",
        "role": "one issued source/cohort slot consumed at most once",
    },
    {
        "name": "CertLedger",
        "schema": "rid TEXT NOT NULL, nonce TEXT NOT NULL, src TEXT NOT NULL UNIQUE, seller TEXT NOT NULL, session TEXT NOT NULL, ctr INTEGER NOT NULL, policy TEXT NOT NULL, price_id TEXT NOT NULL, commitment TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(rid, nonce), UNIQUE(seller, session, ctr), UNIQUE(seller, session, nonce)",
        "role": "terminal and freshness-key provenance",
    },
    {
        "name": "PricePolicy",
        "schema": "policy TEXT NOT NULL, price_id TEXT NOT NULL, quality_class TEXT NOT NULL, price REAL NOT NULL, PRIMARY KEY(policy, price_id)",
        "role": "posted price/SLA row bound before admission",
    },
    {
        "name": "RejectLog",
        "schema": "reject_key TEXT PRIMARY KEY, rid TEXT, nonce TEXT, reason TEXT NOT NULL, seller TEXT NOT NULL, workload TEXT NOT NULL",
        "role": "queryable deterministic rejection provenance keyed by terminal key or receive id",
    },
    {
        "name": "AggregateQueue",
        "schema": "rid TEXT NOT NULL, nonce TEXT NOT NULL, src TEXT NOT NULL, seller TEXT NOT NULL, session TEXT NOT NULL, workload TEXT NOT NULL, quality_class TEXT NOT NULL, price_id TEXT NOT NULL, commitment TEXT NOT NULL, PRIMARY KEY(rid, nonce)",
        "role": "accepted-only estimator input",
    },
]


PREDICATES = [
    {
        "name": "durable_receipt",
        "relations": "AttemptInbox",
        "sqlite": "INSERT OR IGNORE INTO AttemptInbox(attempt_id, receive_hash, state, received_at) VALUES (:attempt_id, :receive_hash, 'received', :now)",
        "postgres": "INSERT INTO AttemptInbox(attempt_id, receive_hash, state, received_at) VALUES ($1, $2, 'received', $3) ON CONFLICT (attempt_id) DO NOTHING",
        "kv": "put absent inbox/attempt_id := receive_hash,state=received",
        "if_omitted": "crash after receive can lose malformed or deterministic rejection provenance",
    },
    {
        "name": "source_once",
        "relations": "SourceToken, CertLedger",
        "sqlite": "UPDATE SourceToken SET state='consumed' WHERE src=:src AND state='issued'; -- rowcount must be 1",
        "postgres": "UPDATE SourceToken SET state='consumed' WHERE src=$1 AND state='issued'; -- rowcount must be 1",
        "kv": "compare value(source/src)=issued",
        "if_omitted": "duplicate source accepted",
    },
    {
        "name": "separate_freshness_keys",
        "relations": "CertLedger",
        "sqlite": "PRIMARY KEY(rid, nonce); UNIQUE(seller, session, ctr); UNIQUE(seller, session, nonce)",
        "postgres": "same unique indexes, enforced inside SERIALIZABLE transaction",
        "kv": "compare absent terminal/rid/nonce, counter/seller/session/ctr, and nonce/seller/session/nonce",
        "if_omitted": "replay or fork accepted",
    },
    {
        "name": "policy_price",
        "relations": "PricePolicy, CertLedger",
        "sqlite": "SELECT 1 FROM PricePolicy WHERE policy=:policy AND price_id=:price_id",
        "postgres": "SELECT ... FOR SHARE FROM PricePolicy WHERE policy=$1 AND price_id=$2",
        "kv": "read immutable policy/price key selected by attested policy",
        "if_omitted": "substituted price paid",
    },
    {
        "name": "reject_provenance",
        "relations": "AttemptInbox, RejectLog",
        "sqlite": "INSERT OR IGNORE INTO RejectLog(reject_key, rid, nonce, reason, seller, workload) VALUES (:reject_key, :rid, :nonce, :reason, :seller, :workload)",
        "postgres": "INSERT INTO RejectLog(reject_key, rid, nonce, reason, seller, workload) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (reject_key) DO NOTHING",
        "kv": "put reject/K_t/reason for parsed failures or reject/K_a/reason for malformed receives",
        "if_omitted": "fill/dispute query loses reason",
    },
    {
        "name": "accepted_only_input",
        "relations": "AggregateQueue, CertLedger",
        "sqlite": "INSERT INTO AggregateQueue(rid, nonce, ...) only after source, freshness, price, commitment, and residual checks",
        "postgres": "same queue insert in the accept branch of one SERIALIZABLE transaction",
        "kv": "put queue/rid/nonce in the success branch of the same compare-put transaction",
        "if_omitted": "unsafe aggregation input",
    },
    {
        "name": "residual_policy",
        "relations": "CertLedger, PricePolicy",
        "sqlite": "CHECK residual evidence before accepted CertLedger status",
        "postgres": "same residual evidence predicate in transaction",
        "kv": "include residual bucket in attested success value",
        "if_omitted": "within-class over-claim without penalty",
    },
]


def create_table_sql(dialect: str) -> str:
    stmts: list[str] = []
    for rel in RELATIONS:
        stmts.append(f"CREATE TABLE {rel['name']} ({rel['schema']});")
    stmts.extend(
        [
            "CREATE INDEX reject_reason_idx ON RejectLog(reason, seller, workload);",
            "CREATE INDEX aggregate_price_idx ON AggregateQueue(price_id, quality_class);",
        ]
    )
    if dialect == "postgres":
        return "\n".join(stmt.replace("INTEGER", "BIGINT") for stmt in stmts) + "\n"
    return "\n".join(stmts) + "\n"


def admission_skeleton(dialect: str) -> str:
    def ph(name: str) -> str:
        if dialect == "postgres":
            return f"%({name})s"
        return f":{name}"

    if dialect == "postgres":
        begin = "BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;"
        insert_prefix = "INSERT INTO"
        conflict_suffix = " ON CONFLICT DO NOTHING"
    else:
        begin = "BEGIN IMMEDIATE;"
        insert_prefix = "INSERT OR IGNORE INTO"
        conflict_suffix = ""
    return f"""
-- CertificateAdmit admission skeleton for {dialect}.
{begin}
-- 0. Durably record receive before parsing, verification, or rejection.
{insert_prefix} AttemptInbox
 (attempt_id, receive_hash, state, received_at)
 VALUES ({ph('attempt_id')}, {ph('receive_hash')}, 'received', {ph('now')}){conflict_suffix};
-- 1. Verify attestation, commitment, policy version, and residual evidence in host code.
-- 2. Consume exactly one issued source token; abort or reject unless rowcount is 1.
UPDATE SourceToken SET state='consumed'
 WHERE src={ph('src')} AND state='issued';
-- 3. Join the attested policy to the posted price row.
SELECT 1 FROM PricePolicy
 WHERE policy={ph('policy')} AND price_id={ph('price_id')};
-- 4. Insert the fresh accepted certificate tuple.
{insert_prefix} CertLedger
 (rid, nonce, src, seller, session, ctr, policy, price_id, commitment, status)
 VALUES ({ph('rid')}, {ph('nonce')}, {ph('src')}, {ph('seller')}, {ph('session')},
         {ph('ctr')}, {ph('policy')},
         {ph('price_id')}, {ph('commitment')}, 'accepted'){conflict_suffix};
-- 5. Insert accepted-only estimator input only after all checks pass.
{insert_prefix} AggregateQueue
 (rid, nonce, src, seller, session, workload, quality_class, price_id, commitment)
 VALUES ({ph('rid')}, {ph('nonce')}, {ph('src')}, {ph('seller')}, {ph('session')},
         {ph('workload')}, {ph('quality_class')},
         {ph('price_id')}, {ph('commitment')}){conflict_suffix};
COMMIT;
""".lstrip()


def kv_plan() -> str:
    lines = [
        "CertificateAdmit linearizable KV plan",
        "",
        "Receive step writes:",
        "- inbox/attempt_id := receive_hash,state=received before parsing or verification",
        "",
        "Success branch compares:",
    ]
    for pred in PREDICATES:
        if pred["name"] in {"durable_receipt", "reject_provenance", "accepted_only_input"}:
            continue
        lines.append(f"- {pred['name']}: {pred['kv']}")
    lines.extend(
        [
            "",
            "Success branch writes:",
            "- source/src := consumed",
            "- terminal/rid/nonce := accepted certificate row",
            "- counter/seller/session/ctr := terminal/rid/nonce",
            "- nonce/seller/session/nonce := terminal/rid/nonce",
            "- queue/rid/nonce := accepted aggregate input",
            "",
            "Failure branch writes:",
            "- reject/rid/nonce/reason := deterministic rejection reason when the attempt is parsed",
            "- reject/attempt_id/reason := malformed receive reason when terminal key cannot be derived",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT_SQLITE.write_text(create_table_sql("sqlite") + "\n" + admission_skeleton("sqlite"), encoding="utf-8")
    OUT_POSTGRES.write_text(create_table_sql("postgres") + "\n" + admission_skeleton("postgres"), encoding="utf-8")
    OUT_KV.write_text(kv_plan(), encoding="utf-8")

    rows = [
        {
            "predicate": pred["name"],
            "relations": pred["relations"],
            "sqlite": pred["sqlite"],
            "postgres": pred["postgres"],
            "kv": pred["kv"],
            "if_omitted": pred["if_omitted"],
        }
        for pred in PREDICATES
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "artifact": "certificate_contract_compiler",
        "description": "declarative CertificateAdmit contract compiled to SQL and linearizable KV checks",
        "relations": RELATIONS,
        "predicates": rows,
        "generated_files": [
            OUT_CSV.name,
            OUT_SQLITE.name,
            OUT_POSTGRES.name,
            OUT_KV.name,
        ],
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
