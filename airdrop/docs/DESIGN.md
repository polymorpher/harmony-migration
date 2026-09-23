# Design notes

## Why "committed batches"

Three familiar ways to push an ERC-20 airdrop were considered.

1. **Hardcode the list in contract bytecode.** Safe, but Ethereum limits a contract's code to
   24,576 bytes, so a large list needs many contracts, each needing its own approval from the
   multisig, and storing data in code costs roughly twelve times more per byte than passing it in a
   transaction.
2. **A flexible contract where an operator wallet sends arbitrary addresses and amounts.**
   Cheapest and simplest, but a mistake in the operator's input file, a bug in the sending script,
   or a stolen operator key can send the whole approved amount anywhere. Irreversible.
3. **A Merkle tree with one leaf per recipient, one proof per recipient.** Safe, but for a list of
   tens of thousands each recipient needs a proof of sixteen or seventeen hashes, which adds about
   half again to the gas of every transfer, and a per-recipient "already paid" record.

The design used here keeps the safety of (3) at nearly the cost of (2): the tree has one leaf per
**batch** instead of per recipient. A batch's fingerprint is the hash of everything in it
(recipients and amounts, in order, plus its index), which the contract recomputes from the
transaction's own input, so the per-recipient cost of the check is a few gas for hashing. The
proof is one short path per batch rather than one per recipient, and "already paid" is one bit
per batch. In measurements against the production token's bytecode, this costs about one percent
more gas than design (2) while giving the same guarantee as design (3): the operator can only
replay the published list.

The root is immutable rather than updatable. An updatable root would let the multisig re-point an
already-granted allowance at a different list; with an immutable root the statement "this
allowance can only ever flow to this list" is checkable once, from the constructor arguments, and
never needs to be re-checked. Changing the list means a new contract, one more deployment and one
Safe transaction that revokes the old allowance and approves the new one, which is the right
amount of friction for changing who gets paid.

## Exact hashing rules

Given the canonical list (`distribution.csv`, rows in order) and a batch size `K`:

1. Batch `i` is rows `i*K` to `i*K + K - 1` (the last batch may be shorter).
2. Its **leaf** is

   ```text
   leaf_i = keccak256( keccak256( abi.encode(uint256 i, address[] recipients_i, uint256[] amounts_i) ) )
   ```

   where `abi.encode` is standard Solidity ABI encoding: three head words (the index, the offset
   `0x60` of the first array, the offset of the second array), then each array as its length
   followed by its 32-byte-padded elements. The outer hash keeps leaves distinct from internal
   nodes (the OpenZeppelin convention).
3. The **tree**: level 0 is the leaves in index order. Each higher level takes neighbours
   `(0,1), (2,3), ...` and replaces each pair with `keccak256(min(a,b) || max(a,b))`. An unpaired
   last node is carried up unchanged. The **root** is the single node at the top.
4. The **proof** for leaf `i` is its sibling at every level where it has one, from the bottom up.
   Verification folds the leaf with each proof element using the same `min || max` rule and
   compares the result with the root.

`tools/common.py` (Python) and `script/lib/BatchTree.sol` (Solidity) both implement these rules;
`src/CommittedBatchAirdrop.sol` implements only step 2 and the verification of step 4.
`test/CommittedBatchAirdrop.t.sol::test_MatchesPythonBuilderVector` pins a vector produced by the
Python tool so the implementations cannot silently drift apart.

## Gas and batch size

One recipient costs about 27,700 gas, of which about 22,100 is the unavoidable cost of writing a
fresh token balance. Ethereum rejects transactions above 16,777,216 gas (EIP-7825), so the
builder caps batches at 500 recipients and defaults to 400 (about 11 million gas per
transaction). The executor script asks the node for a gas estimate and adds ten percent margin.

## Trust summary

| party | can do | cannot do |
|---|---|---|
| executor wallet | send published batches, in any order, each once | change any recipient or amount; add a payment; pay twice; move tokens anywhere else |
| admin (multisig) | pause; replace the executor; recover tokens sent to the contract by mistake | change the root, the total, or any payment |
| reserve (multisig) | approve exactly the total; revoke at any time | nothing else is required of it |
| anyone | read every fixed value and every executed batch; recompute the root from the published list; check any batch against the contract with `verifyBatch` | |
