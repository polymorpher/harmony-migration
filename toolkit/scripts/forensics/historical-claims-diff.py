#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json


def read_staking(path):
    claims = {}
    with open(path, newline="") as source:
        for row in csv.DictReader(source):
            claims[row["address"].lower()] = tuple(
                int(row[column])
                for column in (
                    "active_delegation_atto",
                    "pending_undelegation_atto",
                    "unclaimed_reward_atto",
                )
            )
    return claims


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--liquid-diff", required=True)
    parser.add_argument("--old-staking", required=True)
    parser.add_argument("--new-staking", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    old_staking = read_staking(args.old_staking)
    new_staking = read_staking(args.new_staking)
    records = {}
    with open(args.liquid_diff, newline="") as source:
        for row in csv.DictReader(source):
            address = row["address"].lower()
            if not address:
                raise RuntimeError(f"missing preimage for {row['secure_key']}")
            records[address] = {
                "secure_key": row["secure_key"],
                "old_liquid": int(row["old_balance_atto"]),
                "new_liquid": int(row["new_balance_atto"]),
            }

    zero = (0, 0, 0)
    addresses = set(records) | set(old_staking) | set(new_staking)
    rows = []
    component_totals = [0] * 5
    for address in addresses:
        liquid = records.get(
            address, {"secure_key": "", "old_liquid": 0, "new_liquid": 0}
        )
        old = old_staking.get(address, zero)
        new = new_staking.get(address, zero)
        deltas = (
            liquid["new_liquid"] - liquid["old_liquid"],
            new[0] - old[0],
            new[1] - old[1],
            new[2] - old[2],
        )
        total = sum(deltas)
        for index, delta in enumerate(deltas):
            component_totals[index] += delta
        component_totals[4] += total
        rows.append(
            (
                address,
                liquid["secure_key"],
                liquid["old_liquid"],
                liquid["new_liquid"],
                *deltas,
                *old,
                *new,
                total,
            )
        )

    rows.sort(key=lambda row: (row[-1], row[0]), reverse=True)
    hasher = hashlib.sha256()
    with open(args.output, "xb") as raw_output:
        class HashWriter:
            def write(self, data):
                encoded = data.encode()
                hasher.update(encoded)
                raw_output.write(encoded)
                return len(data)

        writer = csv.writer(HashWriter(), lineterminator="\n")
        writer.writerow(
            (
                "address",
                "secure_key",
                "old_liquid_atto",
                "new_liquid_atto",
                "liquid_delta_atto",
                "active_delegation_delta_atto",
                "pending_undelegation_delta_atto",
                "unclaimed_reward_delta_atto",
                "old_active_delegation_atto",
                "old_pending_undelegation_atto",
                "old_unclaimed_reward_atto",
                "new_active_delegation_atto",
                "new_pending_undelegation_atto",
                "new_unclaimed_reward_atto",
                "total_claim_delta_atto",
            )
        )
        writer.writerows(rows)

    print(
        json.dumps(
            {
                "addresses": len(rows),
                "liquid_delta_atto": str(component_totals[0]),
                "active_delegation_delta_atto": str(component_totals[1]),
                "pending_undelegation_delta_atto": str(component_totals[2]),
                "unclaimed_reward_delta_atto": str(component_totals[3]),
                "total_claim_delta_atto": str(component_totals[4]),
                "output": args.output,
                "output_sha256": hasher.hexdigest(),
                "top_positive": [
                    {"address": row[0], "total_claim_delta_atto": str(row[-1])}
                    for row in rows[:20]
                ],
                "top_negative": [
                    {"address": row[0], "total_claim_delta_atto": str(row[-1])}
                    for row in rows[-20:][::-1]
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
