#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os


def parse_args():
    parser = argparse.ArgumentParser(
        description="Merge liquid, staking, and pending-receipt ONE claims."
    )
    parser.add_argument("--shard0-liquid", required=True)
    parser.add_argument("--shard1-liquid", required=True)
    parser.add_argument("--staking", required=True)
    parser.add_argument("--receipts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary-output", required=True)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rows(path, kind):
    with open(path, newline="") as source:
        previous = None
        for line_number, row in enumerate(csv.DictReader(source), start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(
                    f"{path}:{line_number}: secure keys are not strictly increasing"
                )
            previous = key
            row["secure_key"] = key
            row["_kind"] = kind
            yield row


def next_or_none(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def load_pending_receipts(path):
    with open(path, encoding="utf-8") as source:
        report = json.load(source)
    pending = {}
    total = 0
    for direction in report["directions"]:
        for group in direction.get("pending_groups") or []:
            for receipt in group.get("receipts") or []:
                key = receipt["secure_key"].lower()
                address = receipt["to"]
                amount = int(receipt["amount_atto"])
                if amount < 0:
                    raise ValueError(f"negative receipt amount for {key}")
                entry = pending.setdefault(
                    key,
                    {"address": address, "amount": 0},
                )
                if entry["address"].lower() != address.lower():
                    raise ValueError(f"receipt address mismatch for {key}")
                entry["amount"] += amount
                total += amount
    if total != int(report["pending_active_atto"]):
        raise ValueError(
            f"pending receipt sum {total} != report {report['pending_active_atto']}"
        )
    return pending, report


def int_field(row, field):
    if row is None:
        return 0
    value = int(row[field])
    if value < 0:
        raise ValueError(f"negative {field} for {row['secure_key']}")
    return value


def merge(args):
    for path in (args.output, args.summary_output):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(f"refusing to overwrite {path}")

    input_hashes = {
        "shard0_liquid": file_sha256(args.shard0_liquid),
        "shard1_liquid": file_sha256(args.shard1_liquid),
        "staking": file_sha256(args.staking),
        "receipts": file_sha256(args.receipts),
    }
    receipt_claims, receipt_report = load_pending_receipts(args.receipts)
    receipt_keys = iter(sorted(receipt_claims))
    receipt_key = next_or_none(receipt_keys)

    liquid0 = iter(rows(args.shard0_liquid, "liquid0"))
    liquid1 = iter(rows(args.shard1_liquid, "liquid1"))
    staking = iter(rows(args.staking, "staking"))
    row0 = next_or_none(liquid0)
    row1 = next_or_none(liquid1)
    stake_row = next_or_none(staking)

    summary = {
        "input_sha256": input_hashes,
        "rows": 0,
        "rows_without_address": 0,
        "liquid_shard0_atto": 0,
        "liquid_shard1_atto": 0,
        "active_delegation_atto": 0,
        "pending_undelegation_atto": 0,
        "unclaimed_reward_atto": 0,
        "pending_cross_shard_atto": 0,
        "total_claim_atto": 0,
        "retired_shard_receipt_status_unknown_atto": receipt_report[
            "unsupported_pending_atto"
        ],
    }
    output_partial = args.output + ".partial"
    summary_partial = args.summary_output + ".partial"

    try:
        with open(output_partial, "x", newline="", encoding="utf-8") as output_file:
            writer = csv.writer(output_file, lineterminator="\n")
            writer.writerow(
                [
                    "secure_key",
                    "address",
                    "liquid_shard0_atto",
                    "liquid_shard1_atto",
                    "liquid_total_atto",
                    "active_delegation_atto",
                    "pending_undelegation_atto",
                    "unclaimed_reward_atto",
                    "pending_cross_shard_atto",
                    "total_claim_atto",
                    "nonce_shard0",
                    "nonce_shard1",
                    "code_hash_shard0",
                    "code_hash_shard1",
                ]
            )

            while (
                row0 is not None
                or row1 is not None
                or stake_row is not None
                or receipt_key is not None
            ):
                key = min(
                    item
                    for item in (
                        row0 and row0["secure_key"],
                        row1 and row1["secure_key"],
                        stake_row and stake_row["secure_key"],
                        receipt_key,
                    )
                    if item is not None
                )
                current0 = row0 if row0 and row0["secure_key"] == key else None
                current1 = row1 if row1 and row1["secure_key"] == key else None
                current_stake = (
                    stake_row
                    if stake_row and stake_row["secure_key"] == key
                    else None
                )
                current_receipt = receipt_claims.get(key)
                if current0 is not None:
                    row0 = next_or_none(liquid0)
                if current1 is not None:
                    row1 = next_or_none(liquid1)
                if current_stake is not None:
                    stake_row = next_or_none(staking)
                if receipt_key == key:
                    receipt_key = next_or_none(receipt_keys)

                addresses = [
                    address
                    for address in (
                        current0 and current0["address"],
                        current1 and current1["address"],
                        current_stake and current_stake["address"],
                        current_receipt and current_receipt["address"],
                    )
                    if address
                ]
                if addresses and any(
                    address.lower() != addresses[0].lower()
                    for address in addresses[1:]
                ):
                    raise ValueError(f"address mismatch for {key}: {addresses}")
                address = addresses[0] if addresses else ""

                liquid0_value = int_field(current0, "balance_atto")
                liquid1_value = int_field(current1, "balance_atto")
                active = int_field(current_stake, "active_delegation_atto")
                undelegation = int_field(
                    current_stake,
                    "pending_undelegation_atto",
                )
                reward = int_field(current_stake, "unclaimed_reward_atto")
                pending_receipt = (
                    current_receipt["amount"] if current_receipt else 0
                )
                liquid = liquid0_value + liquid1_value
                total = liquid + active + undelegation + reward + pending_receipt
                if total <= 0:
                    raise ValueError(f"non-positive merged claim for {key}")

                writer.writerow(
                    [
                        key,
                        address,
                        str(liquid0_value),
                        str(liquid1_value),
                        str(liquid),
                        str(active),
                        str(undelegation),
                        str(reward),
                        str(pending_receipt),
                        str(total),
                        current0["nonce"] if current0 else "",
                        current1["nonce"] if current1 else "",
                        current0["code_hash"] if current0 else "",
                        current1["code_hash"] if current1 else "",
                    ]
                )
                summary["rows"] += 1
                summary["rows_without_address"] += not bool(address)
                summary["liquid_shard0_atto"] += liquid0_value
                summary["liquid_shard1_atto"] += liquid1_value
                summary["active_delegation_atto"] += active
                summary["pending_undelegation_atto"] += undelegation
                summary["unclaimed_reward_atto"] += reward
                summary["pending_cross_shard_atto"] += pending_receipt
                summary["total_claim_atto"] += total

            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(output_partial, args.output)

        for field in (
            "liquid_shard0_atto",
            "liquid_shard1_atto",
            "active_delegation_atto",
            "pending_undelegation_atto",
            "unclaimed_reward_atto",
            "pending_cross_shard_atto",
            "total_claim_atto",
        ):
            summary[field] = str(summary[field])
        summary["output_path"] = args.output
        summary["output_sha256"] = file_sha256(args.output)

        with open(summary_partial, "x", encoding="utf-8") as summary_file:
            json.dump(summary, summary_file, indent=2, sort_keys=True)
            summary_file.write("\n")
            summary_file.flush()
            os.fsync(summary_file.fileno())
        os.replace(summary_partial, args.summary_output)
        return summary
    except BaseException:
        for path in (output_partial, summary_partial):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        raise


def main():
    result = merge(parse_args())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
