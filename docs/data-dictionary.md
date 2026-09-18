# Data dictionary

## Account identity

- `secure_key` — lowercase 32-byte Keccak-256 hash of the 20-byte account
  address.
- `address` — EVM hexadecimal address. It is valid only when its Keccak-256 hash
  equals `secure_key`.
- `address_or_secure_key` — address when resolved, otherwise the secure key.
- `address_resolved` — `true` only when `address` is present and verified.

Merge, formatting, eligibility, classification, and routing stages all reject
an address whose raw 20-byte Keccak-256 hash differs from `secure_key`. The
standalone final verifier is defense in depth, not the first identity check.

## Snapshot metadata

- `claims_shard0_block` — fixed shard-0 state block.
- `claims_shard1_block` — fixed shard-1 state block.
- `valuation_price_reference_shard0_block` — historical reference block for the
  display price.
- `valuation_price_usd_per_one` — external display-price input. It is not
  consensus state.

## Claim components

Every component has:

- an `_atto` integer field;
- a `_one` fixed 18-decimal display field.

Components:

- `liquid_shard0` — liquid account balance on shard 0.
- `liquid_shard1` — liquid account balance on shard 1.
- `liquid_total` — sum of shard-0 and shard-1 liquid balances.
- `active_staked_or_delegated` — active validator self-stake or delegation;
  this becomes validator-vault principal, not a direct wallet airdrop.
- `pending_undelegation` — undelegation entries not yet withdrawn.
- `unclaimed_staking_reward` — reward currently claimable in staking state.
- `pending_cross_shard` — supported outgoing receipts not consumed by the
  destination at its cutoff.
- `native_wallet_airdrop` — liquid total, pending undelegation, unclaimed
  reward, and supported pending cross-shard value; excludes active
  stake/delegation.
- `wone_balance` — cutoff WONE balance used for qualification.
- `wone_airdrop` — WONE balance added to the current wallet amount only when
  the inclusive qualification threshold is met.
- `wallet_airdrop` — `native_wallet_airdrop + wone_airdrop`.
- `staked_to_vault` — active stake/delegation moved to validator ERC-4626
  vaults and represented to delegators as vault shares.
- `native_total_claim` — native wallet amount plus `staked_to_vault`.
- `qualification_total` — `native_total_claim + wone_balance`; this is the
  threshold field.
- `total_claim` — `wallet_airdrop + staked_to_vault`; this is the current
  migration amount delivered across wallets and vaults before source routing.

`wallet_airdrop_usd` and `total_usd` are their corresponding display
valuations.

## Account metadata

- `nonce_shard0`, `nonce_shard1` — account nonce when the account exists on that
  shard.
- `code_hash_shard0`, `code_hash_shard1` — code hash supplied by the liquid
  account export. In the all-address claim ledger it can be blank for a
  staking-only or receipt-only account whose liquid balance is zero.

A non-empty code hash usually identifies an EVM contract, but Harmony also
stores an RLP-encoded `ValidatorWrapper` in a validator account's code field.
The code hash therefore triggers classification; it is not by itself proof
that the account is a contract or that it has a recoverable Ethereum owner.

Before destination classification, the complete all-address claim set is
enriched with historical `eth_getCode` and `eth_getTransactionCount` at the
cutoff block. The resulting
`all-address-migration-claims-cutoff-metadata.csv` companion explicitly
resolves every blank shard-0 code field to either the empty-code hash or its
actual code hash, including native below-threshold rows. WONE-only addresses
are added to this current-migration ledger only when they meet the combined
threshold; the complete holder census remains a separate artifact.

Contract review emits `contract-review-policy.csv` and
`validator-policy-accounts.csv` as the deterministic cutoff-pinned inputs to
routing and eligibility. Files containing latest activity context are
investigative outputs and are not authoritative assignment inputs.

## Account activity context

`migration-claims-at-least-1000-one-metadata-activity.csv` is an
activity-enriched companion to the authoritative prioritized-claim CSV. It
appends:

- `last_activity_time_utc` — UTC time of the latest qualifying transaction at
  or before the claim cutoff;
- `last_activity_timestamp_unix` — the same time as a Unix timestamp;
- `last_activity_block` — the transaction's block on its source shard;
- `last_activity_shard` — `0` or `1`;
- `last_activity_type` — `regular` or `staking`;
- `last_activity_tx_hash` — Harmony transaction hash used as evidence;
- `last_activity_index` — transaction position within its regular or staking
  transaction list; and
- `last_activity_detail` — how the address participated, such as `sender`,
  `recipient`, or a staking-message role.

A qualifying activity is a direct regular transaction involving the address
on shard 0 or shard 1, or a shard-0 staking transaction involving that
address. The shard-0 baseline reads the archival node's local per-address
explorer-node index and verifies each selected block against the canonical
chain database. Incremental WONE rows and shard-1 rows use the archival node's
built-in transaction-history RPC when the corresponding local database is not
available. Neither path uses the retiring Explorer website or REST API.
Internal EVM calls and validator consensus signatures are not counted. Blank
activity fields mean that no qualifying indexed transaction was found; they do
not prove that the account was never used.

Indexed activity includes failed, reverted, and zero-value top-level
transactions. A cross-shard `recipient` entry records destination intent in
the source transaction; it does not prove that the destination receipt was
applied. A validator can also receive a staking-index entry because somebody
else delegated or undelegated. Contract creation indexes the creator rather
than the newly created address.

Activity is capped at the cutoff. It does not change the snapshot amount or
threshold membership. The separate migration-stage policy uses the six-month
window only for eligible wallets; genuine contracts are excluded from every
wallet activity row.

When a candidate-set extension reuses an earlier direct-database scan and
fetches only new rows through archival RPC, the combined activity summary is
classified `hybrid`. Its `provenance_breakdown` records exact row,
activity-found, and activity-not-found counts separately for
`database-derived` and `RPC-derived` inputs. Each classification bucket has a
`sources` list with the exact contributing row counts, summary paths, and
hashes; this also preserves both source summaries when multiple increments use
the same classification.

## Difference CSVs

Difference files contain, for every component:

- `old_<component>_atto`
- `final_<component>_atto`
- `delta_<component>_atto`
- matching fixed-decimal `_one` fields

The signed delta is always:

```text
final - old
```

`change_type` is one of:

- `created`
- `deleted`
- `amount_changed`
- `metadata_changed`

The all-difference CSV contains changed rows only. Unchanged row counts remain
in its summary JSON.

## Threshold result

The selected filter is
`qualification_total_atto >= 1,000 ONE`. Active
stake/delegation therefore helps an account qualify, even though it is excluded
from the direct wallet airdrop; WONE also helps qualify and is then included in
that wallet amount.

The threshold result is first split by account/routing classification:

- automatic ordinary-EOA and verified validator-account claims;
- genuine-contract review;
- exchange and other policy-routed claims excluded from implicit same-address
  delivery; and
- non-issued burn, inaccessible, previously-blacklisted, and
  report-identified perpetrator or directly linked theft-recipient amounts.

Reported victim wallets are a distinct incident-evidence category. They are not
automatically classified as perpetrators or routed to `not-issuing`.

`migration-stage-policy.csv` then records policy separately:

- `account_classification` and `contract_category` — factual identity;
- `policy_group` — wallet, validator wallet, multisig, LayerZero collateral,
  1wallet, SmartVault, or other reviewed contract;
- `routing_category` — automatic policy, exchange/manual, or contract policy;
- `migration_stage` — `initial`, `next_stage`, or `deferred`; blank when no
  allocation remains. Compiled explicitly routed below-threshold rows use
  `manual_review`;
- `issuance_treatment` — `issue` or `not_issued` in the stage ledger and
  `issue`, `not_issued`, or `redistributed` in compiled routing;
- gross, prior-deduction, WONE-source-offset, reviewed-contract
  non-issuance, final wallet/vault, and total migration-allocation atto fields;
- `stage_reason` — policy rationale, not destination evidence.

Threshold membership is evaluated before every deduction in that file.

The account-category CSVs carry `native_wallet_airdrop_atto`,
`wone_airdrop_atto`, `wallet_airdrop_atto`, and `staked_to_vault_atto`. A
separate per-validator ledger maps each active delegation to its validator
vault.

## Validator-vault ledgers

Per-delegation fields:

- `validator_address`, `validator_secure_key` — Harmony validator and future
  vault governor identity;
- `delegator_address`, `delegator_secure_key` — share beneficiary identity;
- `staked_to_vault_atto` — active delegation moved to the vault and backing
  the delegator's shares;
- `is_self_delegation` — whether validator and delegator are the same account.

Per-vault deposit fields:

- `vault_assets_atto` — complete active principal deposited into the validator
  vault;
- `priority_staked_to_vault_atto` — principal whose beneficiary is in
  the snapshot-threshold batch; this legacy field name does not mean the
  principal belongs to the six-month initial stage;
- `deferred_staked_to_vault_atto` — principal reserved for later claim/batch;
- `delegation_rows` — positive delegations backing that vault.

Direct wallet distribution files use `wallet_airdrop_atto` only. They retain
`total_claim_atto` and `staked_to_vault_atto` for audit but must not add the
staked amount to the ERC-20 transfer amount.

## Exchange accounting

Private files under `exchanges/wallets-standardized/` use one row per submitted
source wallet. Core fields are:

- `exchange_id`, `source_file`, `source_sha256`, `source_sheet`, and
  `source_row` — raw-input provenance;
- `address_hex` and `address_one` — canonical equivalent account identities;
- `submitted_balance_raw`, `submitted_balance_unit`, and
  `submitted_balance_atto` — optional exchange-supplied reconciliation value;
- `configured_destination` and `configured_destination_status` — requested
  aggregate destination state; and
- authorization type, destination, message/signature hashes, and verification
  status where the submission contains signed evidence.

The normalized files do not decide payout amounts. Address audits under
`artifacts/exchange-accounting-20260917/audits/` join each submitted wallet to
the cutoff claim, complete WONE census, existing prioritized activity record,
and final eligibility category. They add:

- `qualification_status` and `policy_category`;
- `migration_stage`, joined from the separate stage policy for the final
  report pass;
- `issuance_treatment`, kept separate from that stage;
- `planned_delivery_status`, `planned_wallet_airdrop_atto`,
  `planned_staked_to_vault_atto`, `planned_total_entitlement_atto`, and
  `remaining_not_airdropped_atto`;
- prior last-activity evidence or explicit `not_collected` coverage;
- submitted-balance reconciliation and exact signed delta; and
- all native wallet, WONE, vault, qualification, and current-claim components.

For a manual exchange route, `planned_wallet_airdrop_atto` is the direct ERC-20
ONE transfer amount, while `planned_staked_to_vault_atto` remains vault-share
principal. Their sum, `planned_total_entitlement_atto`, equals current
`total_claim_atto`. It can include a below-threshold native claim, but it does
not fabricate WONE that remains outside the current claim. For Gate all three
planned fields are nonzero only when the wallet passes the threshold and
remains in the automatic category.

## Sparse routing outputs

`routing-exceptions.csv` contains only delivery that cannot use the ordinary
code-less EOA implicit default:

- `component` — `wallet_airdrop` or `vault_shares`;
- source and optional validator address/secure-key fields;
- `source_category` — `ordinary_eoa`, `validator_account`,
  `contract_review`, `excluded`, or `deferred`;
- `source_code_bearing` — whether cutoff metadata contains non-empty code;
- `migration_stage` — `initial`, `next_stage`, `deferred`, or `manual_review`;
  terminal rows keep an associated stage only when they deduct part of a staged
  allocation;
- `issuance_treatment` — `issue`, terminal `not_issued`, or source
  `redistributed`;
- `amount_atto` — the affected wallet amount or vault-share principal;
- `exception_type` — explicit route, validator same-address approval, or
  generated hold;
- route, destination, status, reason, and evidence fields.

`destination_status = not_issuing` is a terminal outcome with no destination
address. `destination_status = redistributed` is also terminal and addressless,
but it is paired with WONE already added to holder rows.
`routing-summary.json` records `not_issued_*`, `redistributed_*`, and remaining
`issuable_*` totals. The expanded routing input closes as:

`expanded claim = issuable amount + not-issued amount + redistributed source`.

`stage_readiness` records, for each stage, issued wallet/vault totals, ready and
held portions, held validator-governor assets, scoped policy decisions,
release authorization, blockers, and status. `initial_stage_status` is an
explicit convenience field; global `status` remains conservative.

`validator-governor-exceptions.csv` is separate because control of a validator
vault is not delivery of the validator account's own claim. It contains
explicit governor overrides and safety holds only.

`validator-vault-stages.csv` partitions every base vault's assets into:

- `initial_assets_atto`;
- `next_stage_assets_atto`;
- `qualified_deferred_assets_atto`;
- `manual_review_assets_atto`;
- `uncompiled_deferred_assets_atto`;
- `not_issued_assets_atto`; and
- `post_policy_assets_atto`, equal to base assets minus not-issued assets.

This prevents excluded contract stake from leaving replacement vault assets or
shares and prevents later-stage stake from entering the initial deployment.

The files under `generated/initial-stage/` materialize only
`migration_stage = initial` and `issuance_treatment = issue`. They contain
wallet destinations, per-validator share beneficiaries, validator assets and
governors, unresolved items, and a hash-bound summary.

`unresolved-routing.csv` is the generated subset whose destination status is
not `ready`. It is never a routing input and must not be edited.
