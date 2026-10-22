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
- Tokens that refuse to change a nonzero approval (Ethereum USDT) get an approval of 0 first in the
  batch; `chains.json` lists them.
- Fee-on-transfer tokens (PAXG) deliver the row less the token's fee; the Safe's own transfer from
  the escrow also pays a fee, which the pool's retained collateral covers.
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
contract.

## Verification

- Unit and fuzz tests (`test/BridgeClaims.t.sol`): one payment per row, altered rows rejected,
  permissionless claims paying the account, no-return, false-returning, fee, and code-less tokens,
  native payout, fallback to the wrapped coin, return-data bombs, reentry, revoked approvals,
  batches, sweep, and a random list paid in random order exhausting the approval exactly.
- Python and Solidity agree (`test/TreeFixture.t.sol`, `tools/test_tools.py`).
- Fork test (`test/Fork.t.sol`): on a fork of the real chain, the run's contract is deployed at its
  manifest address, the real escrow balances (and the reserve's ONE) are moved to the Safe, the
  Safe's approvals are made, and the first row of every asset is claimed with the real token.

A single-row claim costs about 50,000 gas of execution on top of the base transaction cost and its
proof's calldata; deployment costs about 1.6 million gas.
