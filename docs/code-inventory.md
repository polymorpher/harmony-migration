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

## Bridge collateral allocations

See `docs/bridge-allocations.md`.

- `bridge/routes.json` — registry of every LayerZero bridge pool and system
  address
- `toolkit/scripts/bridge/resolve-snapshot-blocks.py` — rollback and cutoff
  blocks on every chain
- `toolkit/scripts/bridge/fetch-bridge-state.py` — collateral, supplies,
  endpoint nonces, owners, ONE destinations, remote trusted-peer history, and
  custody Safes at pinned blocks
- `toolkit/scripts/bridge/check-harmony-peers.py` — every remote adapter a
  registered Harmony proxy ever trusted must be registered
- `toolkit/scripts/bridge/build-snapshot-plan.py` — rollback-orphaned delivery
  detection from nonces and the holder snapshot block for every pool
- `toolkit/scripts/bridge/scan-bridge-messages.py` — nonce-assigned bridge
  messages turned into signed per-address adjustments
- `toolkit/cmd/token-holders` — ERC-20 holders at a pinned block on any EVM
  chain, reconciled to `totalSupply()` and spot-checked with `balanceOf()`
- `toolkit/scripts/bridge/run-holder-snapshots.py` and
  `import-holder-snapshot.py` — run or import every planned holder snapshot
- `toolkit/scripts/bridge/classify-holders.py` — key-controlled, multisig,
  1wallet, and contract holders at each snapshot block
- `toolkit/scripts/bridge/build-bridge-allocations.py` — pool scaling,
  allocation statuses, per-chain claim lists, custody and ONE funding plan
- `toolkit/scripts/bridge/verify-bridge-allocations.py` — independent
  recomputation and conservation checks
- `toolkit/scripts/bridge/build-summary-report.py` — per-chain and per-pool
  allocation overview from the verified outputs, with optional USD values
- `toolkit/scripts/bridge/build-migration-exclusions.py` — bridge exclusions
  from the migration's full non-issuance decisions, plus partial and uncovered
  deductions for review
- `toolkit/scripts/bridge/fetch-prices.py` — CoinMarketCap USD price snapshot,
  matched to each pool's escrowed contract

## Bridge claim contracts

See `bridge-claims/README.md` and `bridge-claims/docs/DESIGN.md`.

- `bridge-claims/src/BridgeClaims.sol` — one immutable claim contract per
  chain that pays every asset's rows from the custody multisig's approvals
- `bridge-claims/tools/build_claims.py` — published list, Merkle tree,
  samples, and CREATE2 deployment manifest per contract from the claim lists
- `bridge-claims/tools/approve_claims.py` — Safe Transaction Builder approve
  and revoke batches, Safe transaction hashes, and live funder checks
- `bridge-claims/tools/proof.py` — one payee's rows, proofs, and a
  `claimMany` command
- `bridge-claims/tools/release_plan.py` — the owner transactions that
  release each LayerZero escrow on its own chain to the custody Safe, with no
  cross-chain message or LayerZero service (Safe batches and EOA calldata)
- `bridge-claims/script/Deploy.s.sol` — CREATE2 deployment from a manifest
- `bridge-claims/test/` — unit, fuzz, Python cross-check, claim and
  escrow-release fork tests

## Transaction archive

- `toolkit/scripts/archive/archive_lib.py` — Harmony RPC client, stage
  schemas, transaction and trace classification, cross-shard receipt
  attribution
- `toolkit/scripts/archive/scan-blocks.py` — one shard and branch over a block
  range: transactions and staking of at least the minimum, trace candidates,
  cross-shard receipt flags, and parent-hash continuity
- `toolkit/scripts/archive/find-abandoned-blocks.py` — list the rolled-back
  blocks a reverted node still stores, through its per-address index and a
  parent-hash walk to the fork block
- `toolkit/scripts/archive/import-rpc-export.py` — import a verified export of
  a node's block and receipt RPC responses as a run, checking file hashes,
  parent links, receipts, signed values, and agreement with an overlapping run
- `toolkit/scripts/archive/trace-internal.py` — internal transfers from
  `trace_block` (or `debug_traceBlockByHash` for listed blocks), with revert
  propagation and interleaved, resumable tracing
- `toolkit/scripts/archive/attribute-receipts.py` — explain every outgoing
  receipt root (native, precompile, reverted-constructor, and
  replay-incompatible delegatecall receipts)
- `toolkit/scripts/archive/fetch-calldata.py` — full calldata of a run's
  exported transactions, each proven by its signed hash
- `toolkit/scripts/archive/map-receipt-destinations.py` — destination credit
  of each receipt and coverage of the destination's incoming receipt roots
- `toolkit/scripts/archive/build-archive.py` — merge runs into the archive
  tables and manifest
- `toolkit/scripts/archive/resolve-method-names.py` — function-signature
  labels for the archive's call selectors from public signature databases
- `toolkit/scripts/archive/summarize-archive.py` — counts, distribution,
  addresses, and compressed sizes

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
