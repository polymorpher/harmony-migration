# BridgeClaims design

## What it has to do

The bridge allocation run produces, for each chain where bridge collateral is paid, a claim list:
one row per payee and asset (`allocations/claims/<chain>.csv`). The collateral sits in a custody
multisig on that chain (the bridge admin Safe by default; ERC-20 ONE on Ethereum is funded from
the reserve Safe). Payees must be able to claim their rows, and nothing about the list or the
payer may be changeable after deployment.

## One contract per chain, not per asset

Each chain gets one contract that pays every asset of that chain from one list. A second contract
on the same chain exists only when an asset is held by a different multisig, which the builder
does automatically for ONE when `one_claims.custody_multisig` names a separate Safe.

| | One per chain (chosen) | One per asset and chain |
|---|---|---|
| Contracts to deploy, verify, and publish | one per chain | one per pool (about ten times as many) |
| Payee with several assets | one `claimMany` transaction | one transaction per asset |
| Roots signers check | one per chain | one per asset |
| Stopping one asset | approve 0 for that token | approve 0 for that token |
| Worst case from a contract bug | bounded by each token's approval | bounded by each token's approval |
| Correcting one asset's list | new contract for that asset's rows; old approval for that token set to 0 | new contract for that asset |

The safety properties are the same either way, because exposure is set per token by the Safe's
approval, not by the contract layout: the contract can never move more of a token than the Safe
approved, and the approval for a token is exactly that token's total in the list. The per-chain
layout is cheaper to operate and to verify, and lets payees with several assets claim once.

## The contract

Constructor arguments, all `immutable`:

- `funder`: the multisig that holds the assets and approves the contract;
- `wrappedNative`: WETH or WBNB for chains with native-coin rows, otherwise zero;
- `chainId`: the chain the list belongs to (deployment anywhere else reverts);
- `root`: the Merkle root of the list;
- `claimCount`: the number of rows (indexes run from 0);
- `listHash`: SHA-256 of the published `claims.csv`, informational.

Functions:

- `claim(index, account, token, amount, proof)`: checks the proof, marks the row paid in a bitmap,
  then pays `amount` of `token` from the funder to `account`;
- `claimMany(claims)`: several rows in one transaction; rows already paid are skipped, so a batch
  is not blocked by someone claiming one of its rows first; anything else reverts the batch;
- `verify`, `leaf`, `isClaimed`, `claimedBitmap`: read-only checks;
- `available(token)`: the smaller of the funder's approval and balance, what can be paid now;
- `sweep(token)`: returns anything sent to the contract by mistake to the funder; anyone may call;
- `receive`: accepts the native coin only from the wrapped token, while unwrapping a claim.

There is no owner, admin, pause, deadline, or upgrade. The only control is the funder's approval:
lowering one token's approval stops that asset, the revoke batch stops all of them, and raising an
approval again resumes. Unclaimed assets never leave the multisig.

Anyone may submit a claim, which lets a relayer pay small rows on payees' behalf, but the tokens
always go to the row's account: a submitter can only choose when a row is paid.

## Rows, leaves, and the tree

- `leaf = keccak256(keccak256(abi.encode(uint256 index, address account, address token, uint256 amount)))`;
  native-coin rows use token address 0. The double hash keeps leaves distinct from inner nodes.
- The tree is the airdrop's batch tree: leaves in index order, each pair hashed smaller-first, an
  unpaired last node carried up unchanged; the proof is the sibling at each level that has one.
- Rows are sorted by (account, token) and numbered from 0. The index is part of the leaf and of
  the paid bitmap, so every row pays once.

OpenZeppelin's `StandardMerkleTree.verify` accepts these proofs, but `StandardMerkleTree.of()`
builds a different tree from the same leaves. Anyone recomputing the root must build it as above,
sorted pairs with the unpaired node carried up, and not with `of()`.

`tools/claim_tree.py` builds the tree, `test/lib/ClaimTree.sol` is the Solidity reference, and
`test/TreeFixture.t.sol` pays every row of a list built in Python against the compiled contract.
The builder also recomputes a sample of leaves and proofs on every run, and requires each asset's
total to equal the claimable total of its pools in the allocation run.

## Native coin

ETH and BNB rows are funded as WETH and WBNB: the approval batch wraps the native total and
approves the wrapped token. A native row pulls the wrapped amount into the contract, unwraps it,
and sends the coin with all available gas and no return data copied. If the account refuses the
coin, the contract wraps it again and sends the wrapped token instead (`PaidWrapped`). The paid bit
is set before any external call, so a re-entering account cannot be paid twice.

## Token behaviour

- Tokens that return nothing (Ethereum USDT) are accepted; `false`, a revert, or an address without
  code fail the claim and leave the row unpaid.
- Some tokens refuse to change one nonzero approval into another (on Ethereum USDT, CRV, and AAG).
  The approval batch approves 0 first for every token whose live allowance is nonzero, and every
  listed token accepts that; `chains.json` lists the known ones for batches built without chain reads.
- A fee-on-transfer token delivers the row less the token's fee. PAXG has charged one, but at the
  time of writing it charges none: on an Ethereum fork in October 2026, a transfer from the custody
  Safe arrived in full. A fee introduced later would make its rows deliver less.
- Tokens with transfer hooks (FXS vote checkpoints) work with real balances; the fork test moves
  the real escrow balances to the Safe for that reason.
- A blacklisted or paused token makes its claims revert until the issuer allows transfers again;
  the row stays unpaid.

## Addresses and compilation

The contract is deployed through the standard CREATE2 deployer
(`0x4e59b44847b379578588920ca78fbf26c0b4956c`, present on all five chains) with the salt in
`chains.json`. The address commits to the bytecode and every constructor argument, so it is known
before deployment, the funder can approve it first, anyone can deploy it, and anyone can recompute
it from the published manifest. The compiler is pinned (solc 0.8.36, optimizer, no metadata hash)
and targets Cancun, which Ethereum, BNB Chain, Arbitrum, Base, and Linea all execute (checked with
`eth_call` probes of PUSH0, MCOPY, and TSTORE; Arbitrum lacks only BLOBBASEFEE, which the contract
does not use).

## Corrections

A list cannot change. To correct rows of one asset: set that token's approval to 0 on the old
contract, build a new list for that asset that leaves out or adjusts the rows already paid
(`claimedBitmap` gives them), deploy it, and approve it. Other assets keep paying from the old
contract. Two details matter:

- Read `claimedBitmap` only once the revocation is mined; before that, rows can still be paid.
- Leave out paid rows by account and token, not by index: the new list numbers its rows afresh.

## Verification

- Unit and fuzz tests (`test/BridgeClaims.t.sol`): one payment per row, altered rows rejected,
  permissionless claims paying the account, no-return, false-returning, fee, and code-less tokens,
  native payout, fallback to the wrapped coin, return-data bombs, reentry, revoked approvals,
  batches, sweep, and a random list paid in random order exhausting the approval exactly.
- Python and Solidity agree (`test/TreeFixture.t.sol`, `tools/test_tools.py`).
- Fork test (`test/Fork.t.sol`): on a fork of the real chain, the run's contract is deployed at its
  manifest address, the real escrow balances (and the reserve's ONE) are moved to the Safe, the
  Safe's approvals are made, and the first row of every asset is claimed with the real token.

A single-row claim costs roughly 76,000 to 114,000 gas of execution, depending on the token and the
proof's depth, on top of the base transaction cost and the proof's calldata. Deployment uses about
1.2 million gas; Forge's dry run reports about 1.6 million because it adds a margin.

A native row to an account whose `receive()` burns all the gas it is given and then fails needs
about 3.6 million call gas to succeed. The coin send gets all but 1/64 of the gas, and the
wrapped-token fallback must run on what is left. With less gas the whole transaction reverts and the
row stays unpaid, so the cost falls on whoever submits it. A relayer paying other people's rows
should estimate gas per row, cap it, and skip rows whose recipient reverts.
