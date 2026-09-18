#!/usr/bin/env python3

"""Build routes for the confirmed reviewed-contract migration policy."""

import argparse
import csv
import hashlib
import json
import os
from collections import Counter


LAYERZERO_ADDRESSES = {
    "0x5b18a4e73f9a4fe337a072516b317863ad3046aa",
    "0x905582f21fb9855c809d5b8933272a292dfbb138",
}
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


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts", required=True)
    parser.add_argument("--stage-policy", required=True)
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


def load_stage_policy(path):
    policy = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "account_classification",
            "policy_group",
            "migration_stage",
            "issuance_treatment",
            "migration_wallet_allocation_atto",
            "migration_staked_to_vault_atto",
            "migration_allocation_atto",
            "reviewed_contract_non_issuance_atto",
            "reviewed_contract_wallet_non_issuance_atto",
            "reviewed_contract_staked_non_issuance_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            address = row["address"].lower()
            if address in policy:
                raise ValueError(f"{path}:{line}: duplicate address")
            migration_allocation = int(row["migration_allocation_atto"])
            not_issued = int(row["reviewed_contract_non_issuance_atto"])
            wallet = int(row["migration_wallet_allocation_atto"])
            staked = int(row["migration_staked_to_vault_atto"])
            not_issued_wallet = int(
                row["reviewed_contract_wallet_non_issuance_atto"]
            )
            not_issued_staked = int(
                row["reviewed_contract_staked_non_issuance_atto"]
            )
            if min(
                migration_allocation,
                wallet,
                staked,
                not_issued,
                not_issued_wallet,
                not_issued_staked,
            ) < 0:
                raise ValueError(f"{path}:{line}: negative policy amount")
            if (
                wallet + staked != migration_allocation
                or not_issued_wallet + not_issued_staked != not_issued
            ):
                raise ValueError(f"{path}:{line}: component mismatch")
            policy[address] = {
                "classification": row["account_classification"],
                "group": row["policy_group"],
                "stage": row["migration_stage"],
                "treatment": row["issuance_treatment"],
                "migration_allocation": migration_allocation,
                "wallet": wallet,
                "staked": staked,
                "not_issued": not_issued,
                "not_issued_wallet": not_issued_wallet,
                "not_issued_staked": not_issued_staked,
            }
    return policy


def route_policy(address, category, policy):
    if policy["classification"] != "genuine_contract":
        raise ValueError(f"{address}: reviewed contract is not classified as such")
    if policy["treatment"] == "not_issued":
        if policy["stage"]:
            raise ValueError(f"{address}: not-issued contract has a stage")
        if policy["migration_allocation"] != 0:
            raise ValueError(f"{address}: not-issued contract has an allocation")
        if policy["not_issued"] <= 0:
            raise ValueError(f"{address}: not-issued contract has no deduction")
        return (
            "not-issuing",
            "reviewed_contract_allocation_not_issued",
        )
    if (
        policy["treatment"] != "issue"
        or policy["stage"] != "next_stage"
        or policy["not_issued"] != 0
    ):
        raise ValueError(f"{address}: unsupported contract stage policy")
    if category == "multisig-wallet" and policy["group"] == "multisig":
        return "", "next_stage_multisig_recovery"
    if category == "onewallet" and policy["group"] == "onewallet":
        return (
            "onewallet-recovery-multisig",
            "next_stage_onewallet_recovery_multisig",
        )
    if (
        address in LAYERZERO_ADDRESSES
        and policy["group"] == "layerzero_bridge_collateral"
    ):
        return None
    raise ValueError(f"{address}: unrecognized approved contract policy")


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

    stages = load_stage_policy(args.stage_policy)
    routes = []
    categories = Counter()
    groups = {}
    seen = set()
    input_rows = 0
    validators = 0
    dedicated_layerzero = 0
    policy_state = None
    with open(args.contracts, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "primary_category",
            "subcategory",
            "policy_state_block",
            "policy_state_block_hash",
            "policy_state_root",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"contract review is missing fields: {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            state = {
                "block": int(row["policy_state_block"]),
                "block_hash": row["policy_state_block_hash"],
                "state_root": row["policy_state_root"],
            }
            if not state["block_hash"] or not state["state_root"]:
                raise ValueError(
                    f"contract review has incomplete policy state at line {line}"
                )
            if policy_state is None:
                policy_state = state
            elif state != policy_state:
                raise ValueError(
                    f"contract review policy state differs at line {line}"
                )
            input_rows += 1
            category = row["primary_category"]
            if category == "validator-account":
                validators += 1
                continue
            address = row["address"].lower()
            if address in seen:
                raise ValueError(f"duplicate contract at line {line}")
            seen.add(address)
            if address not in stages:
                raise ValueError(f"missing stage policy for {address}")
            policy = stages[address]
            destination = route_policy(address, category, policy)
            categories[category] += 1
            group = groups.setdefault(
                policy["group"],
                {
                    "addresses": 0,
                    "routes": 0,
                    "migration_allocation_atto": 0,
                    "wallet_allocation_atto": 0,
                    "staked_allocation_atto": 0,
                    "not_issued_atto": 0,
                    "not_issued_wallet_atto": 0,
                    "not_issued_staked_atto": 0,
                    "migration_stage": policy["stage"],
                    "issuance_treatment": policy["treatment"],
                },
            )
            if (
                group["migration_stage"] != policy["stage"]
                or group["issuance_treatment"] != policy["treatment"]
            ):
                raise ValueError(
                    f"mixed stage or treatment in {policy['group']}"
                )
            group["addresses"] += 1
            group["migration_allocation_atto"] += policy[
                "migration_allocation"
            ]
            group["wallet_allocation_atto"] += policy["wallet"]
            group["staked_allocation_atto"] += policy["staked"]
            group["not_issued_atto"] += policy["not_issued"]
            group["not_issued_wallet_atto"] += policy["not_issued_wallet"]
            group["not_issued_staked_atto"] += policy["not_issued_staked"]
            if destination is None:
                dedicated_layerzero += 1
                continue
            destination_id, reason = destination
            group["routes"] += 1
            routes.append(
                {
                    "route_id": (
                        f"contract-policy-{policy['group']}-{address[2:]}"
                    ),
                    "priority": str(args.priority),
                    "source_address": address,
                    "destination_id": destination_id,
                    "destination_address": "",
                    "amount_atto": "ALL",
                    "allocation_method": "wallet_first_pro_rata_vault",
                    "reason": reason,
                    "evidence": args.stage_policy,
                    "notes": f"{category}: {row['subcategory']}",
                    "policy_state_block": row["policy_state_block"],
                    "policy_state_block_hash": row[
                        "policy_state_block_hash"
                    ],
                    "policy_state_root": row["policy_state_root"],
                }
            )

    if input_rows != validators + len(seen):
        raise ValueError("contract category partition does not close")
    if dedicated_layerzero != len(LAYERZERO_ADDRESSES):
        raise ValueError("dedicated LayerZero route partition is incomplete")
    reviewed_stage_contracts = {
        address
        for address, row in stages.items()
        if row["classification"] == "genuine_contract"
    }
    if reviewed_stage_contracts != seen:
        raise ValueError("stage-policy contract set does not match review")

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
        "schema_version": 1,
        "status": "passed",
        "policy": (
            "reviewed multisig, LayerZero, and 1wallet allocations are held "
            "for the next stage; SmartVault and all other reviewed genuine "
            "contract allocations are not issued"
        ),
        "input": args.contracts,
        "input_sha256": file_sha256(args.contracts),
        "stage_policy": args.stage_policy,
        "stage_policy_sha256": file_sha256(args.stage_policy),
        "input_rows": input_rows,
        "validator_rows_skipped": validators,
        "reviewed_contracts": len(seen),
        "routes": len(routes),
        "dedicated_layerzero_routes": dedicated_layerzero,
        "categories": dict(sorted(categories.items())),
        "groups": {
            name: {
                **values,
                "migration_allocation_atto": str(
                    values["migration_allocation_atto"]
                ),
                "not_issued_atto": str(values["not_issued_atto"]),
                "wallet_allocation_atto": str(
                    values["wallet_allocation_atto"]
                ),
                "staked_allocation_atto": str(
                    values["staked_allocation_atto"]
                ),
                "not_issued_wallet_atto": str(
                    values["not_issued_wallet_atto"]
                ),
                "not_issued_staked_atto": str(
                    values["not_issued_staked_atto"]
                ),
            }
            for name, values in sorted(groups.items())
        },
        "priority": args.priority,
        "policy_state": policy_state,
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
