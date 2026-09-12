#!/usr/bin/env python3

"""Format total-claim, wallet-airdrop, and staked-to-vault fields."""

import argparse
import csv
import hashlib
import json
import os
from decimal import Decimal, InvalidOperation


ATTO_SCALE = 18
COMPONENT_FIELDS = {
    "liquid_shard0": "liquid_shard0_atto",
    "liquid_shard1": "liquid_shard1_atto",
    "liquid_total": "liquid_total_atto",
    "active_staked_or_delegated": "active_delegation_atto",
    "pending_undelegation": "pending_undelegation_atto",
    "unclaimed_staking_reward": "unclaimed_reward_atto",
    "pending_cross_shard": "pending_cross_shard_atto",
    "wallet_airdrop": "wallet_airdrop_atto",
    "staked_to_vault": "staked_to_vault_atto",
    "total_claim": "total_claim_atto",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--shard0-block", required=True, type=int)
    parser.add_argument("--shard1-block", required=True, type=int)
    parser.add_argument("--price-reference-shard0-block", required=True, type=int)
    parser.add_argument("--price-usd-per-one", required=True)
    return parser.parse_args()


def parse_price(value):
    try:
        price = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"invalid price: {value}") from error
    if price < 0:
        raise ValueError("price cannot be negative")
    scale = max(0, -price.as_tuple().exponent)
    numerator = int(price * (10**scale))
    return price, numerator, scale


def fixed(value, scale):
    whole, fraction = divmod(value, 10**scale)
    return f"{whole}.{fraction:0{scale}d}"


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_fields():
    fields = [
        "secure_key",
        "address",
        "address_or_secure_key",
        "address_resolved",
        "claims_shard0_block",
        "claims_shard1_block",
        "valuation_price_reference_shard0_block",
        "valuation_price_usd_per_one",
    ]
    fields.extend(f"{component}_atto" for component in COMPONENT_FIELDS)
    fields.extend(f"{component}_one" for component in COMPONENT_FIELDS)
    fields.extend(
        (
            "wallet_airdrop_usd",
            "total_usd",
            "nonce_shard0",
            "nonce_shard1",
            "code_hash_shard0",
            "code_hash_shard1",
        )
    )
    return fields


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(path)
    price, price_numerator, price_scale = parse_price(
        args.price_usd_per_one
    )
    fields = output_fields()
    totals = {component: 0 for component in COMPONENT_FIELDS}
    rows = 0
    unresolved = 0
    previous = None

    with open(args.input, newline="") as source, open(
        args.output + ".partial", "x", newline=""
    ) as output:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(f"secure keys are not increasing at line {line}")
            previous = key
            values = {
                component: int(row[field])
                for component, field in COMPONENT_FIELDS.items()
                if field in row and row[field] != ""
            }
            if any(value < 0 for value in values.values()):
                raise ValueError(f"negative component at line {line}")
            if (
                values["liquid_shard0"] + values["liquid_shard1"]
                != values["liquid_total"]
            ):
                raise ValueError(f"liquid total mismatch at line {line}")
            wallet_airdrop = sum(
                values[component]
                for component in (
                    "liquid_shard0",
                    "liquid_shard1",
                    "pending_undelegation",
                    "unclaimed_staking_reward",
                    "pending_cross_shard",
                )
            )
            staked_to_vault = values["active_staked_or_delegated"]
            total_claim = wallet_airdrop + staked_to_vault
            expected_derived = {
                "wallet_airdrop": wallet_airdrop,
                "staked_to_vault": staked_to_vault,
                "total_claim": total_claim,
            }
            for component, expected in expected_derived.items():
                if component in values and values[component] != expected:
                    raise ValueError(
                        f"{component} mismatch at line {line}"
                    )
                values[component] = expected
            address = row["address"]
            unresolved += int(not address)
            result = {
                "secure_key": key,
                "address": address,
                "address_or_secure_key": address or key,
                "address_resolved": str(bool(address)).lower(),
                "claims_shard0_block": args.shard0_block,
                "claims_shard1_block": args.shard1_block,
                "valuation_price_reference_shard0_block": (
                    args.price_reference_shard0_block
                ),
                "valuation_price_usd_per_one": str(price),
                "wallet_airdrop_usd": fixed(
                    values["wallet_airdrop"] * price_numerator,
                    ATTO_SCALE + price_scale,
                ),
                "total_usd": fixed(
                    values["total_claim"] * price_numerator,
                    ATTO_SCALE + price_scale,
                ),
                "nonce_shard0": row["nonce_shard0"],
                "nonce_shard1": row["nonce_shard1"],
                "code_hash_shard0": row["code_hash_shard0"],
                "code_hash_shard1": row["code_hash_shard1"],
            }
            for component, value in values.items():
                result[f"{component}_atto"] = str(value)
                result[f"{component}_one"] = fixed(value, ATTO_SCALE)
                totals[component] += value
            writer.writerow(result)
            rows += 1
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)

    summary = {
        "input": args.input,
        "input_sha256": file_sha256(args.input),
        "output": args.output,
        "output_sha256": file_sha256(args.output),
        "rows": rows,
        "unresolved_addresses": unresolved,
        "shard0_block": args.shard0_block,
        "shard1_block": args.shard1_block,
        "price_reference_shard0_block": args.price_reference_shard0_block,
        "price_usd_per_one": str(price),
        "component_totals_atto": {
            component: str(value) for component, value in totals.items()
        },
    }
    with open(args.summary + ".partial", "x") as output:
        json.dump(summary, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
