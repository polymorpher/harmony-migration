#!/usr/bin/env python3

"""Run token-holders for every holder snapshot in the snapshot plan.

Snapshots on the same chain and block are replayed in one pass, so the log
range scan is shared by all of their tokens. Each snapshot writes <stem>.csv
and <stem>.summary.json in the holders directory. A snapshot whose files
exist, whose summary passed, and whose block and hash match the plan is kept
unless --replace is given, so an interrupted run resumes where it stopped.
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--holders-dir", required=True)
    parser.add_argument("--bin", default="bin/token-holders")
    parser.add_argument("--only", action="append", default=[], help="snapshot stem")
    parser.add_argument("--chain", action="append", default=[])
    parser.add_argument("--range-size", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument(
        "--from-block",
        action="append",
        default=[],
        help="CHAIN=BLOCK to skip the deployment search",
    )
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def existing_ok(summary_path, csv_path, snapshot):
    if not (os.path.exists(summary_path) and os.path.exists(csv_path)):
        return False
    summary = lib.load_json(summary_path)
    return (
        summary.get("status") == "passed"
        and int(summary.get("block", -1)) == snapshot["block"]
        and str(summary.get("block_hash", "")).lower() == snapshot["block_hash"].lower()
        and summary.get("output_sha256") == lib.file_sha256(csv_path)
    )


def main():
    args = parse_args()
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    plan = lib.load_json(args.plan)
    os.makedirs(args.holders_dir, exist_ok=True)
    from_blocks = dict(item.split("=", 1) for item in args.from_block)
    groups = {}
    for snapshot in plan["holder_snapshots"]:
        stem = snapshot["stem"]
        if args.only and stem not in args.only:
            continue
        if args.chain and snapshot["chain"] not in args.chain:
            continue
        csv_path = os.path.join(args.holders_dir, stem + ".csv")
        summary_path = os.path.join(args.holders_dir, stem + ".summary.json")
        if not args.replace and existing_ok(summary_path, csv_path, snapshot):
            print(f"keep {stem}", flush=True)
            continue
        for path in (csv_path, summary_path):
            if os.path.exists(path):
                if not args.replace:
                    raise FileExistsError(f"{path} exists but does not match the plan")
                os.remove(path)
        key = (snapshot["chain"], snapshot["block"], snapshot["block_hash"])
        groups.setdefault(key, []).append(snapshot)
    failures = []
    for (chain, block, block_hash), snapshots in sorted(groups.items()):
        entry = config["chains"][chain]
        url = os.environ.get(entry["rpc_env"] or "")
        if not url:
            raise SystemExit(f"set {entry['rpc_env']} for {chain}")
        command = [
            args.bin,
            "-rpc", url,
            "-chain-id", str(entry["chain_id"]),
            "-tokens", ",".join(snapshot["token"] for snapshot in snapshots),
            "-block", str(block),
            "-block-hash", block_hash,
            "-output-dir", args.holders_dir,
            "-name-prefix", chain,
        ]
        settings = entry.get("holder_scan") or {}
        range_size = args.range_size or settings.get("range_size")
        workers = args.workers or settings.get("workers")
        if range_size:
            command += ["-range-size", str(range_size)]
        if workers:
            command += ["-workers", str(workers)]
        if chain in from_blocks:
            command += ["-from-block", from_blocks[chain]]
        print(f"run {chain} block {block}: {len(snapshots)} tokens", flush=True)
        if subprocess.run(command).returncode != 0:
            failures += [snapshot["stem"] for snapshot in snapshots]
    if failures:
        raise SystemExit("holder snapshots failed: " + ", ".join(failures))


if __name__ == "__main__":
    main()
