# Eligibility policy

The accounting toolkit preserves the native claim and then applies a separate
WONE qualification overlay with direct-wallet and staked-to-vault components.
The overlay does not change the underlying native state calculation.

## Selected denomination

The public threshold is denominated in ONE, not USD:

```text
1,000 ONE = 1,000,000,000,000,000,000,000 atto-ONE
```

It applies to `qualification_total_atto`:

```text
qualification_total_atto
= native_total_claim_atto + wone_balance_atto
```

The native total combines liquid, active stake/delegation, pending
undelegation, unclaimed reward, and supported cross-shard components by secure
key. WONE comes from the separately verified cutoff holder ledger.

The direct `wallet_airdrop_atto` includes the complete WONE balance for an
ordinary qualifying row or a non-Gate aggregate-exchange row and excludes
active stake/delegation. That active amount is `staked_to_vault_atto` and is
represented by shares in the corresponding validator vault:

```text
total_claim_atto
= wallet_airdrop_atto
+ staked_to_vault_atto
```

The WONE contract's matching shard-0 native reserve is not issued to the
contract. The portion paired with ordinary-threshold and aggregate-exchange
delivery is classified as `redistributed`; the remaining backing is
`not_issuing` and retained in the 2050 premint reserve.

Ordinary threshold membership is determined from the snapshot qualification
total before non-issuance deductions or routing adjustments. It controls the
ordinary wallet path and staging and is not retested against a smaller net
allocation. Non-Gate exchange aggregation is an explicit delivery exception:
the threshold is used only to remove overlap from automatic same-address
delivery, not to limit the exchange's aggregate entitlement. Validator vaults
retain backing for active principal belonging to deferred accounts until their
stage is authorized.

The historical `$1` experiment depended on a time-specific market price and was
not selected for migration. It is retained only in the as-run audit history.

## Equality policy

The selected policy is inclusive: accounts whose qualification total is exactly
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
  --aggregate-delivery-summary exchanges/wallets-standardized/summary.json \
  --minimum-one 1000 \
  --comparison ge

# Strict
python3 toolkit/scripts/claims/filter-claims-by-one.py \
  --input all-address-migration-claims-cutoff.csv \
  --output migration-claims-over-1000-one.csv \
  --summary migration-claims-over-1000-one.json \
  --aggregate-delivery-summary exchanges/wallets-standardized/summary.json \
  --minimum-one 1000 \
  --comparison gt
```

## Exchange delivery overlay

Exchange ownership and destination requests change delivery, not the cutoff
claim calculation. Gate requested no aggregate reroute, so every Gate source
wallet remains under the ordinary inclusive threshold and account
classification. Only a qualifying Gate wallet in the automatic category uses
same-address delivery.

Other exchange-provided source wallets do not use the implicit automatic
same-address path. `build-exchange-accounting.py` produces the complete
qualifying non-Gate exclusion set, and `apply-eligibility-policy.py` consumes it
through `--exclude-addresses-file`. This moves those qualifying rows into the
policy-routed category without changing their balances.

The `excluded_address` routing category is not a non-issuance decision.
For non-Gate exchanges it is only bookkeeping that suppresses implicit
same-address delivery. Confirmed exchange rows compile into
`exchange_aggregate` instead of the ordinary initial/deferred cohorts.

The generated manual exchange routes name every positive native or WONE claim,
including rows below the automatic threshold. Those rows receive full
aggregate entitlement in the separately release-authorized
`exchange_aggregate` stage; they never enter the ordinary initial stage.
Missing inventories or destinations remain holds.

Exchange-submitted balances establish ownership and provide a reconciliation
check. They never replace the cutoff-pinned claim ledger. Raw inventories,
normalized address files, memos, destinations, and address-level audits remain
private under the numerical embargo. The operational configuration under
`exchanges/` is private as well; this section records the public policy
semantics without exposing exchange files.

## WONE holder treatment

Every positive WONE balance is enumerated from cutoff-pinned archival-node
events and reconciled to both WONE `totalSupply()` and the WONE contract's
native reserve. LayerZero's Harmony NativeOFT contracts hold native ONE
directly and have no WONE balance in this ledger.

`apply-wone-qualification.py` verifies the full holder file and adds
`wone_airdrop_atto` for the current inclusive threshold set plus normalized
non-Gate exchange wallets selected for aggregate delivery. It also excludes
the WONE contract's own self-held WONE from recipient delivery.

The source reserve closes as:

```text
WONE reserve
= redistributed ordinary-threshold and aggregate-exchange WONE
+ retained not-issued remainder
```

`redistributed` is not an address or a second issuance. It is the terminal
source offset paired with WONE amounts already added to ordinary-threshold or
aggregate-exchange wallet rows.

## Initial and later stages

Migration stage is recorded separately from account classification,
issuance treatment, destination, and destination readiness. `migration_stage`
is `initial`, `exchange_aggregate`, `next_stage`, or `deferred` only when an
allocation remains; `issuance_treatment` records `issue` or `not_issued`:

1. `exchange_aggregate` contains every positive native ONE, WONE, and
   vault-share entitlement in a confirmed non-Gate inventory. It is
   release-authorized without individual threshold or activity gating.
2. The initial stage contains positive eligible **wallet** allocations with
   indexed activity in the six calendar months before the cutoff, inclusive of
   `2026-03-10T14:00:00Z`.
3. Reviewed multisigs, the two reviewed LayerZero bridge-collateral contracts,
   and reviewed 1wallet allocations are eligible but reserved for the next
   stage regardless of activity.
4. SmartVault and all other reviewed genuine-contract allocations are not
   issued and remain in the 2050 premint reserve.
5. Wallets outside the six-month window remain deferred; absence of indexed
   activity is not proof of abandonment.

The 1wallet category uses next-stage recovery-multisig handling, but no
destination is considered ready without cutoff ownership/recovery evidence.
SmartVault is a separate wallet family and does not share that treatment.
The initial reporting cohort excludes the separately authorized non-Gate
exchange aggregate stage and is not an unconditional same-address deployment
manifest.

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
   genuine contracts then follow the reviewed stage and non-issuance policy
   above.

An EVM contract does not normally control the same address on Ethereum with a
private key. Do not simply send ERC-20 tokens to a genuine contract address
without a recovery design.

## Not-issued amounts

The non-issuance review covers selected burn/inaccessible addresses,
blacklisted direct extra-mint recipients, and positive perpetrator addresses
explicitly named by the wallet-theft reports. It also records separately
reviewed first recipients linked by successful theft transactions. Reported
victim wallets remain a separate recovery population and are not automatically
excluded from issuance. Address counts and category amounts are withheld during
independent reproduction.

For an extra-mint recipient:

`not-issued amount = min(total claim, max(exact extra mint - verified burn, 0))`.

This prevents a full burn or legitimate balance above the unreturned extra
mint from being removed. The total non-issuance amount is withheld during
independent reproduction.

The original whole-row category splitter cannot represent partially
reclaimable addresses. Final allocation generation must split those rows
between `not-issuing` and the original address.

The explicit route engine performs that split across direct wallet tokens and
validator-vault shares. The base category files are not distribution outputs.
See `routing/README.md`.

`not-issuing` is not an address and receives nothing. The gross claim remains
in the audit ledger, while the migration allocation excludes the exact
not-issued wallet and vault amounts.
The amount remains unallocated within the fixed 26.271 billion ONE premint
(`12.6 billion + 441 million × 31 years`); this policy neither changes ERC-20
total supply nor creates a burn or treasury transfer.

The retained WONE reserve remainder follows this definition. The WONE amount
paired with current holder airdrops does not: it is `redistributed`, because a
replacement asset is created for those holders.

Reviewed SmartVault and other non-approved genuine-contract allocations also
follow this definition. An `ALL` contract route consumes both direct-wallet and
staked-to-vault components after any higher-priority exact deduction, so no
replacement vault asset or share survives the exclusion.

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
- six-month initial wallet stage and later-stage treatment;
- separate `migration_stage`, `issuance_treatment`, destination, and readiness;
- contract-account treatment;
- exchange automatic-exclusion, manual-routing, and Gate-exception treatment;
- non-issuance address inventory and policy;
- retired-shard receipt treatment;
- output row count, total amount, and SHA-256.
