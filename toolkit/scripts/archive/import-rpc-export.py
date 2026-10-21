#!/usr/bin/env python3
"""Import a block-range export captured from a Harmony node's RPC as a transaction-archive run.

For abandoned-branch blocks that only a forensic node stored, when that node can
no longer be queried but an export of it exists. Each --export directory holds:

  source_manifest.json   blockRange (start, end, firstHash, lastHash), counts
                         (regularTransactions, stakingTransactions, receipts), and
                         files (path, bytes, sha256)
  blocks_*.json          JSON-RPC responses of hmyv2_getBlocks with full regular and
                         staking transactions
  receipts.jsonl         one hmyv2 receipt per line, regular and staking

Checks before anything is written:
  - every manifest-listed file has its recorded size and SHA-256;
  - blocks are consecutive and parent-linked from the --fork block through the
    last exported block, across all directories, and each directory's range,
    first/last hash and counts equal its manifest;
  - --expect pins further blocks;
  - every exported transaction has exactly one receipt from the same block;
  - every imported plain transaction reproduces its own hash from its signed
    fields, so its value, recipient and calldata are the signed ones (an RPC
    gateway that passes numbers through floating point rounds large values).

Writes the scan-blocks.py files (transactions.csv, staking.csv, trace-blocks.csv,
xshard-blocks.csv, segments.csv, stage.json) for blocks --start..--end, and
receipts/ with the transaction-level cross-shard sends. The export has no traces
and no receipt roots, so internal transfers are recorded as unavailable and a
precompile call made inside a contract cannot be seen.

--compare-run checks the overlap with an existing run of the same shard and
branch: the block hashes in its segments, its rows and an optional
--compare-block-list, and its transaction and staking rows, must all match the
export. A value that differs only because the export's copy fails the signed-hash
check while the run's passes is reported as rounded in the export, not as a
mismatch.
"""

import argparse
import glob
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402

UNAVAILABLE = "the export has no traces; internal transfers were not recorded"
TX_COMPARE = ("block_hash", "value_atto", "status", "gas_used", "to", "kind")
STAKING_COMPARE = ("block_hash", "amount_atto", "status", "type")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", action="append", required=True, help="export directory (repeatable, in order)")
    parser.add_argument("--shard", type=int, required=True, choices=(0, 1))
    parser.add_argument("--branch", required=True, choices=lib.BRANCHES)
    parser.add_argument("--fork", required=True, metavar="NUMBER:HASH",
                        help="block the export attaches to (the canonical rollback block for a rolled-back export)")
    parser.add_argument("--start", type=int, help="first block to import (default: first exported block)")
    parser.add_argument("--end", type=int, help="last block to import (default: last exported block)")
    parser.add_argument("--expect", action="append", default=[], metavar="NUMBER:HASH")
    parser.add_argument("--min-value-atto", type=int, default=lib.ONE)
    parser.add_argument("--compare-run", help="existing run whose overlap with the export must agree")
    parser.add_argument("--compare-block-list", help="block list (block_number, block_hash) of the compared run")
    parser.add_argument("--out", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def verify_manifest(directory):
    manifest = lib.load_json(directory / "source_manifest.json")
    for entry in manifest["files"]:
        path = directory / entry["path"]
        if path.stat().st_size != entry["bytes"] or lib.file_sha256(path) != entry["sha256"]:
            raise SystemExit(f"{path}: size or SHA-256 differs from source_manifest.json")
    return manifest


def load_receipts(directory, index):
    """(block hash, status, gas used, CollectRewards reward) by transaction hash."""
    with open(directory / "receipts.jsonl", encoding="utf-8") as source:
        for line in source:
            receipt = json.loads(line)
            tx_hash = receipt["transactionHash"]
            if tx_hash in index:
                raise SystemExit(f"duplicate receipt for {tx_hash}")
            reward = lib.staking_amount({"type": "CollectRewards", "msg": {}}, receipt)
            index[tx_hash] = (receipt["blockHash"].lower(), lib.integer(receipt["status"]),
                              lib.integer(receipt["gasUsed"]), reward)


def export_blocks(directory):
    files = sorted(glob.glob(str(directory / "blocks_*.json")), key=lambda p: int(Path(p).stem.split("_")[1]))
    for path in files:
        response = lib.load_json(path)
        if response.get("error") or not isinstance(response.get("result"), list):
            raise SystemExit(f"{path}: not a successful hmyv2_getBlocks response")
        yield from response["result"]


class Collector:
    """Archive rows of a block range, in scan-blocks.py's formats."""

    def __init__(self, shard, min_value, receipts, require_signed_values):
        self.shard, self.min_value, self.receipts = shard, min_value, receipts
        self.require_signed_values = require_signed_values
        self.rows = {"tx": [], "staking": [], "trace": [], "xshard": [], "receipts": []}
        self.counters, self.months, self.segments = Counter(), Counter(), []
        self.signed = {}
        self.first = self.last = None

    def add(self, block, number, block_hash):
        timestamp = lib.integer(block["timestamp"])
        if self.first is None:
            self.first = {"block": number, "hash": block_hash, "parent_hash": block["parentHash"].lower(),
                          "timestamp": timestamp, "time_utc": lib.utc(timestamp)}
        self.last = {"block": number, "hash": block_hash, "timestamp": timestamp, "time_utc": lib.utc(timestamp)}
        if not self.segments or self.segments[-1]["end"] - self.segments[-1]["start"] == 999:
            self.segments.append({"start": number, "end": number, "first_parent_hash": block["parentHash"].lower(),
                                  "first_hash": block_hash, "last_hash": block_hash})
        self.segments[-1].update(end=number, last_hash=block_hash)
        counters, rows = self.counters, self.rows
        counters["blocks"] += 1
        txs = block.get("transactions") or []
        stakings = block.get("stakingTransactions") or []
        counters["blocks_with_transactions"] += bool(txs or stakings)
        call_txs = 0
        for tx in txs:
            counters["tx_total"] += 1
            value = lib.integer(tx["value"])
            to = lib.address(tx.get("to"))
            data = tx.get("input") or "0x"
            kind = lib.classify_tx(tx, to)
            counters[f"tx_kind_{kind}"] += 1
            call_txs += kind in ("call", "create", "xshard_precompile")
            counters["tx_value_gt0"] += value > 0
            if value < self.min_value:
                continue
            _, status, gas_used, _ = self.receipts[tx["hash"]]
            self.signed[tx["hash"]] = tx
            if lib.signed_value_matches(tx, value):
                counters["tx_ge_min_signature_verified"] += 1
            elif self.require_signed_values:
                raise SystemExit(f"transaction {tx['hash']}: exported value {value} does not reproduce its hash")
            counters["tx_ge_min"] += 1
            counters[f"tx_ge_min_kind_{kind}"] += 1
            counters["tx_ge_min_failed"] += status != 1
            self.months[datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")] += 1
            to_out, to_shard = to, lib.integer(tx["toShardID"])
            if kind == "xshard_precompile":
                decoded = lib.decode_xshard_input(data)
                if decoded:
                    to_out, to_shard = decoded["to"], decoded["to_shard"]
            rows["tx"].append({
                "kind": kind, "block_number": number, "block_hash": block_hash, "timestamp": timestamp,
                "tx_index": lib.integer(tx["transactionIndex"]), "tx_hash": tx["hash"],
                "eth_hash": tx.get("ethHash", ""), "from": lib.address(tx["from"]), "to": to_out,
                "value_atto": value, "shard_id": lib.integer(tx["shardID"]), "to_shard_id": to_shard,
                "status": status, "gas_used": gas_used, "gas_price": lib.integer(tx["gasPrice"]),
                "input_selector": data[:10] if data != "0x" else "", "input_bytes": (len(data) - 2) // 2,
            })
        for tx in stakings:
            counters["staking_total"] += 1
            counters[f"staking_type_{tx.get('type', '')}"] += 1
            _, status, gas_used, reward = self.receipts[tx["hash"]]
            msg = tx.get("msg") or {}
            if msg.get("amount") is not None:
                amount = lib.integer(msg["amount"])
            else:
                amount = reward if tx.get("type") == "CollectRewards" else None
            if amount is None or amount < self.min_value:
                continue
            counters[f"staking_ge_min_type_{tx.get('type', '')}"] += 1
            rows["staking"].append({
                "type": tx.get("type", ""), "block_number": number, "block_hash": block_hash,
                "timestamp": timestamp, "tx_index": lib.integer(tx["transactionIndex"]), "tx_hash": tx["hash"],
                "from": lib.address(tx["from"]), "validator": lib.address(msg.get("validatorAddress")),
                "amount_atto": amount, "status": status, "gas_used": gas_used,
                "gas_price": lib.integer(tx["gasPrice"]),
            })
        if call_txs:
            rows["trace"].append({"block_number": number, "tx_count": len(txs), "call_tx_count": call_txs,
                                  "reason": "call", "block_hash": block_hash})
            counters["trace_blocks"] += 1
        sends, _ = lib.outgoing_receipts(block, None, self.shard)
        sends = [row for row in sends if self.receipts[row["tx_hash"]][1] == 1]
        if sends:
            rows["xshard"].append({"block_number": number, "block_hash": block_hash, "timestamp": timestamp,
                                   "epoch": lib.integer(block["epoch"]), "incoming": 0, "outgoing": 1})
            counters["blocks_outgoing_receipts"] += 1
            rows["receipts"] += sends


def compare(run, block_list, hashes, overlap):
    """Mismatches between the export and an existing run over their overlap."""
    identity = lib.load_json(run / "stage.json")["identity"]
    low, high = identity["start"], identity["end"]
    checked, mismatches = Counter(), []

    def check_hash(number, value, where):
        if low <= number <= high and number in hashes:
            checked[where] += 1
            if hashes[number] != value.lower():
                mismatches.append(f"{where}: block {number}")

    for segment in lib.iter_csv(run / "segments.csv"):
        check_hash(int(segment["start"]), segment["first_hash"], "segments")
        check_hash(int(segment["end"]), segment["last_hash"], "segments")
    if block_list:
        for row in lib.iter_csv(block_list):
            check_hash(int(row["block_number"]), row["block_hash"], "block list")
    for name, kind, fields in (("transactions.csv", "tx", TX_COMPARE), ("staking.csv", "staking", STAKING_COMPARE)):
        theirs = {}
        for row in lib.iter_csv(run / name):
            check_hash(int(row["block_number"]), row["block_hash"], name)
            theirs[row["tx_hash"]] = tuple(str(row[f]).lower() for f in fields)
        mine = {row["tx_hash"]: tuple(str(row[f]).lower() for f in fields) for row in overlap.rows[kind]}
        checked[f"{name} rows"] = len(theirs)
        for tx_hash in sorted(set(theirs) | set(mine)):
            if theirs.get(tx_hash) == mine.get(tx_hash):
                continue
            if kind == "tx" and rounded_in_export(theirs.get(tx_hash), mine.get(tx_hash), overlap.signed.get(tx_hash)):
                checked["values rounded in the export (run value matches the signature)"] += 1
                continue
            mismatches.append(f"{name}: {tx_hash}")
    return {"run": run.name, "overlap": [overlap.first["block"], overlap.last["block"]],
            "checked": dict(checked), "mismatches": mismatches}


def rounded_in_export(theirs, mine, tx):
    """True when only the value differs and the run's value, not the export's, is the signed one."""
    value_index = TX_COMPARE.index("value_atto")
    if theirs is None or mine is None or tx is None:
        return False
    if theirs[:value_index] + theirs[value_index + 1:] != mine[:value_index] + mine[value_index + 1:]:
        return False
    return lib.signed_value_matches(tx, int(theirs[value_index])) and not lib.signed_value_matches(
        tx, int(mine[value_index]))


def main():
    args = parse_args()
    out = Path(args.out)
    if (out / "stage.json").exists() and not args.replace:
        raise SystemExit(f"{out} exists; pass --replace")
    fork_number, fork_hash = lib.parse_block_pin(args.fork)
    pins = dict(lib.parse_block_pin(text) for text in args.expect)
    exports = [Path(path) for path in args.export]
    manifests = [verify_manifest(directory) for directory in exports]
    print(f"verified {sum(len(m['files']) for m in manifests)} manifest-listed files", flush=True)
    receipts = {}
    for directory in exports:
        load_receipts(directory, receipts)

    first, last = manifests[0]["blockRange"]["start"], manifests[-1]["blockRange"]["end"]
    start = first if args.start is None else args.start
    end = last if args.end is None else args.end
    if not first <= start <= end <= last:
        raise SystemExit(f"--start/--end must lie within the export {first}-{last}")
    compare_range = None
    if args.compare_run:
        identity = lib.load_json(Path(args.compare_run) / "stage.json")["identity"]
        if (identity["shard"], identity["branch"]) != (args.shard, args.branch):
            raise SystemExit("--compare-run is a different shard or branch")
        compare_range = (identity["start"], identity["end"])

    imported = Collector(args.shard, args.min_value_atto, receipts, require_signed_values=True)
    overlap = Collector(args.shard, args.min_value_atto, receipts, require_signed_values=False)
    hashes = {fork_number: fork_hash}
    previous_hash, expected_number = fork_hash, fork_number + 1
    for directory, manifest in zip(exports, manifests):
        block_range, count = manifest["blockRange"], Counter()
        for block in export_blocks(directory):
            number = lib.integer(block["number"])
            block_hash = block["hash"].lower()
            if number != expected_number or block["parentHash"].lower() != previous_hash:
                raise SystemExit(f"{directory}: block {number} does not follow block {expected_number - 1}")
            if number in pins and pins[number] != block_hash:
                raise SystemExit(f"block {number} is {block_hash}, expected {pins[number]}")
            if number == block_range["start"] and block_hash != block_range["firstHash"].lower():
                raise SystemExit(f"{directory}: first hash differs from its manifest")
            hashes[number] = block_hash
            previous_hash, expected_number = block_hash, number + 1
            txs = block.get("transactions") or []
            stakings = block.get("stakingTransactions") or []
            count["regular"] += len(txs)
            count["staking"] += len(stakings)
            for tx in txs + stakings:
                found = receipts.get(tx["hash"])
                if found is None or found[0] != block_hash:
                    raise SystemExit(f"transaction {tx['hash']} has no receipt from block {number}")
            if start <= number <= end:
                imported.add(block, number, block_hash)
            if compare_range and compare_range[0] <= number <= compare_range[1]:
                overlap.add(block, number, block_hash)
        if (expected_number - 1 != block_range["end"] or previous_hash != block_range["lastHash"].lower()
                or count["regular"] != manifest["counts"]["regularTransactions"]
                or count["staking"] != manifest["counts"]["stakingTransactions"]
                or count["regular"] + count["staking"] != manifest["counts"]["receipts"]):
            raise SystemExit(f"{directory}: range, last hash or counts differ from its manifest")
    if len(receipts) != sum(m["counts"]["receipts"] for m in manifests):
        raise SystemExit("receipts.jsonl holds receipts for transactions that are not in the blocks")
    unmatched_pins = [n for n in pins if n not in hashes]
    if unmatched_pins:
        raise SystemExit(f"--expect blocks outside the export: {unmatched_pins}")

    comparison = None
    if compare_range:
        comparison = compare(Path(args.compare_run), args.compare_block_list, hashes, overlap)
        if comparison["mismatches"]:
            raise SystemExit(f"export disagrees with {args.compare_run}: {comparison['mismatches'][:10]}")
        print(f"overlap {comparison['overlap']} agrees with {args.compare_run}: {comparison['checked']}", flush=True)

    out.mkdir(parents=True, exist_ok=True)
    (out / "receipts").mkdir(exist_ok=True)
    anchors = {fork_number: fork_hash, **pins, start - 1: hashes[start - 1]}
    identity = {
        "stage": "import-rpc-export", "shard": args.shard, "branch": args.branch, "start": start, "end": end,
        "min_value_atto": str(args.min_value_atto),
        "anchors": [{"block": n, "hash": h} for n, h in sorted(anchors.items())],
        "exports": [{"source_manifest_sha256": lib.file_sha256(d / "source_manifest.json"),
                     "block_range": m["blockRange"], "counts": m["counts"], "source": m.get("source", "")}
                    for d, m in zip(exports, manifests)],
    }
    rows = imported.rows
    lib.write_csv(out / "transactions.csv", lib.TX_FIELDS, rows["tx"])
    lib.write_csv(out / "staking.csv", lib.STAKING_FIELDS, rows["staking"])
    lib.write_csv(out / "trace-blocks.csv", lib.TRACE_BLOCK_FIELDS, rows["trace"])
    lib.write_csv(out / "xshard-blocks.csv", lib.XSHARD_BLOCK_FIELDS, rows["xshard"])
    lib.write_csv(out / "segments.csv", lib.SEGMENT_FIELDS, imported.segments)
    lib.write_csv(out / "receipts" / "outgoing-receipts.csv", lib.RECEIPT_FIELDS, rows["receipts"])
    lib.write_json(out / "receipts" / "outgoing-receipts-summary.json", {
        "stage": "attribute-receipts", "scan": identity, "source": "transaction-level sends in the export",
        "receipt_roots": "unavailable (the export has no receipt roots)", "outgoing_blocks": len(rows["xshard"]),
        "unexplained_blocks": [], "untraced_blocks": [], "counters": {"receipts": len(rows["receipts"])},
        "files": lib.output_files(out / "receipts", ["outgoing-receipts.csv"]),
    })
    lib.write_json(out / "stage.json", {
        "identity": identity, "complete": True, "finished_utc": lib.utc(datetime.now(timezone.utc).timestamp()),
        "rpc_host": "export of " + "; ".join(sorted({m.get("source", "RPC") for m in manifests})),
        "read_by": "exported RPC responses (receipt roots unavailable)",
        "internal_unavailable": UNAVAILABLE,
        "boundaries": {"start": imported.first, "end": imported.last},
        "chain_continuity": f"verified from block {fork_number} through block {last}",
        "comparison": comparison,
        "counters": dict(sorted(imported.counters.items())),
        "tx_ge_min_by_month": dict(sorted(imported.months.items())),
        "files": lib.output_files(out, ["transactions.csv", "staking.csv", "trace-blocks.csv",
                                        "xshard-blocks.csv", "segments.csv"]),
    })
    print(f"imported blocks {start}-{end}: {len(rows['tx'])} transactions, {len(rows['staking'])} staking, "
          f"{len(rows['receipts'])} cross-shard sends", flush=True)


if __name__ == "__main__":
    main()
