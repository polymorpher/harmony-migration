#!/usr/bin/env python3
"""Rebuild one source direction of a cross-shard-supply report.

    rebuild-receipt-direction.py --base cross-shard-supply-cutoff.json \
        --outgoing shard0-outgoing-receipts.csv \
        --destination-status shard0-to-shard1-destination-status.csv \
        --source-shard 0 --source-cutoff 93623067 --destination-cutoff 95882100 \
        --independent-report reviewer-cross-shard-supply.json \
        --output cross-shard-supply-cutoff.json \
        --retired-output retired-shard-unsupported-receipts-cutoff.csv \
        --summary receipt-correction-summary.json

Use it when a report's source scan read a database that lacked history. The
source receipts come from an outgoing-cx-scan list of an archive database and
the destination status from fetch-cx-destination-status.py. Classification
follows cross-shard-supply: a receipt group toward an active shard is spent
only when every receipt was applied in a canonical destination block at or
before the destination cutoff, and groups toward retired shards are listed
separately. The output is written only when every direction of the rebuilt
report matches the independent report receipt for receipt.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contract-review"))
import contract_review_lib as lib  # noqa: E402

ACTIVE_SHARDS = (0, 1)
RETIRED_FIELDS = (
    "source_shard", "destination_shard", "source_block", "source_block_hash",
    "transaction_hash", "to", "secure_key", "amount_atto",
)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def secure_key(address):
    return "0x" + lib.keccak256(bytes.fromhex(address[2:])).hex()


def load_groups(path, source_shard, source_cutoff):
    groups = OrderedDict()
    later = set()
    with open(path, newline="") as source:
        for row in csv.DictReader(source):
            if int(row["source_shard"]) != source_shard:
                continue
            key = (int(row["destination_shard"]), int(row["source_block"]), row["source_block_hash"].lower())
            if key[1] > source_cutoff:
                later.add(key)
                continue
            groups.setdefault(key, []).append((int(row["receipt_index"]), row))
    return {key: [row for _, row in sorted(rows, key=lambda item: item[0])] for key, rows in groups.items()}, len(later)


def load_status(path):
    with open(path, newline="") as source:
        return {row["transaction_hash"].lower(): row for row in csv.DictReader(source)}


def group_record(destination, number, block_hash, rows):
    receipts = []
    for row in rows:
        to = row["to"].lower()
        receipts.append({
            "amount_atto": str(int(row["amount_atto"])),
            "secure_key": secure_key(to),
            "to": to,
            "transaction_hash": row["tx_hash"].lower(),
        })
    return {
        "amount_atto": str(sum(int(r["amount_atto"]) for r in receipts)),
        "block_hash": block_hash,
        "block_number": number,
        "destination_shard": destination,
        "receipt_count": len(receipts),
        "receipts": receipts,
        "transaction_hashes": [r["transaction_hash"] for r in receipts],
    }


def rebuild(groups, status, source_shard, destination_cutoff):
    totals = {name: 0 for name in (
        "canonical_receipt_groups", "spent_receipt_groups", "spent_receipt_count", "spent_amount_atto",
        "pending_receipt_groups", "pending_receipt_count", "pending_amount_atto",
        "unsupported_receipt_groups", "unsupported_receipt_count", "unsupported_amount_atto",
    )}
    pending, unsupported = [], []
    for (destination, number, block_hash), rows in sorted(groups.items()):
        record = group_record(destination, number, block_hash, rows)
        amount, count = int(record["amount_atto"]), record["receipt_count"]
        if destination not in ACTIVE_SHARDS:
            totals["unsupported_receipt_groups"] += 1
            totals["unsupported_receipt_count"] += count
            totals["unsupported_amount_atto"] += amount
            unsupported.append(record)
            continue
        totals["canonical_receipt_groups"] += 1
        entries = []
        for receipt in record["receipts"]:
            entry = status.get(receipt["transaction_hash"])
            if entry is None:
                raise ValueError(f"no destination status for {receipt['transaction_hash']}")
            entries.append(entry)
        if len({e["destination_block"] for e in entries if e["found"] == "true"}) > 1:
            raise ValueError(f"source block {number} was split across destination blocks")
        applied = [
            e["found"] == "true" and e["canonical"] == "true" and int(e["destination_block"]) <= destination_cutoff
            for e in entries
        ]
        if all(applied):
            totals["spent_receipt_groups"] += 1
            totals["spent_receipt_count"] += count
            totals["spent_amount_atto"] += amount
        else:
            if any(applied):
                raise ValueError(f"source block {number} has both applied and unapplied receipts")
            totals["pending_receipt_groups"] += 1
            totals["pending_receipt_count"] += count
            totals["pending_amount_atto"] += amount
            pending.append(record)
    direction = {"source_shard": source_shard, **{
        key: (str(value) if key.endswith("_atto") else value) for key, value in totals.items()
    }}
    direction.update({
        "empty_receipt_groups": None,
        "missing_canonical_hash_groups": 0,
        "noncanonical_receipt_groups": 0,
        "pending_groups": pending,
        "unsupported_groups": unsupported,
    })
    return direction


def receipt_set(direction, kind):
    return sorted(
        (int(group["block_number"]), receipt["transaction_hash"].lower(), receipt["to"].lower(), int(receipt["amount_atto"]))
        for group in direction.get(kind) or []
        for receipt in group.get("receipts") or []
    )


AGREEMENT_FIELDS = (
    "canonical_receipt_groups", "spent_receipt_groups", "spent_receipt_count", "spent_amount_atto",
    "pending_receipt_groups", "pending_receipt_count", "pending_amount_atto",
    "unsupported_receipt_groups", "unsupported_receipt_count", "unsupported_amount_atto",
)


def agreement(direction, independent):
    differences = [
        f"{field}: {direction.get(field)} != {independent.get(field)}"
        for field in AGREEMENT_FIELDS
        if str(direction.get(field)) != str(independent.get(field))
    ]
    for kind in ("pending_groups", "unsupported_groups"):
        if receipt_set(direction, kind) != receipt_set(independent, kind):
            differences.append(f"{kind} receipts differ")
    return differences


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--outgoing", required=True)
    parser.add_argument("--destination-status", required=True)
    parser.add_argument("--source-shard", type=int, required=True)
    parser.add_argument("--source-cutoff", type=int, required=True)
    parser.add_argument("--destination-cutoff", type=int, required=True)
    parser.add_argument("--independent-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--retired-output", required=True)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()
    for path in (args.output, args.retired_output, args.summary):
        if os.path.exists(path):
            raise FileExistsError(path)

    with open(args.base) as source:
        base = json.load(source)
    with open(args.independent_report) as source:
        independent = json.load(source)
    groups, later_groups = load_groups(args.outgoing, args.source_shard, args.source_cutoff)
    rebuilt = rebuild(groups, load_status(args.destination_status), args.source_shard, args.destination_cutoff)
    rebuilt["after_cutoff_groups"] = later_groups

    directions = [rebuilt if d["source_shard"] == args.source_shard else d for d in base["directions"]]
    independent_by_source = {d["source_shard"]: d for d in independent["directions"]}
    differences = {
        str(d["source_shard"]): agreement(d, independent_by_source.get(d["source_shard"], {}))
        for d in directions
    }
    if any(differences.values()):
        raise SystemExit(f"rebuilt report differs from the independent report: {json.dumps(differences)}")

    report = dict(base)
    report["directions"] = directions
    report["pending_active_atto"] = str(sum(int(d["pending_amount_atto"]) for d in directions))
    report["unsupported_pending_atto"] = str(sum(int(d["unsupported_amount_atto"]) for d in directions))
    inputs = {
        "base_report": {"path": args.base, "sha256": sha256(args.base)},
        "outgoing_receipts": {"path": args.outgoing, "sha256": sha256(args.outgoing)},
        "destination_status": {"path": args.destination_status, "sha256": sha256(args.destination_status)},
        "independent_report": {"path": args.independent_report, "sha256": sha256(args.independent_report)},
    }
    report["corrections"] = list(base.get("corrections") or []) + [{
        "source_shard": args.source_shard,
        "reason": "the original source scan read a compact database that lacked outgoing receipts "
                  "before shard-0 block 87,036,986",
        "method": "source receipts from an archive-database outgoing-cx-scan list; destination status from "
                  "shard-1 CX-receipt lookups with canonical block checks; classification as cross-shard-supply",
        "inputs": inputs,
        "independent_agreement": "every direction matches the independent report in counts, amounts, and "
                                 "pending and retired-shard receipts",
    }]

    retired = []
    for direction in directions:
        for group in direction.get("unsupported_groups") or []:
            for receipt in group["receipts"]:
                retired.append((direction["source_shard"], group["destination_shard"], group["block_number"],
                                group["block_hash"], receipt["transaction_hash"], receipt["to"],
                                receipt["secure_key"], receipt["amount_atto"]))
    retired.sort(key=lambda row: (row[0], row[2], row[1], row[4]))

    with open(args.output + ".partial", "x") as output:
        json.dump(report, output, indent=2, sort_keys=True)
        output.write("\n")
    os.replace(args.output + ".partial", args.output)
    with open(args.retired_output + ".partial", "x", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(RETIRED_FIELDS)
        writer.writerows(retired)
    os.replace(args.retired_output + ".partial", args.retired_output)

    summary = {
        "inputs": inputs,
        "outputs": {
            "report": {"path": args.output, "sha256": sha256(args.output)},
            "retired_receipts": {"path": args.retired_output, "sha256": sha256(args.retired_output),
                                 "rows": len(retired)},
        },
        "pending_active_atto": {"before": base["pending_active_atto"], "after": report["pending_active_atto"]},
        "unsupported_pending_atto": {"before": base["unsupported_pending_atto"],
                                     "after": report["unsupported_pending_atto"]},
        "rebuilt_direction": {key: value for key, value in rebuilt.items()
                              if key not in ("pending_groups", "unsupported_groups")},
        "independent_agreement": differences,
    }
    with open(args.summary + ".partial", "x") as output:
        json.dump(summary, output, indent=2, sort_keys=True)
        output.write("\n")
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
