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
4. send an ordinary EOA's `wallet_airdrop` to the same hexadecimal address and
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
validator accounts are eligible for same-address routing of their direct wallet
amount. Their self-stake and other active delegations remain validator-vault
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

## Reproduction and evidence

The public classifier and evidence schema are documented in:

- `docs/contract-account-review.md`;
- `toolkit/scripts/contract-review/README.md`;
- `toolkit/scripts/claims/apply-eligibility-policy.py`;
- `toolkit/scripts/claims/verify-eligibility-policy.py`;
- `toolkit/scripts/claims/vault-share-ledger-rpc.py`;
- `toolkit/scripts/claims/verify-vault-delegations.py`;
- `toolkit/scripts/claims/build-vault-share-allocation.py`.

Exact address mappings, category counts, balances, and destination candidates
remain under the numerical embargo in `docs/findings/`,
`results/2026-09-11/`, and `artifacts/`.
