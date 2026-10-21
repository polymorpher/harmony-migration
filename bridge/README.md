# Bridge collateral pipeline

Inputs and configuration for the bridge-collateral allocation pipeline. The
method is in [`../docs/bridge-allocations.md`](../docs/bridge-allocations.md).

## Files

- `routes.json` — public registry of every Harmony LayerZero bridge pool,
  derived from the bridge frontend configuration at the commit recorded in
  `sources`, plus system addresses (endpoints, Horizon managers and
  multisigs, the bridge deployer).
- `config.example.json` — snapshot times, pinned Harmony blocks, per-chain RPC
  variable names, custody multisigs, the ONE claim token and reserve Safe,
  and policies.
- `local/` — ignored. Put the real `config.json`, RPC settings, exclusion and
  override files here.

## Registry fields

Each pool in `routes.json` has:

- `kind` (`remote_escrow` or `harmony_origin`) and `claim_asset` (`escrow`,
  `one`, or `unsupported`);
- `remote_adapter` and `remote_token` (`native` for ETH or BNB);
- `harmony`: every Harmony proxy that carried the pool, each with the wrapped
  token it mints; earlier proxies are marked `historical_peer`;
- `previous_remote_adapters`: earlier remote escrows for the same asset;
- `ignored_peers` and `ignored_remote_peers`: addresses an adapter or proxy
  once trusted that carried no messages or belong to another asset, each with
  the reason.

## Configuration

| Field | Meaning |
|---|---|
| `times.rollback`, `times.cutoff` | UTC times; each chain resolves the last block at or before them |
| `chains.<chain>.rpc_env` | environment variable holding an archive JSON-RPC URL |
| `chains.<chain>.pinned_blocks` | blocks that must be verified rather than searched (Harmony) |
| `chains.<chain>.custody_multisig` | where escrowed collateral on that chain moves; `null` until one exists |
| `chains.<chain>.rpc_settings` | optional `workers`, `batch_size`, `min_interval` for slow providers, and `log_range` (blocks per `eth_getLogs` when reading trusted-remote history) |
| `chains.<chain>.holder_scan` | optional `range_size` and `workers` for `token-holders` |
| `custody_requirements` | owner count and threshold every custody Safe must have |
| `one_claims.claim_token` | Ethereum ERC-20 ONE paid to bridged-ONE holders |
| `one_claims.reserve_safe` | supply reserve Safe that funds the ONE claims |
| `one_claims.custody_multisig` | ONE custody; defaults to the claim chain's custody multisig |
| `snapshot_policy.remote_holder_overrides` | force `rollback` or `cutoff` for a remote chain's ONE holders |
| `allocation_policy.minimum_claim_raw` | smallest allocation paid; smaller ones stay in custody |
| `allocation_policy.minimum_claim_raw_by_pool` | per-pool minimum |
| `allocation_policy.orphaned_payouts` | `debit_recipient` or `pool_loss` for escrow payouts of restored burns |

The example uses the existing bridge admin Safes on Ethereum and BNB Chain.
Arbitrum, Base, and Linea escrows are owned by a single key and have no admin
Safe yet, so their custody multisig is `null` until one is deployed.

## Running

Build the toolkit (`./scripts/build-toolkit.sh`) for `bin/token-holders`, then:

```sh
cp bridge/config.example.json bridge/local/config.json
export HARMONY_RPC=... ETHEREUM_RPC=... BSC_RPC=... ARBITRUM_RPC=... BASE_RPC=... LINEA_RPC=...
OUT=artifacts/bridge-allocations-YYYYMMDD
CONFIG=bridge/local/config.json

python3 toolkit/scripts/bridge/resolve-snapshot-blocks.py \
  --routes bridge/routes.json --config $CONFIG \
  --output $OUT/snapshot-blocks.json

python3 toolkit/scripts/bridge/fetch-bridge-state.py \
  --routes bridge/routes.json --config $CONFIG \
  --blocks $OUT/snapshot-blocks.json \
  --output $OUT/bridge-state.json

python3 toolkit/scripts/bridge/check-harmony-peers.py \
  --routes bridge/routes.json --config $CONFIG \
  --blocks $OUT/snapshot-blocks.json --output $OUT/harmony-peers.json

python3 toolkit/scripts/bridge/build-snapshot-plan.py \
  --routes bridge/routes.json --config $CONFIG \
  --state $OUT/bridge-state.json --output $OUT/snapshot-plan.json

python3 toolkit/scripts/bridge/scan-bridge-messages.py \
  --routes bridge/routes.json --config $CONFIG \
  --state $OUT/bridge-state.json --plan $OUT/snapshot-plan.json \
  --output $OUT/bridge-messages.csv --summary $OUT/bridge-messages-summary.json

python3 toolkit/scripts/bridge/run-holder-snapshots.py \
  --routes bridge/routes.json --config $CONFIG \
  --plan $OUT/snapshot-plan.json --holders-dir $OUT/holders

python3 toolkit/scripts/bridge/classify-holders.py \
  --routes bridge/routes.json --config $CONFIG \
  --plan $OUT/snapshot-plan.json --holders-dir $OUT/holders \
  --contract-review artifacts/contract-review-20260911/out/contract-review-all.csv \
  --output $OUT/holder-classes.csv --summary $OUT/holder-classes-summary.json

make bridge-allocations BRIDGE_OUT=$OUT BRIDGE_CONFIG=$CONFIG
```

`fetch-bridge-state.py` reads each remote chain at its current head unless
`--state-block CHAIN=NUMBER` pins it; pin the same blocks when rerunning.
Use archive JSON-RPC for every chain: the Harmony archival node for
`HARMONY_RPC` (its local RPC, directly on the node or through an SSH tunnel),
and the paid archive RPC provider for the other chains. Etherscan's API is a
fallback only when RPC cannot answer. Never use the Harmony explorer (website
or API) or any explorer website as a data source.

Snapshots on the same chain and block are replayed together: `token-holders
-tokens A,B,...` filters `eth_getLogs` on every token at once, so the range
scan is paid once per chain rather than once per token.

Harmony RPC limits `eth_getLogs` to ranges below 1,024 blocks, so a Harmony
replay makes close to a hundred thousand requests. Run it on the archival node
itself against the node's local RPC (`-range-size 1000`), for example:

```sh
token-holders -rpc http://127.0.0.1:9500 -chain-id 1666600000 \
  -tokens <comma-separated Harmony tokens from the plan> \
  -block <Harmony cutoff block> -block-hash <its hash> -range-size 1000 \
  -output-dir holders -name-prefix harmony
```

then copy the holder files and summaries into `$OUT/holders`;
`run-holder-snapshots.py` keeps them because they match the plan.

`run-holder-snapshots.py` resumes: snapshots whose files and summary already
match the plan are kept. `--from-block CHAIN=BLOCK` skips the deployment
search. `import-holder-snapshot.py` converts a holder list reconstructed from
archive RPC at a nearby block (`--list-block`) to a planned snapshot by
replaying the `Transfer` events in between, then applies the same
`totalSupply` and `balanceOf` checks.

`classify-holders.py --previous` reuses an earlier classification.

Apply the migration's non-issuance decisions before building:

```sh
S=artifacts/supply-reconciliation-20260911
python3 toolkit/scripts/bridge/build-migration-exclusions.py \
  --non-issuance $S/non-issuance-inventory.csv \
  --theft $S/reported-wallet-theft-perpetrator-cutoff.csv \
  --theft $S/reported-wallet-theft-perpetrator-related-cutoff.csv \
  --theft $S/wallet-theft-inventory-additions-20260916.csv \
  --burn-audit $S/burn-address-audit.json \
  --inaccessible $S/inaccessible-address-inventory-20260923.csv \
  --retained-initial $S/not-issued-retained-initial-addresses.csv \
  --revert-leak $S/not-issued-revert-leak-recipients.csv \
  --treasury-reclaim $S/treasury-reclaim-inventory.csv \
  --output bridge/local/exclusions.csv --summary bridge/local/exclusions-summary.json
```

and pass `BRIDGE_EXCLUSIONS=bridge/local/exclusions.csv` to `make bridge-allocations`.

`make bridge-allocations` runs `build-bridge-allocations.py`,
`verify-bridge-allocations.py`, and `build-summary-report.py`, which writes the
per-chain and per-pool overview `allocations/BRIDGE_ALLOCATION_SUMMARY.md`.

For USD values, snapshot prices first; the summary picks up
`$OUT/prices.json` when it exists:

```sh
python3 toolkit/scripts/bridge/fetch-prices.py \
  --routes bridge/routes.json --config $CONFIG --output $OUT/prices.json
```

`pricing.stablecoins` lists the USD stablecoins valued at par. Every other
asset is matched to the CoinMarketCap listing whose platform contracts include
the escrowed token on that chain; `pricing.cmc_ids` pins an id (with a `note`)
where a token was renamed or migrated. Assets without a CoinMarketCap quote are
reported as unpriced and left out of USD totals.
Pass `BRIDGE_EXCLUSIONS` and
`BRIDGE_OVERRIDES` to apply reviewed exclusion and override files.

## After moving collateral

Rerun `fetch-bridge-state.py` with the post-transfer blocks pinned as
`--state-block` into a separate file and compare each custody multisig's
balances in `custody` with `custody-plan.json`.
