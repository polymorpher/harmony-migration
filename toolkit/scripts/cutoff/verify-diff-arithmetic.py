#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os


PRICE_NUMERATOR = 74_801
USD_THRESHOLD = 10**26
COMPONENTS = (
    "liquid_shard0",
    "liquid_shard1",
    "liquid_total",
    "active_staked_or_delegated",
    "pending_undelegation",
    "unclaimed_staking_reward",
    "pending_cross_shard",
    "total_claim",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-total-claim-delta")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fixed(value, scale):
    sign = -1 if value.startswith("-") else 1
    if sign < 0:
        value = value[1:]
    parts = value.split(".")
    if len(parts) != 2 or len(parts[1]) != scale:
        raise ValueError(f"invalid fixed decimal {value}")
    return sign * int(parts[0] + parts[1])


def main():
    args = parse_args()
    if os.path.exists(args.output) or os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)
    totals = {component: 0 for component in COMPONENTS}
    counts = {
        "created": 0,
        "deleted": 0,
        "amount_changed": 0,
        "metadata_changed": 0,
    }
    rows = 0
    previous = None
    with open(args.input, newline="") as source:
        for line, row in enumerate(csv.DictReader(source), start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(f"keys are not strictly increasing at line {line}")
            previous = key
            old = {}
            final = {}
            delta = {}
            for component in COMPONENTS:
                old[component] = int(row[f"old_{component}_atto"])
                final[component] = int(row[f"final_{component}_atto"])
                delta[component] = int(row[f"delta_{component}_atto"])
                if old[component] < 0 or final[component] < 0:
                    raise ValueError(f"negative component at line {line}")
                if final[component] - old[component] != delta[component]:
                    raise ValueError(f"{component} delta mismatch at line {line}")
                if fixed(row[f"old_{component}_one"], 18) != old[component]:
                    raise ValueError(f"old {component} rendering at line {line}")
                if fixed(row[f"final_{component}_one"], 18) != final[component]:
                    raise ValueError(f"final {component} rendering at line {line}")
                if fixed(row[f"delta_{component}_one"], 18) != delta[component]:
                    raise ValueError(f"delta {component} rendering at line {line}")
                totals[component] += delta[component]

            for values in (old, final, delta):
                if (
                    values["liquid_shard0"] + values["liquid_shard1"]
                    != values["liquid_total"]
                ):
                    raise ValueError(f"liquid total mismatch at line {line}")
                expected = sum(
                    values[name]
                    for name in (
                        "liquid_shard0",
                        "liquid_shard1",
                        "active_staked_or_delegated",
                        "pending_undelegation",
                        "unclaimed_staking_reward",
                        "pending_cross_shard",
                    )
                )
                if expected != values["total_claim"]:
                    raise ValueError(f"claim total mismatch at line {line}")

            for prefix, value in (
                ("old", old["total_claim"]),
                ("final", final["total_claim"]),
                ("delta", delta["total_claim"]),
            ):
                if fixed(row[f"{prefix}_total_usd"], 26) != (
                    value * PRICE_NUMERATOR
                ):
                    raise ValueError(f"{prefix} USD mismatch at line {line}")
            if (row["old_over_1usd"] == "true") != (
                old["total_claim"] * PRICE_NUMERATOR > USD_THRESHOLD
            ):
                raise ValueError(f"old threshold mismatch at line {line}")
            if (row["final_over_1usd"] == "true") != (
                final["total_claim"] * PRICE_NUMERATOR > USD_THRESHOLD
            ):
                raise ValueError(f"final threshold mismatch at line {line}")
            if row["change_type"] not in counts:
                raise ValueError(f"invalid change type at line {line}")
            counts[row["change_type"]] += 1
            rows += 1

    if (
        args.expected_total_claim_delta is not None
        and totals["total_claim"] != int(args.expected_total_claim_delta)
    ):
        raise ValueError("aggregate total-claim delta mismatch")
    result = {
        "status": "passed",
        "input": args.input,
        "input_sha256": file_sha256(args.input),
        "rows": rows,
        "change_counts": counts,
        "component_delta_atto": {
            component: str(value) for component, value in totals.items()
        },
    }
    with open(args.output + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
