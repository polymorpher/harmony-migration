#!/usr/bin/env python3
"""Trace a scan's candidate blocks and export internal transfers for the transaction archive.

Runs trace_block (Harmony's native Parity-style tracer, on the node's auth RPC
port) over every block in <scan>/trace-blocks.csv. An internal transfer is a
non-root frame that moves ONE: call/callcode with value, create with value, or a
selfdestruct balance. delegatecall and staticcall never move value. A transfer
is effective when neither it nor any ancestor frame reverted.

Outputs in --out (default <scan>/internal):
  internal.csv      internal transfers with value >= the scan's minimum
  block-stats.csv   per traced block: frames and value-moving frame counts
  failures.csv      blocks whose trace failed (retried by --resume)

Blocks are processed in interleaved order (every K-th block first), so a
partial run is a systematic sample; --report-partial projects totals from the
completed passes.
"""

import argparse
import csv
import math
import os
import re
import sys
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402

TRACE = None
PUBLIC = None
MIN_VALUE = lib.ONE


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scan", required=True, help="completed scan-blocks.py output directory")
    parser.add_argument("--rpc", default=os.environ.get("HARMONY_RPC"),
                        help="node JSON-RPC URL (default: $HARMONY_RPC)")
    parser.add_argument("--trace-rpc", default=os.environ.get("HARMONY_TRACE_RPC"),
                        help="node auth RPC URL serving trace_block (default: $HARMONY_TRACE_RPC)")
    parser.add_argument("--out")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--group", type=int, default=20, help="blocks per worker task")
    parser.add_argument("--interleave", type=int, default=20)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-untraceable", action="store_true",
                        help="complete even if some blocks cannot be replayed; they are listed in stage.json")
    parser.add_argument("--report-partial", action="store_true",
                        help="print totals projected from completed passes and exit")
    args = parser.parse_args()
    if not args.report_partial and (not args.rpc or not args.trace_rpc):
        parser.error("--rpc and --trace-rpc (or $HARMONY_RPC/$HARMONY_TRACE_RPC) are required")
    return args


def init_worker(public_url, trace_url, min_value):
    global TRACE, PUBLIC, MIN_VALUE
    TRACE = lib.Rpc(trace_url)
    PUBLIC = lib.Rpc(public_url)
    MIN_VALUE = min_value


def trace_frames(number, block_hash):
    """Parity-style frames of one block: by number for the canonical chain, by hash for listed blocks."""
    if not block_hash:
        return TRACE.call("trace_block", [hex(number)]) or []
    return lib.flatten_block_trace(
        TRACE.call("debug_traceBlockByHash", [block_hash, {"tracer": "ParityBlockTracer"}]), block_hash)


def trace(task):
    blocks, hashes = task
    rows, stats, failures = [], [], []
    counters = Counter()
    for number in blocks:
        try:
            frames = trace_frames(number, hashes.get(number))
        except lib.RpcError as error:
            failures.append({"block_number": number, "error": str(error)[:500]})
            continue
        transfers, frame_counts = lib.internal_transfers(frames)
        selected = [row for row in transfers if row["value_atto"] >= MIN_VALUE]
        counters["blocks_traced"] += 1
        counters["frames"] += frame_counts["frames"]
        counters["internal_value_gt0"] += frame_counts["internal_value_gt0"]
        effective = sum(row["effective"] for row in selected)
        counters["internal_ge_min"] += len(selected)
        counters["internal_ge_min_effective"] += effective
        for row in selected:
            counters[f"internal_ge_min_type_{row['type']}"] += 1
        if selected:
            block = (PUBLIC.call("hmyv2_getBlockByHash", [hashes[number], {"fullTx": False}])
                     if hashes.get(number) else PUBLIC.block(number))
            tx_hashes = block.get("transactions") or []
            trace_hashes = {frame.get("blockHash") for frame in frames} - {None}
            if trace_hashes and trace_hashes != {block["hash"]}:
                failures.append({"block_number": number, "error": "trace block hash differs from block"})
                continue
            for row in selected:
                row.update(block_number=number, block_hash=block["hash"],
                           timestamp=lib.integer(block["timestamp"]),
                           tx_hash=tx_hashes[row["tx_index"]] if row["tx_index"] < len(tx_hashes) else "")
            counters["txs_with_internal_ge_min"] += len({row["tx_index"] for row in selected})
            rows += selected
        stats.append({"block_number": number, "frames": frame_counts["frames"],
                      "internal_value_gt0": frame_counts["internal_value_gt0"],
                      "internal_ge_min": len(selected), "internal_ge_min_effective": effective})
    return blocks, dict(counters), rows, stats, failures


def candidates(scan_dir):
    return sorted({int(row["block_number"]) for row in lib.iter_csv(Path(scan_dir) / "trace-blocks.csv")})


def listed_hashes(scan_dir, scan_state):
    """Block hashes to trace by, for scans that read their blocks by hash."""
    if "block_list_sha256" not in scan_state["identity"]:
        return {}
    return {int(row["block_number"]): row["block_hash"]
            for row in lib.iter_csv(Path(scan_dir) / "trace-blocks.csv")}


def ordered(blocks, interleave):
    return [n for k in range(interleave) for n in blocks[k::interleave]]


def report_partial(cand, stats_path, interleave):
    stats = {}
    for row in lib.iter_csv(stats_path):
        stats[int(row["block_number"])] = {key: int(row[key]) for key in lib.BLOCK_STATS_FIELDS[1:]}
    passes = [cand[k::interleave] for k in range(interleave)]
    complete = [p for p in passes if all(n in stats for n in p)]
    traced = sum(len(p) for p in complete)
    result = {"candidates": len(cand), "complete_passes": len(complete), "blocks_traced": len(stats)}
    for key in lib.BLOCK_STATS_FIELDS[1:]:
        if not complete:
            break
        values = [stats[n][key] for p in complete for n in p]
        estimate = sum(values) * len(cand) / traced
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1)
        error = len(cand) * math.sqrt(variance / len(values) * (1 - len(values) / len(cand)))
        result[key] = {"estimate": round(estimate), "ci95": [round(estimate - 1.96 * error),
                                                             round(estimate + 1.96 * error)]}
    return result


def main():
    args = parse_args()
    scan_dir = Path(args.scan)
    scan_state = lib.load_json(scan_dir / "stage.json")
    if not scan_state.get("complete") or scan_state["identity"]["stage"] != "scan-blocks":
        raise SystemExit(f"{scan_dir} is not a completed scan")
    out = Path(args.out or scan_dir / "internal")
    cand = candidates(scan_dir)
    if args.report_partial:
        import json
        print(json.dumps(report_partial(cand, out / "block-stats.csv", args.interleave), indent=1))
        return
    identity = {"stage": "trace-internal", "scan": scan_state["identity"],
                "trace_blocks_sha256": scan_state["files"]["trace-blocks.csv"]["sha256"],
                "interleave": args.interleave}
    state = lib.open_stage(out, identity, args.resume)
    done = lib.read_done(out / "done.txt")
    todo = [n for n in ordered(cand, args.interleave) if n not in done]
    hashes = listed_hashes(scan_dir, scan_state)
    groups = [(group, {n: hashes[n] for n in group if n in hashes})
              for group in (todo[i:i + args.group] for i in range(0, len(todo), args.group))]
    min_value = int(scan_state["identity"]["min_value_atto"])
    trace_rpc = lib.Rpc(args.trace_rpc)
    print(f"trace {len(cand)} candidate blocks via {trace_rpc.host}: {len(todo)} to trace", flush=True)

    internal = lib.AppendCsv(out / "internal.csv", lib.INTERNAL_FIELDS)
    block_stats = lib.AppendCsv(out / "block-stats.csv", lib.BLOCK_STATS_FIELDS)
    failure_path = out / "failures.csv"
    failures = []
    with open(out / "done.txt", "a", encoding="utf-8") as done_file, \
            Pool(args.workers, init_worker, (args.rpc, args.trace_rpc, min_value)) as pool:
        for index, (blocks, counters, rows, stats, failed) in enumerate(
                pool.imap(trace, groups, chunksize=1), 1):
            internal.write(rows)
            internal.flush()
            block_stats.write(stats)
            block_stats.flush()
            failed_blocks = {item["block_number"] for item in failed}
            failures += failed
            done_file.write("".join(f"{n}\n" for n in blocks if n not in failed_blocks))
            done_file.flush()
            if index % 500 == 0 or index == len(groups):
                print(f"{index}/{len(groups)} groups; failures={len(failures)}", flush=True)
    internal.close()
    block_stats.close()
    with open(failure_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["block_number", "error"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(failures)
    if failures and not args.allow_untraceable:
        raise SystemExit(f"{len(failures)} blocks failed to trace; see {failure_path}, then --resume")
    untraced = sorted({item["block_number"] for item in failures})

    totals = Counter()
    seen = set()
    for row in lib.iter_csv(out / "block-stats.csv"):
        number = int(row["block_number"])
        if number in seen:
            continue
        seen.add(number)
        totals["blocks_traced"] += 1
        for key in lib.BLOCK_STATS_FIELDS[1:]:
            totals[key] += int(row[key])
    if seen | set(untraced) != set(cand):
        raise SystemExit(f"traced {len(seen)} of {len(cand)} candidate blocks")
    totals["blocks_untraced"] = len(untraced)
    lib.finish_stage(out, state, {
        "trace_rpc_host": trace_rpc.host,
        "untraced_blocks": untraced,
        "untraced_errors": sorted({re.sub(r"0x[0-9a-fA-F]+", "<hash>", item["error"])[:300] for item in failures}),
        "counters": dict(sorted(totals.items())),
        "files": lib.output_files(out, ["internal.csv", "block-stats.csv"]),
    })
    print(f"complete: {out / 'stage.json'}", flush=True)


if __name__ == "__main__":
    main()
