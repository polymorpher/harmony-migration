# Independent reproduction

These instructions do not require access to the original archive node or any
private network. You must provide your own Harmony databases.

## 1. Requirements

- Linux x86-64 or ARM64
- Go `1.24.2`
- Python `3.12` or newer
- Git, GNU Make, a C/C++ compiler
- GMP and OpenSSL development packages
- enough local storage for the Harmony databases and generated CSVs

On Debian or Ubuntu:

```sh
sudo apt-get update
sudo apt-get install -y build-essential git libgmp-dev libssl-dev python3
```

Use a distribution release whose `python3` package is Python 3.12 or newer,
then verify the interpreter before running the pipeline:

```sh
python3 scripts/check-python-version.py
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
  toolkit/scripts/contract-review/*.py \
  toolkit/scripts/exchanges/*.py \
  toolkit/scripts/routing/*.py \
  toolkit/scripts/forensics/*.py \
  scripts/*.py
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

State-root discovery is the default. The command retains
`-discover-validators` explicitly above so the recorded invocation shows that
it discovers wrappers from the selected root instead of trusting a validator
list that may describe a later head. Use
`-discover-validators=false` only for an intentional current-list comparison.

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

Format the native all-claims CSV:

```sh
python3 toolkit/scripts/claims/format-all-claims.py \
  --input "$OUT/claims/actual-supply-ledger-cutoff.csv" \
  --output "$OUT/claims/all-address-native-claims-cutoff.csv" \
  --summary "$OUT/claims/all-address-native-claims-cutoff-summary.json" \
  --shard0-block 93623067 \
  --shard1-block 95882100 \
  --price-reference-shard0-block 93448483 \
  --price-usd-per-one 0.00074801
```

The price fields are historical display metadata. They do not determine the
1,000 ONE eligibility result.

Resolve missing shard-0 code and nonce metadata once for the complete claim
population. This protects both the prioritized batch and any later claim
portal:

```sh
export SHARD0_RPC='https://your-shard0-archival-rpc.example'

python3 toolkit/scripts/claims/enrich-claim-metadata-rpc.py \
  --input "$OUT/claims/all-address-native-claims-cutoff.csv" \
  --rpc "$SHARD0_RPC" \
  --block 93623067 \
  --output "$OUT/claims/all-address-native-claims-cutoff-metadata.csv" \
  --summary "$OUT/claims/all-address-native-claims-cutoff-metadata-summary.json"
```

Keep these native files as the database-derived accounting record.

Enumerate WONE from the shard-0 archival node and require exact closure to both
WONE `totalSupply()` and the native reserve:

```sh
mkdir -p "$OUT/wone"

bin/wone-holders \
  -rpc "$SHARD0_RPC" \
  -cutoff-block 93623067 \
  -cutoff-hash 0x23572e11f6ef9afe4c27ab3102f15b99fd7277ae5f685ccaef0ae571fe7ee0b6 \
  -cutoff-state-root 0x5e1927beb00c17c341d02483fbe81a884cc45af1ecfb673366e685aca4632ec3 \
  -code-hash 0x940523b11cbb49f28cdb9798f3031179bc9ef4a309dfd82e21281c61aacc3bd8 \
  -output "$OUT/wone/wone-holders-cutoff.csv" \
  -summary "$OUT/wone/wone-holders-cutoff-summary.json"
```

Before applying WONE, normalize the private exchange submissions. This is
required because exchange manual delivery includes every positive WONE
balance in a confirmed inventory regardless of the ordinary wallet threshold:

```sh
python3 toolkit/scripts/exchanges/normalize-exchange-wallets.py \
  --policy exchanges/exchange-policy.json \
  --raw-dir exchanges/wallets-raw \
  --destinations-dir exchanges/destinations \
  --output-dir exchanges/wallets-standardized \
  --summary exchanges/wallets-standardized/summary.json \
  --replace
```

Resolve cutoff metadata for WONE-only addresses that can meet the threshold or
belong to a confirmed exchange inventory, then apply the same verified overlay
to the plain and metadata-complete native ledgers:

```sh
python3 toolkit/scripts/claims/build-wone-new-holder-metadata.py \
  --native-claims "$OUT/claims/all-address-native-claims-cutoff.csv" \
  --wone-holders "$OUT/wone/wone-holders-cutoff.csv" \
  --rpc "$SHARD0_RPC" \
  --block 93623067 \
  --block-hash 0x23572e11f6ef9afe4c27ab3102f15b99fd7277ae5f685ccaef0ae571fe7ee0b6 \
  --state-root 0x5e1927beb00c17c341d02483fbe81a884cc45af1ecfb673366e685aca4632ec3 \
  --minimum-one 1000 \
  --aggregate-delivery-summary exchanges/wallets-standardized/summary.json \
  --output "$OUT/wone/wone-only-qualified-metadata.csv" \
  --summary "$OUT/wone/wone-only-qualified-metadata-summary.json"

python3 toolkit/scripts/claims/apply-wone-qualification.py \
  --native-claims "$OUT/claims/all-address-native-claims-cutoff.csv" \
  --wone-holders "$OUT/wone/wone-holders-cutoff.csv" \
  --wone-summary "$OUT/wone/wone-holders-cutoff-summary.json" \
  --new-holder-metadata "$OUT/wone/wone-only-qualified-metadata.csv" \
  --aggregate-delivery-summary exchanges/wallets-standardized/summary.json \
  --minimum-one 1000 \
  --exclude-address 0x000000000000000000000000000000000000dEaD \
  --exclude-address 0x7bDeF7Bdef7BDeF7BDEf7bDef7bdef7bdeF6E7AD \
  --exclude-address 0x5b18a4e73f9a4fe337a072516b317863ad3046aa \
  --exclude-address 0x905582f21fb9855c809d5b8933272a292dfbb138 \
  --output "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  --summary "$OUT/claims/all-address-migration-claims-cutoff-summary.json"

python3 toolkit/scripts/claims/apply-wone-qualification.py \
  --native-claims "$OUT/claims/all-address-native-claims-cutoff-metadata.csv" \
  --wone-holders "$OUT/wone/wone-holders-cutoff.csv" \
  --wone-summary "$OUT/wone/wone-holders-cutoff-summary.json" \
  --new-holder-metadata "$OUT/wone/wone-only-qualified-metadata.csv" \
  --aggregate-delivery-summary exchanges/wallets-standardized/summary.json \
  --minimum-one 1000 \
  --exclude-address 0x000000000000000000000000000000000000dEaD \
  --exclude-address 0x7bDeF7Bdef7BDeF7BDEf7bDef7bdef7bdeF6E7AD \
  --exclude-address 0x5b18a4e73f9a4fe337a072516b317863ad3046aa \
  --exclude-address 0x905582f21fb9855c809d5b8933272a292dfbb138 \
  --output "$OUT/claims/all-address-migration-claims-cutoff-metadata.csv" \
  --summary "$OUT/claims/all-address-migration-claims-cutoff-metadata-summary.json"
```

The WONE contract is automatically excluded as a recipient. The overlay adds
WONE to ordinary qualifying rows and normalized confirmed exchange rows, then
records the exact reserve amount left not issued.

## 10. Apply the 1,000 ONE policy

The selected comparison is inclusive:

```sh
python3 toolkit/scripts/claims/filter-claims-by-one.py \
  --input "$OUT/claims/all-address-migration-claims-cutoff.csv" \
  --output "$OUT/claims/migration-claims-at-least-1000-one.csv" \
  --summary "$OUT/claims/migration-claims-at-least-1000-one-summary.json" \
  --aggregate-delivery-summary exchanges/wallets-standardized/summary.json \
  --minimum-one 1000 \
  --comparison ge
```

Filter the metadata-complete file with the same threshold:

```sh
python3 toolkit/scripts/claims/filter-claims-by-one.py \
  --input "$OUT/claims/all-address-migration-claims-cutoff-metadata.csv" \
  --output "$OUT/claims/migration-claims-at-least-1000-one-metadata.csv" \
  --summary "$OUT/claims/migration-claims-at-least-1000-one-metadata-filter-summary.json" \
  --aggregate-delivery-summary exchanges/wallets-standardized/summary.json \
  --minimum-one 1000 \
  --comparison ge
```

Scan cutoff-capped account activity from each archival node's local
per-address explorer-node index and verify timestamps against canonical block
headers. This does not use the retiring Explorer website or REST API. Stop the
corresponding node before opening its LevelDB files:

```sh
export SHARD0_EXPLORER_DB=/path/to/shard0/explorer_storage
export SHARD1_EXPLORER_DB=/path/to/shard1/explorer_storage

bin/account-activity \
  -db "$SHARD0_DB" \
  -explorer-db "$SHARD0_EXPLORER_DB" \
  -candidates "$OUT/claims/migration-claims-at-least-1000-one-metadata.csv" \
  -shard 0 \
  -cutoff-block 93623067 \
  -cutoff-hash 0x23572e11f6ef9afe4c27ab3102f15b99fd7277ae5f685ccaef0ae571fe7ee0b6 \
  -output "$OUT/claims/account-activity-shard0.csv" \
  -summary "$OUT/claims/account-activity-shard0-summary.json"

bin/account-activity \
  -db "$SHARD1_DB" \
  -explorer-db "$SHARD1_EXPLORER_DB" \
  -candidates "$OUT/claims/migration-claims-at-least-1000-one-metadata.csv" \
  -shard 1 \
  -cutoff-block 95882100 \
  -cutoff-hash 0xf801577a5480175c05a63c8e4e5cf3d2623e1a01bdf176e16b46967405ae02ee \
  -output "$OUT/claims/account-activity-shard1.csv" \
  -summary "$OUT/claims/account-activity-shard1-summary.json"
```

If a local explorer-node database is unavailable but the archival explorer
node is running, `fetch-account-activity-rpc.py` produces the same per-shard
CSV through Harmony's built-in RPC. It requests history in descending order,
resolves each transaction, and skips stale fork entries after comparing their
block hashes with canonical blocks:

```sh
export SHARD1_RPC='https://your-shard1-archival-rpc.example'

python3 toolkit/scripts/claims/fetch-account-activity-rpc.py \
  --input "$OUT/claims/migration-claims-at-least-1000-one-metadata.csv" \
  --snapshot-manifest manifests/snapshot-2026-09-10.json \
  --rpc "$SHARD1_RPC" \
  --shard 1 \
  --checkpoint "$OUT/claims/account-activity-shard1-rpc-checkpoint.json" \
  --output "$OUT/claims/account-activity-shard1.csv" \
  --summary "$OUT/claims/account-activity-shard1-summary.json"
```

Merge the per-shard ledgers:

```sh
python3 toolkit/scripts/claims/enrich-claim-activity.py \
  --input "$OUT/claims/migration-claims-at-least-1000-one-metadata.csv" \
  --snapshot-manifest manifests/snapshot-2026-09-10.json \
  --shard0-activity "$OUT/claims/account-activity-shard0.csv" \
  --shard0-summary "$OUT/claims/account-activity-shard0-summary.json" \
  --shard1-activity "$OUT/claims/account-activity-shard1.csv" \
  --shard1-summary "$OUT/claims/account-activity-shard1-summary.json" \
  --output "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity.csv" \
  --summary "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity-summary.json"
```

Build the embargoed raw all-address cumulative 3, 6, 12, 24, 36, and 48
calendar-month context. It includes genuine contracts and is not the initial
migration population:

```sh
python3 toolkit/scripts/claims/summarize-claim-activity.py \
  --input "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity.csv" \
  --activity-summary "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity-summary.json" \
  --snapshot-manifest manifests/snapshot-2026-09-10.json \
  --summary artifacts/claim-accounting-20260911/priority-claim-activity-summary.json \
  --report artifacts/claim-accounting-20260911/PRIORITY_CLAIM_ACTIVITY_2026-09-14.md
```

The activity fields are reporting context only. Activity extraction does not
inspect internal EVM traces or validator consensus signatures, and an empty
activity time does not prove that an account was never used.
The wallet-only initial-stage windows are generated later by
`build-migration-stage-policy.py`, after contract identity and exact
non-issuance inputs are available.

Create the preliminary code-bearing review set:

```sh
python3 toolkit/scripts/claims/apply-eligibility-policy.py \
  --input "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity.csv" \
  --automatic-output "$OUT/claims/migration-claims-automatic-preliminary-complete.csv" \
  --contract-review-output "$OUT/claims/migration-claims-code-bearing-complete.csv" \
  --excluded-address-output "$OUT/claims/migration-claims-excluded-preliminary-complete.csv" \
  --summary "$OUT/claims/migration-claims-preliminary-complete-summary.json" \
  --minimum-one 1000 \
  --comparison ge \
  --exclude-address 0x000000000000000000000000000000000000dEaD \
  --exclude-address 0x7bDeF7Bdef7BDeF7BDEf7bDef7bdef7bdeF6E7AD
```

Run the contract-account review described in
`toolkit/scripts/contract-review/README.md`, using the preliminary code-bearing
CSV as its input. The review must produce
`validator-policy-accounts.csv` from the two independent validator-wrapper
checks.

Using the exchange normalization generated before the WONE overlay, bootstrap
the exchange exclusion/manual-route inputs. This first report pass
intentionally omits final policy categories; it is replaced after the final
split. This operator-only step requires the ignored private `exchanges/`
directory and is not available in the public source package:

```sh
python3 toolkit/scripts/routing/init-local-routing.py  # once, in a new clone

python3 toolkit/scripts/exchanges/build-exchange-accounting.py \
  --policy exchanges/exchange-policy.json \
  --normalized-dir exchanges/wallets-standardized \
  --normalization-summary exchanges/wallets-standardized/summary.json \
  --claims "$OUT/claims/all-address-migration-claims-cutoff-metadata.csv" \
  --wone-holders artifacts/wone-holder-accounting-20260917/wone-holders-cutoff-excluding-layerzero.csv \
  --activity "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity.csv" \
  --output-dir artifacts/exchange-accounting-20260917 \
  --routes-output routing/local/exchanges.csv \
  --destinations-output routing/local/exchange-destinations.csv \
  --replace
```

The normalizer records per-exchange inventory, submitted-balance, and
signature-verification statistics. Format-specific checks include exact
multi-shard merge/total reconciliation and cross-verification of any separate
signature proof artifact. Invalid designated signatures fail the run instead
of becoming accepted normalized rows.

Apply the final destination split. Verified validator-wrapper accounts are
allowed in the automatic output; other code-bearing rows remain in genuine
contract review:

```sh
python3 toolkit/scripts/claims/apply-eligibility-policy.py \
  --input "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity.csv" \
  --automatic-code-addresses "$OUT/contract-review/out/validator-policy-accounts.csv" \
  --automatic-output "$OUT/claims/migration-claims-automatic.csv" \
  --contract-review-output "$OUT/claims/migration-claims-genuine-contract-review.csv" \
  --excluded-address-output "$OUT/claims/migration-claims-excluded.csv" \
  --summary "$OUT/claims/migration-claims-policy-summary.json" \
  --minimum-one 1000 \
  --comparison ge \
  --exclude-address 0x000000000000000000000000000000000000dEaD \
  --exclude-address 0x7bDeF7Bdef7BDeF7BDEf7bDef7bdef7bdeF6E7AD \
  --exclude-addresses-file artifacts/exchange-accounting-20260917/qualified-exchange-exclusions.csv
```

Verify that the final files are disjoint, cover the threshold set exactly, and
contain no unapproved code-bearing account in the automatic output:

```sh
python3 toolkit/scripts/claims/verify-eligibility-policy.py \
  --input "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity.csv" \
  --automatic "$OUT/claims/migration-claims-automatic.csv" \
  --contract-review "$OUT/claims/migration-claims-genuine-contract-review.csv" \
  --excluded-address "$OUT/claims/migration-claims-excluded.csv" \
  --automatic-code-addresses "$OUT/contract-review/out/validator-policy-accounts.csv" \
  --exclude-address 0x000000000000000000000000000000000000dEaD \
  --exclude-address 0x7bDeF7Bdef7BDeF7BDEf7bDef7bdef7bdeF6E7AD \
  --exclude-addresses-file artifacts/exchange-accounting-20260917/qualified-exchange-exclusions.csv \
  --policy-summary "$OUT/claims/migration-claims-policy-summary.json" \
  --output "$OUT/claims/migration-claims-policy.verify.json"
```

Build the reviewed non-issuance inventory, then generate the stage policy from
the final category split. The stage builder verifies source hashes, applies
deductions after snapshot qualification, and keeps stage separate from
issuance treatment. The historical-retention input
`artifacts/supply-reconciliation-20260911/not-issued-retained-initial-addresses.csv`
is the `harmony-supply-audit` toolkit's `build-non-issuance-audit.py` export
(SHA-256 `7a5a73648e404f38aaf465a863756c216e4c45489773684bc5526d00e443376e`),
kept as this repository's own pinned copy rather than referenced from another
agent's forensic workspace:

```sh
python3 toolkit/scripts/routing/merge-wallet-theft-inventory.py \
  --historical-inventory artifacts/supply-reconciliation-20260911/treasury-reclaim-inventory.csv \
  --historical-perpetrators artifacts/supply-reconciliation-20260911/reported-wallet-theft-perpetrator-cutoff.csv \
  --additions artifacts/supply-reconciliation-20260911/wallet-theft-inventory-additions-20260916.csv \
  --victims artifacts/supply-reconciliation-20260911/wallet-theft-victim-inventory-20260916.csv \
  --all-claims "$OUT/claims/all-address-migration-claims-cutoff-metadata.csv" \
  --perpetrator-output artifacts/supply-reconciliation-20260911/reported-wallet-theft-perpetrator-related-cutoff.csv \
  --perpetrator-summary artifacts/supply-reconciliation-20260911/reported-wallet-theft-perpetrator-related-cutoff-summary.json \
  --victim-summary artifacts/supply-reconciliation-20260911/wallet-theft-victim-inventory-summary.json \
  --non-issuance-output artifacts/supply-reconciliation-20260911/non-issuance-inventory.csv \
  --non-issuance-summary artifacts/supply-reconciliation-20260911/non-issuance-inventory-summary.json

python3 toolkit/scripts/claims/build-migration-stage-policy.py \
  --qualified-activity "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity.csv" \
  --activity-summary artifacts/claim-accounting-20260911/priority-claim-activity-summary.json \
  --migration-summary "$OUT/claims/all-address-migration-claims-cutoff-summary.json" \
  --contract-review artifacts/contract-review-20260911/out/contract-review-policy.csv \
  --existing-non-issuance artifacts/supply-reconciliation-20260911/non-issuance-inventory.csv \
  --historical-retention artifacts/supply-reconciliation-20260911/not-issued-retained-initial-addresses.csv \
  --manual-wallets artifacts/exchange-accounting-20260917/qualified-exchange-exclusions.csv \
  --output artifacts/migration-policy-20260917/migration-stage-policy.csv \
  --summary artifacts/migration-policy-20260917/migration-stage-summary.json \
  --report artifacts/migration-policy-20260917/MIGRATION_STAGE_POLICY_2026-09-17.md

python3 toolkit/scripts/contract-review/build-report.py \
  --output-dir artifacts/contract-review-20260911/out \
  --claims "$OUT/claims/migration-claims-code-bearing-complete.csv" \
  --all-claims "$OUT/claims/all-address-migration-claims-cutoff-metadata.csv" \
  --cutoff-block 93623067 \
  --migration-stage-summary artifacts/migration-policy-20260917/migration-stage-summary.json \
  --report artifacts/contract-review-20260911/CONTRACT_ACCOUNT_REVIEW_2026-09-11.md
```

Rebuild the exchange reports with the verified final categories and stage
policy. This replaces the bootstrap disposition with the final manual routes
and resolves Gate's delivery tiers from the initial stage:

```sh
python3 toolkit/scripts/exchanges/build-exchange-accounting.py \
  --policy exchanges/exchange-policy.json \
  --normalized-dir exchanges/wallets-standardized \
  --normalization-summary exchanges/wallets-standardized/summary.json \
  --claims "$OUT/claims/all-address-migration-claims-cutoff-metadata.csv" \
  --wone-holders artifacts/wone-holder-accounting-20260917/wone-holders-cutoff-excluding-layerzero.csv \
  --activity "$OUT/claims/migration-claims-at-least-1000-one-metadata-activity.csv" \
  --automatic-claims "$OUT/claims/migration-claims-automatic.csv" \
  --contract-claims "$OUT/claims/migration-claims-genuine-contract-review.csv" \
  --excluded-claims "$OUT/claims/migration-claims-excluded.csv" \
  --migration-stages artifacts/migration-policy-20260917/migration-stage-policy.csv \
  --output-dir artifacts/exchange-accounting-20260917 \
  --routes-output routing/local/exchanges.csv \
  --destinations-output routing/local/exchange-destinations.csv \
  --replace
```

As an independent check of the database-derived per-validator delegation file,
export the same records from a Harmony shard-0 archival RPC:

```sh
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
  --vault-deposits-output "$OUT/claims/base-validator-vault-deposits.csv" \
  --priority-shares-output "$OUT/claims/base-priority-vault-shares.csv" \
  --deferred-shares-output "$OUT/claims/base-deferred-vault-shares.csv" \
  --automatic-wallet-output "$OUT/claims/base-automatic-wallet.csv" \
  --contract-wallet-output "$OUT/claims/base-contract-wallet.csv" \
  --excluded-wallet-output "$OUT/claims/base-excluded-wallet.csv" \
  --summary "$OUT/claims/base-delivery-summary.json" \
  --minimum-one 1000
```

Build exact non-issuance routes from the historical audited inventory, combine
them with locally reviewed manual routes, and apply them to both delivery
paths:

```sh
python3 toolkit/scripts/routing/build-non-issuance-routes.py \
  --inventory artifacts/supply-reconciliation-20260911/non-issuance-inventory.csv \
  --inventory artifacts/supply-reconciliation-20260911/not-issued-retained-initial-addresses.csv \
  --output routing/local/not-issuing.csv \
  --summary routing/local/not-issuing-summary.json

python3 toolkit/scripts/routing/build-wone-routes.py \
  --wone-summary "$OUT/claims/all-address-migration-claims-cutoff-summary.json" \
  --input routing/local/bridge-reserves.base.csv \
  --output routing/local/bridge-reserves.csv \
  --summary routing/local/bridge-reserves-summary.json \
  --replace

python3 toolkit/scripts/routing/build-contract-policy-routes.py \
  --contracts artifacts/contract-review-20260911/out/contract-review-policy.csv \
  --stage-policy artifacts/migration-policy-20260917/migration-stage-policy.csv \
  --output routing/local/contract-policy.csv \
  --summary routing/local/contract-policy-summary.json

python3 toolkit/scripts/routing/apply-routes.py \
  --all-claims "$OUT/claims/all-address-migration-claims-cutoff-metadata.csv" \
  --automatic-claims "$OUT/claims/migration-claims-automatic.csv" \
  --contract-claims "$OUT/claims/migration-claims-genuine-contract-review.csv" \
  --excluded-claims "$OUT/claims/migration-claims-excluded.csv" \
  --priority-shares "$OUT/claims/base-priority-vault-shares.csv" \
  --deferred-shares "$OUT/claims/base-deferred-vault-shares.csv" \
  --base-vault-deposits "$OUT/claims/base-validator-vault-deposits.csv" \
  --validator-accounts artifacts/contract-review-20260911/out/validator-policy-accounts.csv \
  --migration-stages artifacts/migration-policy-20260917/migration-stage-policy.csv \
  --routes routing/local/manual.csv \
  --routes routing/local/multisigs.csv \
  --routes routing/local/lost-wallets.csv \
  --routes routing/local/frozen-wallets.csv \
  --routes routing/local/bridge-reserves.csv \
  --routes routing/local/not-issuing.csv \
  --routes routing/local/exchanges.csv \
  --routes routing/local/contract-policy.csv \
  --destinations routing/local/destinations.csv \
  --destinations routing/local/exchange-destinations.csv \
  --governors routing/local/validator-governors.csv \
  --policy-decisions routing/local/policy-decisions.csv \
  --exceptions-output routing/local/generated/routing-exceptions.csv \
  --governor-exceptions-output routing/local/generated/validator-governor-exceptions.csv \
  --vault-stage-output routing/local/generated/validator-vault-stages.csv \
  --unresolved-output routing/local/generated/unresolved-routing.csv \
  --summary routing/local/generated/routing-summary.json \
  --replace

python3 toolkit/scripts/exchanges/verify-exchange-routing.py \
  --policy exchanges/exchange-policy.json \
  --exchange-summary artifacts/exchange-accounting-20260917/summary.json \
  --audits-dir artifacts/exchange-accounting-20260917/audits \
  --routes routing/local/exchanges.csv \
  --routing-exceptions routing/local/generated/routing-exceptions.csv \
  --routing-summary routing/local/generated/routing-summary.json \
  --output artifacts/exchange-accounting-20260917/routing-verification.json \
  --replace

python3 toolkit/scripts/exchanges/build-exchange-native-policy.py \
  --policy exchanges/exchange-policy.json \
  --audits-dir artifacts/exchange-accounting-20260917/audits \
  --normalization-summary exchanges/wallets-standardized/summary.json \
  --native-claims artifacts/cutoff-20260910/claims/all-address-native-claims-cutoff-metadata.csv \
  --delegations artifacts/cutoff-20260910/state/staked-to-vault-by-delegation-rpc.csv \
  --vaults artifacts/contract-review-20260911/out/base-validator-vault-deposits.csv \
  --gate-supplemental exchanges/wallets-raw/gate-addition.txt \
  --gate-reported-total exchanges/wallets-raw/gate-reported-total.txt \
  --output-dir artifacts/exchange-accounting-20260917 \
  --summary artifacts/exchange-accounting-20260917/exchange-native-summary.json \
  --report artifacts/exchange-accounting-20260917/EXCHANGE_MANUAL_DELIVERY_2026-09-22.md \
  --replace

python3 toolkit/scripts/claims/verify-migration-stage-policy.py \
  --stage-policy artifacts/migration-policy-20260917/migration-stage-policy.csv \
  --stage-summary artifacts/migration-policy-20260917/migration-stage-summary.json \
  --routing-summary routing/local/generated/routing-summary.json \
  --output artifacts/migration-policy-20260917/migration-stage.verify.json \
  --replace

python3 toolkit/scripts/routing/materialize-initial-stage.py \
  --stage-policy artifacts/migration-policy-20260917/migration-stage-policy.csv \
  --base-priority-shares "$OUT/claims/base-priority-vault-shares.csv" \
  --routing-exceptions routing/local/generated/routing-exceptions.csv \
  --routing-summary routing/local/generated/routing-summary.json \
  --vault-stages routing/local/generated/validator-vault-stages.csv \
  --governor-exceptions routing/local/generated/validator-governor-exceptions.csv \
  --wallet-output routing/local/generated/initial-stage/wallet-allocations.csv \
  --shares-output routing/local/generated/initial-stage/vault-shares.csv \
  --vault-output routing/local/generated/initial-stage/validator-vaults.csv \
  --unresolved-output routing/local/generated/initial-stage/unresolved.csv \
  --summary routing/local/generated/initial-stage/summary.json \
  --report artifacts/migration-policy-20260917/INITIAL_STAGE_MATERIALIZATION_2026-09-17.md \
  --replace

python3 toolkit/scripts/claims/build-entitlement-report.py \
  --claims-summary "$OUT/claims/all-address-migration-claims-cutoff-summary.json" \
  --all-metadata-summary "$OUT/claims/all-address-native-claims-cutoff-metadata-summary.json" \
  --all-metadata-ledger "$OUT/claims/all-address-migration-claims-cutoff-metadata.csv" \
  --vault-rpc-summary "$OUT/state/staked-to-vault-by-delegation-rpc-summary.json" \
  --vault-allocation-summary "$OUT/claims/base-delivery-summary.json" \
  --policy-summary "$OUT/claims/migration-claims-policy-summary.json" \
  --routing-summary routing/local/generated/routing-summary.json \
  --output artifacts/claim-accounting-20260911/MIGRATION_CLAIM_DELIVERY_2026-09-11.md \
  --replace

python3 toolkit/scripts/routing/build-wallet-theft-inventory-report.py \
  --perpetrator-summary artifacts/supply-reconciliation-20260911/reported-wallet-theft-perpetrator-related-cutoff-summary.json \
  --victim-summary artifacts/supply-reconciliation-20260911/wallet-theft-victim-inventory-summary.json \
  --additions artifacts/supply-reconciliation-20260911/wallet-theft-inventory-additions-20260916.csv \
  --non-issuance-summary artifacts/supply-reconciliation-20260911/non-issuance-inventory-summary.json \
  --output artifacts/supply-reconciliation-20260911/WALLET_THEFT_INVENTORY_UPDATE_2026-09-16.md

python3 toolkit/scripts/routing/build-non-issuance-report.py \
  --inventory-summary artifacts/supply-reconciliation-20260911/non-issuance-inventory-summary.json \
  --route-summary routing/local/not-issuing-summary.json \
  --contract-policy-summary routing/local/contract-policy-summary.json \
  --routing-summary routing/local/generated/routing-summary.json \
  --output artifacts/supply-reconciliation-20260911/NON_ISSUANCE_POLICY_2026-09-16.md

python3 toolkit/scripts/claims/verify-wone-allocation.py \
  --output artifacts/wone-holder-accounting-20260917/wone-allocation-final-verify.json \
  --replace

python3 toolkit/scripts/claims/build-wone-report.py \
  --holder-scan artifacts/wone-holder-accounting-20260917/wone-holders-cutoff-summary.json \
  --overlay-summary "$OUT/claims/all-address-migration-claims-cutoff-summary.json" \
  --threshold-summary "$OUT/claims/migration-claims-at-least-1000-one-summary.json" \
  --policy-summary artifacts/contract-review-20260911/out/policy-summary.json \
  --prior-policy-summary artifacts/wone-holder-accounting-20260917/pre-wone/contract-review/out/policy-summary.json \
  --routing-summary routing/local/generated/routing-summary.json \
  --verification-summary artifacts/wone-holder-accounting-20260917/wone-allocation-final-verify.json \
  --output artifacts/wone-holder-accounting-20260917/WONE_MIGRATION_INTEGRATION_2026-09-17.md \
  --replace
```

The generated directory contains sparse routing exceptions, separate
validator-governor exceptions, the unresolved work queue, per-validator stage
assets, and a materialized initial-stage plan. The sparse file deliberately
does not repeat ordinary code-less EOA same-address delivery; the materializer
adds those rows and filters every non-initial or non-issued allocation.
Release the initial plan only when `stage_readiness.initial` and the
materializer summary both report `status: ready`. The global routing status is
the conservative all-stage gate. Execute the exchange manual delivery from the
2050 supply reserve only when `stage_readiness.exchange_manual`, exchange
routing verification, and the exchange native summary all report
`ready`/`passed`, using the private manual delivery worksheet. See
`routing/README.md` for the file contracts.

The deployment build must exclude every row with
`destination_status: not_issuing`, `redistributed`, or `exchange_manual`. A
redistributed row is a source offset already represented in WONE holder wallet
rows, not a second destination. An exchange-manual row is delivered by hand
from the 2050 supply reserve and is never airdropped. For a not-issued or
exchange-manual staked row, subtract the same amount from both the validator's
vault deposit and share mint. Verify that issued wallet and vault totals equal
the routing summary's `issuable_*` totals, and that issued plus not-issued plus
redistributed source plus exchange-manual amounts close to the expanded routing
input.

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

As with `staking-claims`, validator discovery from the selected root is the
default when `-staking` is enabled. `-validator-only` intentionally selects the
current validator-list comparison because it does not traverse the state trie;
it therefore requires an explicit `-discover-validators=false`.

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

When the exact released native artifact bundle is downloaded under
`artifacts/`, run its historical cutoff verifier:

```sh
bin/cutoff-final-verifier \
  -artifact-root artifacts/cutoff-20260910 \
  -old-artifact-root artifacts/migration-claims-20260909

python3 toolkit/scripts/claims/build-pre-wone-archive-manifest.py \
  --archive-root artifacts/wone-holder-accounting-20260917/pre-wone \
  --output artifacts/wone-holder-accounting-20260917/pre-wone-manifest.json \
  --check

python3 toolkit/scripts/claims/verify-wone-allocation.py \
  --check-output artifacts/wone-holder-accounting-20260917/wone-allocation-final-verify.json
```

The Go command verifies the immutable native cutoff and historical
USD-difference files. The Python commands verify the preserved native-only
archive map and independently recompute the complete post-WONE qualification
and reserve-routing checks. Expected counts and hashes are documented under
`results/` and `manifests/`.
