# Certificate Admission Demo

This directory contains a minimal, dependency-free verifier artifact for the
budget/class-integrity certificate described in the paper.

From the repository root, the short smoke test is:

```bash
make smoke
```

The full local reproduction path for non-AWS artifact components is:

```bash
make reproduce
```

The optional PostgreSQL serializable-admission benchmark is:

```bash
make postgres
```

Run:

```bash
python3 artifact/certificate_demo.py
python3 artifact/certificate_sql_demo.py
python3 artifact/component_ablation.py
python3 artifact/minimal_design_ablation.py
python3 artifact/certificate_store_benchmark.py
python3 artifact/end_to_end_admission_benchmark.py
```

The script writes `certificate_demo_results.json` and demonstrates:

- valid certificate admission;
- rejection of replayed counters;
- PCR allow-list failure;
- stale policy version failure;
- missing source-admission token failure;
- nonce reuse failure;
- residual-control deposit failure;
- price-row substitution failure;
- source-token reuse failure;
- report-commitment mutation failure;
- forked counter race where only one branch commits;
- joining accepted certificate rows with a price table before aggregation.

`certificate_sql_demo.py` materializes the accepted/rejected rows into SQLite,
adds indexes over policy/workload/seller/quality/rejection reason, and writes
`certificate_sql_queries.json` with price, source-token, accepted-only, and audit
queries.

`certificate_contract_compiler.py` treats CertificateAdmit as a declarative
admission/provenance contract. It writes
`certificate_contract_compiler.csv`, `certificate_contract_compiler.json`,
`certificate_contract_sqlite.sql`, `certificate_contract_postgres.sql`, and
`certificate_contract_kv_plan.txt`, which list the required predicates and the
store-level SQL/KV checks generated for ordinary transactions, full
transactional outboxes, and linearizable KV implementations.

`certificate_admit_invariants.py` writes
`certificate_admit_invariants.csv/json/tla` and
`certificate_admit_contract_equivalence.csv`, a compact visible-state invariant
sheet for source-once, freshness, price binding, commitment binding,
reject-no-queue, consumed-missing-no-queue, accepted-only queueing, and
disclosure-mode predicates, plus the P0--P7/V1--V3 contract-equivalence
conditions for full outbox, SQL, KV, and valid-view implementations. The
TLA-style file is a readable invariant skeleton; the executable artifacts
instantiate these predicates in SQL/KV tests.

`contract_equivalence_checker.py` writes
`contract_equivalence_checker.csv` and `contract_equivalence_checker.json`, a
deterministic design-by-predicate matrix over NoVerify metadata, attestation
blob logging, append-only/reconciled logs, safe materialized views, source-token
gates, partial/full attested outboxes, serializable SQL, and linearizable KV
implementations. A design is marked contract-equivalent only when all P0--P7
predicates and V1--V3 visibility conditions are covered.

`component_ablation.py` writes `component_ablation.csv` and
`component_ablation.json`, a deterministic field-level map from each certificate
component to the admission invariant and attack that becomes possible if the
component is omitted.

`minimal_design_ablation.py` writes `minimal_design_ablation.csv` and
`minimal_design_ablation.json`, comparing the full admission primitive against
natural simpler alternatives such as attestation-only logging, append-only
certificate logs, TEE-only class assertions, and source-token-only gates.

`admission_design_comparison.py` runs a mixed admission/query workload against
metadata-only logging, attestation-blob-only logging, append-only certificate
logging, append-only logging with offline reconciliation, a safe materialized
view that withholds rows until validation, source-token-only admission, an
attested transactional-outbox baseline with source-token and price checks but no
separate counter/nonce freshness or residual contract, a full attested transactional outbox that is
semantically equivalent to `CertificateAdmit`, and full `CertificateAdmit`. It writes
`admission_design_comparison.csv` and `admission_design_comparison.json` with
throughput, representative query latency, final invariant failures, the
pre-reconciliation unsafe invariant window, and publication delay for the safe
view baseline.

The marketplace workflow check is:

```bash
python3 artifact/marketplace_workflow_benchmark.py
```

It writes `marketplace_workflow_benchmark.csv` and
`marketplace_workflow_benchmark.json`, comparing append-only reconciliation, a
safe materialized view, and `CertificateAdmit` on a multi-seller workflow with
multiple workloads, quality classes, policy-version churn, payment joins,
aggregation windows, source-fill queries, and rejection-dispute queries.

`safe_view_freshness_tradeoff.py` writes
`safe_view_freshness_tradeoff.csv` and
`safe_view_freshness_tradeoff.json`, measuring safe materialized-view
publication at several validation-watermark batch sizes and reporting the
modeled query-visible freshness delay against the per-row
`CertificateAdmit` p95 admission-latency reference.

The public data-product offer scale anchor is:

```bash
python3 artifact/public_market_offer_case_study.py
```

It writes `public_market_offer_case_study_offers.csv/json` and
`public_market_offer_case_study_risk.csv/json`, using four public AWS
Marketplace/AWS Data Exchange 12-month ProductAccess listings as observed
contract-scale examples and transparent 10/25/50% quality-haircut amounts. The
listings are not LDP products, not evidence of fraud, and not calibrated market
damages; they anchor the dollar scale at which data-product quality claims can
matter.

`certificate_store_benchmark.py` generates 1M synthetic certificate rows in
SQLite with source-token, price-row, counter, and commitment uniqueness
constraints. It writes `certificate_store_benchmark.csv` and
`certificate_store_benchmark.json` with insert throughput, query latency, and
SQLite query plans for accepted-only aggregation, price joins, source-token
joins, ledger probes, concurrent ledger probes, and rejection audits.

`end_to_end_admission_benchmark.py` runs a transactional SQLite-WAL admission
pipeline with source-token issuance, policy and price-row checks, atomic token
consumption, certificate-key insertion, rejection auditing, accepted-only
aggregation, 32-way duplicate races, and crash/recovery checks. It writes
`end_to_end_admission_benchmark.csv` and
`end_to_end_admission_benchmark.json`. The default run uses 100k submitted
attempts per worker-count setting and a deterministic invalid schedule that
includes 5% duplicate source/counter attempts.

`local_integrated_verifier_path.py` runs a local integrated verifier path with
source-token state, attestation/policy digest checking, SQLite admission,
rejection provenance, accepted-only queueing, aggregation, race checks, and
crash/recovery in one path. It writes `local_integrated_verifier_path.csv` and
`local_integrated_verifier_path.json`. This is not a live AWS Nitro/KMS or
replicated-ledger deployment.

`postgres_admission_benchmark.py` is an optional production-store check. When
PostgreSQL binaries and `psycopg` are available, it starts a temporary local
PostgreSQL cluster with trust authentication, runs the admission gate under
SERIALIZABLE transactions with unique indexes, and writes
`postgres_admission_benchmark.csv` and `postgres_admission_benchmark.json`. The
generated reference output uses 100k submissions at 1/4/16 workers and includes
32-way source-token and counter race tests plus crash/recovery checks.

`isolation_anomaly_demo.py` is run by `make postgres`. It starts the same
temporary PostgreSQL cluster and demonstrates why admission must be an atomic
serializable/linearizable contract: a deliberately weak read-check-write source
token workflow accepts both concurrent branches under READ COMMITTED, while
SERIALIZABLE aborts one branch. It writes `isolation_anomaly_demo.csv` and
`isolation_anomaly_demo.json`.

The public direct-marketing sanity check uses the UCI Bank Marketing
`bank-full.csv` file included under `data/bank_marketing/bank/`:

```bash
python3 artifact/public_marketing_workload.py
python3 artifact/public_mechanism_comparison.py
```

It writes `public_marketing_workload_summary.csv` and
`public_marketing_workload_summary.json`.
The full mechanism comparison writes `public_mechanism_comparison.csv` and
`public_mechanism_comparison.json`.

A second public web-session conversion workload uses the UCI Online Shoppers
Purchasing Intention CSV included under `data/online_shoppers/`:

```bash
python3 artifact/public_websession_workload.py
```

It writes `public_websession_workload.csv` and
`public_websession_workload.json`.

The no-PoW sealed-class policy-selection sensitivity is:

```bash
python3 artifact/nopow_policy_selection.py
```

It writes `nopow_policy_selection.csv` and `nopow_policy_selection.json`.

The residual-control value-scale sensitivity is:

```bash
python3 artifact/residual_policy_sensitivity.py
```

It writes `residual_policy_sensitivity.csv` and
`residual_policy_sensitivity.json`, varying the business-value scale, deposit
cap, and audit-recovery fraction. This is a no-PoW policy sensitivity check,
not an empirical market-pricing claim.

The selective-missingness source-contract sensitivity is:

```bash
python3 artifact/selective_missingness_sensitivity.py
```

It writes `selective_missingness_sensitivity.csv/json` and
`selective_missingness_mitigation_summary.csv/json`, quantifying
consumed-but-missing rates, SLA decisions, and how stricter fill-rate policies
reduce the worst accepted conditioned-estimator bias under simple
positive/negative suppression scenarios. This is a fill-rate stress test, not
a missing-at-random proof.

The output-only statistical diagnostic baseline is:

```bash
python3 artifact/output_stat_baseline.py
```

It writes `output_stat_baseline.csv` and `output_stat_baseline.json`, reporting
detection power for BRR and OUE batch output-count tests at a 5% honest
false-reject rate using a Poisson-binomial normal approximation for fixed batch
composition. This baseline does not provide execution-identity,
freshness, source-token, or price-row binding.

The deployment cost-accounting unit ledger is:

```bash
python3 artifact/deployment_cost_accounting.py
```

It writes `deployment_cost_accounting.csv` and
`deployment_cost_accounting.json`, separating the measured EC2/enclave runtime
component from deployment-specific KMS/session-key release requests,
linearizable ledger writes, certificate-row storage, audit operations, source
tokens, and accepted aggregation rows.

The component-derived deployment envelope is:

```bash
python3 artifact/integrated_deployment_envelope.py
```

It writes `integrated_deployment_envelope.csv` and
`integrated_deployment_envelope.json`, combining recorded Nitro p95/throughput,
local SQLite admission, local three-node etcd, KMS release-request units, and
ledger-write units into conservative path envelopes. This is not a live
AWS/KMS/replicated-ledger benchmark.

The linearizable-ledger latency sensitivity model is:

```bash
python3 artifact/ledger_latency_sensitivity.py
```

It writes `ledger_latency_sensitivity.csv` and
`ledger_latency_sensitivity.json`, reporting the throughput upper bound implied
by two linearizable admission writes per report at 1/5/10/25/50 ms write
latencies.

The optional three-node etcd linearizable-admission check is:

```bash
ETCD_BIN_DIR=/path/to/etcd/bin python3 artifact/etcd_linearizable_admission_benchmark.py
```

or, if `etcd` and `etcdctl` are on `PATH`:

```bash
make etcd
```

It writes `etcd_linearizable_admission_benchmark.csv` and
`etcd_linearizable_admission_benchmark.json`. If etcd is unavailable, the
script records a skipped JSON/CSV result and exits successfully because the
dependency is optional.

The paper reports the 1,000-attempt run:

```bash
ETCD_BIN_DIR=/path/to/etcd/bin python3 artifact/etcd_linearizable_admission_benchmark.py --n 1000 --workers 1 4 16
```

The figure artifact manifest is:

```bash
python3 artifact/figure_reproduction_manifest.py
```

It writes `figure_reproduction_manifest.csv` and
`figure_reproduction_manifest.json` with hashes for the shipped figures and the
script or data product that supports each figure. The minimal package includes
prebuilt figures; this manifest records provenance and missing-file checks
without claiming that every plotting step is redrawn from raw measurements.
