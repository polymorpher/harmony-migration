# Runbook: preparing, rehearsing and running a distribution

All commands run from the `airdrop/` directory and read `.env`. Start with
`cp .env.example .env` and fill in the values; the comments in the file explain each one.

There are three roles. **Reserve**: the wallet that holds the tokens and approves the contract for
the exact total (on Ethereum, the supply-reserve multisig). **Admin**: the wallet that can pause
the contract and replace the executor (normally the same multisig). **Executor**: the operational
wallet that sends the batch transactions. The executor can only send the published batches, so
it does not need to be a multisig, but its key should live on a dedicated machine or a hardware
wallet (`FORGE_WALLET_ARGS="--ledger --sender 0x..."`).

The same steps apply to every distribution. What changes is the input list, the run directory
name, and on the first live use the size of the list.

## 0. One-time setup

```bash
cd airdrop
cp .env.example .env         # then edit
./airdrop.py test            # 20+ Solidity tests should pass
./airdrop.py local-demo      # end-to-end on a local chain, no network or keys needed
```

## 1. Build the run directory

Input: a CSV file or a directory of CSV files with an address column and an amount column (see
README, "Input format"). The list comes from the accounting pipeline; this tool only formats and
checks it.

```bash
./airdrop.py build --input path/to/list.csv --run-dir runs/<name> --label "<what this is>"
```

Read the summary it prints: number of recipients, number of batches, total, root. The tool stops
on duplicate addresses, wrong checksums, the zero address, and non-positive amounts. Put the run
directory name in `.env` as `RUN_DIR=runs/<name>`.

If a few rows should go out first (for example, exchange destinations), put them at the top of
the input and build with `--order input`; otherwise keep the default address order.

## 2. Verify the run directory, twice

```bash
./airdrop.py verify --offline
```

This recomputes everything from `distribution.csv` in Python and again in Solidity and compares
both with `manifest.json` and the batch files. Both must print OK. Have a second person run the
same command on a copy of the directory before going further. Independently, check that the total
printed here equals the total the accounting pipeline expects for this stage.

## 3. Simulate before deploying

```bash
./airdrop.py simulate
```

This runs on a fork of the chain in `RPC_URL`. It deploys the contract inside the simulation,
pretends to be the reserve to grant the approval, pretends to be the executor to send every batch,
then checks that the reserve paid exactly the total and that every recipient in the first, middle
and last batch received exactly the published amount. It sends nothing. Anything wrong with the
list, the token, or the reserve balance shows up here.

## 4. Deploy and publish the source

```bash
./airdrop.py deploy                # prints the address; writes runs/<name>/deployment-<chainid>.json
# put the address in .env as AIRDROP_ADDRESS, then:
./airdrop.py verify                # now also compares with the contract on-chain
./airdrop.py verify-contract       # publishes the source on Etherscan
```

After this, anyone can read `root()`, `batchCount()`, `totalAmount()`, `listHash()`, `token()`,
`reserve()`, `admin()` and `executor()` on Etherscan and compare them with the published run
directory.

## 5. The reserve approves exactly the total

```bash
./airdrop.py approve-calldata
```

This prints the call data for `approve(<airdrop address>, <total>)` on the token and writes
`runs/<name>/safe-approve-<chainid>.json`, which can be imported into the Safe web app's
Transaction Builder. The signers should confirm three things before signing: the `to` is the
token, the spender is the contract address from step 4, and the contract's `root()` on Etherscan
equals the root in `manifest.json`. That single comparison is the whole approval; nothing else
about the list needs to be trusted from the operator.

On a testnet where the reserve is a plain wallet:

```bash
cast send $TOKEN_ADDRESS "approve(address,uint256)" $AIRDROP_ADDRESS <total> --rpc-url $RPC_URL --private-key <reserve key>
```

## 6. Send the batches

```bash
./airdrop.py execute --dry-run 0 3     # simulate the first three batches
./airdrop.py execute 0 3               # send them, one per block
./airdrop.py status                    # confirm 3 executed, allowance = remaining amount
./airdrop.py execute 0 0               # send everything that is left, in order
```

`execute <from> <to>` sends batches `from` up to but not including `to`; `to = 0` means "to the
end". Batches already marked executed on-chain are skipped, so re-running after an interruption
is safe. Transactions are sent one at a time and each waits to be mined (`--slow`). Foundry writes
a log of every transaction to `broadcast/Execute.s.sol/<chainid>/`.

Pace: for a large list, run in chunks of a few dozen batches and watch the base fee between
chunks. Each batch of 400 recipients uses about 11 million gas, a third of a block's target, so
sending one per block nudges the base fee upward; sending a chunk, pausing a minute, and
continuing keeps that small.

If a transaction fails, the batch is untouched (a batch either fully succeeds or fully reverts).
The usual causes are all visible in `status`: paused, allowance lower than the batch, or the
reserve balance too low.

## 7. Reconcile and publish

```bash
./airdrop.py status
./airdrop.py reconcile
```

`reconcile` reads every recipient's balance at one block and writes
`runs/<name>/reconciliation-<chainid>-<block>.json`. It passes when every recipient holds at
least the published amount, all batches are executed, `distributedAmount == totalAmount`, and the
remaining allowance is zero. Publish the run directory (including the deployment and
reconciliation files) and the broadcast log as the record of the distribution.

## Stopping, pausing, and changing the list

- **Pause**: the admin calls `setPaused(true)` on the contract. No tokens move.
- **Hard stop**: the reserve sets the contract's allowance to zero
  (`./airdrop.py approve-calldata --revoke` prepares the Safe transaction). This works even if the
  contract or the executor were somehow compromised.
- **Replace the executor**: the admin calls `setExecutor(newWallet)`.
- **Change the list**: the root cannot be changed, by design. Build a new run directory containing
  only the recipients not yet paid (every executed batch's recipients removed), deploy a new
  contract for it, and have the reserve send one Safe transaction that revokes the old allowance
  and approves the new contract for the new total. Publish both run directories and note which
  batches of the first contract were executed.

## Rehearsal plan

1. **Local**: `./airdrop.py local-demo`. Exercises every command against a local chain.
2. **Sepolia**: set `RPC_URL` and `CHAIN_ID=11155111`; deploy the rehearsal token with
   `./airdrop.py deploy-rehearsal-token <reserve wallet> <supply in atto>` and put its address in
   `TOKEN_ADDRESS`; then follow steps 1 to 7 with a test list of any size. Use a list at least as
   large as the real one once, to see the timing and the gas of full-size batches.
3. **Small live test on Ethereum**: with the real token, build a run from a short list of
   team-controlled addresses and small amounts, follow steps 1 to 7, including the multisig
   approval. This proves the multisig flow, the Etherscan verification and the executor setup
   with real funds at minimal exposure.
4. **Priority list**, then **full list**: same steps, larger inputs. Each gets its own run
   directory and its own contract.
