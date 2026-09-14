#!/usr/bin/env python3

"""Split verified migration entitlements into destination categories."""

import argparse
import csv
import hashlib
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


ATTO_PER_ONE = 10**18
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
    parser.add_argument("--automatic-output", required=True)
    parser.add_argument("--contract-review-output", required=True)
    parser.add_argument("--excluded-address-output", required=True)
    parser.add_argument(
        "--automatic-code-addresses",
        action="append",
        default=[],
        help=(
            "CSV containing an address column for independently verified "
            "code-bearing, key-controlled accounts; repeatable"
        ),
    )
    parser.add_argument("--summary", required=True)
    parser.add_argument("--minimum-one", required=True, type=parse_one)
    parser.add_argument(
        "--comparison", required=True, choices=("ge", "gt")
    )
    parser.add_argument(
        "--exclude-address",
        action="append",
        default=[],
        help="address to exclude before contract classification; repeatable",
    )
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def new_totals():
    return {component: 0 for component in COMPONENTS}


def add_totals(totals, row):
    for component in COMPONENTS:
        value = int(row[f"{component}_atto"])
        if value < 0:
            raise ValueError(f"negative {component} for {row['secure_key']}")
        totals[component] += value


def code_bearing(row):
    if not row.get("code_hash_shard0", "").strip():
        return None
    hashes = [
        row.get(field, "").strip().lower()
        for field in ("code_hash_shard0", "code_hash_shard1")
        if row.get(field, "").strip()
    ]
    if not hashes:
        return None
    return any(value != EMPTY_CODE_HASH for value in hashes)


def main():
    args = parse_args()
    outputs = {
        "automatic": args.automatic_output,
        "contract_review": args.contract_review_output,
        "excluded_address": args.excluded_address_output,
    }
    for path in (*outputs.values(), args.summary):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(path)
    excluded = {address.lower() for address in args.exclude_address}
    if any(
        len(address) != 42 or not address.startswith("0x")
        for address in excluded
    ):
        raise ValueError("excluded addresses must be 20-byte hexadecimal values")
    automatic_code, automatic_code_sources = load_automatic_code_addresses(
        args.automatic_code_addresses
    )
    if automatic_code & excluded:
        raise ValueError(
            "automatic code-address overrides overlap excluded addresses"
        )

    handles = {}
    writers = {}
    stats = {
        label: {"rows": 0, "components_atto": new_totals()}
        for label in outputs
    }
    input_rows = 0
    threshold_rows = 0
    below_threshold_rows = 0
    exact_threshold_rows = 0
    seen_excluded = set()
    seen_automatic_code = set()
    previous = None
    try:
        with open(args.input, newline="") as source:
            reader = csv.DictReader(source)
            required = {
                "secure_key",
                "address",
                "code_hash_shard0",
                "code_hash_shard1",
                *(f"{component}_atto" for component in COMPONENTS),
            }
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"input is missing fields: {sorted(missing)}")
            for label, path in outputs.items():
                handles[label] = open(path + ".partial", "x", newline="")
                writers[label] = csv.DictWriter(
                    handles[label],
                    fieldnames=reader.fieldnames,
                    lineterminator="\n",
                )
                writers[label].writeheader()

            for line, row in enumerate(reader, start=2):
                key = row["secure_key"].lower()
                if previous is not None and key <= previous:
                    raise ValueError(
                        f"secure keys are not increasing at line {line}"
                    )
                previous = key
                if not row["address"]:
                    raise ValueError(f"unresolved address at line {line}")
                lib.require_address_secure_key(
                    row["address"], key, f"input line {line}"
                )
                has_code = code_bearing(row)
                if has_code is None:
                    raise ValueError(
                        f"unresolved code metadata at line {line}"
                    )
                value = int(row["total_claim_atto"])
                wallet = int(row["wallet_airdrop_atto"])
                vault = int(row["staked_to_vault_atto"])
                if (
                    wallet < 0
                    or vault < 0
                    or wallet + vault != value
                ):
                    raise ValueError(
                        f"allocation component mismatch at line {line}"
                    )
                input_rows += 1
                exact_threshold_rows += int(value == args.minimum_one)
                eligible = (
                    value >= args.minimum_one
                    if args.comparison == "ge"
                    else value > args.minimum_one
                )
                if not eligible:
                    below_threshold_rows += 1
                    continue
                threshold_rows += 1
                address = row["address"].lower()
                if address in excluded:
                    label = "excluded_address"
                    seen_excluded.add(address)
                elif address in automatic_code:
                    if not has_code:
                        raise ValueError(
                            f"automatic code-address override has empty code: {address}"
                        )
                    label = "automatic"
                    seen_automatic_code.add(address)
                elif has_code:
                    label = "contract_review"
                else:
                    label = "automatic"
                writers[label].writerow(row)
                stats[label]["rows"] += 1
                add_totals(stats[label]["components_atto"], row)

        if seen_excluded != excluded:
            raise ValueError(
                "not every requested excluded address was present above threshold"
            )
        if seen_automatic_code != automatic_code:
            missing = sorted(automatic_code - seen_automatic_code)
            raise ValueError(
                "not every automatic code-address override was present "
                f"above threshold: {missing}"
            )
        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        for label, path in outputs.items():
            os.replace(path + ".partial", path)
            stats[label]["output"] = path
            stats[label]["output_sha256"] = file_sha256(path)
            stats[label]["components_atto"] = {
                component: str(value)
                for component, value in stats[label][
                    "components_atto"
                ].items()
            }

        result = {
            "input": args.input,
            "input_sha256": file_sha256(args.input),
            "input_rows": input_rows,
            "comparison": args.comparison,
            "minimum_atto": str(args.minimum_one),
            "threshold_rows": threshold_rows,
            "exact_threshold_rows": exact_threshold_rows,
            "below_threshold_rows": below_threshold_rows,
            "excluded_addresses_requested": sorted(excluded),
            "excluded_addresses_found": sorted(seen_excluded),
            "automatic_code_address_sources": automatic_code_sources,
            "automatic_code_addresses_requested": sorted(automatic_code),
            "automatic_code_addresses_found": sorted(seen_automatic_code),
            "categories": stats,
        }
        with open(args.summary + ".partial", "x") as output:
            json.dump(result, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(args.summary + ".partial", args.summary)
        print(json.dumps(result, sort_keys=True))
    except BaseException:
        for handle in handles.values():
            if not handle.closed:
                handle.close()
        for path in (*outputs.values(), args.summary):
            try:
                os.remove(path + ".partial")
            except FileNotFoundError:
                pass
        raise


if __name__ == "__main__":
    main()
