#!/usr/bin/env python3

"""Convert a directional activity export into the per-candidate activity format.

The directional export (harmony-airdrop-tracking `data/activity-20260910.csv`,
built from toolkit/cmd/account-directional-activity on the shard-0 archival
database and the shard-1 archival RPC) records, per address, the newest
transaction the address signed and the newest regular transaction another
account sent to it. That scan classifies every index entry from the canonical
block body and repairs index entries whose key records the wrong block or
position, which the single-entry account-activity scan treats as stale.

For each candidate the later of the two events becomes its activity record.
Every selected transaction is re-resolved through the archival RPC to obtain
its position in the block, and its block hash is checked against the canonical
block at that height. The output feeds enrich-claim-activity.py as a
supplemental source; the enrichment keeps the latest of all sources.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


ACTIVITY_FIELDS = (
    "last_activity_time_utc",
    "last_activity_timestamp_unix",
    "last_activity_block",
    "last_activity_shard",
    "last_activity_type",
    "last_activity_tx_hash",
    "last_activity_index",
    "last_activity_detail",
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="candidate CSV (secure_key, address)")
    parser.add_argument("--snapshot-manifest", required=True)
    parser.add_argument("--directional", required=True, help="directional activity CSV")
    parser.add_argument("--directional-summary", required=True)
    parser.add_argument("--rpc-shard0", required=True)
    parser.add_argument("--rpc-shard1", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def integer(value):
    if isinstance(value, int):
        return value
    return int(str(value), 0)


def load_snapshot(path):
    with open(path, encoding="utf-8") as source:
        manifest = json.load(source)
    cutoff = manifest["cutoff"]
    requested = datetime.fromisoformat(cutoff["requested_time_utc"][:-1] + "+00:00")
    return {
        "requested_time_utc": cutoff["requested_time_utc"],
        "requested_timestamp_unix": int(requested.timestamp()),
        "shards": {str(s): {"block": cutoff[f"shard{s}"]["block"], "hash": cutoff[f"shard{s}"]["hash"].lower()} for s in (0, 1)},
    }


def latest_event(row):
    events = []
    if row["last_signed_block"]:
        events.append({
            "timestamp": int(row["last_signed_timestamp_unix"]), "shard": int(row["last_signed_shard"]),
            "block": int(row["last_signed_block"]), "type": row["last_signed_type"] or "regular",
            "hash": row["last_signed_tx_hash"].lower(), "detail": "sender",
        })
    if row["last_inbound_block"]:
        events.append({
            "timestamp": int(row["last_inbound_timestamp_unix"]), "shard": int(row["last_inbound_shard"]),
            "block": int(row["last_inbound_block"]), "type": "regular",
            "hash": row["last_inbound_tx_hash"].lower(), "detail": "recipient",
        })
    return max(events, key=lambda e: (e["timestamp"], e["shard"], e["block"], e["detail"])) if events else None


def resolve(clients, events, snapshot, stats):
    """Fill each event's position and Harmony hash, and check canonical membership."""
    for shard in (0, 1):
        group = [e for e in events if e["shard"] == shard]
        calls = [
            ("hmyv2_getStakingTransactionByHash" if e["type"] == "staking" else "hmyv2_getTransactionByHash", [e["hash"]])
            for e in group
        ]
        results = clients[shard].batch(calls)
        retry = []
        for event, (tx, error) in zip(group, results):
            if tx:
                event["tx"] = tx
            elif event["type"] == "regular":
                retry.append(event)
            else:
                raise ValueError(f"shard {shard} staking transaction {event['hash']} not found: {error}")
        for event, (tx, error) in zip(retry, clients[shard].batch([("eth_getTransactionByHash", [e["hash"]]) for e in retry])):
            if not tx:
                raise ValueError(f"shard {shard} transaction {event['hash']} not found: {error}")
            event["tx"] = tx
            stats["resolved_by_eth_hash"] += 1
        blocks = sorted({e["block"] for e in group})
        canonical = {}
        for number, (block, error) in zip(blocks, clients[shard].batch(
            [("hmyv2_getBlockByNumber", [n, {"fullTx": False, "inclTx": False, "inclStaking": False}]) for n in blocks]
        )):
            if error or not block or integer(block["number"]) != number:
                raise ValueError(f"shard {shard} canonical block {number}: {error}")
            canonical[number] = str(block["hash"]).lower()
            if integer(block["timestamp"]) > snapshot["requested_timestamp_unix"]:
                raise ValueError(f"shard {shard} block {number} is after the cutoff time")
        for event in group:
            tx = event["tx"]
            if integer(tx["blockNumber"]) != event["block"]:
                raise ValueError(f"{event['hash']}: block {tx['blockNumber']} != {event['block']}")
            if str(tx["blockHash"]).lower() != canonical[event["block"]]:
                raise ValueError(f"{event['hash']}: block hash is not canonical")
            event["index"] = integer(tx["transactionIndex"])
            event["hash"] = str(tx.get("hash") or event["hash"]).lower()
            stats[f"resolved_shard{shard}"] += 1


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)
    snapshot = load_snapshot(args.snapshot_manifest)
    directional = {}
    with open(args.directional, newline="") as source:
        for row in csv.DictReader(source):
            directional[lib.normalize_address(row["address"])] = row

    candidates = []
    previous = None
    with open(args.input, newline="") as source:
        for line, row in enumerate(csv.DictReader(source), start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(f"secure keys are not increasing at line {line}")
            previous = key
            address = lib.require_address_secure_key(row["address"], key, f"candidate line {line}")
            if address not in directional:
                raise ValueError(f"candidate line {line} has no directional record")
            candidates.append((key, row["address"], latest_event(directional[address])))

    for _key, _address, event in candidates:
        if event and event["block"] > snapshot["shards"][str(event["shard"])]["block"]:
            raise ValueError(f"{event['hash']} is after the shard {event['shard']} cutoff")
    stats = Counter()
    clients = {
        0: lib.RpcClient(args.rpc_shard0, batch_size=50, workers=12),
        1: lib.RpcClient(args.rpc_shard1, batch_size=50, workers=12),
    }
    resolve(clients, [e for _k, _a, e in candidates if e], snapshot, stats)

    partial = args.output + ".partial"
    with open(partial, "w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=("secure_key", "address", *ACTIVITY_FIELDS), lineterminator="\n")
        writer.writeheader()
        for key, address, event in candidates:
            row = {"secure_key": key, "address": address, **{field: "" for field in ACTIVITY_FIELDS}}
            if event:
                row.update({
                    "last_activity_time_utc": utc(event["timestamp"]),
                    "last_activity_timestamp_unix": event["timestamp"],
                    "last_activity_block": event["block"],
                    "last_activity_shard": event["shard"],
                    "last_activity_type": event["type"],
                    "last_activity_tx_hash": event["hash"],
                    "last_activity_index": event["index"],
                    "last_activity_detail": event["detail"],
                })
                stats["activity_found"] += 1
            else:
                stats["activity_not_found"] += 1
            writer.writerow(row)
    os.replace(partial, args.output)

    summary = {
        "schema_version": 1,
        "status": "passed",
        "source_kind": (
            "directional activity: newest transaction signed by the address and newest regular transaction "
            "sent to it by another account, from the explorer-node index classified by canonical block bodies "
            "(shard 0) and archival transaction-history RPC (shard 1); every selected transaction re-resolved "
            "and its block hash checked against the canonical chain"
        ),
        "candidates_path": args.input,
        "candidates_sha256": file_sha256(args.input),
        "candidates": len(candidates),
        "cutoff": snapshot,
        "directional": {"path": args.directional, "sha256": file_sha256(args.directional)},
        "directional_summary": {"path": args.directional_summary, "sha256": file_sha256(args.directional_summary)},
        "rpc": {"0": args.rpc_shard0, "1": args.rpc_shard1},
        "activity_found": stats["activity_found"],
        "activity_not_found": stats["activity_not_found"],
        "counters": {k: v for k, v in sorted(stats.items()) if k not in ("activity_found", "activity_not_found")},
        "output_path": args.output,
        "output_sha256": file_sha256(args.output),
    }
    with open(args.summary, "w", encoding="utf-8") as output:
        json.dump(summary, output, indent=2, sort_keys=True)
        output.write("\n")
    print(json.dumps({k: summary[k] for k in ("candidates", "activity_found", "activity_not_found", "counters")}))


if __name__ == "__main__":
    main()
