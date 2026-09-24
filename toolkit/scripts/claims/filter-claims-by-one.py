#!/usr/bin/env python3

"""Filter migration claims by exact total claim in ONE."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path


ATTO_PER_ONE = 10**18


def parse_one(value):
    """Exact ONE -> atto-ONE from decimal text without context rounding."""
    text = str(value).strip()
    whole, dot, fraction = text.partition(".")
    if not whole.isdigit() or (dot and not fraction.isdigit()) or len(fraction) > 18:
        raise argparse.ArgumentTypeError(
            f"invalid exact ONE value: {value!r} (non-negative, at most 18 decimals)"
        )
    return int(whole) * ATTO_PER_ONE + int(fraction.ljust(18, "0") or 0)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument(
        "--aggregate-delivery-summary",
        help=(
            "exchange normalization summary authorizing below-threshold "
            "WONE in manual aggregate-delivery rows"
        ),
    )
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


def load_aggregate_delivery_addresses(path):
    if not path:
        return set()
    with open(path, encoding="utf-8") as source:
        summary = json.load(source)
    if summary.get("schema_version") != 2:
        raise ValueError("unsupported exchange normalization summary")
    addresses = set()
    for exchange_id, exchange in sorted(
        summary.get("exchanges", {}).items()
    ):
        if exchange.get("delivery_policy") != "manual_from_reserve":
            continue
        output_path = Path(exchange["output"])
        if file_sha256(output_path) != exchange["output_sha256"]:
            raise ValueError(
                f"{exchange_id}: normalized exchange source hash mismatch"
            )
        source_rows = 0
        with output_path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if not {"exchange_id", "address_hex"} <= set(
                reader.fieldnames or ()
            ):
                raise ValueError(
                    f"{output_path}: missing normalized exchange fields"
                )
            for line, row in enumerate(reader, start=2):
                if row["exchange_id"] != exchange_id:
                    raise ValueError(
                        f"{output_path}:{line}: exchange id mismatch"
                    )
                address = row["address_hex"].strip().lower()
                if (
                    len(address) != 42
                    or not address.startswith("0x")
                    or any(
                        character not in "0123456789abcdef"
                        for character in address[2:]
                    )
                ):
                    raise ValueError(f"{output_path}:{line}: invalid address")
                if address in addresses:
                    raise ValueError(
                        f"{output_path}:{line}: duplicate exchange address"
                    )
                addresses.add(address)
                source_rows += 1
        if source_rows != int(exchange["normalized_rows"]):
            raise ValueError(
                f"{exchange_id}: normalized exchange row count mismatch"
            )
    return addresses


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
    wone_airdrop_atto = 0
    previous_key = None
    aggregate_delivery = load_aggregate_delivery_addresses(
        args.aggregate_delivery_summary
    )
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
        wone_fields = {
            "native_total_claim_atto",
            "wone_balance_atto",
            "wone_airdrop_atto",
            "qualification_total_atto",
        }
        have_wone = bool(wone_fields & set(reader.fieldnames))
        if have_wone and not wone_fields <= set(reader.fieldnames):
            raise ValueError("input has an incomplete WONE overlay")
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
            qualification_value = value
            wone_value = 0
            if have_wone:
                native_value = int(row["native_total_claim_atto"])
                wone_balance = int(row["wone_balance_atto"])
                wone_value = int(row["wone_airdrop_atto"])
                qualification_value = int(row["qualification_total_atto"])
                if min(native_value, wone_balance, wone_value) < 0:
                    raise ValueError(
                        f"negative WONE overlay value at line {line}"
                    )
                if qualification_value != native_value + wone_balance:
                    raise ValueError(
                        f"qualification total mismatch at line {line}"
                    )
                address = row.get("address", "").strip().lower()
                expected_wone = (
                    wone_balance
                    if (
                        qualification_value >= args.minimum_one
                        or address in aggregate_delivery
                    )
                    else 0
                )
                if (
                    wone_value != expected_wone
                    or value != native_value + wone_value
                ):
                    raise ValueError(
                        f"WONE airdrop mismatch at line {line}"
                    )
            input_rows += 1
            exact_threshold_rows += int(
                qualification_value == args.minimum_one
            )
            include = (
                qualification_value >= args.minimum_one
                if args.comparison == "ge"
                else qualification_value > args.minimum_one
            )
            if include:
                writer.writerow(row)
                output_rows += 1
                total_claim_atto += value
                wallet_airdrop_atto += wallet_value
                staked_to_vault_atto += vault_value
                wone_airdrop_atto += wone_value
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
        "aggregate_delivery_summary": args.aggregate_delivery_summary,
        "aggregate_delivery_summary_sha256": (
            file_sha256(args.aggregate_delivery_summary)
            if args.aggregate_delivery_summary
            else None
        ),
        "input_rows": input_rows,
        "output_rows": output_rows,
        "exact_threshold_rows": exact_threshold_rows,
        "total_claim_atto": str(total_claim_atto),
        "wallet_airdrop_atto": str(wallet_airdrop_atto),
        "staked_to_vault_atto": str(staked_to_vault_atto),
        "wone_airdrop_atto": str(wone_airdrop_atto),
        "threshold_field": (
            "qualification_total_atto" if have_wone else "total_claim_atto"
        ),
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
