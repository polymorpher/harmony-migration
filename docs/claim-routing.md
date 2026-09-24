# Claim destination mapping

The cutoff ledger records what Harmony state recognizes at the selected roots.
Destination mapping is a later policy step with two outputs:

- direct ERC-20 ONE sent to a wallet or recovery destination; and
- ERC-4626 shares backed by active stake deposited into the
  corresponding validator vault.

Active stake/delegation counts toward the total-claim threshold but
is never added to its direct wallet airdrop. Destination policy must preserve:

`total_claim = wallet_airdrop + staked_to_vault`.

Validator-vault deposits cover the complete active principal. The priority
threshold controls when a delegator receives or can claim shares; it does not
remove deferred delegators' backing from the vault.

## Routing precedence

Apply routing decisions in this order:

1. determine inclusive `>= 1,000 ONE` snapshot membership before deductions,
   classify account identity, and assign migration stage separately;
2. apply explicitly approved non-issuance, treasury, and incident-recovery
   rules;
3. exclude every qualifying exchange wallet from the implicit automatic path
   and apply exchange manual-delivery routes to every positive native, WONE,
   and delegated claim in a confirmed exchange inventory regardless of the
   ordinary threshold; the whole exchange entitlement is delivered manually
   from the 2050 supply reserve and never enters the airdrop;
4. identify Harmony validator-wrapper accounts and treat them as
   key-controlled accounts;
5. apply the six-month activity rule only to eligible wallets for the initial
   stage;
6. apply an implicit same-address destination only to ordinary code-less EOAs,
   without treating destination readiness as stage authorization;
7. apply the reviewed next-stage or non-issuance policy to genuine contracts
   and assign each remaining `staked_to_vault` entry through its validator
   vault.

The higher-priority rule wins. For example, detecting a validator wrapper does
not override an explicit incident-recovery destination.

## Validator-wrapper accounts

Harmony stores an RLP-encoded `ValidatorWrapper` in the account code field.
Consequently, a non-empty `code_hash` does not by itself prove that the account
is an EVM contract.

A code-bearing account is treated as a validator-controlled EOA only when both
checks pass:

1. `hmyv2_getValidatorInformation(address)` returns a validator wrapper; and
2. the code decodes as a validator-wrapper RLP list whose embedded address
   equals the account address.

The wrapper's `creation-height` is independently checked against the
`CreateValidator` staking transaction in that canonical block. Verified
validator accounts are eligible for same-address delivery, but remain explicit
routing exceptions because their account state is code-bearing. Their direct
wallet and vault-share exception rows cite the validator classification
evidence. Self-stake and other active delegations remain validator-vault
principal. The validator controls the corresponding vault as governor under
the migration design.

## Genuine contracts

Do not automatically send ERC-20 ONE or vault shares to the same hexadecimal
address as a Harmony contract. Contract creation addresses and state do not
generally carry over to Ethereum.

The contract review records a classification and recovery evidence:

- Safe multisig: owners, threshold, singleton version, creation, and funding;
- 1wallet: version, user-set recovery address, forward address, and expiry;
- SmartVault: owner, guardians, implementation, creation, and funding;
- fungible-token contract: token identity, proxy implementation, and
  application relationship;
- NFT contract: standard, application relationship, and recoverable ownership
  evidence where enumerable;
- known application: verified registry or on-chain parent relationship;
- type-only identification: bytecode/function evidence without an attributed
  operator;
- unidentified: insufficient evidence for an automatic destination.

All ownership, Safe-threshold, recovery, proxy, guardian, and holder facts used
for classification or destination evidence are read at the shard-0 claim
cutoff block. Latest balances, delegations, and activity may be retained as
investigative context but cannot select a recovery destination.

### Recovery principles

- A Safe claim requires an Ethereum Safe or claim process preserving the
  verified owner set and threshold; do not route it to the old contract
  address. Initial manual outreach prioritizes Safes with at least
  `1,000,000 ONE` of total claim; smaller claims remain eligible for
  the later recovery portal.
- A 1wallet treasury-default address is not a user-set recovery address.
  Prefer a verified forward or user recovery address; otherwise require manual
  proof.
- SmartVault allocations are not issued and remain in the 2050 premint
  reserve. Owner or guardian evidence remains factual identity evidence but no
  longer selects a migration destination.
- Token, bridge, pool, and application contracts require liability-aware
  treatment. Sending their ONE balance to a deployer can double-pay or
  confiscate user-backed assets.
- NFT holders do not automatically own the ONE balance of the NFT contract.
- Operator-unattributed or unidentified contracts remain on hold.

## Incident non-issuance overlay

For a direct extra-mint recipient, the current policy ceiling is:

`min(total claim, max(exact extra mint - verified incident-linked burn, 0))`.

The exact amount previously assigned to treasury is now marked
`not-issuing`. It receives no ERC-20 tokens. If the route reaches a staked
component, the corresponding vault deposit assets and shares are also omitted.
Any existing remainder stays at the ordinary destination. Burn/inaccessible and
report-identified perpetrator rows retain their previously audited exact
amounts. Separately reviewed direct theft recipients may be added with their
own evidence category. Those amounts are also not issued. Reported victim
wallets are not included in this outcome.

The gross cutoff claim ledger remains unchanged for audit. The migration
allocation is `gross claim - not-issued amount`; the omitted amount stays
unallocated in the fixed 2050 premint reserve. This is not a reduction of fixed
ERC-20 total supply, an unresolved destination, or a transfer to treasury.

That equation describes the native incident overlay. The expanded WONE routing
input additionally subtracts a `redistributed` source offset paired with WONE
already added to holder rows.

## Explicit routing files

Real routes live under the ignored `routing/local/` directory. Separate CSVs
may be maintained for non-issuance, treasury, bridge reserves, multisigs, lost
wallets, frozen wallets, exchanges, and other manual decisions. Exchange routes
and destinations are generated separately as `exchanges.csv` and
`exchange-destinations.csv`; `apply-routes.py` accepts multiple destination
files. See `routing/README.md` for the schema and precedence.

Routes are applied to direct wallet tokens first and then proportionally across
the source's validator-vault positions when necessary. A missing destination
becomes a hold; it never falls back to the original address.

The exchange route builder uses the cutoff ledger for payout amounts and the
exchange submission only for membership, destination authorization, and
reconciliation. Every positive native, WONE, and delegated claim in a confirmed
exchange inventory receives a manual route without an ordinary threshold test.
Those routes compile into the `exchange_manual` stage with
`issuance_treatment = manual_from_reserve` and
`destination_status = exchange_manual`, regardless of individual source
activity or ordinary stage. The destination is the exchange's confirmed
aggregate address, a wallet/staking destination pair (one route per component
group), or the source wallet itself for same-address exchanges. Gate is tiered:
a source wallet whose ordinary stage is `initial` is delivered at its own
address, and every other Gate wallet is aggregated to Gate's confirmed
destination. Delegated principal owned by an exchange wallet is released from
the validator vault (`exchange_manual_assets_atto` in the vault stage ledger)
instead of being issued as vault shares.

Generated routing is sparse:

- `routing-exceptions.csv` records explicit wallet/vault routes, all verified
  validator-wrapper same-address exceptions, and contract/policy holds;
- `validator-governor-exceptions.csv` separately records governor overrides and
  holds;
- `unresolved-routing.csv` is the derived, never-edited work queue.

Ordinary code-less EOA same-address delivery is implicit and absent from these
files. A complete deployment allocation must be built later from the base
entitlements and approved exceptions; the sparse routing outputs are not
themselves a wallet distribution or Merkle input.

### Reviewed contract policy

The former blanket `contract-recovery-custody` recommendation is superseded.
`build-contract-policy-routes.py` applies the confirmed address-based
partition from the cutoff contract review:

- reviewed multisigs are next-stage allocations and remain held until a
  replacement Ethereum Safe or other approved destination preserves the
  verified owner set and threshold; generated holds have no shared fillable
  destination, and each approved manual route must precede priority `500`;
- the two reviewed LayerZero collateral contracts are next-stage allocations
  regardless of activity, but their destinations remain held pending
  reconciliation;
- reviewed 1wallet allocations are next-stage recovery-multisig allocations;
  no unverified destination address is embedded;
- SmartVault and every other reviewed genuine contract are terminal
  `not_issuing`, including both wallet-token and staked-vault components.

Activity does not move any genuine contract into the initial wallet stage.
The public “Abandoned contracts” label is an aggregate reporting label, not an
inactivity classifier or a finding that every underlying owner abandoned the
assets. The route data preserves the actual identity and policy reason.

## Additional policy decisions

- Rollback-exploit proceeds are not issued (decision
  `rollback-exploit-proceeds`, resolved 2026-09-23). Two exact inventories
  implement it: the retained balances at the wallets the May 2025 and April
  2026 incident contracts funded, and, for every wallet credited by a proven
  rollback-leak receipt, the credited amount capped at what the wallet still
  holds after earlier deductions. A wallet that holds only exploit credit loses
  its whole claim; a wallet that also held legitimate funds keeps the
  difference. Wallets that merely passed exploit funds on are not deducted.
- If a validator address is explicitly frozen, non-issued, or
  treasury-routed, its vault governor defaults to hold until
  `validator-governors.csv` names an approved Ethereum governor.

These are release gates in `routing/local/policy-decisions.csv`. The routing
summary records both a conservative global status and stage-scoped readiness.
The LayerZero gate applies to the next stage; a policy decision that can affect
ordinary wallet claims remains an initial-stage gate. Exchange manual delivery
uses its own scoped readiness and is not blocked by ordinary threshold,
activity, or initial-stage policy gates; it is executed by hand from the 2050
supply reserve using the private manual delivery worksheet.

### WONE holder redistribution

The native ONE in the WONE contract is reserve backing for WONE. It is not the
treasury's own spendable money and it is not a beneficial claim of the retiring
contract.

Cutoff WONE is included directly in the current qualification total and wallet
airdrop. The source reserve is then split exactly:

```text
WONE shard-0 native reserve
= ordinary-threshold and exchange manual-delivery redistribution
+ retained not-issued remainder
```

The first route has terminal status `redistributed`. It has no destination
address because the corresponding ONE is already present in
ordinary-threshold or exchange manual-delivery wallet rows. The second route is
terminal `not_issuing`; no replacement token is created for the remainder,
which remains in the 2050 premint reserve.

`build-wone-routes.py` derives both exact amounts from the verified holder
overlay. The WONE contract's self-held WONE is excluded as a circular system
balance. Same-address value on shard 1 is not WONE backing and falls through
to reviewed-contract non-issuance. It is not subtracted as backing a second
time.

### LayerZero NativeOFT reconciliation

The LayerZero NativeOFT contracts do not use the WONE reserve; they directly
hold native ONE backing their cross-chain token system. Their reserves must be
routed separately in the next stage. They must not be treated as the
treasury's own spendable money.

Next-stage eligibility is resolved. Before approving their destination,
reconcile each Harmony reserve
against the corresponding remote-chain supply and messages in flight at pinned
blocks. The `layerzero-nativeoft-reconciliation` release gate remains pending
only for destination readiness and settlement evidence; it does not return the
two contracts to the initial stage or make them ineligible.

## Reproduction and evidence

The public classifier and evidence schema are documented in:

- `docs/contract-account-review.md`;
- `toolkit/scripts/contract-review/README.md`;
- `toolkit/scripts/claims/apply-eligibility-policy.py`;
- `toolkit/scripts/claims/verify-eligibility-policy.py`;
- `toolkit/scripts/claims/build-migration-stage-policy.py`;
- `toolkit/scripts/claims/verify-migration-stage-policy.py`;
- `toolkit/scripts/claims/vault-share-ledger-rpc.py`;
- `toolkit/scripts/claims/verify-vault-delegations.py`;
- `toolkit/scripts/claims/build-vault-share-allocation.py`;
- `toolkit/scripts/routing/build-non-issuance-routes.py`;
- `toolkit/scripts/routing/build-wone-routes.py`;
- `toolkit/scripts/routing/build-contract-policy-routes.py`;
- `toolkit/scripts/routing/materialize-initial-stage.py`;
- `toolkit/scripts/exchanges/normalize-exchange-wallets.py`;
- `toolkit/scripts/exchanges/build-exchange-accounting.py`;
- `toolkit/scripts/exchanges/verify-exchange-routing.py`;
- `toolkit/scripts/routing/apply-routes.py`.

Exact address mappings, category counts, balances, and destination candidates
remain under the numerical embargo in `docs/findings/`,
`results/2026-09-11/`, and `artifacts/`.
