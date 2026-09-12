.PHONY: setup build test test-python verify verify-private manifest private-manifest check-public

setup:
	./scripts/setup-harmony-dependencies.sh

build:
	./scripts/build-toolkit.sh

test: test-python verify
	./scripts/build-toolkit.sh

test-python:
	python3 -m unittest discover -s toolkit/tests
	python3 -m py_compile \
		toolkit/scripts/census/*.py \
		toolkit/scripts/claims/*.py \
		toolkit/scripts/cutoff/*.py \
		toolkit/scripts/contract-review/*.py \
		toolkit/scripts/forensics/*.py \
		scripts/*.py

verify:
	python3 scripts/verify-source-manifest.py

verify-private: verify
	python3 scripts/verify-private-results.py
	python3 scripts/verify-release-manifest.py

manifest:
	python3 scripts/update-source-manifest.py

private-manifest:
	python3 scripts/prepare-private-embargo.py --replace
	python3 scripts/update-results-manifest.py
	python3 scripts/update-release-manifest.py
	python3 scripts/update-source-manifest.py

check-public:
	python3 scripts/check-public-package.py
	python3 scripts/check-numerical-embargo.py
