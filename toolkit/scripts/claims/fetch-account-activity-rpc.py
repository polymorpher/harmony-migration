#!/usr/bin/env python3

"""Fetch pre-cutoff account activity from a Harmony archival node."""

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--snapshot-manifest", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--shard", required=True, type=int, choices=(0, 1))
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--chunk-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=10000)
    args = parser.parse_args()
    for name in (
        "batch_size",
        "workers",
        "page_size",
        "chunk_size",
        "max_pages",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc(value):
    if not value.endswith("Z"):
        raise ValueError(f"UTC timestamp must end in Z: {value}")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"timestamp is not UTC: {value}")
    return parsed


def format_utc(timestamp):
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def integer(value, context):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as error:
            raise ValueError(
                f"{context}: invalid integer {value!r}"
            ) from error
    raise ValueError(f"{context}: invalid integer {value!r}")


def load_snapshot(path, shard):
    with open(path, encoding="utf-8") as source:
        manifest = json.load(source)
    cutoff = manifest.get("cutoff") or {}
    requested_text = cutoff.get("requested_time_utc")
    entry = cutoff.get(f"shard{shard}") or {}
    if (
        not requested_text
        or not isinstance(entry.get("block"), int)
        or not entry.get("hash")
    ):
        raise ValueError("snapshot manifest is missing cutoff data")
    requested = parse_utc(requested_text)
    shard_time = parse_utc(entry.get("timestamp_utc", ""))
    if shard_time > requested:
        raise ValueError("shard cutoff is after requested cutoff")
    return {
        "requested_time_utc": requested_text,
        "requested_timestamp_unix": int(requested.timestamp()),
        "block": entry["block"],
        "hash": entry["hash"].lower(),
        "timestamp_utc": entry["timestamp_utc"],
    }


def load_candidates(path, shard, cutoff_block):
    result = []
    previous_key = None
    seen_addresses = set()
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        block_field = f"claims_shard{shard}_block"
        required = {
            "secure_key",
            "address",
            "total_claim_atto",
            block_field,
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"input is missing fields: {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous_key is not None and key <= previous_key:
                raise ValueError(
                    f"secure keys are not increasing at line {line}"
                )
            previous_key = key
            address = lib.require_address_secure_key(
                row["address"], key, f"input line {line}"
            )
            if address in seen_addresses:
                raise ValueError(f"duplicate address at line {line}")
            seen_addresses.add(address)
            if int(row[block_field]) != cutoff_block:
                raise ValueError(f"wrong shard cutoff at line {line}")
            if int(row["total_claim_atto"]) < 1000 * 10**18:
                raise ValueError(f"claim below 1,000 ONE at line {line}")
            result.append((key, address))
    return result


def source_specs(shard):
    result = [
        {
            "activity_type": "regular",
            "history_method": "hmyv2_getTransactionsHistory",
            "transaction_method": "hmyv2_getTransactionByHash",
            "result_key": "transactions",
        }
    ]
    if shard == 0:
        result.append(
            {
                "activity_type": "staking",
                "history_method": "hmyv2_getStakingTransactionsHistory",
                "transaction_method": "hmyv2_getStakingTransactionByHash",
                "result_key": "staking_transactions",
            }
        )
    return result


def history_params(address, page, page_size):
    return [
        {
            "address": address,
            "pageIndex": page,
            "pageSize": page_size,
            "fullTx": False,
            "txType": "ALL",
            "order": "DESC",
        }
    ]


def transaction_activity(transaction, spec, shard, snapshot):
    context = f"shard {shard} {spec['activity_type']} transaction"
    block = integer(transaction.get("blockNumber"), context)
    timestamp = integer(transaction.get("timestamp"), context)
    tx_hash = transaction.get("hash")
    block_hash = transaction.get("blockHash")
    if (
        not isinstance(tx_hash, str)
        or not tx_hash.startswith("0x")
        or not isinstance(block_hash, str)
        or not block_hash.startswith("0x")
    ):
        raise ValueError(f"{context}: missing hash")
    if (
        block > snapshot["block"]
        or timestamp > snapshot["requested_timestamp_unix"]
    ):
        return None
    return {
        "last_activity_time_utc": format_utc(timestamp),
        "last_activity_timestamp_unix": timestamp,
        "last_activity_block": block,
        "last_activity_shard": shard,
        "last_activity_type": spec["activity_type"],
        "last_activity_tx_hash": tx_hash.lower(),
        "last_activity_index": integer(
            transaction.get("transactionIndex", 0), context
        ),
        "last_activity_detail": (
            f"{spec['activity_type']}_history"
        ),
        "_block_hash": block_hash.lower(),
    }


def activity_sort_key(activity):
    return (
        activity["last_activity_timestamp_unix"],
        activity["last_activity_block"],
        activity["last_activity_index"],
        activity["last_activity_type"],
        activity["last_activity_tx_hash"],
    )


def collect_source(
    client,
    addresses,
    spec,
    shard,
    snapshot,
    page_size,
    max_pages,
    canonical_hashes,
    stats,
):
    results = {}
    pending = {address: 0 for address in addresses}
    while pending:
        page_items = list(pending.items())
        listings = client.batch(
            [
                (
                    spec["history_method"],
                    history_params(address, page, page_size),
                )
                for address, page in page_items
            ]
        )
        page_hashes = {}
        resolve_calls = []
        resolve_owners = []
        for (address, page), (listing, error) in zip(
            page_items, listings
        ):
            if error:
                raise ValueError(
                    f"{spec['history_method']} failed for "
                    f"{address}: {error}"
                )
            if not isinstance(listing, dict):
                raise ValueError(
                    f"{spec['history_method']} missing for {address}"
                )
            hashes = listing.get(spec["result_key"])
            if not isinstance(hashes, list):
                raise ValueError(
                    f"{spec['history_method']} malformed for {address}"
                )
            if any(
                not isinstance(tx_hash, str)
                or not tx_hash.startswith("0x")
                for tx_hash in hashes
            ):
                raise ValueError(
                    f"{spec['history_method']} invalid hash for {address}"
                )
            page_hashes[address] = hashes
            for tx_hash in hashes:
                resolve_calls.append(
                    (spec["transaction_method"], [tx_hash])
                )
                resolve_owners.append((address, tx_hash.lower()))

        by_address = {address: [] for address, _page in page_items}
        activity_candidates = []
        resolved = client.batch(resolve_calls)
        for (address, expected_hash), (transaction, error) in zip(
            resolve_owners, resolved
        ):
            if error or not isinstance(transaction, dict):
                raise ValueError(
                    f"{spec['transaction_method']} failed for "
                    f"{expected_hash}: {error or 'missing result'}"
                )
            response_hashes = {
                str(transaction.get("hash", "")).lower(),
                str(transaction.get("ethHash", "")).lower(),
            }
            if expected_hash not in response_hashes:
                raise ValueError("resolved transaction hash mismatch")
            activity = transaction_activity(
                transaction, spec, shard, snapshot
            )
            if activity is not None:
                activity_candidates.append((address, activity))

        missing_blocks = sorted(
            {
                activity["last_activity_block"]
                for _address, activity in activity_candidates
                if activity["last_activity_block"]
                not in canonical_hashes
            }
        )
        block_results = client.batch(
            [
                (
                    "hmyv2_getBlockByNumber",
                    [
                        number,
                        {"fullTx": False, "inclStaking": False},
                    ],
                )
                for number in missing_blocks
            ]
        )
        for number, (block, error) in zip(
            missing_blocks, block_results
        ):
            if error or not isinstance(block, dict):
                raise ValueError(
                    f"canonical block {number} lookup failed: "
                    f"{error or 'missing result'}"
                )
            if integer(block.get("number"), "canonical block") != number:
                raise ValueError(
                    f"canonical block {number} returned wrong number"
                )
            block_hash = str(block.get("hash", "")).lower()
            if not block_hash.startswith("0x"):
                raise ValueError(
                    f"canonical block {number} is missing its hash"
                )
            canonical_hashes[number] = block_hash
        for address, activity in activity_candidates:
            if (
                activity.pop("_block_hash")
                != canonical_hashes[activity["last_activity_block"]]
            ):
                stats["stale_index_entries"] += 1
                continue
            by_address[address].append(activity)

        next_pending = {}
        for address, page in page_items:
            activities = by_address[address]
            if activities:
                results[address] = max(
                    activities, key=activity_sort_key
                )
            elif len(page_hashes[address]) < page_size:
                results[address] = None
            elif page + 1 >= max_pages:
                raise ValueError(
                    f"history exceeded {max_pages} pages for {address}"
                )
            else:
                next_pending[address] = page + 1
        pending = next_pending
    return results


def checkpoint_config(args, input_sha256, manifest_sha256, snapshot):
    return {
        "schema_version": 1,
        "input_sha256": input_sha256,
        "snapshot_manifest_sha256": manifest_sha256,
        "rpc": args.rpc,
        "shard": args.shard,
        "cutoff": snapshot,
        "page_size": args.page_size,
    }


def load_checkpoint(path, config):
    if not os.path.exists(path):
        return {"config": config, "records": {}}
    with open(path, encoding="utf-8") as source:
        result = json.load(source)
    if result.get("config") != config:
        raise ValueError("checkpoint configuration does not match")
    if not isinstance(result.get("records"), dict):
        raise ValueError("checkpoint records are malformed")
    return result


def write_json_atomic(path, value):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    partial = path + ".partial"
    if os.path.exists(partial):
        os.remove(partial)
    with open(partial, "x", encoding="utf-8") as output:
        json.dump(value, output, indent=1, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def combine(records):
    present = [record for record in records if record is not None]
    if not present:
        return {field: None for field in ACTIVITY_FIELDS}
    return max(present, key=activity_sort_key)


def write_output(input_path, output_path, records):
    partial = output_path + ".partial"
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(input_path, newline="") as source, open(
        partial, "x", newline=""
    ) as output:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(
            output,
            fieldnames=("secure_key", "address", *ACTIVITY_FIELDS),
            lineterminator="\n",
        )
        writer.writeheader()
        for line, row in enumerate(reader, start=2):
            address = lib.normalize_address(row["address"])
            record = records.get(address)
            if record is None:
                raise ValueError(
                    f"checkpoint missing input line {line}"
                )
            writer.writerow(
                {
                    "secure_key": row["secure_key"].lower(),
                    "address": row["address"],
                    **{
                        field: (
                            "" if record[field] is None else record[field]
                        )
                        for field in ACTIVITY_FIELDS
                    },
                }
            )
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, output_path)


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path + ".partial")
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)

    snapshot = load_snapshot(args.snapshot_manifest, args.shard)
    input_sha256 = file_sha256(args.input)
    manifest_sha256 = file_sha256(args.snapshot_manifest)
    candidates = load_candidates(
        args.input, args.shard, snapshot["block"]
    )
    client = lib.RpcClient(
        args.rpc,
        batch_size=args.batch_size,
        workers=args.workers,
    )
    cutoff_block = client.call(
        "hmyv2_getBlockByNumber",
        [
            snapshot["block"],
            {"fullTx": False, "inclStaking": False},
        ],
    )
    if str(cutoff_block.get("hash", "")).lower() != snapshot["hash"]:
        raise ValueError("RPC cutoff block hash does not match manifest")

    config = checkpoint_config(
        args, input_sha256, manifest_sha256, snapshot
    )
    checkpoint = load_checkpoint(args.checkpoint, config)
    records = checkpoint["records"]
    addresses = {address for _key, address in candidates}
    if set(records) - addresses:
        raise ValueError("checkpoint contains unknown addresses")
    remaining = [
        address for _key, address in candidates if address not in records
    ]
    specs = source_specs(args.shard)
    canonical_hashes = {snapshot["block"]: snapshot["hash"]}
    stats = Counter()
    for offset in range(0, len(remaining), args.chunk_size):
        chunk = remaining[offset : offset + args.chunk_size]
        source_results = []
        for spec in specs:
            print(
                f"activity shard={args.shard} "
                f"type={spec['activity_type']} "
                f"chunk={offset // args.chunk_size + 1}",
                file=sys.stderr,
                flush=True,
            )
            source_results.append(
                collect_source(
                    client,
                    chunk,
                    spec,
                    args.shard,
                    snapshot,
                    args.page_size,
                    args.max_pages,
                    canonical_hashes,
                    stats,
                )
            )
        for address in chunk:
            records[address] = combine(
                [source[address] for source in source_results]
            )
        write_json_atomic(args.checkpoint, checkpoint)
        print(
            f"activity shard={args.shard}: "
            f"{len(records)}/{len(candidates)} candidates",
            file=sys.stderr,
            flush=True,
        )

    if set(records) != addresses:
        raise ValueError("checkpoint does not cover all candidates")
    write_output(args.input, args.output, records)
    found = sum(
        record["last_activity_timestamp_unix"] is not None
        for record in records.values()
    )
    result = {
        "schema_version": 1,
        "status": "passed",
        "source_kind": (
            "Harmony archival-node built-in transaction-history RPC; "
            "no Explorer website or REST API"
        ),
        "db_path": None,
        "candidates_path": args.input,
        "candidates_sha256": input_sha256,
        "candidates": len(candidates),
        "shard": args.shard,
        "cutoff_block": snapshot["block"],
        "cutoff_hash": snapshot["hash"],
        "transaction_index_tail": None,
        "activity_found": found,
        "activity_not_found": len(candidates) - found,
        "rpc": args.rpc,
        "requests_sent": client.requests_sent,
        "items_sent": client.items_sent,
        "canonical_blocks_checked": len(canonical_hashes),
        "stale_index_entries": stats["stale_index_entries"],
        "checkpoint": args.checkpoint,
        "checkpoint_sha256": file_sha256(args.checkpoint),
        "output_path": args.output,
        "output_sha256": file_sha256(args.output),
    }
    write_json_atomic(args.summary, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
