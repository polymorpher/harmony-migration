# Data dictionary

## Account identity

- `secure_key` — lowercase 32-byte Keccak-256 hash of the 20-byte account
  address.
- `address` — EVM hexadecimal address. It is valid only when its Keccak-256 hash
  equals `secure_key`.
- `address_or_secure_key` — address when resolved, otherwise the secure key.
- `address_resolved` — `true` only when `address` is present and verified.

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
- `code_hash_shard0`, `code_hash_shard1` — account code hash when the account
  exists on that shard.

A non-empty code hash usually identifies an EVM contract, but Harmony also
stores an RLP-encoded `ValidatorWrapper` in a validator account's code field.
The code hash therefore triggers classification; it is not by itself proof
that the account is a contract or that it has a recoverable Ethereum owner.

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
- treasury-routed burn, inaccessible, previously-blacklisted, and
  report-identified perpetrator addresses.

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
