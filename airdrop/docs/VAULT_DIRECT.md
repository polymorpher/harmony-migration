# Deploying and funding validator vaults from the Safe

Staked and delegated ONE is not paid to the delegator's wallet. It becomes principal in a vault
for the validator it was delegated to: one `GovernorDelegator` contract per validator, governed by
the validator, in which each delegator holds a credit it can withdraw at any time. The
supply-reserve Safe deploys the vaults and funds every credit, in Safe transactions the signers
check and sign, the same way as the direct payments in `docs/SAFE_DIRECT.md`. The deployments can
instead be sent from a private key (see "Deploying from a private key instead"); the funding always
comes from the Safe.

## How it works

- **The contract** is the audited build pinned in `vault/GovernorDelegator.json`: source SHA-256
  `df551a62…ffbe8`, creation code keccak-256 `0x72dad98e…d4f2`, Solidity 0.8.30, optimizer 200,
  Cancun, no metadata. `vault/GovernorDelegator.compiler-input.json` is the solc standard-JSON
  input, source included, so anyone can recompile it. The tool refuses any other build.
- **Deployment** is a plain call to the standard CREATE2 deployer
  `0x4e59b44847b379578588920cA78FbF26c0B4956C` (69 bytes of code, keccak-256 `0x2fa86add…4989`).
  Its call data is a 32-byte salt followed by the init code. The salt is the validator's address and
  the init code is the creation code followed by `abi.encode(token, governor)`, so each vault's address is fixed
  before anything is signed:
  `keccak256(0xff ++ deployer ++ salt ++ keccak256(init code))[12:]`. Safe's own `CreateCall`
  library is not used: it needs a delegate call, which the Transaction Builder cannot express.
  Whoever sends the call gets no role in the vault, so the address and code are the same whether
  the Safe or a plain wallet deploys it.
- **The governor** of a vault is the validator's own address unless `--governors` or `--governor`
  says otherwise. The governor only sets the fee record and can hand the role on; it cannot touch
  anyone's principal. Because the governor is part of the init code, choosing a different one
  changes the vault's address.
- **Funding** is two calls per vault: `approve(vault, exact total)` on the token, then
  `depositBatch(amounts, delegators)` on the vault, amounts first. The vault pulls the total from
  the Safe in one transfer and credits each delegator. The approval is used up exactly, so nothing
  stays approved.
- **Batches.** Deployments are bundled 16 to a Safe transaction (`--deploys-per-tx`). Funding
  calls are packed so each transaction stays under about 10 million gas (`--gas-target`); a vault
  with more than 350 delegators (`--max-rows-per-call`) is split into several deposit calls.
  Everything goes through Safe's MultiSendCallOnly, as with direct payments.
- **The vault minimum.** A vault whose deposits total less than 10,000 ONE is left out of the
  build (`--min-vault-one`, `0` keeps every vault). Its deposits go to `excluded-vaults.csv` and
  `excluded-deposits.csv`, so nothing disappears silently; they can be deposited later, when the
  vault is deployed for a later stage. A vault that already exists on-chain is funded whatever the
  size of the new deposits.

## Step 1. The deposit list

One CSV with a header and three columns: the validator, the delegator and the amount.

```text
validator_address,delegator_address,amount_one
0x0a85516793405f87F1d5E3bC8AF59C28D799C252,0x0a85516793405f87F1d5E3bC8AF59C28D799C252,10000
0x0a85516793405f87F1d5E3bC8AF59C28D799C252,0xfec326f9e1a0672757d5f56708135481c4b3e39a,0.8284
```

- The delegator column may also be called `beneficiary_address` or `delegator`, the validator
  column `validator`. `amount_atto` (whole numbers in the smallest unit) or `amount_one` (decimal
  ONE) say their unit; a bare `amount` column needs `--amount-unit`.
- The pipeline's `initial-stage/vault-shares.csv` works as it is. When a file has a
  `destination_status` column, every row must be `ready`. Rows whose amount is 0 are skipped and
  counted before any check.
- The claim portal's `confirmed-wallets.sh --csv` companion file,
  `confirmed-wallets-<time>[-<filters>]-vault-shares.csv`, also works as it is (`validator_address`,
  `address`, `expected_shares_atto`). `build` recognises it by its `decision` and `sent_status`
  columns and deposits it only if every non-zero row is `approved` and `pending` (not sent, not
  blocked by a hold, redirect, exchange delivery or a mismatch with the ledger). Export it with
  `--decision approved --vault pending`; an unfiltered or older export stops the build. `show
  --compare` does not apply this check. Lists without those columns are read as always.
- `--input` can be repeated, for example the initial-stage list together with the confirmed
  wallets. Each file's columns and unit are detected separately, and a delegator listed twice for
  one validator, across files too, is refused. Building such lists together means each vault is
  deployed once and the vault minimum is judged on the combined total.
- A delegator repeated within one vault is refused (`--merge-duplicates` adds them up). Zero
  amounts, the zero address, and a delegator equal to any vault, the Safe, the token or the
  deployer are refused. The last of these is audit finding L-01: a credit owned by the vault itself
  can never be withdrawn.

## Step 2. Prepare the Safe transactions

```bash
cd harmony-migration/airdrop

LIST=~/Downloads/vault-deposits.csv   # edit: the deposit list
OUT=runs/vaults-initial               # edit: a new folder for each list

SAFE=0xf868743BE6bbdA38929C03616234684E6D3Ded20
TOKEN=0xFf74317E948695297acb79Cb2De06111F5Cc16b3
RPC=https://ethereum-rpc.publicnode.com
./airdrop.py vault-batch build --input "$LIST" --out-dir "$OUT" --safe $SAFE --token $TOKEN --rpc-url $RPC --no-nonce
```

With `--rpc-url` the tool reads the Safe's chain id and version, checks that the deployer exists,
and leaves out deployments of vaults that already exist with the expected code and governor.
`--no-nonce` builds files that do not depend on where they land in the queue (see
`docs/SAFE_DIRECT.md`); without it the first transaction gets the Safe's next executed nonce, or
`--nonce N`.

Other options: `--validator 0x…|one1…` (only these validators' vaults; repeatable), `--skip-funded`
(see "A pilot first" below), `--governors FILE` (columns `validator_address` and `governor_address`, or
`destination_address`; the pipeline's `initial-stage/validator-vaults.csv` works, and its
`initial_assets_atto` is checked against the deposits), `--governor 0x…` (one governor for every
vault, for example the Safe as an interim governor), `--phase deploy|fund`, `--expect-total`,
`--expect-count`, `--expect-vaults`, `--label`. `--gas-price-gwei 0.1,0.3,1` sets the prices of
the printed cost estimate.

Output:

```text
runs/vaults-initial/SIGNING-SHEET.md       every value a signer compares, per transaction
runs/vaults-initial/manifest.json          parameters, input hashes, per-transaction hashes and gas estimates
runs/vaults-initial/vaults.csv             validator, governor, vault address, salt, deposits, total
runs/vaults-initial/deposits.csv           every credit: validator, vault, delegator, amount
runs/vaults-initial/excluded-vaults.csv    vaults below the minimum, and excluded-deposits.csv their credits
runs/vaults-initial/tx-01-deploy/          transaction-builder.json, safe-transaction.json, vaults.csv
runs/vaults-initial/tx-06-fund/            transaction-builder.json, safe-transaction.json, deposits.csv
```

The deploy transactions come first. A funding transaction reverts as a whole, without using its
nonce, if one of its vaults does not exist yet.

## Step 3. A second person verifies the files

```bash
./airdrop.py vault-batch verify --out-dir "$OUT"                                  # offline
./airdrop.py vault-batch verify --out-dir "$OUT" --rpc-url $RPC --harmony-rpc https://api.s0.t.hmny.io
```

Offline, it rebuilds every vault address, call, Transaction Builder file and hash from
`vaults.csv` and `deposits.csv`. It also checks that every left-out vault is below the minimum and
every funded one is not.

With `--rpc-url` it checks the Safe, MultiSendCallOnly and the deployer's code. It reports which
vaults already exist and checks their code, token and governor. It checks that the Safe holds
enough ONE, and it lists governors and delegators that are contracts, plus how many governors have
never sent an Ethereum transaction.

`--harmony-rpc` reads every validator's delegations from Harmony at the shard-0 cutoff block
93,623,067 (its hash is checked first) and compares each deposit with that delegator's stake.

## Step 4. Propose in the Safe web app

As in `docs/SAFE_DIRECT.md`: for each folder in order, open Apps, then Transaction Builder, drag in
`transaction-builder.json`, create the batch and send it. Propose all deploy transactions before
the funding ones. A proposer wallet can do this without signing (see `docs/SAFE_DIRECT.md`).

The Transaction Builder shows each call as raw data. The tool writes the call data itself, so
what is hashed and signed is exactly what was verified.

## Step 5. Review the queue

Reviewers need no files. `show` reads the queued transactions from Safe's Transaction Service,
decodes the raw call data itself, recomputes every Safe transaction hash and lists every vault and
every deposit:

```bash
git clone git@github.com:polymorpher/harmony-migration.git   # once
cd harmony-migration/airdrop && git pull

SAFE=0xf868743BE6bbdA38929C03616234684E6D3Ded20
TOKEN=0xFf74317E948695297acb79Cb2De06111F5Cc16b3
RPC=https://ethereum-rpc.publicnode.com
./airdrop.py vault-batch show --safe $SAFE --token $TOKEN --rpc-url $RPC \
    --harmony-rpc https://api.s0.t.hmny.io --out queued-vaults.csv
# to also compare with the published list exactly, add:  --compare deposits.csv
```

It writes three files:

- `queued-vaults.csv`: one row per deposit, with the columns
  `validator_one1,validator_address,vault_address,delegator_one1,delegator_address,amount`.
  `amount` is the exact ONE credited.
- `queued-vaults-vaults.csv`: one row per vault. It gives the validator (with its Harmony name when
  `--harmony-rpc` is used), the governor, the vault address, where it was deployed, the number of
  deposits and the total.
- `queued-vaults-details.csv`: the nonce, call and position of every deposit, its amount in the
  smallest unit, the governor, and, with `--harmony-rpc` or `--compare`, the amount on Harmony or
  in the list and a `check` column.

`show` fails, and says not to sign, if any of the following holds:

- **Hashes:** a hash does not match its data.
- **Calls:** a call is anything other than:
  - a deployment of the audited vault for the token;
  - an `approve` of a vault's exact total, directly followed by that vault's `depositBatch`;
  - a canonically encoded `depositBatch`.
- **Governors:** a vault gets a governor other than the expected one.
- **Deposit targets:** deposits go to an address that is neither deployed by the listed
  transactions nor a genuine vault on-chain. Vaults deployed by transactions that have already run
  are checked through `--rpc-url`.
- **Deposits:**
  - a delegator is credited twice in one vault;
  - a deposit is zero;
  - a delegator is a vault, the Safe or the token.
- **Amounts:** with `--harmony-rpc` or `--compare`, a deposit differs from Harmony's stake or from
  the list.

With neither `--harmony-rpc` nor `--compare`, amounts are not compared with anything, and an amount
moved between two delegators of one vault would go unnoticed; `show` says so. `--harmony-rpc` is
the independent check: it compares every deposit with Harmony's own record and needs nothing from
us. By default `show` covers everything still queued; `--nonce 40-52` picks transactions, executed
ones included. Compare the printed Safe transaction hashes with the Safe web app, and the domain and
message hashes with the hardware wallet screen.

A signer can also check one transaction from its raw data:

```bash
./airdrop.py vault-batch hash --safe $SAFE --token $TOKEN --chain-id 1 --nonce <nonce> \
    --to <MultiSendCallOnly address> --operation 1 --data-file data.hex --list-out decoded.csv
```

## Step 6. Execute, then reconcile

Execute in nonce order, deploy transactions first. Afterwards:

```bash
./airdrop.py vault-batch reconcile --out-dir "$OUT" --rpc-url $RPC
```

For every vault, it reports whether the vault is deployed with the expected code, token and
governor, its fee record, its ONE balance against the planned total, the Safe's remaining
allowance, and whether every delegator's credit equals the plan. It writes
`reconcile-block-<block>.csv`.

Publish the source of the vaults the run deployed on Etherscan (`ETHERSCAN_API_KEY` in `.env`):

```bash
./airdrop.py vault-batch etherscan --out-dir "$OUT"
```

It submits the pinned compiler input with `vault/LICENSE` and `vault/NOTICE.txt` added as two more
files, so they appear next to the contract on Etherscan; the contract's header refers to both.
solc compiles every file it is given, so the two go in as block comments. They contain no code,
and the build has no metadata hash, so the compiled bytecode is identical to the audited build
(`test_the_submitted_input_compiles_to_the_audited_code` checks this with solc 0.8.30). Each vault
is submitted with its own constructor arguments (token and governor), all vaults before waiting on
any, and a vault Etherscan has not indexed yet is retried, so the command can be run again at any
time. Each vault is then reported as an **exact match** (its own verification) or a **similar
match**: once one vault is verified, Etherscan may show the identical vaults as matches of it and
refuse their own submissions. A similar match shows the audited source with that other vault's
constructor arguments; `governor()` under Read Contract still shows each vault's own governor, and
`show` and `verify --rpc-url` check every governor on-chain.

## A pilot first, then everything else

Fund one or two vaults completely before the rest, from the same lists:

```bash
I=../results/2026-09-11/destination-mapping/initial-stage/vault-shares.csv
# from the claim portal: db/ops/confirmed-wallets.sh --decision approved --vault pending --csv
C=~/Downloads/confirmed-wallets-<time>-decision-approved-vault-pending-vault-shares.csv
# 1. one vault (here modulo.so), then check it and test withdraw / setFeeBps on mainnet
./airdrop.py vault-batch build --input $I --input $C --validator 0x951020A7047483d09173aF5d71B7C9CF60f273f6 \
    --out-dir runs/vaults-pilot-1 --safe $SAFE --token $TOKEN --rpc-url $RPC --no-nonce
# 2. a second, external validator (here Fortune)
./airdrop.py vault-batch build --input $I --input $C --validator 0x63e7E9BB58aA72739a7CEc06f6EA9Fe73eb7A598 \
    --out-dir runs/vaults-pilot-2 --safe $SAFE --token $TOKEN --rpc-url $RPC --no-nonce
# 3. everything else, leaving out the piloted vaults
./airdrop.py vault-batch build --input $I --input $C \
    --exclude-validator 0x951020A7047483d09173aF5d71B7C9CF60f273f6,0x63e7E9BB58aA72739a7CEc06f6EA9Fe73eb7A598 \
    --out-dir runs/vaults-3 --safe $SAFE --token $TOKEN --rpc-url $RPC --no-nonce
```

- **Leave the piloted vaults out by name.** `--exclude-validator` does this, which is necessary once
  a pilot has been tested with a `withdraw`: that delegator's credit no longer equals the list, so
  `--skip-funded` would refuse to build.
- **`--skip-funded`** covers the other case: a later rebuild after some transactions of a run were
  already executed.

- **The pilots fund whole vaults.** `--validator` keeps every row of the chosen validators, so a
  piloted vault is complete and step 3 leaves it alone: no second deployment, no second deposit.
- **Rebuilding is guarded.** Deposits add to a credit, so `build --rpc-url` stops if any delegator
  already holds a credit in its vault. `--skip-funded` instead leaves out each deposit whose credit
  already equals the list exactly, and lists it in `already-funded.csv`. A credit that differs
  from the list still stops the build; `--allow-existing-credits` is only for deliberate top-ups.
- **Reconcile each run.** Run `reconcile` after each run executes. A run's report covers only its
  own vaults.
- **Then mark it in the claim portal.** From the portal checkout,
  `db/ops/record-review.sh --sent vault --from-run <this airdrop>/runs/vaults-pilot-1 --apply`
  records each confirmed wallet's position as sent by that run; initial-stage delegators are left
  out. The next `--vault pending` export no longer lists those positions, and marking another run
  that pays them again is refused. The batch id is the run directory's name, so give every run its
  own name with a sequence number (`vaults-pilot-1`, `vaults-3`, `vaults-4`), never a reused one.

## Deploying from a private key instead

A deployment moves no ONE and gives its sender no role in the vault, so a plain wallet can send the
deployments and the Safe signs only the funding. It costs about the same gas (see "Gas and cost")
and saves the signers the deploy transactions. Build a run with only the deployments, deploy it,
then fund from the Safe as usual.

Once, in `airdrop/.env`; `build` and `deploy` read these when `--safe`, `--token` and `--rpc-url`
are left out, and `verify` uses `RPC_URL` when `CHAIN_ID` matches the run:

```text
RPC_URL=https://ethereum-rpc.publicnode.com
CHAIN_ID=1
RESERVE_ADDRESS=0xf868743BE6bbdA38929C03616234684E6D3Ded20
TOKEN_ADDRESS=0xFf74317E948695297acb79Cb2De06111F5Cc16b3
VAULT_SENDER_ACCOUNT=<keystore name>
TOUCHID_HELPER=<optional; see below>
```

The deposit lists are given on the command line for every run:

```bash
cd harmony-migration/airdrop
# the initial stage, as published
I=../results/2026-09-11/destination-mapping/initial-stage/vault-shares.csv
# the claim portal's approved, still-pending positions, exported just before the build with
# `db/ops/confirmed-wallets.sh --decision approved --vault pending --csv` (in harmony-claim-portal,
# which writes them to data/confirmed-wallets/)
C=../../harmony-claim-portal/data/confirmed-wallets/confirmed-wallets-<time>-decision-approved-vault-pending-vault-shares.csv

# 1. the deployments, from the same lists and options as the Safe build
./airdrop.py vault-batch build --input $I --input $C --out-dir runs/vaults-3-deploy --no-nonce --phase deploy
# 2. dry run: unlocks the key, checks the chain, simulates every deployment; sends nothing
./airdrop.py vault-batch deploy --out-dir runs/vaults-3-deploy
# 3. send them, one transaction per vault
./airdrop.py vault-batch deploy --out-dir runs/vaults-3-deploy --broadcast
# 4. fund from the Safe: the build sees the vaults on-chain and leaves out their deployment
./airdrop.py vault-batch build --input $I --input $C --out-dir runs/vaults-3 --no-nonce --phase fund
```

`vaults-3` stands for the next unused run name (see "A pilot first"). Every initial-stage governor
is the validator itself, which is also the default, so no `--governors` file is needed. If a
validator ever gets another governor, pass the same `--governors` file to both builds, or the
funding build will not find the deployed vaults.

- **The key** is asked for once per command. `VAULT_SENDER_ACCOUNT` in `.env` (or `--account`)
  names a Foundry keystore: the name given to `cast wallet import <name> --interactive`, which
  stores the encrypted key as `~/.foundry/keystores/<name>` (`--keystore-dir` for another folder).
  Its password is typed once. If `TOUCHID_HELPER` in `.env` (or `--touchid-helper`) names a
  `touchid-helper` binary, which keeps the password in the macOS keychain, the password comes from
  it after a Touch ID approval instead; `--no-touchid` types it anyway. The helper is asked for
  service `foundry-keystore` (`--keychain-service`) and the keystore's name as account, so store the
  password once with `touchid-helper store -s foundry-keystore -a <name>`. The Touch ID dialog
  names Python as the caller. Without a keystore name, or without that keystore, the private key
  itself is typed.
- **What is sent.** `deploy` first checks the run as `verify` does, and leaves out vaults already
  on-chain with the audited code and token. It writes `sender-deploy-<time>.json` into the run: the
  call data and the address each call must create. `script/DeployVaults.s.sol` then sends those
  calls with the key from an environment variable, each after the previous one is mined (`forge
  script --slow`), and stops unless each call creates its planned address. `deploy` refuses to
  start if the wallet has a pending transaction or holds less ETH than the estimate. `--sender 0x…`
  stops if the key belongs to another wallet; `--max-fee-gwei` and `--priority-fee-gwei` set the
  fees.
- **The record.** Every vault of the run goes into `vault/deployments-<time>.csv` (UTC, for example
  `deployments-20261011T091700Z.csv`): `chain_id`, the validator (also as `one1`), governor and
  vault, then `status`. A `deployed` vault has its transaction hash, block number and sender. An
  `existing` vault was already on-chain; it carries the transaction from an earlier
  `deployments-*.csv` in the same folder when one records it. Commit the file. On any chain other
  than Ethereum mainnet it goes into the run directory instead (`--ledger-dir` chooses).
- **Etherscan.** With `ETHERSCAN_API_KEY` in `.env`, `deploy --broadcast` then publishes every
  vault's source on Etherscan, with LICENSE and NOTICE.txt, as `vault-batch etherscan` does (see
  Step 6). Without the key it says so; run `vault-batch etherscan --out-dir <run>` later.
- **Interrupted or partly failed?** Once the wallet has nothing pending, run the same command
  again: it deploys only what is missing, and its file lists the rest as `existing`.
- **The run's own Safe transactions** (`tx-…-deploy`) are not needed then; do not propose them.
- **The run directory must be inside `runs/`**, because forge may only read files there.

## Rehearsing on Sepolia

The reserve Safe has a Sepolia deployment at the same address (a 1-of-1 Safe there), holding the
whole supply of the test token `0x6dF02e9a623f9d11d6c73B1fc2aBfAbb63f11E72`, whose code is
byte-identical to the mainnet ONE token. Keep the
Sepolia settings in `airdrop/.env.sepolia` and start every command with `AIRDROP_ENV=.env.sepolia`:
`airdrop.py` then reads that file instead of `.env`, so no command falls back to mainnet settings.

```text
RPC_URL=https://ethereum-sepolia-rpc.publicnode.com
CHAIN_ID=11155111
RESERVE_ADDRESS=0xf868743BE6bbdA38929C03616234684E6D3Ded20
TOKEN_ADDRESS=0x6dF02e9a623f9d11d6c73B1fc2aBfAbb63f11E72
VAULT_SENDER_ACCOUNT=<keystore name>
TOUCHID_HELPER=<optional>
ETHERSCAN_API_KEY=<the same key works for Sepolia>
```

Then follow the mainnet steps with the same lists, giving every run a `sepolia-` name:

```bash
export AIRDROP_ENV=.env.sepolia
./airdrop.py vault-batch build --input $I --input $C --validator $MODULO --validator $FORTUNE \
    --out-dir runs/sepolia-vaults-pilots-deploy --no-nonce --phase deploy
./airdrop.py vault-batch deploy --out-dir runs/sepolia-vaults-pilots-deploy --broadcast
./airdrop.py vault-batch build --input $I --input $C --validator $MODULO \
    --out-dir runs/sepolia-vaults-pilot-1 --no-nonce --phase fund --max-rows-per-call 100 --gas-target 3000000
./airdrop.py vault-batch verify --out-dir runs/sepolia-vaults-pilot-1 --rpc-url https://ethereum-sepolia-rpc.publicnode.com \
    --harmony-rpc https://api.s0.t.hmny.io
# propose tx-01-fund in the Safe web app on Sepolia, then review the queue:
./airdrop.py vault-batch show --chain-id 11155111 --rpc-url https://ethereum-sepolia-rpc.publicnode.com \
    --harmony-rpc https://api.s0.t.hmny.io --out /tmp/sepolia-queued.csv
# sign and execute, then:
./airdrop.py vault-batch reconcile --out-dir runs/sepolia-vaults-pilot-1
unset AIRDROP_ENV
```

- The vault addresses differ from mainnet, because the token address is part of each vault's init
  code. Everything else is the same contract, calls and checks.
- **Sepolia prices state creation far higher than mainnet.** Measured on 2026-10-11: 1,542 gas per
  byte of deployed code (mainnet 202) and 110,979 gas per new storage slot (mainnet 22,441). A vault
  deployment takes about 3.77 million gas there instead of 565,000. `deploy` sets each
  transaction's gas limit from the node's `eth_estimateGas` plus 20%, so this is handled; forge's
  own "estimated gas" figures still use the older prices. A deposit to a new delegator costs
  about 115,000 gas instead of 26,000, so build Sepolia funding runs with `--max-rows-per-call 100
  --gas-target 3000000`: about 100 deposits per Safe transaction, about 11.5 million gas, under the
  16,777,216 per-transaction cap. The build's gas estimates assume mainnet prices.
- `deployments-<time>.csv` goes into the run directory, not `vault/`, and Etherscan verification
  goes to sepolia.etherscan.io.
- `show` needs `--chain-id 11155111` to read Safe's Sepolia Transaction Service.
- Whoever executes the Safe transaction pays Sepolia gas, so that wallet needs Sepolia ETH.
- **Never mark a Sepolia run in the claim portal** (`record-review --sent vault --from-run`): it
  would record mainnet positions as paid.

## Gas and cost

Measured on a mainnet fork, executing the initial stage through the reserve Safe with the
default options (80 vaults of at least 10,000 ONE, 2,872 deposits, 1,421,185,794.70 ONE):

| | Safe transactions | Gas used | at 0.1 gwei | at 0.3 gwei | at 1 gwei |
|---|---:|---:|---:|---:|---:|
| Deploy (16 vaults each) | 5 | 43,280,309 | 0.0043 ETH | 0.0130 ETH | 0.0433 ETH |
| Fund | 8 | 77,674,941 | 0.0078 ETH | 0.0233 ETH | 0.0777 ETH |
| **Total** | **13** | **120,955,250** | **0.0121 ETH** | **0.0363 ETH** | **0.1210 ETH** |

- **Per transaction:** each deploy transaction used 8.66 million gas and each funding transaction
  up to 9.93 million, against the 16,777,216 per-transaction cap. A deployment costs about 540,000
  gas, and a deposit to a new delegator about 26,000.
- **Estimates:** `build` prints its estimate, which came within 1% of these figures.
- **Who pays:** the wallet that executes pays the gas, not the Safe.
- **Without the minimum:** all 117 vaults would take about 144 million gas (0.0144, 0.0433 and
  0.144 ETH at the same prices).
- **Deploying from a private key:** the same 80 vaults took 44,365,704 gas in 80 transactions
  (0.0044, 0.0133 and 0.0444 ETH at the same prices), measured on a local Anvil chain. That is 2.5%
  more than through the Safe, because every transaction pays the 21,000 base fee. The funding is
  unchanged.

## Things to know

- **L-01 is open** in the audited contract. Our own deposits cannot trigger it (no delegator may be
  a vault), but a holder who withdraws to a vault's address loses that amount for good.
- **Front-running a deployment** is possible, because the deployer is public. Someone else can
  deploy a vault first; it is the identical contract with the same governor, and funding works as
  usual. Our deploy transaction then reverts without using its nonce. Rebuild with `--rpc-url`
  and the existing vault is skipped.
- **Repeating a funding transaction credits again.** Deposits are additive, so before re-proposing
  anything, run `reconcile`.
- **Governor key control** is assumed, not proven: a validator's key controls the same address on
  Ethereum. A lost governor key freezes that vault's fee record and governor role, never the
  holders' principal.
- **Every vault starts with `feeBps` 0.** The audited constructor takes no fee, and only the
  governor (the validator) can call `setFeeBps`. `feeBps` 0 means the validator's commission on
  Harmony mainnet at the cutoff block; the UI and the compensation accounting read it that way, and
  any other value as the fee itself. A validator who wants close to nothing sets 1 (0.01%). At the
  cutoff, 52 of the 82 vaults at the 10,000 ONE minimum charged 7% on Harmony, 21 charged 10%, and
  the rest between 9% and 100%; modulo.so 10%, Fortune 7%.

## Exact rules, for anyone reimplementing the check

- **Deploy call:** `to` = the deployer; data = `salt ++ creation ++ word(token) ++ word(governor)`.
  `salt` is the validator address as a 32-byte word.
- **Approve call:** `to` = the token; data = `0x095ea7b3 ++ word(vault) ++ word(total)`.
- **depositBatch call:** `to` = the vault; data = `0xc2fff781 ++ word(0x40) ++ word(0x60 + 32n) ++
  word(n) ++ amounts ++ word(n) ++ delegators`, every value a 32-byte word.
- **Safe transaction:** calls are packed for MultiSendCallOnly exactly as in `docs/SAFE_DIRECT.md`,
  and the Safe transaction hashes follow the same rules. A single call is sent directly with
  operation 0.
- **Expected runtime:** `vault/GovernorDelegator.json`'s `runtime_template` with the token address
  written as a 32-byte word at each `asset_immutable_offsets` entry. For mainnet ONE its keccak-256
  is `0xc1f4cf5c480968601dde8f0e72521acfa7948d4d6b668dfa8f446f81375a47e6`.
