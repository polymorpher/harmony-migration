# Deploying and funding validator vaults from the Safe

Staked and delegated ONE is not paid to the delegator's wallet. It becomes principal in a vault
for the validator it was delegated to: one `GovernorDelegator` contract per validator, governed by
the validator, in which each delegator holds a credit it can withdraw at any time. The
supply-reserve Safe deploys the vaults and funds every credit, in Safe transactions the signers
check and sign, the same way as the direct payments in `docs/SAFE_DIRECT.md`.

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
  `destination_status` column, every row must be `ready`.
- The claim portal's `confirmed-wallets.sh --csv` companion file,
  `confirmed-wallets-<time>[-<filters>]-vault-shares.csv`, also works as it is (`validator_address`,
  `address`, `expected_shares_atto`). Rows whose amount is 0 are skipped and counted. Export it with
  `--decision approved --vault pending` to get only approved wallets' positions not yet marked sent.
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
