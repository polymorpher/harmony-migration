#!/usr/bin/env python3

"""Prepare and merge incremental account-activity scans."""

import argparse
import csv
import hashlib
import json
import os


ACTIVITY_FIELDS = (
    "secure_key",
    "address",
    "last_activity_time_utc",
    "last_activity_timestamp_unix",
    "last_activity_block",
    "last_activity_shard",
    "last_activity_type",
    "last_activity_tx_hash",
    "last_activity_index",
    "last_activity_detail",
)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--candidates", required=True)
    prepare.add_argument("--existing-activity", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--summary", required=True)

    merge = subparsers.add_parser("merge")
    merge.add_argument("--candidates", required=True)
    merge.add_argument("--existing-activity", required=True)
    merge.add_argument("--existing-summary", required=True)
    merge.add_argument("--incremental-activity", required=True)
    merge.add_argument("--incremental-summary", required=True)
    merge.add_argument("--output", required=True)
    merge.add_argument("--summary", required=True)
    merge.add_argument("--replace", action="store_true")
    return parser.parse_args()


def load_activity(path):
    records = {}
    previous = None
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != ACTIVITY_FIELDS:
            raise ValueError(f"{path}: unexpected activity fields")
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(
                    f"{path}:{line}: secure keys are not increasing"
                )
            previous = key
            if key in records:
                raise ValueError(f"{path}:{line}: duplicate secure key")
            records[key] = row
    return records


def load_summary(path):
    with open(path, encoding="utf-8") as source:
        summary = json.load(source)
    if summary.get("status") != "passed":
        raise ValueError(f"{path}: activity summary did not pass")
    return summary


def provenance_classification(summary):
    if summary.get("db_path") and summary.get("explorer_db_path"):
        return "database-derived"
    if summary.get("rpc") or "RPC" in str(summary.get("source_kind", "")):
        return "RPC-derived"
    raise ValueError("activity summary has unknown provenance")


def provenance_row_text(value, kind):
    suffix = "row" if value == 1 else "rows"
    return f"{value:,} {kind} {suffix}"


def prepare(args):
    for path in (args.output, args.summary):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(path)
    existing = load_activity(args.existing_activity)
    rows = 0
    missing = 0
    previous = None
    with open(args.candidates, newline="") as source, open(
        args.output + ".partial", "x", newline=""
    ) as output:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not {
            "secure_key",
            "address",
        } <= set(reader.fieldnames):
            raise ValueError("candidate CSV is missing identity fields")
        writer = csv.DictWriter(
            output,
            fieldnames=reader.fieldnames,
            lineterminator="\n",
        )
        writer.writeheader()
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(
                    f"candidates:{line}: secure keys are not increasing"
                )
            previous = key
            rows += 1
            if key not in existing:
                writer.writerow(row)
                missing += 1
            elif (
                existing[key]["address"].lower()
                != row["address"].lower()
            ):
                raise ValueError(
                    f"candidates:{line}: existing address mismatch"
                )
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    if len(existing) + missing != rows:
        raise ValueError("existing activity is not a subset of candidates")
    result = {
        "status": "passed",
        "candidates": args.candidates,
        "candidates_sha256": file_sha256(args.candidates),
        "existing_activity": args.existing_activity,
        "existing_activity_sha256": file_sha256(args.existing_activity),
        "candidate_rows": rows,
        "existing_rows": len(existing),
        "incremental_rows": missing,
        "output": args.output,
        "output_sha256": file_sha256(args.output),
    }
    with open(args.summary + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(result, sort_keys=True))


def merge(args):
    for path in (args.output, args.summary):
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path + ".partial")
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)
    existing = load_activity(args.existing_activity)
    incremental = load_activity(args.incremental_activity)
    overlap = set(existing) & set(incremental)
    if overlap:
        raise ValueError("existing and incremental activity overlap")
    records = {**existing, **incremental}
    candidate_addresses = {}
    with open(args.candidates, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if key in candidate_addresses:
                raise ValueError(
                    f"candidates:{line}: duplicate secure key"
                )
            candidate_addresses[key] = row["address"].lower()
    if set(records) != set(candidate_addresses):
        raise ValueError("activity union does not equal candidate set")
    for key, row in records.items():
        if row["address"].lower() != candidate_addresses[key]:
            raise ValueError(f"activity address mismatch for {key}")

    existing_summary = load_summary(args.existing_summary)
    incremental_summary = load_summary(args.incremental_summary)
    for field in ("shard", "cutoff_block", "cutoff_hash"):
        if existing_summary.get(field) != incremental_summary.get(field):
            raise ValueError(f"activity summaries disagree on {field}")
    if existing_summary.get("output_sha256") != file_sha256(
        args.existing_activity
    ):
        raise ValueError("existing activity hash mismatch")
    if incremental_summary.get("output_sha256") != file_sha256(
        args.incremental_activity
    ):
        raise ValueError("incremental activity hash mismatch")

    with open(args.output + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=ACTIVITY_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        for key in sorted(records):
            writer.writerow(records[key])
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    found = sum(
        bool(row["last_activity_timestamp_unix"])
        for row in records.values()
    )
    existing_classification = provenance_classification(existing_summary)
    incremental_classification = provenance_classification(
        incremental_summary
    )
    classifications = {
        existing_classification,
        incremental_classification,
    }
    classification = (
        classifications.pop() if len(classifications) == 1 else "hybrid"
    )
    provenance_breakdown = {
        existing_classification: {
            "rows": int(existing_summary["candidates"]),
            "activity_found": int(existing_summary["activity_found"]),
            "activity_not_found": int(
                existing_summary["activity_not_found"]
            ),
            "source_kind": existing_summary["source_kind"],
            "sources": [
                {
                    "rows": int(existing_summary["candidates"]),
                    "activity_found": int(
                        existing_summary["activity_found"]
                    ),
                    "activity_not_found": int(
                        existing_summary["activity_not_found"]
                    ),
                    "source_kind": existing_summary["source_kind"],
                    "summary": args.existing_summary,
                    "summary_sha256": file_sha256(
                        args.existing_summary
                    ),
                }
            ],
        }
    }
    incremental_bucket = provenance_breakdown.setdefault(
        incremental_classification,
        {
            "rows": 0,
            "activity_found": 0,
            "activity_not_found": 0,
            "source_kind": incremental_summary["source_kind"],
            "sources": [],
        },
    )
    incremental_source = {
        "rows": int(incremental_summary["candidates"]),
        "activity_found": int(incremental_summary["activity_found"]),
        "activity_not_found": int(
            incremental_summary["activity_not_found"]
        ),
        "source_kind": incremental_summary["source_kind"],
        "summary": args.incremental_summary,
        "summary_sha256": file_sha256(args.incremental_summary),
    }
    incremental_bucket["rows"] += incremental_source["rows"]
    incremental_bucket["activity_found"] += incremental_source[
        "activity_found"
    ]
    incremental_bucket["activity_not_found"] += incremental_source[
        "activity_not_found"
    ]
    incremental_bucket["sources"].append(incremental_source)
    for bucket in provenance_breakdown.values():
        source_kinds = list(
            dict.fromkeys(
                source["source_kind"] for source in bucket["sources"]
            )
        )
        bucket["source_kind"] = (
            source_kinds[0]
            if len(source_kinds) == 1
            else "multiple: " + "; ".join(source_kinds)
        )
        if len(bucket["sources"]) == 1:
            source = bucket["sources"][0]
            bucket["summary"] = source["summary"]
            bucket["summary_sha256"] = source["summary_sha256"]
    if sum(bucket["rows"] for bucket in provenance_breakdown.values()) != len(
        records
    ):
        raise ValueError("activity provenance row counts do not close")
    result = {
        "schema_version": 1,
        "status": "passed",
        "classification": classification,
        "source_kind": (
            "hybrid: "
            + " plus ".join(
                provenance_row_text(
                    provenance_breakdown[kind]["rows"],
                    kind,
                )
                for kind in ("database-derived", "RPC-derived")
                if kind in provenance_breakdown
            )
            if classification == "hybrid"
            else f"{classification} activity"
        ),
        "provenance_breakdown": provenance_breakdown,
        "candidates_path": args.candidates,
        "candidates_sha256": file_sha256(args.candidates),
        "candidates": len(records),
        "shard": existing_summary["shard"],
        "cutoff_block": existing_summary["cutoff_block"],
        "cutoff_hash": existing_summary["cutoff_hash"],
        "transaction_index_tail": None,
        "activity_found": found,
        "activity_not_found": len(records) - found,
        "existing_summary": args.existing_summary,
        "existing_summary_sha256": file_sha256(args.existing_summary),
        "incremental_summary": args.incremental_summary,
        "incremental_summary_sha256": file_sha256(
            args.incremental_summary
        ),
        "output_path": args.output,
        "output_sha256": file_sha256(args.output),
    }
    with open(args.summary + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(result, sort_keys=True))


def main():
    args = parse_args()
    if args.command == "prepare":
        prepare(args)
    else:
        merge(args)


if __name__ == "__main__":
    main()
