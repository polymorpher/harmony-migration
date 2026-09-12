# Independent reproduction

These instructions do not require access to the original archive node or any
private network. You must provide your own Harmony databases.

## 1. Requirements

- Linux x86-64 or ARM64
- Go `1.24.2`
- Python `3.10` or newer
- Git, GNU Make, a C/C++ compiler
- GMP and OpenSSL development packages
- enough local storage for the Harmony databases and generated CSVs

On Debian or Ubuntu:

```sh
sudo apt-get update
sudo apt-get install -y build-essential git libgmp-dev libssl-dev python3
```

## 2. Obtain the source dependencies

From the repository root:

```sh
./scripts/setup-harmony-dependencies.sh
```

This creates:

```text
third_party/harmony
third_party/mcl
third_party/bls
```

The setup script checks out exact revisions. `toolkit/go.mod` assumes the
Harmony source is at `third_party/harmony`.

The Harmony dependency is a pinned fork because the exact recovery-era
revision is not present in the upstream `harmony-one/harmony` repository.

## 3. Build and test

```sh
./scripts/build-toolkit.sh
source ./scripts/toolkit-env.sh
python3 -m unittest discover -s toolkit/tests
python3 -m py_compile \
  toolkit/scripts/census/*.py \
  toolkit/scripts/claims/*.py \
  toolkit/scripts/cutoff/*.py \
  toolkit/scripts/forensics/*.py
```

Compiled commands are written to `bin/`.

Most state tools are CGO-free, but the canonical block and transition recovery
commands import Harmony block types and require the MCL/BLS native libraries.
The build script compiles and links those libraries.

## 4. Obtain database inputs

You need:

- a shard-0 archive/full LevelDB containing state root
  `0x5e1927beb00c17c341d02483fbe81a884cc45af1ecfb673366e685aca4632ec3`;
- a shard-1 full LevelDB containing state root
  `0x312a34c0254608c59013bc967c486fe036b784e0d2012b266d5fa4ecc2531760`;
- canonical block bodies and cross-shard lookup records through the cutoff if
  you want to independently reproduce pending-receipt classification.

The roots correspond to the blocks in
`manifests/snapshot-2026-09-10.json`.

LevelDB permits only one opener. Stop the Harmony process cleanly or use a
consistent cold filesystem/volume snapshot. Do not copy a live LevelDB
directory file-by-file.

Example variables:

```sh
export SHARD0_DB=/path/to/shard0/harmony_db_0
export SHARD1_DB=/path/to/shard1/harmony_db_1
export OUT=$PWD/artifacts/cutoff-20260910
mkdir -p "$OUT/state" "$OUT/claims"
```

## 5. Export every positive liquid balance

```sh
bin/account-snapshot \
  -db "$SHARD0_DB" \
  -root 0x5e1927beb00c17c341d02483fbe81a884cc45af1ecfb673366e685aca4632ec3 \
  -threshold-atto 1 \
  -output "$OUT/state/shard0-positive-balances.csv" \
  -require-preimages=false \
  > "$OUT/state/shard0-positive-balances-summary.json"

bin/account-snapshot \
  -db "$SHARD1_DB" \
  -root 0x312a34c0254608c59013bc967c486fe036b784e0d2012b266d5fa4ecc2531760 \
  -threshold-atto 1 \
  -output "$OUT/state/shard1-positive-balances.csv" \
  -require-preimages=false \
  > "$OUT/state/shard1-positive-balances-summary.json"
```

`-threshold-atto` controls only scanner statistics. Every positive account is
written to the CSV.

## 6. Export staking claims

Staking state is on shard 0:

```sh
bin/staking-claims \
  -db "$SHARD0_DB" \
  -root 0x5e1927beb00c17c341d02483fbe81a884cc45af1ecfb673366e685aca4632ec3 \
  -discover-validators \
  -output "$OUT/state/staking-claims.csv" \
  -delegations-output "$OUT/state/staked-to-vault-by-delegation.csv" \
  > "$OUT/state/staking-claims-summary.json"
```

`-discover-validators` is important. It discovers wrappers from the selected
root instead of trusting a validator list that may describe a later head.

## 7. Recover missing addresses

If either liquid export has blank addresses, scan one or more independent
preimage databases:

```sh
bin/account-preimage-resolve \
  -input "$OUT/state/shard0-positive-balances.csv" \
  -output "$OUT/state/shard0-positive-balances-resolved.csv" \
  -preimage-db /path/to/another/harmony_db_0
```

Repeat `-preimage-db` for additional databases.

If mappings are still missing, use the bounded canonical recovery commands:

- `canonical-address-resolve`
- `account-transition-locate`
- `transition-trace-resolve`
- `account-nonce-preimage-resolve`
- `address-match-apply`

See `docs/preimage-recovery.md`. Never accept an address unless its
Keccak-256 hash equals the secure key.

## 8. Reproduce pending cross-shard claims

With both full databases available locally:

```sh
bin/cross-shard-supply \
  -shard0-db "$SHARD0_DB" \
  -shard1-db "$SHARD1_DB" \
  -shard0-cutoff 93623067 \
  -shard1-cutoff 95882100 \
  -output "$OUT/cross-shard-supply-cutoff.json"
```

The tool restricts source receipt groups and destination lookups to canonical
blocks at or before their respective cutoffs.

## 9. Merge all claim components

```sh
python3 toolkit/scripts/claims/actual-supply-ledger.py \
  --shard0-liquid "$OUT/state/shard0-positive-balances-resolved.csv" \
  --shard1-liquid "$OUT/state/shard1-positive-balances-resolved.csv" \
  --staking "$OUT/state/staking-claims.csv" \
  --receipts "$OUT/cross-shard-supply-cutoff.json" \
  --output "$OUT/claims/actual-supply-ledger-cutoff.csv" \
  --summary-output "$OUT/claims/actual-supply-ledger-cutoff-summary.json"
```

Format the public all-claims CSV:

```sh
python3 toolkit/scripts/claims/format-all-claims.py \
  --input "$OUT/claims/actual-supply-ledger-cutoff.csv" \
  --output "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  --summary "$OUT/claims/all-address-migration-claims-cutoff-summary.json" \
  --shard0-block 93623067 \
  --shard1-block 95882100 \
  --price-reference-shard0-block 93448483 \
  --price-usd-per-one 0.00074801
```

The price fields are historical display metadata. They do not determine the
1,000 ONE eligibility result.

## 10. Apply the 1,000 ONE policy

The selected comparison is inclusive:

```sh
python3 toolkit/scripts/claims/filter-claims-by-one.py \
  --input "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  --output "$OUT/claims/migration-claims-at-least-1000-one.csv" \
  --summary "$OUT/claims/migration-claims-at-least-1000-one-summary.json" \
  --minimum-one 1000 \
  --comparison ge
```

Create the preliminary code-bearing review set:

```sh
python3 toolkit/scripts/claims/apply-eligibility-policy.py \
  --input "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  --automatic-output "$OUT/claims/migration-claims-automatic-preliminary.csv" \
  --contract-review-output "$OUT/claims/migration-claims-code-bearing-preliminary.csv" \
  --excluded-address-output "$OUT/claims/migration-claims-excluded-preliminary.csv" \
  --summary "$OUT/claims/migration-claims-preliminary-summary.json" \
  --minimum-one 1000 \
  --comparison ge \
  --exclude-address 0x000000000000000000000000000000000000dEaD \
  --exclude-address 0x7bDeF7Bdef7BDeF7BDEf7bDef7bdef7bdeF6E7AD
```

Run the contract-account review described in
`toolkit/scripts/contract-review/README.md`, using the preliminary code-bearing
CSV as its input. The review must produce `validator-accounts.csv` from the two
independent validator-wrapper checks.

Apply the final destination split. Verified validator-wrapper accounts are
allowed in the automatic output; other code-bearing rows remain in genuine
contract review:

```sh
python3 toolkit/scripts/claims/apply-eligibility-policy.py \
  --input "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  --automatic-code-addresses "$OUT/contract-review/out/validator-accounts.csv" \
  --automatic-output "$OUT/claims/migration-claims-automatic.csv" \
  --contract-review-output "$OUT/claims/migration-claims-genuine-contract-review.csv" \
  --excluded-address-output "$OUT/claims/migration-claims-excluded.csv" \
  --summary "$OUT/claims/migration-claims-policy-summary.json" \
  --minimum-one 1000 \
  --comparison ge \
  --exclude-address 0x000000000000000000000000000000000000dEaD \
  --exclude-address 0x7bDeF7Bdef7BDEf7BDEf7bDef7bdef7bdeF6E7AD
```

Verify that the final files are disjoint, cover the threshold set exactly, and
contain no unapproved code-bearing account in the automatic output:

```sh
python3 toolkit/scripts/claims/verify-eligibility-policy.py \
  --input "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  --automatic "$OUT/claims/migration-claims-automatic.csv" \
  --contract-review "$OUT/claims/migration-claims-genuine-contract-review.csv" \
  --excluded-address "$OUT/claims/migration-claims-excluded.csv" \
  --automatic-code-addresses "$OUT/contract-review/out/validator-accounts.csv" \
  --policy-summary "$OUT/claims/migration-claims-policy-summary.json" \
  --output "$OUT/claims/migration-claims-policy.verify.json"
```

As an independent check of the database-derived per-validator delegation file,
export the same records from a Harmony shard-0 archival RPC:

```sh
export SHARD0_RPC='https://your-shard0-archival-rpc.example'

python3 toolkit/scripts/claims/vault-share-ledger-rpc.py \
  --rpc "$SHARD0_RPC" \
  --block 93623067 \
  --output "$OUT/state/staked-to-vault-by-delegation-rpc.csv" \
  --summary "$OUT/state/staked-to-vault-by-delegation-rpc-summary.json"
```

The database and RPC exports must be identical after deterministic sorting:

```sh
python3 toolkit/scripts/claims/verify-vault-delegations.py \
  --database "$OUT/state/staked-to-vault-by-delegation.csv" \
  --rpc "$OUT/state/staked-to-vault-by-delegation-rpc.csv" \
  --output "$OUT/state/staked-to-vault.verify.json"
```

Build the validator deposits, direct wallet payments, and priority/deferred
vault-share ledgers from the database export:

```sh
python3 toolkit/scripts/claims/build-vault-share-allocation.py \
  --all-claims "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  --delegations "$OUT/state/staked-to-vault-by-delegation.csv" \
  --automatic-claims "$OUT/claims/migration-claims-automatic.csv" \
  --contract-review-claims "$OUT/claims/migration-claims-genuine-contract-review.csv" \
  --excluded-claims "$OUT/claims/migration-claims-excluded.csv" \
  --vault-deposits-output "$OUT/claims/validator-vault-deposits.csv" \
  --priority-shares-output "$OUT/claims/priority-vault-shares.csv" \
  --deferred-shares-output "$OUT/claims/deferred-vault-shares.csv" \
  --automatic-wallet-output "$OUT/claims/automatic-wallet-airdrop.csv" \
  --contract-wallet-output "$OUT/claims/contract-wallet-recovery.csv" \
  --excluded-wallet-output "$OUT/claims/excluded-wallet-routing.csv" \
  --summary "$OUT/claims/vault-share-allocation-summary.json" \
  --minimum-one 1000
```

See `docs/eligibility-policy.md`, `docs/claim-routing.md`, and
`docs/contract-account-review.md` before publication.

## 11. Independent verification

Run a second full supply traversal:

```sh
bin/actual-supply \
  -db "$SHARD0_DB" \
  -root 0x5e1927beb00c17c341d02483fbe81a884cc45af1ecfb673366e685aca4632ec3 \
  -output "$OUT/state/actual-supply-cutoff.json" \
  -staking \
  -discover-validators
```

Verify the formatted claims:

```sh
bin/migration-claims-verify \
  -input "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  -require-all-addresses \
  -expected-shard0-block 93623067 \
  -expected-shard1-block 95882100 \
  -expected-price-reference-shard0-block 93448483 \
  -expected-price-usd-per-one 0.00074801
```

The scripts in `toolkit/scripts/cutoff/` provide additional independent checks:

- canonical interval continuity and transaction counts;
- trie-difference versus historical RPC balances and nonces;
- execution-trace evidence for newly created accounts;
- signed difference arithmetic;
- component and aggregate reconciliation.

When the exact released artifact bundle is downloaded under `artifacts/`, run:

```sh
bin/cutoff-final-verifier \
  -artifact-root artifacts/cutoff-20260910 \
  -old-artifact-root artifacts/migration-claims-20260909
```

Expected counts and hashes are documented under `results/` and `manifests/`.
