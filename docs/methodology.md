# Methodology

## Goal

Produce a deterministic account-level claim ledger at fixed Harmony block
roots. Each row identifies one secure account key and its verified EVM address,
then records every included ONE claim component in integer atto-ONE.

One ONE is exactly:

```text
1,000,000,000,000,000,000 atto-ONE
```

All arithmetic is integer arithmetic. Decimal ONE and USD strings are display
fields derived from the integer values.

## Fixed state

The calculation uses two explicit state roots:

- shard 0 block `93,623,067`
- shard 1 block `95,882,100`

The block numbers, hashes, timestamps, and roots are pinned in
`manifests/snapshot-2026-09-10.json`.

Shard 1 deserves special mention. Its cutoff root is identical to the root at
block `94,978,278`. The independent interval audit checked the complete
canonical interval between those heights for transaction, staking, gas, and
state-root activity. Observed interval counts are withheld during independent
reproduction.

## Claim components

For each secure key, the accounting ledger records:

1. liquid shard-0 account balance;
2. liquid shard-1 account balance;
3. active validator self-stake or delegation;
4. pending undelegation;
5. unclaimed staking reward;
6. supported pending cross-shard receipts.

The total claim is:

```text
liquid_shard0
+ liquid_shard1
+ active_staked_or_delegated
+ pending_undelegation
+ unclaimed_staking_reward
+ pending_cross_shard
```

The destination split is:

```text
wallet_airdrop
= liquid_shard0
+ liquid_shard1
+ pending_undelegation
+ unclaimed_staking_reward
+ pending_cross_shard

staked_to_vault
= active_staked_or_delegated

total_claim
= wallet_airdrop + staked_to_vault
```

Active stake/delegation is included in the total-claim threshold but is not sent to
the account as a direct ERC-20 airdrop. It funds the corresponding validator's
ERC-4626 vault, and the delegator receives a share entitlement.

Validator lifetime `BlockReward` is not an additional claim. It is cumulative
historical metadata and would double-count rewards already represented in
delegation state.

## Primary state calculation

### Liquid balances

`cmd/account-snapshot` opens the secure state trie at the selected root and
walks every account leaf. It decodes the consensus `StateAccount` value and
exports every positive balance.

The trie key is `keccak256(address)`, not the plaintext address. When a local
preimage exists, the scanner verifies it before attaching it.

### Staking

`cmd/staking-claims` discovers validator wrappers from the selected shard-0
state root. It decodes every delegation, pending undelegation, and unclaimed
reward, then aggregates by delegator secure key.

State-root discovery is used instead of trusting the database's current
`validator-list`, because the list may describe a later head.

### Pending cross-shard receipts

Outgoing receipt groups are restricted to canonical source blocks at or before
the source cutoff. A receipt is treated as spent only when its destination
lookup points to a canonical destination block at or before the destination
cutoff.

Supported pending-receipt and audited-interval counts are withheld during
independent reproduction.

### Merge

`scripts/claims/actual-supply-ledger.py` performs a sorted merge by secure key.
It rejects negative values, inconsistent addresses, invalid component totals,
and unsorted inputs.

## Address recovery

Preimages are auxiliary database data, not consensus state. A complete balance
can therefore exist under a secure key even when a particular database cannot
reverse that key to an address.

Recovery used these sources in order:

1. `secure-key-` mappings from full/archive databases;
2. signed canonical transaction senders and recipients;
3. staking transaction addresses;
4. cross-shard receipt senders and recipients;
5. contract creation and execution traces;
6. targeted state-transition and nonce-transition searches.

No candidate is accepted merely because an indexer reports it. Every candidate
must satisfy the Keccak equality with its secure key.

The public toolkit includes the recovery commands, but a developer who already
has complete preimages can skip this phase.

## Eligibility filtering

Eligibility is applied only after all claim components have been merged.
`scripts/claims/filter-claims-by-one.py` compares the integer
`total_claim_atto` against an exact ONE-denominated threshold.

The historical `$1` filter is not part of the public method. It depended on an
external price and was not selected for migration.

The selected comparison is `total_claim_atto >= 1,000 ONE`. The
threshold set is first split by explicit policy and code presence. Code-bearing
rows are then classified to distinguish key-controlled validator-wrapper
accounts from genuine EVM contracts. The final outputs carry direct wallet
airdrop and staked-to-vault amounts separately for automatic key-controlled
claims, class-specific contract recovery, and policy-routed claims. The gross
claim remains auditable, while terminal `not_issuing` amounts are excluded from
final token and vault-share creation.

Incident evidence preserves separate roles for explicitly reported
perpetrators, transaction-linked theft recipients, and reported victims.
Victim classification alone never creates a non-issuance route.

## Difference calculation

`scripts/cutoff/migration-claims-diff.py` performs a sorted union of the
original and cutoff claim ledgers. For every changed key it records:

- old, final, and signed delta values for every component;
- old, final, and signed decimal ONE values;
- old, final, and signed USD display values;
- nonce and code-hash changes;
- created, deleted, amount-changed, or metadata-changed classification.

## Independent verification

The calculation was checked through independent paths:

- a second complete shard-0 trie traversal;
- a direct trie-difference traversal between the old and cutoff roots;
- historical RPC balance and nonce queries for every changed liquid account;
- canonical transaction and staking interval enumeration;
- execution-trace confirmation for every newly created account;
- independent CSV address, ordering, component, decimal, and threshold checks;
- aggregate reconciliation between raw exports, merged claims, and differences.

The local final reconciliation passed. Its result count is withheld during
independent reproduction.

## Explicit exclusion

Historical shard-1 receipts to retired shards 2 and 3 have destination spent
status that cannot be proven from the active shard databases. They are listed
separately and excluded from the primary total to avoid double-counting
balances potentially migrated under HIP-30. Their count and amount are withheld
during independent reproduction.

This is a policy/data qualification, not an arithmetic uncertainty in the two
active cutoff state roots.
