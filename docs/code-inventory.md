# Code inventory

## Final cutoff pipeline

- `toolkit/cmd/account-snapshot` — full liquid state export
- `toolkit/cmd/staking-claims` — aggregate staking export plus optional
  per-validator active-delegation principal
- `toolkit/cmd/actual-supply` — independent full state/staking total
- `toolkit/cmd/cross-shard-supply` — cutoff-aware receipt classification
- `toolkit/cmd/historical-state-diff` — old-to-final trie difference
- `toolkit/cmd/address-match-apply` — verified address enrichment
- `toolkit/cmd/migration-claims-verify` — strict final CSV verification
- `toolkit/scripts/claims/actual-supply-ledger.py` — component merge
- `toolkit/scripts/claims/format-all-claims.py` — total-claim, wallet-airdrop,
  and staked-to-vault formatting
- `toolkit/scripts/claims/filter-claims-by-one.py` — generic ONE threshold
- `toolkit/scripts/claims/enrich-claim-metadata-rpc.py` — complete missing
  cutoff code metadata before contract classification
- `toolkit/cmd/account-activity` — read each archival node's local
  per-address explorer-node index in descending order and verify selected
  activity against canonical block headers; does not use the Explorer website
  or REST API
- `toolkit/scripts/claims/fetch-account-activity-rpc.py` — produce the same
  per-shard activity ledger through an archival explorer node's built-in
  Harmony RPC when its local database is unavailable
- `toolkit/scripts/claims/enrich-claim-activity.py` — merge the independent
  shard-0 and shard-1 activity scans into the prioritized claim CSV
- `toolkit/scripts/claims/summarize-claim-activity.py` — calculate cumulative
  prioritized-claim totals for cutoff-relative activity windows and render
  the embargoed activity finding
- `toolkit/scripts/claims/vault-share-ledger-rpc.py` — independent historical
  RPC export of per-validator active delegation
- `toolkit/scripts/claims/verify-vault-delegations.py` — byte-for-byte database
  versus RPC delegation-ledger comparison
- `toolkit/scripts/claims/build-vault-share-allocation.py` — validator deposits
  plus priority/deferred vault-share ledgers
- `toolkit/scripts/claims/build-entitlement-report.py` — private exact
  total-claim wallet/vault delivery report
- `toolkit/scripts/claims/apply-eligibility-policy.py` — preliminary/final
  category split with verified validator-account overrides
- `toolkit/scripts/claims/verify-eligibility-policy.py` — independent category
  and validator-override verification

## Address recovery

- `toolkit/cmd/account-preimage-resolve`
- `toolkit/cmd/canonical-address-resolve`
- `toolkit/cmd/account-transition-locate`
- `toolkit/cmd/transition-trace-resolve`
- `toolkit/cmd/account-nonce-preimage-resolve`

These commands are needed only when a state database lacks auxiliary plaintext
address preimages.

## Independent cutoff checks

Scripts under `toolkit/scripts/cutoff/` verify:

- canonical block continuity and transaction counts;
- old-to-final claim differences;
- changed account balances and nonces through historical RPC;
- origins of newly created accounts through execution traces;
- pending receipt status;
- signed difference arithmetic;
- final component reconciliation.

## Original census

- `toolkit/scripts/census/account-balance-merge.py`
- historical source snapshots under `repro/source-snapshots/2026-09-06/`

## Supply and exploit forensics

Go commands under `toolkit/cmd/forensics/` and Python scripts under
`toolkit/scripts/forensics/` preserve:

- block reward accumulation;
- cross-shard lookup and replay checks;
- outgoing receipt scans;
- historical staking queries;
- exploit flow export and address tracing.

They explain the investigation but are not required to regenerate the final
cutoff ledger.

## Non-core experiment

`repro/experiments/address-corpus-resolve` preserves a protocol-source address
corpus experiment. It produced zero final matches and is not part of the
authoritative pipeline. Because it imports a Harmony `internal` package, build
it from inside the pinned Harmony checkout if historical reproduction is
needed.

## Exact run history

The ignored `repro/as-run/` tree preserves the original machine-specific shell
scripts locally.
`repro/source-snapshots/` preserves earlier versions of source files copied
from the run directories.

These are audit records. Portable users should run the maintained `toolkit/`
commands through `docs/reproduce.md`.

## Contract-account review

Scripts under `toolkit/scripts/contract-review/` classify the code-bearing
manual-review rows (validator accounts, Safe multisigs, 1wallets, SmartVault
wallets, ERC-20, NFT, well-known applications) from archival RPC facts only:

- `fetch-contract-facts.py` — cutoff-policy code/storage/probes plus contextual
  balances, traces, and transaction history
- `selector-census.py` — dispatcher signatures and cutoff-policy proxy
  implementations
- `enrich-contract-facts.py` — cutoff-policy pair symbols, singletons,
  SmartVault ownership, and NFT owner census
- `classify-contracts.py` — category CSVs and `summary.json`
- `contract-review-policy.csv` / `validator-policy-accounts.csv` — generated
  cutoff-pinned eligibility and routing inputs without mutable activity context
- `build-report.py` — Markdown statistics report
- `known-apps.json` — verified application registry and fingerprint rules

See `docs/contract-account-review.md`.

## Explicit destination routing

- `toolkit/scripts/routing/init-local-routing.py` — create held-by-default local
  route and policy files without overwriting operator decisions
- `toolkit/scripts/routing/build-non-issuance-routes.py` — convert the exact
  audited amounts formerly assigned to treasury into terminal non-issuance
  routes without changing partial-row remainders
- `toolkit/scripts/routing/build-contract-treasury-routes.py` — route every
  reviewed non-multisig contract to a specific Safe or multisig holding
  address for later verified claims
- `toolkit/scripts/routing/apply-routes.py` — merge non-issuance and manual
  routes, split partial routes across wallet/vault delivery, verify implicit
  defaults, and emit sparse routing, governor, and unresolved exceptions
- `routing/` — public schema/examples plus ignored real routing files

## Embargo and publication tooling

- `scripts/check-python-version.py` — enforces the supported Python 3.12+
  workflow before Make targets run
- `toolkit/cmd/cutoff-final-verifier` — ignored exact artifact-bundle verifier;
  restored with the result package because it embeds expected output identities
- `scripts/check-numerical-embargo.py` — rejects publishable result values and
  private finding paths
- `scripts/check-public-package.py` — rejects private machine paths, binary
  data, and oversized generated files
- `scripts/prepare-private-embargo.py` — refreshes ignored narrative and compact
  result copies
- `scripts/update-results-manifest.py` — records ignored result identities
- `scripts/verify-private-results.py` — verifies the private result manifest
- `scripts/update-release-manifest.py` and `verify-release-manifest.py` —
  record and verify large ignored allocation artifacts
- `scripts/update-source-manifest.py` and `verify-source-manifest.py` — record
  and verify the public nonignored package
