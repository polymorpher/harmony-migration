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
        / "claim-accounting-20260911"
        / "PRIORITY_CLAIM_ACTIVITY_2026-09-14.md",
        CLAIM_FINDINGS / "priority-claim-activity.md",
        "claim-accounting",
    ),
    (
        ROOT
        / "artifacts"
        / "migration-policy-20260917"
        / "MIGRATION_STAGE_POLICY_2026-09-17.md",
        CLAIM_FINDINGS / "migration-stage-policy.md",
        "claim-accounting",
    ),
    (
        ROOT
        / "artifacts"
        / "migration-policy-20260917"
        / "INITIAL_STAGE_MATERIALIZATION_2026-09-17.md",
        CLAIM_FINDINGS / "initial-stage-materialization.md",
        "claim-accounting",
    ),
    (
        ROOT
        / "artifacts"
        / "wone-holder-accounting-20260917"
        / "WONE_MIGRATION_INTEGRATION_2026-09-17.md",
        CLAIM_FINDINGS / "wone-holder-qualification.md",
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
        DESTINATION_FINDINGS / "historical-treasury-routing-audit.md",
        "destination-mapping",
    ),
    (
        ROOT
        / "artifacts"
        / "supply-reconciliation-20260911"
        / "WALLET_THEFT_INVENTORY_UPDATE_2026-09-16.md",
        DESTINATION_FINDINGS / "wallet-theft-inventory-update.md",
        "destination-mapping",
    ),
    (
        ROOT
        / "artifacts"
        / "supply-reconciliation-20260911"
        / "NON_ISSUANCE_POLICY_2026-09-16.md",
        DESTINATION_FINDINGS / "non-issuance.md",
        "destination-mapping",
    ),
    (
        ROOT
        / "artifacts"
        / "exchange-accounting-20260917"
        / "EXCHANGE_MIGRATION_ACCOUNTING_2026-09-17.md",
        DESTINATION_FINDINGS / "exchange-migration-accounting.md",
        "destination-mapping",
    ),
    (
        ROOT
        / "artifacts"
        / "exchange-accounting-20260917"
        / "GATE_AUTOMATIC_AIRDROP_AUDIT_2026-09-17.md",
        DESTINATION_FINDINGS / "gate-automatic-airdrop-audit.md",
        "destination-mapping",
    ),
    *(
        (
            ROOT
            / "artifacts"
            / "exchange-accounting-20260917"
            / "memos"
            / f"{exchange_id}.md",
            DESTINATION_FINDINGS
            / "exchange-memos"
            / f"{exchange_id}.md",
            "destination-mapping",
        )
        for exchange_id in (
            "binance",
            "binance-us",
            "gate",
            "mexc",
            "okx",
            "kucoin",
        )
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
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "migration-policy-20260917"
        / "migration-stage.verify.json",
        CLAIM_RESULTS / "migration-stage.verify.json",
        "claim-accounting",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "wone-holder-accounting-20260917"
        / "wone-holders-cutoff-summary.json",
        CLAIM_RESULTS / "wone-holder-scan-summary.json",
        "claim-accounting",
        "RPC-derived",
    ),
    (
        ROOT
        / "artifacts"
        / "wone-holder-accounting-20260917"
        / "wone-only-qualified-metadata-summary.json",
        CLAIM_RESULTS / "wone-only-qualified-metadata-summary.json",
        "claim-accounting",
        "RPC-derived",
    ),
    (
        ROOT
        / "artifacts"
        / "wone-holder-accounting-20260917"
        / "wone-allocation-final-verify.json",
        CLAIM_RESULTS / "wone-allocation-final-verify.json",
        "claim-accounting",
        "policy-scenario",
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
        / "all-address-migration-claims-cutoff-metadata-summary.json",
        CLAIM_RESULTS / "all-address-claim-metadata-summary.json",
        "claim-accounting",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "migration-claims-at-least-1000-one-metadata-summary.json",
        CLAIM_RESULTS / "priority-claim-metadata-summary.json",
        "claim-accounting",
        "RPC-derived",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "migration-claims-at-least-1000-one-metadata-filter-summary.json",
        CLAIM_RESULTS / "priority-claim-metadata-filter-summary.json",
        "claim-accounting",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "account-activity-shard0-summary.json",
        CLAIM_RESULTS / "account-activity-shard0-summary.json",
        "claim-accounting",
        "hybrid",
    ),
    (
        ROOT
        / "artifacts"
        / "migration-policy-20260917"
        / "migration-stage-summary.json",
        CLAIM_RESULTS / "migration-stage-summary.json",
        "claim-accounting",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "migration-policy-20260917"
        / "migration-stage-policy.csv",
        CLAIM_RESULTS / "migration-stage-policy.csv",
        "claim-accounting",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "account-activity-shard1-summary.json",
        CLAIM_RESULTS / "account-activity-shard1-summary.json",
        "claim-accounting",
        "RPC-derived",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "migration-claims-at-least-1000-one-metadata-activity-summary.json",
        CLAIM_RESULTS / "priority-claim-activity-source-summary.json",
        "claim-accounting",
        "hybrid",
    ),
    (
        ROOT
        / "artifacts"
        / "claim-accounting-20260911"
        / "priority-claim-activity-summary.json",
        CLAIM_RESULTS / "priority-claim-activity-summary.json",
        "claim-accounting",
        "hybrid",
    ),
    (
        ROOT
        / "artifacts"
        / "cutoff-20260910"
        / "claims"
        / "migration-claims-preliminary-complete-summary.json",
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
        DESTINATION_RESULTS / "historical-treasury-inventory-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "exchanges"
        / "wallets-standardized"
        / "summary.json",
        DESTINATION_RESULTS / "exchange-normalization-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "exchange-accounting-20260917"
        / "summary.json",
        DESTINATION_RESULTS / "exchange-accounting-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "exchange-accounting-20260917"
        / "routing-verification.json",
        DESTINATION_RESULTS / "exchange-routing-verification.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "supply-reconciliation-20260911"
        / "reported-wallet-theft-perpetrator-related-cutoff-summary.json",
        DESTINATION_RESULTS / "wallet-theft-perpetrator-related-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "supply-reconciliation-20260911"
        / "wallet-theft-victim-inventory-summary.json",
        DESTINATION_RESULTS / "wallet-theft-victim-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "artifacts"
        / "supply-reconciliation-20260911"
        / "non-issuance-inventory-summary.json",
        DESTINATION_RESULTS / "non-issuance-inventory-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "not-issuing.csv",
        DESTINATION_RESULTS / "routes" / "not-issuing.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "contract-policy.csv",
        DESTINATION_RESULTS / "routes" / "contract-policy.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "manual.csv",
        DESTINATION_RESULTS / "routes" / "manual.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "multisigs.csv",
        DESTINATION_RESULTS / "routes" / "multisigs.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "lost-wallets.csv",
        DESTINATION_RESULTS / "routes" / "lost-wallets.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "frozen-wallets.csv",
        DESTINATION_RESULTS / "routes" / "frozen-wallets.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "bridge-reserves.base.csv",
        DESTINATION_RESULTS / "routes" / "bridge-reserves.base.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "bridge-reserves.csv",
        DESTINATION_RESULTS / "routes" / "bridge-reserves.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "bridge-reserves-summary.json",
        DESTINATION_RESULTS / "routes" / "bridge-reserves-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "destinations.csv",
        DESTINATION_RESULTS / "routes" / "destinations.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "validator-governors.csv",
        DESTINATION_RESULTS / "routes" / "validator-governors.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "policy-decisions.csv",
        DESTINATION_RESULTS / "routes" / "policy-decisions.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT / "routing" / "local" / "not-issuing-summary.json",
        DESTINATION_RESULTS / "routes" / "not-issuing-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "routing"
        / "local"
        / "contract-policy-summary.json",
        DESTINATION_RESULTS
        / "routes"
        / "contract-policy-summary.json",
        "destination-mapping",
        "policy-scenario",
    ),
    *(
        (
            ROOT
            / "routing"
            / "local"
            / "generated"
            / "initial-stage"
            / filename,
            DESTINATION_RESULTS / "initial-stage" / filename,
            "destination-mapping",
            "policy-scenario",
        )
        for filename in (
            "wallet-allocations.csv",
            "vault-shares.csv",
            "validator-vaults.csv",
            "unresolved.csv",
            "summary.json",
        )
    ),
    (
        ROOT
        / "routing"
        / "local"
        / "generated"
        / "validator-vault-stages.csv",
        DESTINATION_RESULTS / "validator-vault-stages.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "routing"
        / "local"
        / "generated"
        / "routing-exceptions.csv",
        DESTINATION_RESULTS / "routing-exceptions.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "routing"
        / "local"
        / "generated"
        / "validator-governor-exceptions.csv",
        DESTINATION_RESULTS / "validator-governor-exceptions.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "routing"
        / "local"
        / "generated"
        / "unresolved-routing.csv",
        DESTINATION_RESULTS / "unresolved-routing.csv",
        "destination-mapping",
        "policy-scenario",
    ),
    (
        ROOT
        / "routing"
        / "local"
        / "generated"
        / "routing-summary.json",
        DESTINATION_RESULTS / "routing-summary.json",
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
        / "base-delivery-summary.json",
        DESTINATION_RESULTS / "base-delivery-summary.json",
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
- `claim-accounting/priority-claim-activity.md` — raw all-address threshold
  totals by indexed activity window;
- `claim-accounting/migration-stage-policy.md` — exact wallet-only initial
  stage, deferred wallets, reviewed contract stages, and conservation;
- `claim-accounting/initial-stage-materialization.md` — initial-only wallet,
  vault-share, validator-vault, unresolved, and readiness outputs;
- `claim-accounting/wone-holder-qualification.md` — cutoff WONE holder
  reconciliation, combined threshold, wallet amount, and source split;
- `destination-mapping/contract-account-review.md` — contract
  classifications, balances, destination evidence, and reviewed stage policy;
- `destination-mapping/historical-treasury-routing-audit.md` — retained
  burn-aware calculation of the exact amounts formerly assigned to treasury;
- `destination-mapping/wallet-theft-inventory-update.md` — expanded
  perpetrator-related inventory and separate reported-victim reconciliation;
- `destination-mapping/non-issuance.md` — current terminal non-issuance policy
  for those exact amounts.
- `destination-mapping/exchange-migration-accounting.md` and
  `exchange-memos/` — per-exchange cutoff totals, balance/activity breakdowns,
  destination readiness, and input reconciliation;
- `destination-mapping/gate-automatic-airdrop-audit.md` — Gate automatic and
  residual totals plus the identities of its complete private address lists.

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

- `claim-accounting/` contains native and WONE claim overlays,
  wallet-airdrop, staked-to-vault, threshold, cutoff-relative activity, and
  independent RPC summaries;
- `destination-mapping/` contains the preliminary and corrected eligibility
  splits, contract-review summary, vault-share allocation, sparse routing and
  governor exceptions, exchange normalization/accounting summaries, unresolved
  work queue, and non-issuance scenario.

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
