#!/usr/bin/env python3

"""Convert the audited treasury inventory into explicit routing rows."""

import argparse
import csv
import hashlib
import json
import os


FIELDS = (
    "route_id",
    "priority",
    "source_address",
    "destination_id",
    "destination_address",
    "amount_atto",
    "allocation_method",
    "reason",
    "evidence",
    "notes",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--destination-id", default="treasury")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    rows = []
    seen = set()
    total = 0
    with open(args.inventory, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address_hex",
            "category",
            "treasury_reclaim_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"inventory is missing fields: {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            amount = int(row["treasury_reclaim_atto"])
            if amount < 0:
                raise ValueError(f"negative reclaim at line {line}")
            if amount == 0:
                continue
            address = row["address_hex"].lower()
            if address in seen:
                raise ValueError(f"duplicate inventory address at line {line}")
            seen.add(address)
            category = row["category"]
            route_id = f"treasury-{category}-{address[2:]}"
            rows.append(
                {
                    "route_id": route_id,
                    "priority": "100",
                    "source_address": address,
                    "destination_id": args.destination_id,
                    "destination_address": "",
                    "amount_atto": str(amount),
                    "allocation_method": "wallet_first_pro_rata_vault",
                    "reason": category,
                    "evidence": (
                        "artifacts/supply-reconciliation-20260911/"
                        "treasury-reclaim-inventory.csv"
                    ),
                    "notes": "generated from audited treasury inventory",
                }
            )
            total += amount
    rows.sort(key=lambda row: (int(row["priority"]), row["route_id"]))
    with open(args.output + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=FIELDS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    result = {
        "status": "passed",
        "inventory": args.inventory,
        "inventory_sha256": file_sha256(args.inventory),
        "routes": len(rows),
        "routed_atto": str(total),
        "destination_id": args.destination_id,
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


if __name__ == "__main__":
    main()
