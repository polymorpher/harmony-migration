# Transaction archive

The Harmony explorer is being shut down. This pipeline exports a fixed record
of the transactions that moved at least 1 ONE, so that a lookup page (the
`/history` page of harmony-claim-portal) can answer "what happened to this
address" and "what is this transaction" after the explorer and the chain are
gone.

Every fact comes from a Harmony node's own JSON-RPC: block bodies, headers,
receipts and `trace_block`. The explorer website and its API are not data
sources.

## Scope

- Shards 0 and 1, from the first block of 2025-01-01 UTC to the last block of
  each chain.
- Both branches:
  - `canonical`: the chain as finalized after the August 2026 rollback.
  - `rolled_back`: blocks produced after the rollback point that were later
    abandoned. Their transactions never became final and are labeled as such.
- Threshold: value ≥ 1 ONE (`--min-value-atto`, default 10^18). The threshold
  applies to each record: a transaction's value, an internal transfer's value,
  a cross-shard receipt's amount, or a staking amount.

Four record types:

| Table | One row per |
|---|---|
| `transactions.csv` | plain (EVM) transaction with value ≥ 1 ONE, failed ones included with `status = 0` |
| `internal_transfers.csv` | value-moving trace frame below the root: `call`/`callcode` with value, `create` with value, or a `selfdestruct` balance |
| `cross_shard.csv` | outgoing cross-shard receipt, with where the destination shard credited it |
| `staking.csv` | `Delegate`, `Undelegate`, `CreateValidator` amount, or `CollectRewards` reward |

## Runs

A run is one shard and one branch over one block range, read from one node.
Each run directory is produced by four stages:

```sh
export HARMONY_RPC=<node JSON-RPC>             # e.g. http://127.0.0.1:9500
export HARMONY_TRACE_RPC=<node auth RPC>       # trace_block lives on the auth port, e.g. :9501
S=toolkit/scripts/archive

python3 $S/scan-blocks.py --shard 0 --branch canonical \
  --start <first block of 2025> --end <last block> --out runs/s0-canonical \
  --expect <block>:<hash>                     # pin known block hashes (repeatable)
python3 $S/trace-internal.py --scan runs/s0-canonical
python3 $S/attribute-receipts.py --scan runs/s0-canonical
python3 $S/fetch-calldata.py --scan runs/s0-canonical
```

The first three stages are resumable (`--resume`), and all four record their
arguments in a `stage.json`; resuming with different arguments is refused.

- `scan-blocks.py` reads 1,000 blocks per call with `hmyv2_getBlocks`, reads
  the cross-shard receipt roots with `hmyv2_getFullHeader`, and fetches the
  receipt of every exported transaction. It marks a block for tracing when
  it holds a contract call, a creation, a cross-shard precompile call, or a
  value transfer to an address that has code at the node's head. Every task
  checks parent-hash links, and the finished run checks the links between
  tasks, so a run is one connected chain.
- `trace-internal.py` runs `trace_block` (Harmony's native Parity-style
  tracer) over the marked blocks. A transfer is `effective = 1` when neither
  it nor any ancestor frame reverted. Blocks are traced in interleaved order
  (every 20th block first), so `--report-partial` can project totals from an
  unfinished run. A block whose trace fails is listed in `failures.csv` and
  retried on `--resume`; the stage completes only when every marked block is
  traced.
- `attribute-receipts.py` explains every block whose header carries a
  non-empty outgoing receipt root (see below). An unexplained block fails the
  stage.
- `fetch-calldata.py` reads the full input of every exported transaction that
  has calldata (`hmyv2_getTransactionByHash`, or the transaction's block by
  hash when the node has no lookup entry). The input must have the length and
  selector the scan recorded, and the transaction must reproduce its own
  signed hash, which proves the calldata is exactly what was signed. The stage
  is tied to the SHA-256 of the run's `transactions.csv`.

To find the 2025-01-01 boundary of a shard, binary-search block timestamps
(`first_block_at_or_after` in `archive_lib.py`).

### Rolled-back branches

Run the same three stages against a forensic node that still serves the
abandoned branch. Pass `--branch rolled_back`, start one block after the
rollback point, and pin the rollback block with its canonical hash:

```sh
python3 $S/scan-blocks.py --shard 1 --branch rolled_back \
  --start <rollback block + 1> --end <forensic head> --out runs/s1-rolled_back \
  --expect <rollback block>:<canonical hash of that block>
```

The scan refuses to start if the forensic node's block at the rollback height
differs from the canonical hash, and `build-archive.py` refuses a rolled-back
run without that pin. A transaction can appear in both branches if it was
included again after the rollback; both rows are kept.

A node that was itself reverted keeps the abandoned blocks under their hashes
but loses their number-to-hash mapping, so they cannot be read by number.
`find-abandoned-blocks.py` recovers them through the node's own per-address
transaction index (explorer-mode nodes serve `hmyv2_getTransactionsHistory`).
An indexed transaction above the fork whose block hash differs from the
canonical hash at that height lies on an abandoned block. Addresses found in
those blocks are queried again until no new block appears. The script then
walks parent hashes from the highest such block back to the fork block, so
every block in between is listed, empty ones included:

```sh
python3 $S/find-abandoned-blocks.py --fork <rollback block>:<canonical hash> \
  --max-block <highest abandoned block the node stored> \
  --addresses <CSV with a from or to column of addresses active on the abandoned branch> \
  --out runs/s0-rolled_back-discovery
python3 $S/scan-blocks.py --shard 0 --branch rolled_back \
  --block-list runs/s0-rolled_back-discovery/abandoned-blocks.csv \
  --expect <rollback block>:<canonical hash> --step 100 --out runs/s0-rolled_back
python3 $S/trace-internal.py --scan runs/s0-rolled_back
python3 $S/attribute-receipts.py --scan runs/s0-rolled_back
python3 $S/fetch-calldata.py --scan runs/s0-rolled_back
```

In `--block-list` mode the scan reads blocks with `hmyv2_getBlockByHash`,
checks each listed hash and parent hash, and reads receipts per block with
`hmyv2_getBlockReceipts`, because a transaction's lookup may point at its
canonical re-inclusion. Tracing uses `debug_traceBlockByHash` with the
`ParityBlockTracer`. No RPC returns a header by hash, so incoming and outgoing
receipt roots are unknown for these blocks. Outgoing receipts are attributed
from cross-shard transactions and from completed precompile calls in every
traced block. `discovery.json` records how many blocks hold indexed
transactions and the highest listed block. Blocks above it are unreachable
this way.

### Importing an RPC export

When the only node that stored part of a branch can no longer be queried, a
copy exported from its RPC can be imported as a run instead.
`import-rpc-export.py` reads export directories, each holding
`source_manifest.json`, the `hmyv2_getBlocks` responses (`blocks_*.json`), and
`receipts.jsonl`. It writes the same files as `scan-blocks.py`, plus a
`receipts/` stage of transaction-level cross-shard sends and a `calldata/`
stage taken from the export itself:

```sh
python3 $S/import-rpc-export.py --shard 0 --branch rolled_back \
  --export <first export directory> --export <next export directory> \
  --fork <rollback block>:<canonical hash> --start <first block not covered by another run> \
  --compare-run runs/s0-rolled_back \
  --compare-block-list runs/s0-rolled_back-discovery/abandoned-blocks.csv \
  --out runs/s0-rolled_back-export
```

The import checks every manifest-listed file's size and SHA-256. It also
checks that blocks link by parent hash from the fork block through the last
exported block, that each directory's range, boundary hashes and counts match
its manifest, and that every transaction has a receipt from the same block.
Each imported plain transaction must reproduce its own Harmony or
Ethereum-style hash from its signed fields, which proves its value, recipient
and calldata. That check matters because an RPC gateway that parses JSON
numbers as floating point rounds large values. One export captured through
such a gateway reported 5000000522000000000000000000 for a transaction that
signed 5000000522000000533215051776.

`--compare-run` checks the overlap with a run made from a node: block hashes
(segments, rows, and the block list), transaction rows and staking rows must
agree. A value counts as rounded in the export, not as a mismatch, only when
the run's value reproduces the signed hash and the export's does not. The
import records `internal_unavailable`, because an export has no traces.
`build-archive.py` accepts such a run without an `internal/` stage, records
the reason in the manifest, and requires a run that starts right after
another run of the same shard and branch to link to it by parent hash.

Replaying an abandoned block also re-verifies its parent's committee
signature against the committee the node holds for that epoch. Where the
abandoned branch's signers differ, the node rejects the replay ("Unable to
verify aggregated signature"). Such blocks keep their transactions and staking
rows, but their internal transfers cannot be traced. Pass
`--allow-untraceable` to complete the stage anyway; the blocks and the
normalized errors are listed in `internal/stage.json` and counted as
`blocks_untraced` in the manifest.

## Cross-shard receipts

A cross-shard transfer debits the source shard and creates a receipt that the
destination shard credits later. Three source mechanisms exist:

- `native`: a transaction whose `toShardID` differs from its `shardID`.
- `precompile`: a completed call to the cross-shard precompile
  `0x00000000000000000000000000000000000000f9`, either as the transaction
  itself or from inside a contract. Before epoch 2964
  (`cx-receipt-state-rollback`), a receipt survived a revert of an ancestor
  frame. A contract could therefore `CREATE` a child whose constructor called
  the precompile, revert the child, and refund the sender, while the receipt
  still credited the destination shard. Those rows have
  `ancestor_reverted = 1`; the shard-0 transfer looks failed, but the
  destination credit happened.
- `precompile_replay_incompatible`: the header proves a receipt, but the
  current node binary replays the call as failed (Harmony `31752f21aa` changed
  the contract context of precompiles reached by `DELEGATECALL`). The amount,
  recipient and destination shard are decoded from the attempted call.

No RPC method lists a block's incoming receipts. Map each source shard's
receipts to their destination instead:

```sh
python3 $S/map-receipt-destinations.py --dest-shard 1 --dest-rpc <shard-1 node> \
  --receipts runs/s0-canonical/receipts/outgoing-receipts.csv \
  --dest-scan runs/s1-canonical \
  --output runs/destinations-0-to-1.csv --summary runs/destinations-0-to-1.json
```

`hmyv2_getCXReceiptByHash` on the destination node answers from that node's
own receipt index. A precompile receipt is keyed by `tx.HashByType()`, which
is the Ethereum-style hash when the transaction came from an Ethereum wallet,
so the lookup tries the Harmony hash and then the Ethereum-style hash. Amount,
recipient and shard must match the source receipt.
With `--dest-scan`, every destination block whose header shows incoming
receipts must be matched by a mapped receipt, so a missing source shard or
range fails the stage.

## Build

```sh
make tx-archive ARCHIVE_OUT=<dir> \
  ARCHIVE_RUNS="runs/s0-canonical runs/s1-canonical runs/s0-rolled_back runs/s1-rolled_back" \
  ARCHIVE_DESTINATIONS="runs/destinations-0-to-1.csv runs/destinations-1-to-0.csv" \
  ARCHIVE_NOTES=<file with one coverage note per line>
```

`build-archive.py` requires completed stages, the same minimum for every run,
no overlapping runs per shard and branch, and the rollback pin on rolled-back
runs. It joins each run's `calldata/` stage into `transactions.csv` and
refuses a run without one unless `--allow-missing-calldata` is passed. It
removes duplicate rows left by an interrupted stage, sorts each
table, and writes `manifest.json`: every run's range, boundary hashes, node
host (no path or credentials), stage counters, coverage notes, and each
table's row count and SHA-256.

`resolve-method-names.py` names the distinct call selectors (not the leading
bytes of contract-creation code) from the OpenChain signature database, with
4byte.directory as fallback, and writes `method_names.csv`
(`selector, signature, verified, candidates, source`) into the archive and its
manifest. A selector can match several signatures; one seen in a verified
contract is preferred, and `verified = 0` marks a name no verified contract
uses. The name is a label, not a decoding proof.

`summarize-archive.py` writes `summary.json` and
`SUMMARY.md` with row counts per shard and branch, monthly counts, the value
distribution, distinct addresses, and CSV, gzip and zstd sizes.

### Columns

All addresses are lowercase `0x` hex, amounts are integer atto-ONE, and
timestamps are Unix seconds (UTC).

- `transactions.csv`: `shard, branch, kind, block_number, block_hash,
  timestamp, tx_index, tx_hash, eth_hash, from, to, value_atto, fee_atto,
  status, to_shard, method, input`. `kind` is `transfer`, `call`, `create`,
  `xshard_native` or `xshard_precompile`. For a precompile call, `to` and
  `to_shard` are the decoded recipient and shard. `method` is the 4-byte
  selector (shorter only when the calldata is) and `input` the full calldata,
  or for `create` the creation code. `eth_hash` is the Ethereum-style hash
  that wallets such as MetaMask show.
- `internal_transfers.csv`: `shard, branch, block_number, block_hash,
  timestamp, tx_index, tx_hash, eth_hash, trace_address, type, from, to,
  value_atto, effective`. `tx_hash` and `eth_hash` identify the parent
  transaction, whose own value may be below the threshold.
- `cross_shard.csv`: `branch, from_shard, to_shard, mechanism, block_number,
  block_hash, timestamp, tx_index, tx_hash, from, to, value_atto,
  ancestor_reverted, eth_hash, dest_status, dest_block_number,
  dest_block_hash, dest_timestamp`. `dest_status` is `credited`, `not_found`,
  `mismatch`, or `unmapped` when no destination mapping was supplied.
- `staking.csv`: `shard, branch, type, block_number, block_hash, timestamp,
  tx_index, tx_hash, delegator, validator, amount_atto, status`.

## Limits

- Shard-1 blocks after the last block a node serves are covered only by a
  coverage note. A note must say how emptiness was established, for example
  identical state roots at both ends of the range, which rules out any
  included transaction or credited receipt.
- A value transfer to a contract that later self-destructed is not marked for
  tracing, because code is checked at the node's head.
- Staking-precompile calls made by contracts (liquid staking) move stake
  through precompile arguments, not call value, so they appear only as the
  contract call itself.
- The archive is chain data, not claim results. It is not covered by the
  numerical embargo, but the output directory stays under the ignored
  `artifacts/` tree and is published as a release asset.
