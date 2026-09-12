#!/usr/bin/env python3

"""Independently verify total-claim and destination-category splits."""

import argparse
import csv
import hashlib
import json
import os


EMPTY_CODE_HASH = (
    "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
)
COMPONENTS = (
    "liquid_shard0",
    "liquid_shard1",
    "liquid_total",
    "active_staked_or_delegated",
    "pending_undelegation",
    "unclaimed_staking_reward",
    "pending_cross_shard",
    "wallet_airdrop",
    "staked_to_vault",
    "total_claim",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--automatic", required=True)
    parser.add_argument("--contract-review", required=True)
    parser.add_argument("--excluded-address", required=True)
    parser.add_argument(
        "--automatic-code-addresses",
        action="append",
        default=[],
        help=(
            "CSV containing an address column for independently verified "
            "code-bearing, key-controlled accounts; repeatable"
        ),
    )
    parser.add_argument("--policy-summary", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_bearing(row):
    return any(
        row[field]
        and row[field].lower() != EMPTY_CODE_HASH
        for field in ("code_hash_shard0", "code_hash_shard1")
    )


def load_automatic_code_addresses(paths):
    addresses = set()
    sources = []
    for path in paths:
        with open(path, newline="") as source:
            reader = csv.DictReader(source)
            if "address" not in (reader.fieldnames or ()):
                raise ValueError(f"{path} is missing address column")
            source_addresses = set()
            for line, row in enumerate(reader, start=2):
                address = row["address"].lower()
                if len(address) != 42 or not address.startswith("0x"):
                    raise ValueError(f"{path}:{line}: invalid address")
                if address in source_addresses:
                    raise ValueError(f"{path}:{line}: duplicate address")
                source_addresses.add(address)
            addresses.update(source_addresses)
        sources.append(
            {
                "path": path,
                "sha256": file_sha256(path),
                "addresses": len(source_addresses),
            }
        )
    return addresses, sources


def read_category(path, expected, automatic_code):
    keys = set()
    automatic_code_found = set()
    totals = {component: 0 for component in COMPONENTS}
    rows = 0
    previous = None
    with open(path, newline="") as source:
        for line, row in enumerate(csv.DictReader(source), start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(f"{path}:{line}: keys are not increasing")
            previous = key
            if key in keys:
                raise ValueError(f"{path}:{line}: duplicate key")
            keys.add(key)
            total_claim = int(row["total_claim_atto"])
            wallet = int(row["wallet_airdrop_atto"])
            vault = int(row["staked_to_vault_atto"])
            if (
                wallet < 0
                or vault < 0
                or wallet + vault != total_claim
            ):
                raise ValueError(
                    f"{path}:{line}: allocation component mismatch"
                )
            if total_claim < 1000 * 10**18:
                raise ValueError(f"{path}:{line}: below threshold")
            is_contract = code_bearing(row)
            address = row["address"].lower()
            if expected == "automatic":
                if is_contract and address not in automatic_code:
                    raise ValueError(
                        f"{path}:{line}: unapproved code-bearing account "
                        "in automatic output"
                    )
                if address in automatic_code:
                    if not is_contract:
                        raise ValueError(
                            f"{path}:{line}: automatic code-address override "
                            "has empty code"
                        )
                    automatic_code_found.add(address)
            if expected == "contract_review" and (
                not is_contract or address in automatic_code
            ):
                raise ValueError(
                    f"{path}:{line}: invalid genuine-contract review row"
                )
            for component in COMPONENTS:
                totals[component] += int(row[f"{component}_atto"])
            rows += 1
    return {
        "keys": keys,
        "rows": rows,
        "totals": totals,
        "automatic_code_addresses": automatic_code_found,
    }


def main():
    args = parse_args()
    if os.path.exists(args.output) or os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)
    policy = json.load(open(args.policy_summary))
    if policy["comparison"] != "ge":
        raise ValueError("selected policy must use inclusive comparison")
    excluded_addresses = set(policy["excluded_addresses_requested"])
    automatic_code, automatic_code_sources = load_automatic_code_addresses(
        args.automatic_code_addresses
    )
    if automatic_code & excluded_addresses:
        raise ValueError(
            "automatic code-address overrides overlap excluded addresses"
        )
    if automatic_code != set(
        policy.get("automatic_code_addresses_requested", ())
    ):
        raise ValueError(
            "automatic code-address override does not match policy summary"
        )

    automatic = read_category(
        args.automatic, "automatic", automatic_code
    )
    contracts = read_category(
        args.contract_review, "contract_review", automatic_code
    )
    excluded = read_category(
        args.excluded_address, "excluded_address", automatic_code
    )
    if automatic["automatic_code_addresses"] != automatic_code:
        raise ValueError(
            "not every automatic code-address override is present in "
            "automatic output"
        )
    categories = {
        "automatic": automatic,
        "contract_review": contracts,
        "excluded_address": excluded,
    }
    category_paths = {
        "automatic": args.automatic,
        "contract_review": args.contract_review,
        "excluded_address": args.excluded_address,
    }
    if (
        automatic["keys"] & contracts["keys"]
        or automatic["keys"] & excluded["keys"]
        or contracts["keys"] & excluded["keys"]
    ):
        raise ValueError("eligibility categories overlap")

    expected_keys = set()
    expected_excluded_keys = set()
    exact = 0
    with open(args.input, newline="") as source:
        for row in csv.DictReader(source):
            value = int(row["total_claim_atto"])
            if value == 1000 * 10**18:
                exact += 1
            if value >= 1000 * 10**18:
                expected_keys.add(row["secure_key"].lower())
                if row["address"].lower() in excluded_addresses:
                    expected_excluded_keys.add(row["secure_key"].lower())
    actual_keys = (
        automatic["keys"] | contracts["keys"] | excluded["keys"]
    )
    if actual_keys != expected_keys:
        raise ValueError("category union does not equal inclusive threshold set")
    if excluded["keys"] != expected_excluded_keys:
        raise ValueError("excluded-address output does not match policy")
    if exact != policy["exact_threshold_rows"]:
        raise ValueError("exact-threshold row count mismatch")

    for label, category in categories.items():
        expected = policy["categories"][label]
        if category["rows"] != expected["rows"]:
            raise ValueError(f"{label} row count mismatch")
        if file_sha256(category_paths[label]) != expected["output_sha256"]:
            raise ValueError(f"{label} output hash mismatch")
        for component in COMPONENTS:
            if str(category["totals"][component]) != expected[
                "components_atto"
            ][component]:
                raise ValueError(f"{label} {component} total mismatch")

    result = {
        "status": "passed",
        "input_sha256": file_sha256(args.input),
        "threshold_rows": len(expected_keys),
        "exact_threshold_rows": exact,
        "automatic_rows": automatic["rows"],
        "contract_review_rows": contracts["rows"],
        "excluded_address_rows": excluded["rows"],
        "automatic_code_address_sources": automatic_code_sources,
        "automatic_code_addresses": sorted(automatic_code),
        "policy_summary_sha256": file_sha256(args.policy_summary),
        "output_sha256": {
            "automatic": file_sha256(args.automatic),
            "contract_review": file_sha256(args.contract_review),
            "excluded_address": file_sha256(args.excluded_address),
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
