#!/usr/bin/env python3

"""Filter migration claims by exact total claim in ONE."""

import argparse
import csv
import hashlib
import json
import os
from decimal import Decimal, InvalidOperation


ATTO_PER_ONE = 10**18


def parse_one(value):
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if amount < 0 or amount.as_tuple().exponent < -18:
        raise argparse.ArgumentTypeError(
            "threshold must be non-negative with at most 18 decimals"
        )
    atto = amount * ATTO_PER_ONE
    if atto != atto.to_integral_value():
        raise argparse.ArgumentTypeError("threshold is not an exact atto value")
    return int(atto)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--minimum-one", required=True, type=parse_one)
    parser.add_argument(
        "--comparison",
        required=True,
        choices=("ge", "gt"),
        help="ge includes rows exactly at the threshold; gt excludes them",
    )
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

    input_rows = 0
    output_rows = 0
    exact_threshold_rows = 0
    total_claim_atto = 0
    wallet_airdrop_atto = 0
    staked_to_vault_atto = 0
    previous_key = None
    with open(args.input, newline="") as source, open(
        args.output + ".partial", "x", newline=""
    ) as output:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "secure_key" not in reader.fieldnames:
            raise ValueError("input is missing secure_key")
        required = {
            "total_claim_atto",
            "wallet_airdrop_atto",
            "staked_to_vault_atto",
        }
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"input is missing fields: {sorted(missing)}")
        writer = csv.DictWriter(
            output, fieldnames=reader.fieldnames, lineterminator="\n"
        )
        writer.writeheader()
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous_key is not None and key <= previous_key:
                raise ValueError(f"secure keys are not increasing at line {line}")
            previous_key = key
            value = int(row["total_claim_atto"])
            if value < 0:
                raise ValueError(f"negative total claim at line {line}")
            wallet_value = int(row["wallet_airdrop_atto"])
            vault_value = int(row["staked_to_vault_atto"])
            if (
                wallet_value < 0
                or vault_value < 0
                or wallet_value + vault_value != value
            ):
                raise ValueError(
                    f"allocation component mismatch at line {line}"
                )
            input_rows += 1
            exact_threshold_rows += int(value == args.minimum_one)
            include = (
                value >= args.minimum_one
                if args.comparison == "ge"
                else value > args.minimum_one
            )
            if include:
                writer.writerow(row)
                output_rows += 1
                total_claim_atto += value
                wallet_airdrop_atto += wallet_value
                staked_to_vault_atto += vault_value
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)

    result = {
        "input": args.input,
        "input_sha256": file_sha256(args.input),
        "output": args.output,
        "output_sha256": file_sha256(args.output),
        "comparison": args.comparison,
        "minimum_atto": str(args.minimum_one),
        "input_rows": input_rows,
        "output_rows": output_rows,
        "exact_threshold_rows": exact_threshold_rows,
        "total_claim_atto": str(total_claim_atto),
        "wallet_airdrop_atto": str(wallet_airdrop_atto),
        "staked_to_vault_atto": str(staked_to_vault_atto),
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
