.PHONY: setup build check-python test test-python verify verify-private migration-policy initial-stage exchange-native-report manifest private-manifest check-public

SUPPLY_AUDIT_REPO ?= ../harmony-supply-audit

setup:
	./scripts/setup-harmony-dependencies.sh

build:
	./scripts/build-toolkit.sh

check-python:
	@python3 scripts/check-python-version.py

test: test-python verify
	./scripts/build-toolkit.sh

test-python: check-python
	python3 -m unittest discover -s toolkit/tests
	python3 -m py_compile \
		toolkit/scripts/census/*.py \
		toolkit/scripts/claims/*.py \
		toolkit/scripts/cutoff/*.py \
		toolkit/scripts/contract-review/*.py \
		toolkit/scripts/exchanges/*.py \
		toolkit/scripts/routing/*.py \
		toolkit/scripts/forensics/*.py \
		scripts/*.py

verify: check-python
	python3 scripts/verify-source-manifest.py

verify-private: verify
	python3 toolkit/scripts/claims/build-pre-wone-archive-manifest.py \
		--archive-root artifacts/wone-holder-accounting-20260917/pre-wone \
		--output artifacts/wone-holder-accounting-20260917/pre-wone-manifest.json \
		--check
	python3 toolkit/scripts/claims/verify-wone-allocation.py \
		--check-output artifacts/wone-holder-accounting-20260917/wone-allocation-final-verify.json
	python3 toolkit/scripts/claims/verify-migration-stage-policy.py \
		--stage-policy artifacts/migration-policy-20260917/migration-stage-policy.csv \
		--stage-summary artifacts/migration-policy-20260917/migration-stage-summary.json \
		--routing-summary routing/local/generated/routing-summary.json
	python3 scripts/verify-private-results.py
	python3 scripts/verify-release-manifest.py

migration-policy: check-python
	python3 toolkit/scripts/claims/build-migration-stage-policy.py \
		--qualified-activity artifacts/cutoff-20260910/claims/migration-claims-at-least-1000-one-metadata-activity.csv \
		--activity-summary artifacts/claim-accounting-20260911/priority-claim-activity-summary.json \
		--migration-summary artifacts/cutoff-20260910/claims/all-address-migration-claims-cutoff-summary.json \
		--contract-review artifacts/contract-review-20260911/out/contract-review-policy.csv \
		--existing-non-issuance artifacts/supply-reconciliation-20260911/non-issuance-inventory.csv \
		--existing-non-issuance artifacts/supply-reconciliation-20260911/inaccessible-address-inventory-20260923.csv \
		--historical-retention artifacts/supply-reconciliation-20260911/not-issued-retained-initial-addresses.csv \
		--historical-retention artifacts/supply-reconciliation-20260911/not-issued-revert-leak-recipients.csv \
		--manual-wallets artifacts/exchange-accounting-20260917/qualified-exchange-exclusions.csv \
		--output artifacts/migration-policy-20260917/migration-stage-policy.csv \
		--summary artifacts/migration-policy-20260917/migration-stage-summary.json \
		--report artifacts/migration-policy-20260917/MIGRATION_STAGE_POLICY_2026-09-17.md \
		--replace
	python3 toolkit/scripts/routing/build-contract-policy-routes.py \
		--contracts artifacts/contract-review-20260911/out/contract-review-policy.csv \
		--stage-policy artifacts/migration-policy-20260917/migration-stage-policy.csv \
		--output routing/local/contract-policy.csv \
		--summary routing/local/contract-policy-summary.json \
		--replace

initial-stage: check-python
	python3 toolkit/scripts/claims/verify-migration-stage-policy.py \
		--stage-policy artifacts/migration-policy-20260917/migration-stage-policy.csv \
		--stage-summary artifacts/migration-policy-20260917/migration-stage-summary.json \
		--routing-summary routing/local/generated/routing-summary.json \
		--output artifacts/migration-policy-20260917/migration-stage.verify.json \
		--replace
	python3 toolkit/scripts/routing/materialize-initial-stage.py \
		--stage-policy artifacts/migration-policy-20260917/migration-stage-policy.csv \
		--base-priority-shares artifacts/contract-review-20260911/out/base-priority-vault-shares.csv \
		--routing-exceptions routing/local/generated/routing-exceptions.csv \
		--routing-summary routing/local/generated/routing-summary.json \
		--vault-stages routing/local/generated/validator-vault-stages.csv \
		--governor-exceptions routing/local/generated/validator-governor-exceptions.csv \
		--wallet-output routing/local/generated/initial-stage/wallet-allocations.csv \
		--shares-output routing/local/generated/initial-stage/vault-shares.csv \
		--vault-output routing/local/generated/initial-stage/validator-vaults.csv \
		--unresolved-output routing/local/generated/initial-stage/unresolved.csv \
		--summary routing/local/generated/initial-stage/summary.json \
		--report artifacts/migration-policy-20260917/INITIAL_STAGE_MATERIALIZATION_2026-09-17.md \
		--replace

exchange-native-report: check-python
	python3 toolkit/scripts/exchanges/build-exchange-native-policy.py \
		--policy exchanges/exchange-policy.json \
		--exchange-summary artifacts/exchange-accounting-20260917/summary.json \
		--audits-dir artifacts/exchange-accounting-20260917/audits \
		--normalization-summary exchanges/wallets-standardized/summary.json \
		--native-claims artifacts/cutoff-20260910/claims/all-address-native-claims-cutoff-metadata.csv \
		--delegations artifacts/cutoff-20260910/state/staked-to-vault-by-delegation-rpc.csv \
		--vaults artifacts/contract-review-20260911/out/base-validator-vault-deposits.csv \
		--gate-supplemental exchanges/wallets-raw/gate-addition.txt \
		--gate-reported-total exchanges/wallets-raw/gate-reported-total.txt \
		--output-dir artifacts/exchange-accounting-20260917 \
		--summary artifacts/exchange-accounting-20260917/exchange-native-summary.json \
		--report artifacts/exchange-accounting-20260917/EXCHANGE_MANUAL_DELIVERY_2026-09-22.md \
		--replace

manifest: check-python
	python3 scripts/update-source-manifest.py

private-manifest: check-python
	python3 scripts/prepare-private-embargo.py --replace
	python3 scripts/update-results-manifest.py
	python3 scripts/update-release-manifest.py
	python3 scripts/update-source-manifest.py

check-public: check-python
	python3 scripts/check-public-package.py
	python3 scripts/check-numerical-embargo.py
