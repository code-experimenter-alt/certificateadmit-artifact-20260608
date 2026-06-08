.PHONY: reproduce smoke figures postgres etcd

reproduce:
	bash scripts/reproduce_all.sh

smoke:
	python3 artifact/certificate_demo.py
	python3 artifact/certificate_sql_demo.py
	python3 artifact/certificate_contract_compiler.py
	python3 artifact/certificate_admit_invariants.py
	python3 artifact/contract_equivalence_checker.py
	python3 artifact/component_ablation.py
	python3 artifact/minimal_design_ablation.py
	python3 artifact/admission_design_comparison.py --n 5000
	python3 artifact/marketplace_workflow_benchmark.py --n 5000
	python3 artifact/safe_view_freshness_tradeoff.py --n 5000 --batch-sizes 128 512 2048 5000
	python3 artifact/end_to_end_admission_benchmark.py --n 2000 --workers 1 4
	python3 artifact/local_integrated_verifier_path.py --n 2000 --workers 1 4
	python3 artifact/private_exact_verifier_benchmark.py
	python3 artifact/public_market_offer_case_study.py
	python3 artifact/selective_missingness_sensitivity.py
	python3 artifact/residual_policy_sensitivity.py
	python3 artifact/integrated_deployment_envelope.py
	python3 artifact/output_stat_baseline.py

figures:
	python3 artifact/figure_reproduction_manifest.py

postgres:
	python3 artifact/postgres_admission_benchmark.py
	python3 artifact/isolation_anomaly_demo.py

etcd:
	python3 artifact/etcd_linearizable_admission_benchmark.py --n 1000 --workers 1 4 16
