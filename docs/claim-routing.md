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

1. enforce the inclusive `>= 1,000 ONE` eligibility threshold;
2. apply explicitly approved treasury, burn, inaccessible-address, and
   incident-recovery rules;
3. identify Harmony validator-wrapper accounts and treat them as
   key-controlled accounts;
4. apply an implicit same-address rule only to ordinary code-less EOAs and
   assign each `staked_to_vault` entry through its validator vault;
5. classify genuine contract accounts and route them through a class-specific
   recovery process.

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
- A SmartVault owner or guardian set is evidence for manual recovery, not an
  automatic ownership transfer.
- Token, bridge, pool, and application contracts require liability-aware
  treatment. Sending their ONE balance to a deployer can double-pay or
  confiscate user-backed assets.
- NFT holders do not automatically own the ONE balance of the NFT contract.
- Operator-unattributed or unidentified contracts remain on hold.

## Incident and treasury overlays

For a direct extra-mint recipient, the current policy ceiling is:

`min(total claim, max(exact extra mint - verified incident-linked burn, 0))`.

This may split both direct-wallet and vault-share delivery between treasury
and the ordinary destination. Burn/inaccessible and report-identified
perpetrator policies are separate. No routing overlay may reduce or inflate the
total claim.

## Explicit routing files

Real routes live under the ignored `routing/local/` directory. Separate CSVs
may be maintained for treasury, bridge reserves, multisigs, lost wallets,
frozen wallets, and other manual decisions. See `routing/README.md` for the
schema and precedence.

Routes are applied to direct wallet tokens first and then proportionally across
the source's validator-vault positions when necessary. A missing destination
becomes a hold; it never falls back to the original address.

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

The current policy sends reviewed non-multisig contracts to
`contract-recovery-custody` unless a higher-priority incident or reserve route
applies. That name is only a label for one Ethereum Safe or multisig holding
address. The ONE there waits until a verified claimant is paid. Treasury
operators may sign for the holding address, but they may not spend that ONE as
ordinary treasury money. Later claimant payments come from the amount already
in that address; no extra ONE is created.

`build-contract-treasury-routes.py` always writes those rows to
`contract-recovery-custody`. It has no option that sends them to `treasury`.
If a generated row is later edited to use `treasury`, `apply-routes.py`
rejects the file. Send a contract to the general treasury only with a
separate, higher-priority manual route and recorded evidence.

Multisig rows stay on hold until a replacement Ethereum Safe with the verified
owner set and threshold is supplied.

## Additional policy decisions

- Historical rollback-exploit proceeds remain part of state-derived total
  claims unless an explicit route file redirects identified addresses. The
  treasury inventory does not implicitly cover that incident.
- If a validator address is explicitly frozen or treasury-routed, its vault
  governor defaults to hold until `validator-governors.csv` names an approved
  Ethereum governor.

These are release gates in `routing/local/policy-decisions.csv`; a pending
decision keeps the routing summary on hold even if every address is populated.

### WONE reserve custody

The native ONE in the WONE contract is reserve backing for WONE. It is not
the treasury's own spendable money. It is migrated once to a dedicated reserve
multisig, separate from the general treasury. A later claim portal pays eligible
WONE and bridged-WONE claimants by transferring ONE from that finite reserve.
It does not mint or allocate additional ONE for those claims.

The WONE contract therefore receives a higher-priority
`wone-reserve-custody` route instead of generic contract-recovery custody.
That route consumes only `SHARD0_LIQUID`, the component held by the actual WONE
contract. Same-address value on another shard is not WONE backing and falls
through to generic contract-recovery custody. The destination remains held
until the approved multisig address is supplied. Aggregate portal payments may
not exceed the amount transferred to that reserve, and each entitlement must
be claimable only once.

### LayerZero NativeOFT reconciliation

The LayerZero NativeOFT contracts do not use the WONE reserve; they directly
hold native ONE backing their cross-chain token system. Their reserves must be
routed separately. They must not be treated as the treasury's own spendable
money.

Before approving their custody destination, reconcile each Harmony reserve
against the corresponding remote-chain supply and messages in flight at pinned
blocks. The `layerzero-nativeoft-reconciliation` decision remains pending until
that evidence and settlement method are recorded.

## Reproduction and evidence

The public classifier and evidence schema are documented in:

- `docs/contract-account-review.md`;
- `toolkit/scripts/contract-review/README.md`;
- `toolkit/scripts/claims/apply-eligibility-policy.py`;
- `toolkit/scripts/claims/verify-eligibility-policy.py`;
- `toolkit/scripts/claims/vault-share-ledger-rpc.py`;
- `toolkit/scripts/claims/verify-vault-delegations.py`;
- `toolkit/scripts/claims/build-vault-share-allocation.py`;
- `toolkit/scripts/routing/build-treasury-routes.py`;
- `toolkit/scripts/routing/build-contract-treasury-routes.py`;
- `toolkit/scripts/routing/apply-routes.py`.

Exact address mappings, category counts, balances, and destination candidates
remain under the numerical embargo in `docs/findings/`,
`results/2026-09-11/`, and `artifacts/`.
