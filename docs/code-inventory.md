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
- `toolkit/cmd/wone-holders` — replay cutoff-pinned WONE deposit, withdrawal,
  and transfer logs from an archival node and require exact supply/reserve
  closure
- `toolkit/scripts/claims/build-wone-new-holder-metadata.py` — resolve cutoff
  code and nonce for WONE-only threshold or exchange-inventory candidates
- `toolkit/scripts/claims/apply-wone-qualification.py` — add
  ordinary-threshold and exchange manual-delivery WONE to wallet rows while
  preserving native claim fields and reserve conservation
- `toolkit/scripts/claims/build-wone-report.py` — render the integrated WONE
  holder, qualification, reserve-split, and routing finding
- `toolkit/scripts/claims/build-pre-wone-archive-manifest.py` — map immutable
  pre-WONE captures back to their original paths without resolving against
  overwritten post-WONE files
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
- `toolkit/scripts/claims/extend-account-activity.py` — reuse a verified
  baseline activity ledger and merge an incremental WONE-candidate scan
- `toolkit/scripts/claims/summarize-claim-activity.py` — calculate raw
  all-address threshold totals for cutoff-relative activity windows; these are
  context, not the initial-stage population
- `toolkit/scripts/claims/build-migration-stage-policy.py` — apply exact
  deductions, separate wallets from genuine contracts, assign migration stage,
  and render the exact wallet-only activity reconciliation
- `toolkit/scripts/claims/verify-migration-stage-policy.py` — independently
  re-sum ordinary stage, exchange-aggregate overrides, issuance treatment,
  wallet/vault components, activity, contract groups, and compiled routing
  closure
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

## Exchange accounting

- `docs/EXCHANGE_UPDATE_SCOPE.md` — mandatory exchange-only fast-path and
  explicit boundary against WONE/global-policy regeneration
- `toolkit/scripts/exchanges/normalize-exchange-wallets.py` — deterministic
  CSV/XLSX/DOCX ingestion, address normalization, overlap checks, exact
  single- and multi-shard balance parsing, MEXC EIP-191 signer verification,
  KuCoin workbook/proof cross-verification, and DigitalX formula-scoped
  summary/destination/rolled-back-deposit reconciliation
- `toolkit/scripts/exchanges/build-exchange-accounting.py` — reuse the cutoff
  claim, WONE, activity, and policy ledgers to build exchange memos, address
  audits, wallet/balance/signature statistics, per-exchange destination modes
  and Gate delivery tiers, the all-exchange airdrop exclusion set, and manual
  reserve-delivery route inputs
- `toolkit/scripts/exchanges/verify-exchange-routing.py` — independently require
  every memo amount and positive manual source to match the compiled
  `exchange_manual` routing exceptions, including split wallet/staking routes,
  same-address destinations, and Gate tiers
- `toolkit/scripts/exchanges/build-exchange-native-policy.py` — the manual
  delivery worksheet funded from the 2050 supply reserve (per exchange, tier,
  and destination), delegated principal released from every affected validator
  vault, Gate tier and reported-total reconciliation, and the DigitalX
  submitted-total reconciliation with its excluded rolled-back deposit claim;
  refuses audit CSVs whose hashes, destinations, modes, or readiness differ
  from the accounting summary they came from

The runtime configuration, raw/normalized inventories, destinations, and
operator README under `exchanges/` are private and intentionally absent from
the public source package.

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
- `toolkit/scripts/routing/build-non-issuance-routes.py` — convert the current
  reviewed non-issuance inventory, the retained historical-incident caps and
  the revert-leak credit inventory into terminal routes without changing
  partial-row remainders
- `toolkit/scripts/routing/build-inaccessible-inventory.py` — turn reviewed
  inaccessible addresses into `burn_or_inaccessible` inventory rows that
  withhold the whole cutoff claim
- `toolkit/scripts/routing/build-wone-routes.py` — split the WONE source
  reserve into exact redistributed and retained-not-issued routes
- `toolkit/scripts/routing/merge-wallet-theft-inventory.py` — validate reviewed
  perpetrator additions and separate victim positions against the global claim
  ledger, then build the current non-issuance inventory
- `toolkit/scripts/routing/build-wallet-theft-inventory-report.py` and
  `build-non-issuance-report.py` — render the embargoed incident and routing
  findings
- `toolkit/scripts/routing/build-contract-policy-routes.py` — hold reviewed
  multisig and 1wallet allocations for the next stage and mark SmartVault and
  other reviewed genuine-contract allocations as not issued
- `toolkit/scripts/routing/build-contract-treasury-routes.py` — fail-fast
  compatibility stub for the superseded blanket custody policy
- `toolkit/scripts/routing/apply-routes.py` — merge non-issuance and manual
  routes, split partial routes across wallet/vault delivery, verify implicit
  defaults, and emit sparse routing, per-stage readiness, governor, vault-stage,
  and unresolved outputs
- `toolkit/scripts/routing/materialize-initial-stage.py` — expand implicit
  delivery and produce verified initial-only wallet, vault-share, and
  validator-vault plans without deferred or next-stage allocations
- `routing/` — public schema/examples plus ignored real routing files

## Embargo and publication tooling

- `scripts/check-python-version.py` — enforces the supported Python 3.12+
  workflow before Make targets run
- `toolkit/cmd/cutoff-final-verifier` — ignored exact native cutoff and
  historical USD-difference bundle verifier; restored with the result package
  because it embeds expected output identities
- `toolkit/scripts/claims/verify-wone-allocation.py` — independently verify the
  complete WONE holder overlay, threshold subset, exchange manual-delivery
  exception, reserve split, NativeOFT separation, and shard-1 reviewed-contract
  non-issuance
- `toolkit/scripts/build-evidence-bundle.py` — freeze pipeline outputs, the
  pinned cutoff, source metadata, and declared totals into a hash-bound evidence
  bundle
- `toolkit/scripts/random-sample-verify.py` — verify a reproducible random
  sample of an evidence bundle, plus its metadata, totals, airdrop commitment,
  and optional archived chain evidence
- `toolkit/verifier/rules.json`, `sample_core.py`, `sample_core.js` — the
  shared sample-verification rules and their Python and JavaScript engines;
  `node-run.js` runs the JavaScript engine for the conformance test
- `toolkit/verifier/random-sample-verifier.html` — offline single-page verifier
  generated by `scripts/build-sample-verifier-html.py`
- `scripts/check-numerical-embargo.py` — rejects publishable result values and
  private finding and exchange-input paths
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
