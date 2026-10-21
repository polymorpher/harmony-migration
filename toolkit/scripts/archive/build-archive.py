#!/usr/bin/env python3
"""Merge scan, trace and receipt outputs into the transaction-archive tables.

Each --run is a completed scan-blocks.py directory with its internal/ stage
(trace-internal.py) and receipts/ stage (attribute-receipts.py). --destinations
files come from map-receipt-destinations.py. Runs of the same shard and branch
must not overlap, and every run must use the same minimum value. A rolled_back
run must have been scanned with --expect <start-1>:<canonical hash>, which pins
the last block it shares with the canonical chain.

Outputs in --out:
  transactions.csv        plain transactions >= minimum (all kinds, both branches)
  internal_transfers.csv  internal transfers >= minimum
  cross_shard.csv         cross-shard receipts >= minimum, with destination credit
  staking.csv             staking transactions with amount >= minimum
  manifest.json           runs, coverage notes, row counts and file hashes

Addresses are lowercase 0x hex; amounts are integer atto-ONE; timestamps are
Unix seconds.
"""

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402

TRANSACTIONS = ["shard", "branch", "kind", "block_number", "block_hash", "timestamp", "tx_index",
                "tx_hash", "eth_hash", "from", "to", "value_atto", "fee_atto", "status", "to_shard",
                "method"]
INTERNAL = ["shard", "branch", "block_number", "block_hash", "timestamp", "tx_index", "tx_hash",
            "eth_hash", "trace_address", "type", "from", "to", "value_atto", "effective"]
CROSS_SHARD = ["branch", "from_shard", "to_shard", "mechanism", "block_number", "block_hash",
               "timestamp", "tx_index", "tx_hash", "from", "to", "value_atto", "ancestor_reverted",
               "eth_hash", "dest_status", "dest_block_number", "dest_block_hash", "dest_timestamp"]
STAKING = ["shard", "branch", "type", "block_number", "block_hash", "timestamp", "tx_index", "tx_hash",
           "delegator", "validator", "amount_atto", "status"]
TABLES = {"transactions.csv": TRANSACTIONS, "internal_transfers.csv": INTERNAL,
          "cross_shard.csv": CROSS_SHARD, "staking.csv": STAKING}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="append", required=True, help="scan directory (repeatable)")
    parser.add_argument("--destinations", action="append", default=[],
                        help="map-receipt-destinations.py output (repeatable)")
    parser.add_argument("--note", action="append", default=[], help="coverage note for the manifest")
    parser.add_argument("--notes-file", help="file with one coverage note per line")
    parser.add_argument("--allow-missing-internal", action="store_true",
                        help="accept runs without a completed internal/ stage (recorded in the manifest)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


SCAN_STAGES = ("scan-blocks", "import-rpc-export")


def load_run(path, allow_missing_internal):
    path = Path(path)
    scan = lib.load_json(path / "stage.json")
    if not scan.get("complete") or scan["identity"]["stage"] not in SCAN_STAGES:
        raise SystemExit(f"{path}: not a completed scan or import")
    identity = scan["identity"]
    internal_path = path / "internal" / "stage.json"
    internal = lib.load_json(internal_path) if internal_path.exists() else None
    if internal and internal["identity"]["scan"] != identity:
        raise SystemExit(f"{path}: internal/ was traced from a different scan")
    if not (internal and internal.get("complete")):
        if not (allow_missing_internal or scan.get("internal_unavailable")):
            raise SystemExit(f"{path}: internal/ stage is missing or incomplete")
        internal = None
    receipts_path = path / "receipts" / "outgoing-receipts-summary.json"
    receipts = lib.load_json(receipts_path) if receipts_path.exists() else None
    if receipts is None:
        raise SystemExit(f"{path}: receipts/ stage is missing (run attribute-receipts.py)")
    if receipts["scan"] != identity or receipts["unexplained_blocks"]:
        raise SystemExit(f"{path}: receipts/ is from a different scan or has unexplained blocks")
    if identity["branch"] == "rolled_back":
        pins = {anchor["block"] for anchor in identity["anchors"]}
        if identity["start"] - 1 not in pins:
            raise SystemExit(f"{path}: rolled_back run lacks --expect {identity['start'] - 1}:<canonical hash>")
    return {"path": path, "scan": scan, "identity": identity, "internal": internal, "receipts": receipts}


def check_overlaps(runs):
    """No overlap per shard and branch; adjacent runs must link by parent hash."""
    by_key = {}
    for run in runs:
        key = (run["identity"]["shard"], run["identity"]["branch"])
        by_key.setdefault(key, []).append(run)
    for key, group in by_key.items():
        group.sort(key=lambda run: run["identity"]["start"])
        for previous, current in zip(group, group[1:]):
            if current["identity"]["start"] <= previous["identity"]["end"]:
                raise SystemExit(f"overlapping runs for shard {key[0]} {key[1]}")
            if (current["identity"]["start"] == previous["identity"]["end"] + 1
                    and current["scan"]["boundaries"]["start"]["parent_hash"].lower()
                    != previous["scan"]["boundaries"]["end"]["hash"].lower()):
                raise SystemExit(f"shard {key[0]} {key[1]} runs at block {current['identity']['start']} do not link")


def main():
    args = parse_args()
    if args.notes_file:
        with open(args.notes_file, encoding="utf-8") as source:
            args.note += [line.strip() for line in source if line.strip()]
    out = Path(args.out)
    for name in [*TABLES, "manifest.json"]:
        if (out / name).exists() and not args.replace:
            raise SystemExit(f"{out / name} exists; pass --replace")
    out.mkdir(parents=True, exist_ok=True)
    runs = [load_run(path, args.allow_missing_internal) for path in args.run]
    minimums = {run["identity"]["min_value_atto"] for run in runs}
    if len(minimums) != 1:
        raise SystemExit(f"runs use different minimum values: {sorted(minimums)}")
    minimum = int(minimums.pop())
    check_overlaps(runs)

    destinations = {}
    for path in args.destinations:
        for row in lib.iter_csv(path):
            destinations[(row["from_shard"], row["tx_hash"])] = row

    tables = {name: [] for name in TABLES}
    seen = {name: set() for name in TABLES}
    counts = {name: Counter() for name in TABLES}

    def add(name, key, row, group):
        if key in seen[name]:
            return
        seen[name].add(key)
        tables[name].append(row)
        counts[name][group] += 1

    for run in runs:
        shard, branch = run["identity"]["shard"], run["identity"]["branch"]
        group = f"shard{shard}/{branch}"
        for row in lib.iter_csv(run["path"] / "transactions.csv"):
            add("transactions.csv", (shard, branch, row["tx_hash"]), {
                "shard": shard, "branch": branch, "kind": row["kind"],
                "block_number": row["block_number"], "block_hash": row["block_hash"],
                "timestamp": row["timestamp"], "tx_index": row["tx_index"], "tx_hash": row["tx_hash"],
                "eth_hash": row["eth_hash"], "from": row["from"], "to": row["to"],
                "value_atto": row["value_atto"],
                "fee_atto": int(row["gas_used"] or 0) * int(row["gas_price"] or 0),
                "status": row["status"], "to_shard": row["to_shard_id"], "method": row["input_selector"],
            }, group)
        if run["internal"]:
            for row in lib.iter_csv(run["path"] / "internal" / "internal.csv"):
                add("internal_transfers.csv",
                    (shard, branch, row["block_number"], row["tx_index"], row["trace_address"]),
                    {"shard": shard, "branch": branch, **{k: row[k] for k in INTERNAL[2:]}}, group)
        for row in lib.iter_csv(run["path"] / "receipts" / "outgoing-receipts.csv"):
            if int(row["value_atto"]) < minimum:
                continue
            dest = destinations.get((row["from_shard"], row["tx_hash"]), {})
            add("cross_shard.csv", (branch, row["from_shard"], row["tx_hash"]), {
                "branch": branch, **{k: row.get(k, "") for k in CROSS_SHARD[1:14]},
                "dest_status": dest.get("dest_status", "unmapped"),
                "dest_block_number": dest.get("dest_block_number", ""),
                "dest_block_hash": dest.get("dest_block_hash", ""),
                "dest_timestamp": dest.get("dest_timestamp", ""),
            }, f"{branch}/{row['from_shard']}->{row['to_shard']}")
        for row in lib.iter_csv(run["path"] / "staking.csv"):
            add("staking.csv", (shard, branch, row["tx_hash"]), {
                "shard": shard, "branch": branch, "type": row["type"],
                "block_number": row["block_number"], "block_hash": row["block_hash"],
                "timestamp": row["timestamp"], "tx_index": row["tx_index"], "tx_hash": row["tx_hash"],
                "delegator": row["from"], "validator": row["validator"],
                "amount_atto": row["amount_atto"], "status": row["status"],
            }, group)

    files = {}
    for name, fields in TABLES.items():
        rows = tables[name]
        shard_key = "from_shard" if name == "cross_shard.csv" else "shard"
        rows.sort(key=lambda r: (int(r[shard_key]), r["branch"], int(r["block_number"]),
                                 int(r["tx_index"]), r.get("trace_address", "")))
        count, digest = lib.write_csv(out / name, fields, rows)
        files[name] = {"rows": count, "sha256": digest, "bytes": (out / name).stat().st_size,
                       "by_group": dict(sorted(counts[name].items()))}

    manifest = {
        "schema_version": lib.SCHEMA_VERSION,
        "kind": "harmony-transaction-archive",
        "min_value_atto": str(minimum),
        "runs": [{
            "shard": run["identity"]["shard"], "branch": run["identity"]["branch"],
            "start": run["identity"]["start"], "end": run["identity"]["end"],
            "anchors": run["identity"]["anchors"], "rpc_host": run["scan"]["rpc_host"],
            "boundaries": run["scan"]["boundaries"], "chain_continuity": run["scan"]["chain_continuity"],
            "source": run["identity"]["stage"], "read_by": run["scan"].get("read_by", "block number"),
            "scan_counters": run["scan"]["counters"],
            "internal": (run["internal"]["counters"] if run["internal"]
                         else "unavailable: " + run["scan"].get("internal_unavailable", "not traced")),
            "receipts": run["receipts"]["counters"],
        } for run in runs],
        "destinations": [os.path.basename(path) for path in args.destinations],
        "notes": args.note,
        "files": files,
    }
    lib.write_json(out / "manifest.json", manifest)
    for name, entry in files.items():
        print(f"{name}: {entry['rows']} rows {entry['by_group']}", flush=True)


if __name__ == "__main__":
    main()
