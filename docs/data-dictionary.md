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
- `wallet_airdrop` — liquid total, pending undelegation, unclaimed reward, and
  supported pending cross-shard value; excludes active stake/delegation.
- `staked_to_vault` — active stake/delegation moved to validator ERC-4626
  vaults and represented to delegators as vault shares.
- `total_claim` — `wallet_airdrop + staked_to_vault`; this is the amount used
  for the threshold and the total amount delivered across wallets and vaults.

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
actual code hash, including below-threshold rows reserved for a later portal.

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
address. The scanner reads the archival node's local per-address
explorer-node index and verifies each selected block against the canonical
chain database. It does not use the retiring Explorer website or REST API.
Internal EVM calls and validator consensus signatures are not counted. Blank
activity fields mean that no qualifying indexed transaction was found; they do
not prove that the account was never used.

Indexed activity includes failed, reverted, and zero-value top-level
transactions. A cross-shard `recipient` entry records destination intent in
the source transaction; it does not prove that the destination receipt was
applied. A validator can also receive a staking-index entry because somebody
else delegated or undelegated. Contract creation indexes the creator rather
than the newly created address.

Activity is capped at the cutoff and is reporting context only. It does not
change claim amounts, eligibility, or routing.

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

The selected filter is `total_claim_atto >= 1,000 ONE`. Active
stake/delegation therefore helps an account qualify, even though it is excluded
from the direct wallet airdrop.

The threshold result is split into:

- automatic ordinary-EOA and verified validator-account claims;
- genuine-contract manual review and class-specific recovery;
- non-issued burn, inaccessible, previously-blacklisted, and
  report-identified perpetrator amounts.

The account-category CSVs carry both `wallet_airdrop_atto` and
`staked_to_vault_atto`. A separate per-validator ledger maps each active
delegation to its validator vault.

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
  the prioritized batch;
- `deferred_staked_to_vault_atto` — principal reserved for later claim/batch;
- `delegation_rows` — positive delegations backing that vault.

Direct wallet distribution files use `wallet_airdrop_atto` only. They retain
`total_claim_atto` and `staked_to_vault_atto` for audit but must not add the
staked amount to the ERC-20 transfer amount.

## Sparse routing outputs

`routing-exceptions.csv` contains only delivery that cannot use the ordinary
code-less EOA implicit default:

- `component` — `wallet_airdrop` or `vault_shares`;
- source and optional validator address/secure-key fields;
- `source_category` — `ordinary_eoa`, `validator_account`,
  `contract_review`, `excluded`, or `deferred`;
- `source_code_bearing` — whether cutoff metadata contains non-empty code;
- `amount_atto` — the affected wallet amount or vault-share principal;
- `exception_type` — explicit route, validator same-address approval, or
  generated hold;
- route, destination, status, reason, and evidence fields.

`destination_status = not_issuing` is a terminal outcome with no destination
address. `routing-summary.json` records `not_issued_*` totals and the remaining
`issuable_*` totals. Gross claims remain unchanged, and:

`gross claim = issuable amount + not-issued amount`.

`validator-governor-exceptions.csv` is separate because control of a validator
vault is not delivery of the validator account's own claim. It contains
explicit governor overrides and safety holds only.

`unresolved-routing.csv` is the generated subset whose destination status is
not `ready`. It is never a routing input and must not be edited.
