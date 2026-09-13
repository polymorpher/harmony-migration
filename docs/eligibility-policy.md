# Eligibility policy

The accounting toolkit produces a total claim with separate direct-wallet and
staked-to-vault components. Eligibility policy is a separate step and must not
change the underlying state calculation.

## Selected denomination

The public threshold is denominated in ONE, not USD:

```text
1,000 ONE = 1,000,000,000,000,000,000,000 atto-ONE
```

It applies to `total_claim_atto`, after liquid, active
stake/delegation, pending undelegation, unclaimed reward, and supported
cross-shard components are combined by secure key.

The direct `wallet_airdrop_atto` excludes active stake/delegation. That active
amount is `staked_to_vault_atto` and is represented by shares in the
corresponding validator vault:

```text
total_claim_atto
= wallet_airdrop_atto
+ staked_to_vault_atto
```

The threshold controls prioritization. Validator vaults retain backing for
active principal belonging to below-threshold accounts so those shares can be
claimed in a later batch or portal.

The historical `$1` experiment depended on a time-specific market price and was
not selected for migration. It is retained only in the as-run audit history.

## Equality policy

The selected policy is inclusive: accounts whose total claim is exactly
`1,000 ONE` are included. A strict `> 1,000 ONE` result is retained locally as
a comparison artifact.

The observed inclusive, strict, and exact-threshold population counts are
withheld during independent reproduction.

Commands:

```sh
# Inclusive
python3 toolkit/scripts/claims/filter-claims-by-one.py \
  --input all-address-migration-claims-cutoff.csv \
  --output migration-claims-at-least-1000-one.csv \
  --summary migration-claims-at-least-1000-one.json \
  --minimum-one 1000 \
  --comparison ge

# Strict
python3 toolkit/scripts/claims/filter-claims-by-one.py \
  --input all-address-migration-claims-cutoff.csv \
  --output migration-claims-over-1000-one.csv \
  --summary migration-claims-over-1000-one.json \
  --minimum-one 1000 \
  --comparison gt
```

## Code-bearing and validator accounts

The gross threshold results include accounts with non-empty code. Their
population counts and monetary totals are withheld during independent
reproduction.

Code-bearing is a preliminary review signal, not the final destination class.
Harmony stores an RLP-encoded `ValidatorWrapper` in the code field of validator
accounts. Those accounts remain ECDSA key-controlled and eligible for
same-address delivery when both validator checks in `docs/claim-routing.md`
pass. Routing nevertheless records them as explicit code-bearing exceptions;
only ordinary code-less EOAs use the implicit default.

The policy therefore uses two stages:

1. resolve missing cutoff code metadata for every staking-only or receipt-only
   claim account, then produce the prioritized code-bearing review set;
2. classify verified validator-wrapper accounts into the eligible
   key-controlled category while retaining explicit validator routing evidence;
   genuine contracts remain in class-specific manual recovery.

An EVM contract does not normally control the same address on Ethereum with a
private key. Do not simply send ERC-20 tokens to a genuine contract address
without a recovery design.

## Treasury-routed addresses

The treasury-routing review covers selected burn/inaccessible addresses,
blacklisted direct extra-mint recipients, and positive perpetrator addresses
explicitly named by the wallet-theft reports. Address counts and category
amounts are withheld during independent reproduction.

For an extra-mint recipient:

`treasury reclaim = min(total claim, max(exact extra mint - verified burn, 0))`.

This prevents a full burn or legitimate balance above the unreturned extra
mint from being seized. The total treasury-routing amount is withheld during
independent reproduction.

The original whole-row category splitter cannot represent partially
reclaimable addresses. Final allocation generation must split those rows
between treasury and the original address.

The explicit route engine performs that split across direct wallet tokens and
validator-vault shares. The base category files are not distribution outputs.
See `routing/README.md`.

Treasury routing changes the destination of direct tokens and, where
applicable, vault shares. It does not subtract anything from the total claim or
any supply total.

## Retired-shard receipts

Historical receipts to retired shards 2 and 3 remain outside the primary claim
total because their spent status cannot be proven from active-shard state.
Their count and amount are withheld during independent reproduction. Including
them without retired-shard proof could double-count balances migrated under
HIP-30.

## Required publication fields

The final release metadata must state:

- cutoff blocks, hashes, and roots;
- threshold denomination and exact atto value;
- inclusive `>=`;
- contract-account treatment;
- treasury-routing address inventory and policy;
- retired-shard receipt treatment;
- output row count, total amount, and SHA-256.
