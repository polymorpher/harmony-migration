#!/usr/bin/env python3

"""Convert audited inventories into exact non-issuance routes."""

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
ALLOWED_CATEGORIES = {
    "blacklisted_extra_mint_recipient",
    "burn_or_inaccessible",
    "historical_incident_retained_cap",
    "report_linked_theft_recipient",
    "reported_wallet_theft_perpetrator",
    "revert_leak_credit_recipient",
}
# The revert-leak inventory is computed after the retained-cap deduction, so
# an address may appear in both; every other combination is an overlap error.
STACKABLE_CATEGORIES = {
    frozenset({"historical_incident_retained_cap", "revert_leak_credit_recipient"}),
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inventory",
        action="append",
        required=True,
        help=(
            "audited inventory; repeatable. Existing inventories use "
            "not_issued_atto; retained historical and revert-leak "
            "inventories use retained_cap_atto"
        ),
    )
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
    seen = {}
    total = 0
    categories = {}
    inventory_records = []
    for inventory in args.inventory:
        inventory_rows = 0
        inventory_routes = 0
        inventory_total = 0
        with open(inventory, newline="") as source:
            reader = csv.DictReader(source)
            fields = set(reader.fieldnames or ())
            if "address_hex" not in fields:
                raise ValueError(f"{inventory}: missing address_hex")
            if {"category", "not_issued_atto"} <= fields:
                amount_field = "not_issued_atto"
                fixed_category = None
            elif {"unbacked_credit_atto", "retained_cap_atto", "migration_treatment"} <= fields:
                amount_field = "retained_cap_atto"
                fixed_category = "revert_leak_credit_recipient"
            elif {"incident", "retained_cap_atto", "migration_treatment"} <= fields:
                amount_field = "retained_cap_atto"
                fixed_category = "historical_incident_retained_cap"
            else:
                raise ValueError(
                    f"{inventory}: unsupported non-issuance inventory schema"
                )
            for line, row in enumerate(reader, start=2):
                amount = int(row[amount_field])
                if amount < 0:
                    raise ValueError(f"{inventory}:{line}: negative amount")
                address = row["address_hex"].lower()
                if (
                    len(address) != 42
                    or not address.startswith("0x")
                    or any(
                        character not in "0123456789abcdef"
                        for character in address[2:]
                    )
                ):
                    raise ValueError(f"{inventory}:{line}: invalid address")
                category = fixed_category or row["category"]
                if address in seen and frozenset(
                    {seen[address], category}
                ) not in STACKABLE_CATEGORIES:
                    raise ValueError(
                        f"{inventory}:{line}: duplicate inventory address"
                    )
                seen[address] = category
                if category not in ALLOWED_CATEGORIES:
                    raise ValueError(
                        f"{inventory}:{line}: unsupported category {category}"
                    )
                if fixed_category and row["migration_treatment"] != "not_issued":
                    raise ValueError(
                        f"{inventory}:{line}: {fixed_category} row is not not_issued"
                    )
                category_stats = categories.setdefault(
                    category,
                    {"inventory_rows": 0, "routes": 0, "amount_atto": 0},
                )
                category_stats["inventory_rows"] += 1
                inventory_rows += 1
                if amount == 0:
                    continue
                category_stats["routes"] += 1
                category_stats["amount_atto"] += amount
                inventory_routes += 1
                inventory_total += amount
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
                        "evidence": inventory,
                        "notes": (
                            "exact reviewed amount excluded from issuance"
                        ),
                    }
                )
                total += amount
        inventory_records.append(
            {
                "path": inventory,
                "sha256": file_sha256(inventory),
                "amount_field": amount_field,
                "inventory_rows": inventory_rows,
                "routes": inventory_routes,
                "not_issued_atto": str(inventory_total),
            }
        )

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
            "do not issue the exact reviewed amounts; preserve every "
            "existing remainder at its ordinary destination"
        ),
        "inventories": inventory_records,
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
