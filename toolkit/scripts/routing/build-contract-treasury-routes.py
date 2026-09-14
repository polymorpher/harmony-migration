#!/usr/bin/env python3

"""Route non-multisig claims to a dedicated contract-funds holding address."""

import argparse
import csv
import hashlib
import json
import os
from collections import Counter


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
    "policy_state_block",
    "policy_state_block_hash",
    "policy_state_root",
)
DESTINATION_ID = "contract-recovery-custody"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--priority", type=int, default=500)
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

    routes = []
    categories = Counter()
    seen = set()
    input_rows = 0
    validators = 0
    multisigs = 0
    with open(args.contracts, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "primary_category",
            "subcategory",
            "total_claim_one",
            "policy_state_block",
            "policy_state_block_hash",
            "policy_state_root",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"contract review is missing fields: {sorted(missing)}"
            )
        policy_state = None
        for line, row in enumerate(reader, start=2):
            row_policy_state = {
                "block": int(row["policy_state_block"]),
                "block_hash": row["policy_state_block_hash"],
                "state_root": row["policy_state_root"],
            }
            if not row_policy_state["block_hash"] or not row_policy_state[
                "state_root"
            ]:
                raise ValueError(
                    f"contract review has incomplete policy state at line {line}"
                )
            if policy_state is None:
                policy_state = row_policy_state
            elif row_policy_state != policy_state:
                raise ValueError(
                    f"contract review policy state differs at line {line}"
                )
            input_rows += 1
            category = row["primary_category"]
            if category == "validator-account":
                validators += 1
                continue
            if category == "multisig-wallet":
                multisigs += 1
                continue
            address = row["address"].lower()
            if address in seen:
                raise ValueError(f"duplicate contract at line {line}")
            seen.add(address)
            categories[category] += 1
            routes.append(
                {
                    "route_id": f"contract-custody-{address[2:]}",
                    "priority": str(args.priority),
                    "source_address": address,
                    "destination_id": DESTINATION_ID,
                    "destination_address": "",
                    "amount_atto": "ALL",
                    "allocation_method": "wallet_first_pro_rata_vault",
                    "reason": "non_multisig_contract_recovery_custody",
                    "evidence": (
                        "artifacts/contract-review-20260911/out/"
                        "contract-review-policy.csv"
                    ),
                    "notes": f"{category}: {row['subcategory']}",
                    "policy_state_block": row["policy_state_block"],
                    "policy_state_block_hash": row[
                        "policy_state_block_hash"
                    ],
                    "policy_state_root": row["policy_state_root"],
                }
            )

    if input_rows != validators + multisigs + len(routes):
        raise ValueError("contract category partition does not close")
    routes.sort(key=lambda row: row["source_address"])
    with open(args.output + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=FIELDS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(routes)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)

    result = {
        "status": "passed",
        "input": args.contracts,
        "input_sha256": file_sha256(args.contracts),
        "input_rows": input_rows,
        "validator_rows_skipped": validators,
        "multisig_rows_skipped": multisigs,
        "non_multisig_contract_routes": len(routes),
        "categories": dict(sorted(categories.items())),
        "destination_id": DESTINATION_ID,
        "priority": args.priority,
        "policy_state": policy_state,
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
