#!/usr/bin/env python3

"""Merge per-shard canonical activity into a prioritized claim CSV."""

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
    parser.add_argument("--shard0-activity", required=True)
    parser.add_argument("--shard0-summary", required=True)
    parser.add_argument("--shard1-activity", required=True)
    parser.add_argument("--shard1-summary", required=True)
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


def parse_utc(value):
    if not value.endswith("Z"):
        raise ValueError(f"UTC timestamp must end in Z: {value}")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"timestamp is not UTC: {value}")
    return parsed


def load_snapshot(path):
    with open(path, encoding="utf-8") as source:
        manifest = json.load(source)
    cutoff = manifest.get("cutoff") or {}
    cutoff_text = cutoff.get("requested_time_utc")
    if not cutoff_text:
        raise ValueError("snapshot manifest is missing cutoff time")
    cutoff_time = parse_utc(cutoff_text)
    shards = {}
    for shard in (0, 1):
        source = cutoff.get(f"shard{shard}") or {}
        if (
            not isinstance(source.get("block"), int)
            or not source.get("hash")
        ):
            raise ValueError(
                f"snapshot manifest is missing shard {shard}"
            )
        shards[str(shard)] = {
            "block": source["block"],
            "hash": source["hash"].lower(),
        }
    return {
        "requested_time_utc": cutoff_text,
        "requested_timestamp_unix": int(cutoff_time.timestamp()),
        "shards": shards,
    }


def load_scan_summary(path, activity_path, input_sha256, snapshot, shard):
    with open(path, encoding="utf-8") as source:
        summary = json.load(source)
    expected = snapshot["shards"][str(shard)]
    if summary.get("status") != "passed":
        raise ValueError(f"shard {shard} activity scan did not pass")
    if summary.get("shard") != shard:
        raise ValueError(f"shard {shard} summary has wrong shard")
    if (
        summary.get("cutoff_block") != expected["block"]
        or str(summary.get("cutoff_hash", "")).lower()
        != expected["hash"]
    ):
        raise ValueError(f"shard {shard} scan used the wrong cutoff")
    if summary.get("candidates_sha256") != input_sha256:
        raise ValueError(
            f"shard {shard} scan used a different candidate CSV"
        )
    if summary.get("output_sha256") != file_sha256(activity_path):
        raise ValueError(
            f"shard {shard} activity CSV does not match its summary"
        )
    if summary.get("transaction_index_tail") not in (None, 0):
        raise ValueError(
            f"shard {shard} transaction lookup table is incomplete"
        )
    if (
        summary.get("activity_found", 0)
        + summary.get("activity_not_found", 0)
        != summary.get("candidates")
    ):
        raise ValueError(
            f"shard {shard} activity coverage does not close"
        )
    return summary


def load_activity(path, shard, snapshot):
    records = {}
    previous_key = None
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"secure_key", "address", *ACTIVITY_FIELDS}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"shard {shard} activity is missing fields: "
                f"{sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous_key is not None and key <= previous_key:
                raise ValueError(
                    f"shard {shard} secure keys are not increasing "
                    f"at line {line}"
                )
            previous_key = key
            address = lib.require_address_secure_key(
                row["address"],
                key,
                f"shard {shard} activity line {line}",
            )
            timestamp = row["last_activity_timestamp_unix"]
            other = [
                row[field]
                for field in ACTIVITY_FIELDS
                if field != "last_activity_timestamp_unix"
            ]
            if not timestamp:
                if any(other):
                    raise ValueError(
                        f"shard {shard} line {line} has partial activity"
                    )
            else:
                if not all(other):
                    raise ValueError(
                        f"shard {shard} line {line} has partial activity"
                    )
                if int(row["last_activity_shard"]) != shard:
                    raise ValueError(
                        f"shard {shard} line {line} has wrong shard"
                    )
                if (
                    int(row["last_activity_block"])
                    > snapshot["shards"][str(shard)]["block"]
                ):
                    raise ValueError(
                        f"shard {shard} line {line} is after cutoff"
                    )
                activity_time = parse_utc(
                    row["last_activity_time_utc"]
                )
                if (
                    int(activity_time.timestamp()) != int(timestamp)
                    or int(timestamp)
                    > snapshot["requested_timestamp_unix"]
                ):
                    raise ValueError(
                        f"shard {shard} line {line} has invalid time"
                    )
            if key in records:
                raise ValueError(
                    f"shard {shard} activity duplicates {key}"
                )
            records[key] = {
                "address": address,
                **{field: row[field] for field in ACTIVITY_FIELDS},
            }
    return records


def activity_sort_key(record):
    return (
        int(record["last_activity_timestamp_unix"]),
        int(record["last_activity_shard"]),
        int(record["last_activity_block"]),
        int(record["last_activity_index"]),
        record["last_activity_type"],
        record["last_activity_detail"],
        record["last_activity_tx_hash"],
    )


def select_activity(records):
    present = [
        record
        for record in records
        if record["last_activity_timestamp_unix"]
    ]
    if not present:
        return {field: "" for field in ACTIVITY_FIELDS}
    selected = max(present, key=activity_sort_key)
    return {field: selected[field] for field in ACTIVITY_FIELDS}


def write_enriched(input_path, output_path, by_shard):
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    partial = output_path + ".partial"
    counts = Counter()
    timestamps = []
    rows = 0
    with open(input_path, newline="") as source, open(
        partial, "x", newline=""
    ) as output:
        reader = csv.DictReader(source)
        fields = tuple(reader.fieldnames or ())
        overlap = set(fields) & set(ACTIVITY_FIELDS)
        if overlap:
            raise ValueError(
                f"input already has activity fields: {sorted(overlap)}"
            )
        writer = csv.DictWriter(
            output,
            fieldnames=fields + ACTIVITY_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            try:
                shard_records = (
                    by_shard["0"][key],
                    by_shard["1"][key],
                )
            except KeyError as error:
                raise ValueError(
                    f"activity is missing input line {line}"
                ) from error
            address = lib.normalize_address(row["address"])
            if any(
                record["address"] != address
                for record in shard_records
            ):
                raise ValueError(
                    f"activity address differs at input line {line}"
                )
            selected = select_activity(shard_records)
            row.update(selected)
            writer.writerow(row)
            rows += 1
            timestamp = selected["last_activity_timestamp_unix"]
            if timestamp:
                timestamps.append(int(timestamp))
                counts[selected["last_activity_type"]] += 1
                counts[f"shard_{selected['last_activity_shard']}"] += 1
            else:
                counts["not_found"] += 1
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, output_path)
    return rows, counts, timestamps


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path + ".partial")
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)

    snapshot = load_snapshot(args.snapshot_manifest)
    input_sha256 = file_sha256(args.input)
    sources = (
        (0, args.shard0_activity, args.shard0_summary),
        (1, args.shard1_activity, args.shard1_summary),
    )
    summaries = {
        str(shard): load_scan_summary(
            summary_path,
            activity_path,
            input_sha256,
            snapshot,
            shard,
        )
        for shard, activity_path, summary_path in sources
    }
    by_shard = {
        str(shard): load_activity(activity_path, shard, snapshot)
        for shard, activity_path, _summary_path in sources
    }
    if set(by_shard["0"]) != set(by_shard["1"]):
        raise ValueError("per-shard activity candidate sets differ")
    rows, counts, timestamps = write_enriched(
        args.input, args.output, by_shard
    )
    if rows != len(by_shard["0"]):
        raise ValueError("candidate CSV and activity row counts differ")

    result = {
        "schema_version": 1,
        "status": "passed",
        "activity_definition": (
            "latest canonical direct regular transaction involving the "
            "address on shard 0 or shard 1, or canonical shard-0 staking "
            "transaction involving the address, at or before the claim "
            "cutoff; internal EVM calls and validator consensus signatures "
            "are excluded"
        ),
        "source_kind": (
            "archival-node per-address indexes with canonical block "
            "verification; no Explorer website or REST API"
        ),
        "input": args.input,
        "input_sha256": input_sha256,
        "snapshot_manifest": args.snapshot_manifest,
        "snapshot_manifest_sha256": file_sha256(
            args.snapshot_manifest
        ),
        "cutoff": snapshot,
        "candidates": rows,
        "activity_found": len(timestamps),
        "activity_not_found": rows - len(timestamps),
        "activity_by_type": {
            key: counts[key] for key in ("regular", "staking")
        },
        "activity_by_shard": {
            key[-1]: counts[key]
            for key in ("shard_0", "shard_1")
        },
        "earliest_activity_time_utc": (
            datetime.fromtimestamp(
                min(timestamps), timezone.utc
            )
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
            if timestamps
            else None
        ),
        "latest_activity_time_utc": (
            datetime.fromtimestamp(
                max(timestamps), timezone.utc
            )
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
            if timestamps
            else None
        ),
        "sources": {
            str(shard): {
                "activity": activity_path,
                "activity_sha256": file_sha256(activity_path),
                "summary": summary_path,
                "summary_sha256": file_sha256(summary_path),
                "scan": summaries[str(shard)],
            }
            for shard, activity_path, summary_path in sources
        },
        "output": args.output,
        "output_sha256": file_sha256(args.output),
    }
    parent = os.path.dirname(args.summary)
    if parent:
        os.makedirs(parent, exist_ok=True)
    partial = args.summary + ".partial"
    with open(partial, "x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, args.summary)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
