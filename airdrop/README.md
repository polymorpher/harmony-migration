# Harmony ONE airdrop on Ethereum: contract and tools

This directory contains everything used to send the new ERC-20 ONE to eligible wallets on
Ethereum: the smart contract, the scripts that deploy and run it, and the tools that turn a
recipient list into the exact data the contract accepts. It is written so that anyone can check,
before and after the fact, that the contract can only ever pay the published list.

The recipient lists themselves are **not** in this directory. They are produced by the accounting
pipeline in the rest of this repository and published separately when a distribution is
announced. The code here does not know or care which list it is given, so the same code is used
for the testnet rehearsal, a small live test on Ethereum, the priority list, and the full list.

## The idea in one paragraph

A list of "address, amount" rows is split into numbered batches. Each batch is reduced to a
32-byte fingerprint, and all fingerprints are combined into a single 32-byte value called the
**root**. A contract is deployed with that root written into it permanently. The wallet that
holds the tokens (the supply-reserve multisig) allows the contract to spend exactly the list's
total, and nothing more. An operator wallet then sends the batches one transaction at a time. For
every batch, the contract recomputes the fingerprint from the addresses and amounts it is given and
checks that the fingerprint belongs to the root. Anything not on the published list is rejected;
anything already sent is rejected. The operator cannot change an address, change an amount, add a
payment, or pay anyone twice. If the list ever has to change, a new contract is deployed with a new
root and the old one is switched off by revoking its allowance.

## A second way: paying directly from the Safe

For a distribution the signers prefer to approve batch by batch, the tools can also prepare Safe
transactions that pay a list with plain token transfers from the Safe itself, with no contract and
no operator wallet. Every batch is then a Safe transaction the signers check against three hashes
before signing. `docs/SAFE_DIRECT.md` explains the procedure; `tools/select_batch.py` chooses the
recipients of a batch and `tools/safe_batch.py` prepares, verifies and hashes the Safe
transactions.

## What you can check yourself

- That the published list produces the root written in the contract, using either the Python
  tool or the Solidity script, which are two separate implementations that must agree.
- That the contract's fixed values (root, number of batches, total, list hash, token, reserve)
  match the published files.
- That your address appears in the list with the amount you expect, and, after the run, that your
  balance reflects it.
- Which batches have been sent and how much has been distributed so far.

`docs/VERIFY.md` walks through each of these with exact commands.

## What is in here

```text
airdrop.py                     command-line entry point; run ./airdrop.py help
                               (prints every forge command it runs; extra forge flags go after --)
.env.example                   settings template; copy to .env (never committed)
src/CommittedBatchAirdrop.sol  the contract
src/IERC20.sol                 the small token interface it needs
src/testnet/RehearsalToken.sol stand-in token for testnet rehearsals (never used on mainnet)
script/Deploy.s.sol            deploy the contract for a run directory
script/Execute.s.sol           send batches, skipping the ones already sent
script/Simulate.s.sol          full dress rehearsal on a fork; sends nothing
script/VerifyRoot.s.sol        recompute the root in Solidity and compare with files and contract
script/DeployRehearsalToken.s.sol
script/lib/                    shared helpers: the tree construction, reading run files, .env
tools/build.py                 recipient list -> run directory (batches, proofs, root)
tools/verify.py                recompute everything from the list and compare (Python)
tools/status.py                what the deployed contract says
tools/reconcile.py             read every recipient's balance after the run
tools/approve_calldata.py      the one transaction the reserve has to send
tools/select_batch.py          choose the recipients of the next batch (budget, required, held, paid)
tools/safe_batch.py            without the contract: Safe transactions that pay a list, their hashes
tools/common.py                keccak, ABI encoding, the tree, CSV reading, JSON-RPC
test/                          Solidity tests, including a cross-check against the Python tool
examples/recipients-example.csv a small made-up list to try the tools on
runs/                          generated run directories (ignored by git, published on release)
```

Requirements: [Foundry](https://getfoundry.sh) (`forge`, `cast`, `anvil`) and Python 3.12 or
newer. The Python tools use only the standard library; if `pycryptodome` happens to be installed
it is used to hash faster, otherwise a built-in pure-Python keccak is used (about half a minute
for a list of tens of thousands of recipients).

## Try it in two minutes

```bash
cd airdrop
./airdrop.py test          # Solidity test suite
./airdrop.py local-demo    # whole flow on a local Anvil chain with examples/recipients-example.csv
```

The demo builds a run directory, deploys a rehearsal token and the airdrop contract, verifies the
files against the contract in both Python and Solidity, approves, sends the batches in two steps
(showing that an interrupted run resumes cleanly), and reconciles every balance.

## Input format

`tools/build.py` accepts one CSV file or a directory of CSV files. Each file needs a header row
with an address column and an amount column. Recognised column names include `address`,
`destination_address`, `recipient` for the address and `amount_atto`, `amount_one`, `amount` for
the amount; pass `--address-column` / `--amount-column` for anything else. Amounts are either
whole numbers in the smallest unit (`atto`, 18 decimals) or decimal numbers of whole tokens
(`one`); pass `--amount-unit` when the column name does not say which.

The builder refuses to continue on a mixed-case address with a wrong checksum, the zero address,
a non-positive amount, or a duplicate address (add `--merge-duplicates` to sum repeats
deliberately). Recipients are sorted by address by default so the same input always yields the
same root; `--order input` keeps the file order instead, which is useful when specific rows should
go out in the first batch.

## Run directory

`tools/build.py` writes:

```text
runs/<name>/manifest.json            root, batch count, batch size, total, list hash, sources, per-batch summary
runs/<name>/distribution.csv         the canonical list: address,amount (checksummed, smallest unit)
runs/<name>/batches/batch-00000.json one file per batch: recipients, amounts, fingerprint, proof
```

Later steps add `deployment-<chainid>.json`, `safe-approve-<chainid>.json` and
`reconciliation-<chainid>-<block>.json`. The whole directory is what gets published.

## Safety properties, and their limits

Guaranteed by the contract:

- Only batches whose fingerprint is under the root can be sent. The root cannot be changed.
- Each batch can be sent once.
- Tokens move straight from the reserve to the recipients; the contract holds nothing.
- The reserve's approval caps the total that can ever leave, and the reserve can revoke it at any
  time without the contract's cooperation.
- `admin` (the multisig) can pause and can replace the operator wallet; it cannot alter payments.

Not guaranteed by the contract, and handled by process instead:

- That the list itself is correct. The contract faithfully pays whatever list it was given. The
  list is produced and cross-checked by the accounting pipeline, published before the multisig
  approves, and rehearsed on a fork (`./airdrop.py simulate`) before anything is sent.
- That a recipient address can use the tokens. Addresses that are contracts on Harmony but not on
  Ethereum are handled by the routing policy upstream, not here.

Gas: a batch of 400 recipients uses about 11 million gas. Ethereum caps a single transaction at
16,777,216 gas, so the builder limits batches to 500 recipients and defaults to 400.

## Where to read next

- `docs/RUNBOOK.md`: step-by-step operations for the Sepolia rehearsal, the small live test on
  Ethereum, and the real distributions.
- `docs/VERIFY.md`: how to check the published files and the contract without trusting anyone.
- `docs/DESIGN.md`: why this design was chosen over alternatives, and the exact hashing rules.
- `docs/SAFE_DIRECT.md`: paying a list directly from the Safe, and what each signer checks.
