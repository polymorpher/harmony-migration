#!/usr/bin/env python3
"""Find where each outgoing cross-shard receipt was credited on its destination shard.

Reads one or more source-side outgoing-receipts.csv files (attribute-receipts.py)
and looks each receipt up with hmyv2_getCXReceiptByHash on the destination
shard's node, which answers from the destination's own receipt index, first by
the Harmony transaction hash and then by the Ethereum-style hash (precompile
receipts are keyed by tx.HashByType()). Amount, recipient and shards must match
the source receipt.

With --dest-scan, every destination block whose header has a non-empty incoming
receipt root inside that scan's range must be accounted for by a mapped receipt;
otherwise the stage fails (a source shard or range is missing).

Writes --output (source receipt plus dest_status, dest_block_number,
dest_block_hash, dest_timestamp) and --summary.
"""

import argparse
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipts", action="append", required=True,
                        help="source-side outgoing-receipts.csv (repeatable)")
    parser.add_argument("--dest-shard", type=int, required=True, choices=(0, 1))
    parser.add_argument("--dest-rpc", required=True)
    parser.add_argument("--dest-scan", help="destination scan directory for the coverage check")
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path) and not args.replace:
            raise SystemExit(f"{path} exists; pass --replace")
    rpc = lib.Rpc(args.dest_rpc)
    if rpc.shard() != args.dest_shard:
        raise SystemExit(f"{rpc.host} does not serve shard {args.dest_shard}")
    receipts = [row for path in args.receipts for row in lib.iter_csv(path)
                if int(row["to_shard"]) == args.dest_shard]
    timestamps = {}

    def lookup(row):
        # A precompile receipt is keyed by tx.HashByType(): the Ethereum-style hash
        # for a transaction sent from an Ethereum wallet, the Harmony hash otherwise.
        found = None
        for key in dict.fromkeys(h for h in (row["tx_hash"], row.get("eth_hash")) if h):
            found = rpc.call("hmyv2_getCXReceiptByHash", [key])
            if found is not None:
                break
        out = {**row, "dest_status": "not_found", "dest_block_number": "", "dest_block_hash": "",
               "dest_timestamp": ""}
        if found is None:
            return out
        if (lib.integer(found["value"]) != int(row["value_atto"])
                or lib.address(found.get("to")) != row["to"].lower()
                or lib.integer(found["toShardID"]) != args.dest_shard):
            out["dest_status"] = "mismatch"
            return out
        number = lib.integer(found["blockNumber"])
        if number not in timestamps:
            timestamps[number] = lib.integer(rpc.block(number)["timestamp"])
        out.update(dest_status="credited", dest_block_number=number,
                   dest_block_hash=found.get("blockHash", ""), dest_timestamp=timestamps[number])
        return out

    with ThreadPoolExecutor(args.workers) as pool:
        mapped = list(pool.map(lookup, receipts))
    status = Counter(row["dest_status"] for row in mapped)
    uncovered = []
    if args.dest_scan:
        scan = lib.load_json(Path(args.dest_scan) / "stage.json")
        if scan["identity"]["shard"] != args.dest_shard:
            raise SystemExit("--dest-scan is for a different shard")
        credited = {int(row["dest_block_number"]) for row in mapped if row["dest_status"] == "credited"}
        flagged = {int(row["block_number"]) for row in lib.iter_csv(Path(args.dest_scan) / "xshard-blocks.csv")
                   if row["incoming"] == "1"}
        uncovered = sorted(flagged - credited)
    count, digest = lib.write_csv(args.output, lib.DESTINATION_FIELDS, mapped)
    lib.write_json(args.summary, {
        "stage": "map-receipt-destinations", "dest_shard": args.dest_shard, "dest_rpc_host": rpc.host,
        "receipts": count, "status": dict(status), "dest_scan": args.dest_scan is not None,
        "uncovered_incoming_blocks": uncovered,
        "output": {"path": os.path.basename(args.output), "sha256": digest},
    })
    print(f"{count} receipts to shard {args.dest_shard}: {dict(status)}; "
          f"uncovered incoming blocks={len(uncovered)}", flush=True)
    if status.get("mismatch") or uncovered:
        raise SystemExit("destination mapping failed: mismatches or uncovered incoming blocks")


if __name__ == "__main__":
    main()
