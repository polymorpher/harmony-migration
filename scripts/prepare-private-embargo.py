#!/usr/bin/env python3

import argparse
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FINDINGS = ROOT / "docs" / "findings"
RESULTS = ROOT / "results" / "2026-09-11"
EMBARGOED = ROOT / "embargoed"
CLAIM_FINDINGS = FINDINGS / "claim-accounting"
DESTINATION_FINDINGS = FINDINGS / "destination-mapping"
CLAIM_RESULTS = RESULTS / "claim-accounting"
DESTINATION_RESULTS = RESULTS / "destination-mapping"

FINDING_MAPPINGS = (
    (
        ROOT
        / "artifacts"
        / "claim-accounting-20260911"
        / "MIGRATION_CLAIM_DELIVERY_2026-09-11.md",
        CLAIM_FINDINGS / "claim-delivery-split.md",
        "claim-accounting",
    ),
    (
        ROOT
        / "artifacts"
        / "contract-review-20260911"
        / "CONTRACT_ACCOUNT_REVIEW_2026-09-11.md",
        DESTINATION_FINDINGS / "contract-account-review.md",
        "destination-mapping",
    ),
    (
        ROOT
        / "artifacts"
        / "source-workspace-top-level"
        / "BLACKLISTED_ADDRESS_TREASURY_RECLAIM_2026-09-11.md",
        DESTINATION_FINDINGS / "treasury-routing.md",
        "destination-mapping",
    ),
)

PRESERVED_FINDINGS = (
    (
        ROOT
        / "artifacts"
        / "source-workspace-top-level"
        / "CUTOFF_MIGRATION_CLAIMS_2026-09-10.md",
        CLAIM_FINDINGS / "cutoff-claim-reconciliation.md",
        "claim-accounting",
    ),
    (
        ROOT
        / "artifacts"
        / "source-workspace-top-level"
        / "PREIMAGE_RECOVERY.md",
        CLAIM_FINDINGS / "address-preimage-recovery.md",
        "claim-accounting",
    ),
    (
        ROOT
        / "artifacts"
        / "source-workspace-top-level"
        / "BLOCK_TIME_AND_SNAPSHOT_REFERENCE_2026-09-10.md",
        CLAIM_FINDINGS / "block-time-reference.md",
        "claim-accounting",
    ),
)

RESULT_MAPPINGS = (
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "all-address-migration-claims-cutoff-summary.json",
        CLAIM_RESULTS / "migration-claims-summary.json",
        "claim-accounting",
        "database-derived",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "migration-claims-at-least-1000-one-summary.json",
        CLAIM_RESULTS / "migration-claims-at-least-1000-one-summary.json",
        "claim-accounting",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "migration-claims-over-1000-one-summary.json",
        CLAIM_RESULTS / "migration-claims-over-1000-one-summary.json",
        "claim-accounting",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "state"
        / "staked-to-vault-by-delegation-rpc-summary.json",
        CLAIM_RESULTS / "staked-to-vault-rpc-summary.json",
        "claim-accounting",
        "RPC-derived",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "migration-claims-preliminary-summary.json",
        DESTINATION_RESULTS / "pre-contract-review-eligibility-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "contract-review-20260911"
        / "out"
        / "summary.json",
        DESTINATION_RESULTS / "contract-account-review-summary.json",
        "destination-mapping",
        "RPC-derived",
    ),
    (
        ROOT
        / "artifacts"
        / "supply-reconciliation-20260911"
        / "treasury-reclaim-inventory-summary.json",
        DESTINATION_RESULTS / "treasury-routing-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
)

OPTIONAL_RESULT_MAPPINGS = (
    (
        ROOT
        / "artifacts"
        / "contract-review-20260911"
        / "out"
        / "policy-summary.json",
        DESTINATION_RESULTS / "post-contract-review-eligibility-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "contract-review-20260911"
        / "out"
        / "policy-verify.json",
        DESTINATION_RESULTS / "post-contract-review-eligibility-verify.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "contract-review-20260911"
        / "out"
        / "vault-share-allocation-summary.json",
        DESTINATION_RESULTS / "vault-share-allocation-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
)

FINDINGS_README = """# Private migration findings

These unredacted files are under the numerical-results embargo:

- `claim-accounting/cutoff-claim-reconciliation.md` — cutoff claim components,
  differences, and verification evidence;
- `claim-accounting/address-preimage-recovery.md` — complete address-recovery
  findings;
- `claim-accounting/block-time-reference.md` — block/time mapping evidence;
- `claim-accounting/claim-delivery-split.md` — exact separation
  of direct wallet airdrop and the amount staked to validator vaults;
- `destination-mapping/contract-account-review.md` — contract
  classifications, balances, and claim destination evidence;
- `destination-mapping/treasury-routing.md` — burn-aware incident and treasury
  destination policy.

`SOURCE-HASHES.json` maps each packaged finding to its retained local source.

Supply-formula and exploit-specific findings are maintained in the separate
`harmony-supply-audit` repository. This directory focuses on migration claim
construction and destination mapping.

See `../numerical-embargo.md` for release conditions and
`../../embargoed/REDACTION-NOTES.md` for restoration instructions.
"""

RESULTS_README = """# Private migration result package

This ignored dated directory contains compact numerical outputs used to compare
an independent reproduction with the original migration calculation.

- `claim-accounting/` contains total-claim, wallet-airdrop,
  staked-to-vault, threshold, and independent RPC summaries;
- `destination-mapping/` contains the preliminary and corrected eligibility
  splits, contract-review summary, vault-share allocation, and
  treasury-routing scenario.

`index.json` records each result's domain, evidence classification, source
identity, packaged identity, byte size, and optional row count.

Complete CSVs and supporting evidence remain under `artifacts/`.
`manifests/results.sha256` identifies this package and the private narrative
findings.
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Refresh ignored private finding and compact result copies"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace existing packaged copies",
    )
    return parser.parse_args()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy(source, target, replace):
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists() and not replace:
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(target) + ".partial")
    if temporary.exists():
        raise FileExistsError(temporary)
    shutil.copyfile(source, temporary)
    temporary.replace(target)


def write(path, content, replace):
    if path.exists() and not replace:
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(path) + ".partial")
    if temporary.exists():
        raise FileExistsError(temporary)
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main():
    args = parse_args()
    EMBARGOED.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source, target, _domain in FINDING_MAPPINGS:
        copy(source, target, args.replace)
        copied += 1
    result_mappings = list(RESULT_MAPPINGS)
    result_mappings.extend(
        mapping
        for mapping in OPTIONAL_RESULT_MAPPINGS
        if mapping[0].is_file()
    )
    for source, target, _domain, _classification in result_mappings:
        copy(source, target, args.replace)
        copied += 1
    for source, target, _domain in PRESERVED_FINDINGS:
        if not source.is_file():
            raise FileNotFoundError(source)
        if not target.is_file():
            raise FileNotFoundError(target)
    write(FINDINGS / "README.md", FINDINGS_README, args.replace)
    provenance = {
        "schema_version": 1,
        "entries": [
            {
                "source": source.relative_to(ROOT).as_posix(),
                "source_sha256": sha256(source),
                "target": target.relative_to(ROOT).as_posix(),
                "target_sha256": sha256(target),
                "domain": domain,
            }
            for source, target, domain in (
                FINDING_MAPPINGS + PRESERVED_FINDINGS
            )
        ],
    }
    write(
        FINDINGS / "SOURCE-HASHES.json",
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        args.replace,
    )
    write(RESULTS / "README.md", RESULTS_README, args.replace)
    index = {
        "schema_version": 1,
        "result_set": "2026-09-11",
        "entries": [
            {
                "path": target.relative_to(RESULTS).as_posix(),
                "domain": domain,
                "classification": classification,
                "bytes": target.stat().st_size,
                "rows": None,
                "sha256": sha256(target),
                "source": source.relative_to(ROOT).as_posix(),
                "source_sha256": sha256(source),
            }
            for source, target, domain, classification in result_mappings
        ],
    }
    write(
        RESULTS / "index.json",
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        args.replace,
    )
    print(
        f"prepared {copied} private files plus private READMEs "
        f"under {FINDINGS.relative_to(ROOT)} and {RESULTS.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
