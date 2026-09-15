#!/usr/bin/env python3

"""Convert the audited incident inventory into exact non-issuance routes."""

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
DESTINATION_ID = "not-issuing"
AMOUNT_FIELD = "treasury_reclaim_atto"
ALLOWED_CATEGORIES = {
    "blacklisted_extra_mint_recipient",
    "burn_or_inaccessible",
    "reported_wallet_theft_perpetrator",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
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


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path + ".partial")
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    rows = []
    seen = set()
    total = 0
    categories = {}
    with open(args.inventory, newline="") as source:
        reader = csv.DictReader(source)
        required = {"address_hex", "category", AMOUNT_FIELD}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"inventory is missing fields: {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            amount = int(row[AMOUNT_FIELD])
            if amount < 0:
                raise ValueError(f"negative amount at line {line}")
            address = row["address_hex"].lower()
            if (
                len(address) != 42
                or not address.startswith("0x")
                or any(
                    character not in "0123456789abcdef"
                    for character in address[2:]
                )
            ):
                raise ValueError(f"invalid address at line {line}")
            if address in seen:
                raise ValueError(
                    f"duplicate inventory address at line {line}"
                )
            seen.add(address)
            category = row["category"]
            if category not in ALLOWED_CATEGORIES:
                raise ValueError(
                    f"unsupported category at line {line}: {category}"
                )
            category_stats = categories.setdefault(
                category, {"inventory_rows": 0, "routes": 0, "amount_atto": 0}
            )
            category_stats["inventory_rows"] += 1
            if amount == 0:
                continue
            category_stats["routes"] += 1
            category_stats["amount_atto"] += amount
            rows.append(
                {
                    "route_id": f"not-issuing-{category}-{address[2:]}",
                    "priority": "100",
                    "source_address": address,
                    "destination_id": DESTINATION_ID,
                    "destination_address": "",
                    "amount_atto": str(amount),
                    "allocation_method": "wallet_first_pro_rata_vault",
                    "reason": f"not_issuing_{category}",
                    "evidence": (
                        "artifacts/supply-reconciliation-20260911/"
                        "treasury-reclaim-inventory.csv"
                    ),
                    "notes": (
                        "exact amount previously assigned to treasury; "
                        "now excluded from issuance"
                    ),
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
        "policy": (
            "do not issue the exact amounts previously assigned to treasury; "
            "preserve every existing remainder at its ordinary destination"
        ),
        "inventory": args.inventory,
        "inventory_sha256": file_sha256(args.inventory),
        "source_amount_field": AMOUNT_FIELD,
        "inventory_rows": len(seen),
        "routes": len(rows),
        "not_issued_atto": str(total),
        "destination_id": DESTINATION_ID,
        "categories": {
            category: {
                "inventory_rows": values["inventory_rows"],
                "routes": values["routes"],
                "not_issued_atto": str(values["amount_atto"]),
            }
            for category, values in sorted(categories.items())
        },
        "output": args.output,
        "output_sha256": file_sha256(args.output),
    }
    with open(args.summary + ".partial", "x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
