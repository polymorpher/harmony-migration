#!/usr/bin/env python3
"""Attribute every outgoing cross-shard receipt of a scan to its transaction.

For every block of <scan>/xshard-blocks.csv whose header has a non-empty
outgoing receipt root, read the block and its trace and list the receipts:

  native          a transaction with toShardID != shardID
  precompile      a completed call to the cross-shard precompile 0x..f9, at any
                  depth; before the receipt-rollback fix epoch a reverted
                  ancestor frame does not cancel the receipt
  precompile_replay_incompatible
                  the header proves a receipt but the current binary's replay
                  rejects the call (historical DELEGATECALL context); the amount
                  and recipient are decoded from the attempted call

Every flagged block must be explained by at least one receipt. Writes
--out/outgoing-receipts.csv and the stage summary.
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
    parser.add_argument("--scan", required=True)
    parser.add_argument("--rpc", default=os.environ.get("HARMONY_RPC"))
    parser.add_argument("--trace-rpc", default=os.environ.get("HARMONY_TRACE_RPC"),
                        help="auth RPC for trace_block; without it only transaction-level receipts are found")
    parser.add_argument("--rollback-fix-epoch", type=int, default=lib.RECEIPT_ROLLBACK_FIX_EPOCH)
    parser.add_argument("--out")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if not args.rpc:
        parser.error("--rpc or $HARMONY_RPC is required")
    return args


def main():
    args = parse_args()
    scan_dir = Path(args.scan)
    scan_state = lib.load_json(scan_dir / "stage.json")
    if not scan_state.get("complete") or scan_state["identity"]["stage"] != "scan-blocks":
        raise SystemExit(f"{scan_dir} is not a completed scan")
    shard = scan_state["identity"]["shard"]
    out = Path(args.out or scan_dir / "receipts")
    output = out / "outgoing-receipts.csv"
    if output.exists() and not args.replace:
        raise SystemExit(f"{output} exists; pass --replace")
    out.mkdir(parents=True, exist_ok=True)
    rpc = lib.Rpc(args.rpc)
    tracer = lib.Rpc(args.trace_rpc) if args.trace_rpc else None
    flagged = {int(row["block_number"]): row["block_hash"] for row in lib.iter_csv(scan_dir / "xshard-blocks.csv")
               if row["outgoing"] == "1"}
    listed = "block_list_sha256" in scan_state["identity"]
    hashes = dict(flagged) if listed else {}
    if listed:
        # Blocks read by hash have no receipt roots: also look for precompile calls in every
        # block that was traced for internal transfers.
        for row in lib.iter_csv(scan_dir / "trace-blocks.csv"):
            hashes.setdefault(int(row["block_number"]), row["block_hash"])
    blocks = sorted(set(flagged) | set(hashes))

    def attribute(number):
        if listed:
            block = rpc.call("hmyv2_getBlockByHash", [hashes[number], {"fullTx": True, "inclTx": True}])
        else:
            block = rpc.block(number, full=True)
        frames = None
        if tracer is not None:
            try:
                if listed:
                    frames = lib.flatten_block_trace(tracer.call(
                        "debug_traceBlockByHash", [hashes[number], {"tracer": "ParityBlockTracer"}]), hashes[number])
                else:
                    frames = tracer.call("trace_block", [hex(number)]) or []
            except lib.RpcError:
                frames = None
        rows, fallback = lib.outgoing_receipts(block, frames, shard, args.rollback_fix_epoch)
        return number, rows, fallback, frames is not None

    counters = Counter()
    rows, unexplained, untraced = [], [], []
    with ThreadPoolExecutor(args.workers) as pool:
        for number, found, fallback, traced in sorted(pool.map(attribute, blocks)):
            if not found and number in flagged:
                unexplained.append(number)
            if not traced:
                untraced.append(number)
            counters["fallback_blocks"] += fallback
            for row in found:
                key = f"{row['mechanism']}_depth{'0' if row['depth'] == 0 else '1+'}"
                if row["ancestor_reverted"]:
                    key += "_ancestor_reverted"
                counters[key] += 1
                counters["receipts"] += 1
            rows += found
    lib.write_csv(output, lib.RECEIPT_FIELDS, rows)
    summary = {
        "stage": "attribute-receipts", "scan": scan_state["identity"], "rpc_host": rpc.host,
        "trace_rpc_host": tracer.host if tracer else None,
        "rollback_fix_epoch": args.rollback_fix_epoch,
        "outgoing_blocks": len(flagged), "examined_blocks": len(blocks),
        "receipt_roots": "unavailable (blocks read by hash)" if listed else "header",
        "unexplained_blocks": unexplained,
        "untraced_blocks": untraced, "counters": dict(sorted(counters.items())),
        "files": lib.output_files(out, ["outgoing-receipts.csv"]),
    }
    lib.write_json(out / "outgoing-receipts-summary.json", summary)
    print(f"{len(rows)} receipts in {len(blocks)} blocks; unexplained={len(unexplained)} "
          f"untraced={len(untraced)}", flush=True)
    if unexplained:
        raise SystemExit(f"unexplained outgoing receipt blocks: {unexplained[:20]}")


if __name__ == "__main__":
    main()
