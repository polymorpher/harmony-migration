.PHONY: setup build check-python test test-python verify verify-private manifest private-manifest check-public

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
	python3 scripts/verify-private-results.py
	python3 scripts/verify-release-manifest.py

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
