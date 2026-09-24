# Checking a distribution yourself

You do not need to trust the operators. Everything the contract enforces can be recomputed from
the published files, and everything the contract has done can be read from the chain. This page
lists what to check and how, from the quickest check to the most thorough.

You need the published run directory (`manifest.json`, `distribution.csv`, `batches/`), the
contract address, and a copy of this repository. For the on-chain checks you also need an
Ethereum RPC endpoint (any public one works for reads) or just Etherscan.

## 1. Is the contract locked to the published list?

Open the contract on Etherscan, "Read Contract", and compare:

| contract value  | should equal                                   |
|-----------------|------------------------------------------------|
| `root()`        | `root` in `manifest.json`                      |
| `batchCount()`  | `batch_count` in `manifest.json`               |
| `totalAmount()` | `total_amount` in `manifest.json`              |
| `listHash()`    | `list_sha256` in `manifest.json`, and `sha256sum distribution.csv` |
| `token()`       | the ONE token contract                         |
| `reserve()`     | the supply-reserve multisig                    |
| `admin()`       | the multisig                                   |

These are all `immutable`: the contract's source (verified on Etherscan) shows they are set once
in the constructor and have no setter.

## 2. Does the published list really produce that root?

Two independent implementations recompute the root from `distribution.csv`; run either or both.

Python (standard library only):

```bash
python3 tools/verify.py --run-dir <path to run directory> --offline
```

Solidity, via Foundry:

```bash
forge script script/VerifyRoot.s.sol --sig "run(string)" <path to run directory>
```

Both rebuild every batch from `distribution.csv` alone, recompute each fingerprint and the root,
and compare with `manifest.json` and every batch file. Add `--rpc-url <endpoint> --airdrop
<address>` (Python) or set `AIRDROP_ADDRESS` and pass `--rpc-url` (Solidity) to also compare with
the contract.

If you would rather not run any of this code, the hashing rules are short enough to implement
from scratch; they are written out in `docs/DESIGN.md`.

## 3. Am I on the list, and for how much?

```bash
grep -i 0xYourAddress <run directory>/distribution.csv
```

Amounts are in the smallest unit (18 decimals); divide by 10^18 for whole tokens. The same row
appears in exactly one batch file under `batches/`; its `batch_index` tells you which transaction
will pay you.

## 4. Can the contract pay anyone who is not on the list?

No, and you can test that yourself without a wallet: the contract exposes `verifyBatch(index,
recipients, amounts, proof)`. Feed it a real batch file and it returns true; change one amount by
one unit, or one address, and it returns false. `execute` performs exactly this check and reverts
on false. `test/CommittedBatchAirdrop.t.sol` contains these cases and more; run
`forge test` to execute them.

## 5. What has been sent so far?

```bash
python3 tools/status.py --airdrop <address> --rpc-url <endpoint>
```

Or on Etherscan: `executedBatches()`, `distributedAmount()`, `isExecuted(i)` per batch, and the
`BatchExecuted` events, each of which names the batch index, its fingerprint, the number of
recipients and the amount paid. The reserve's remaining allowance to the contract
(`allowance(reserve, contract)` on the token) should always equal `totalAmount() -
distributedAmount()`.

## 6. Did everyone receive what the list says?

After the run, the operators publish `reconciliation-<chainid>-<block>.json`. To reproduce it:

```bash
python3 tools/reconcile.py --run-dir <run directory> --airdrop <address> --rpc-url <endpoint> --block <block>
```

It reads each recipient's balance at that block and reports any address holding less than its
published amount (there should be none), plus the contract's counters and the remaining allowance.

## Batches paid directly from the Safe

Some batches may be paid by the Safe itself instead of the contract (see `docs/SAFE_DIRECT.md`).
For those, the published folder contains `recipients.csv`, `manifest.json` and one folder per Safe
transaction. To check them:

```bash
python3 tools/safe_batch.py verify --out-dir <published folder> --rpc-url <endpoint> --balances
```

This rebuilds every Safe transaction and hash from `recipients.csv`, checks the Safe and token on
the chain, and counts the recipients whose balance covers their amount. To check an executed
transaction directly, copy its input data from Etherscan (the Safe's `execTransaction` call; the
`data` argument) into a file and run `python3 tools/safe_batch.py hash --safe <Safe> --chain-id 1
--nonce <nonce> --to <to argument> --operation <operation argument> --data-file data.hex
--list-out decoded.csv`: it decodes every transfer and recomputes the Safe transaction hash that
the owners signed.

## 7. Is the deployed code what is in this repository?

The contract is verified on Etherscan, so the source is visible there and Etherscan has already
compared the compiled bytecode with what is deployed. To repeat that comparison yourself, build
with the pinned compiler settings in `foundry.toml` (`forge build`) and compare
`deployedBytecode` in `out/CommittedBatchAirdrop.sol/CommittedBatchAirdrop.json` with
`cast code <address>`. The two differ only at the positions listed under `immutableReferences`
in the same file, where the deployed code carries the eight constructor values (root, total, and
so on) and the compiled artifact carries zeros. Metadata hashes are disabled in the build settings,
so there is no other source of difference.
