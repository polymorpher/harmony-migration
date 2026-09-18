#!/usr/bin/env python3

"""Add current-batch WONE airdrops to the migration claim ledger."""

import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


ATTO_PER_ONE = 10**18
WONE_ADDRESS = "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a"
EXTRA_FIELDS = (
    "native_wallet_airdrop_atto",
    "wone_balance_atto",
    "wone_airdrop_atto",
    "qualification_total_atto",
    "native_total_claim_atto",
    "native_wallet_airdrop_one",
    "wone_balance_one",
    "wone_airdrop_one",
    "qualification_total_one",
    "native_total_claim_one",
)


def parse_one(value):
    try:
        whole, dot, fraction = value.partition(".")
        if not whole.isdigit() or (dot and not fraction.isdigit()):
            raise ValueError
        if len(fraction) > 18:
            raise ValueError
        return int(whole) * ATTO_PER_ONE + int(fraction.ljust(18, "0") or 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid exact ONE value: {value}") from error


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-claims", required=True)
    parser.add_argument("--wone-holders", required=True)
    parser.add_argument("--wone-summary", required=True)
    parser.add_argument("--new-holder-metadata", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--minimum-one", required=True, type=parse_one)
    parser.add_argument(
        "--exclude-address",
        action="append",
        default=[],
        help="WONE holder address omitted from qualification; repeatable",
    )
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_address(value, context):
    address = lib.normalize_address(value)
    if address is None:
        raise ValueError(f"{context}: invalid address {value!r}")
    try:
        raw = bytes.fromhex(address[2:])
    except ValueError as error:
        raise ValueError(f"{context}: invalid address {value!r}") from error
    if len(raw) != 20:
        raise ValueError(f"{context}: invalid address {value!r}")
    return address


def fixed(value):
    whole, fraction = divmod(value, ATTO_PER_ONE)
    return f"{whole}.{fraction:018d}"


def output_fields(fields):
    if any(field in fields for field in EXTRA_FIELDS):
        raise ValueError("native input already contains WONE overlay fields")
    result = []
    for field in fields:
        if field == "wallet_airdrop_atto":
            result.extend(
                (
                    "native_wallet_airdrop_atto",
                    "wone_balance_atto",
                    "wone_airdrop_atto",
                )
            )
        if field == "total_claim_atto":
            result.extend(
                (
                    "qualification_total_atto",
                    "native_total_claim_atto",
                )
            )
        if field == "wallet_airdrop_one":
            result.extend(
                (
                    "native_wallet_airdrop_one",
                    "wone_balance_one",
                    "wone_airdrop_one",
                )
            )
        if field == "total_claim_one":
            result.extend(
                (
                    "qualification_total_one",
                    "native_total_claim_one",
                )
            )
        result.append(field)
    return result


def load_wone(path, excluded):
    holders = {}
    rows = 0
    total = 0
    excluded_rows = 0
    excluded_atto = 0
    excluded_holders = []
    previous = None
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"address", "wone_balance_atto", "wone_balance"}
        if set(reader.fieldnames or ()) != required:
            raise ValueError(f"unexpected WONE fields: {reader.fieldnames}")
        for line, row in enumerate(reader, start=2):
            address = normalize_address(row["address"], f"WONE line {line}")
            raw = bytes.fromhex(address[2:])
            if previous is not None and raw <= previous:
                raise ValueError(
                    f"WONE holder addresses are not increasing at line {line}"
                )
            previous = raw
            amount = int(row["wone_balance_atto"])
            if amount <= 0 or row["wone_balance"] != fixed(amount):
                raise ValueError(f"invalid WONE amount at line {line}")
            rows += 1
            total += amount
            if address in excluded:
                excluded_rows += 1
                excluded_atto += amount
                excluded_holders.append(
                    {
                        "address": address,
                        "wone_balance_atto": str(amount),
                    }
                )
                continue
            holders[address] = amount
    return (
        holders,
        rows,
        total,
        excluded_rows,
        excluded_atto,
        excluded_holders,
    )


def load_new_holder_metadata(path):
    records = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "wone_balance_atto",
            "combined_total_atto",
            "claim_row_present",
            "nonce_shard0",
            "code_hash_shard0",
            "code_status",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"new-holder metadata is missing fields: {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            if row["claim_row_present"].lower() != "false":
                continue
            address = normalize_address(
                row["address"], f"new-holder metadata line {line}"
            )
            if address in records:
                raise ValueError(
                    f"duplicate new-holder metadata at line {line}"
                )
            if not row["code_hash_shard0"]:
                raise ValueError(f"missing code hash at line {line}")
            records[address] = row
    return records


def require_complete_new_holder_metadata(
    holders,
    native_addresses,
    metadata_addresses,
    threshold,
):
    expected = {
        address
        for address, amount in holders.items()
        if amount >= threshold and address not in native_addresses
    }
    actual = set(metadata_addresses)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            "WONE-only metadata does not equal the complete threshold set: "
            f"missing={missing[:10]} extra={extra[:10]} "
            f"missing_count={len(missing)} extra_count={len(extra)}"
        )


def validate_native_row(row, line):
    native_wallet = int(row["wallet_airdrop_atto"])
    staked = int(row["staked_to_vault_atto"])
    native_total = int(row["total_claim_atto"])
    liquid_total = int(row["liquid_total_atto"])
    if (
        min(native_wallet, staked, native_total, liquid_total) < 0
        or native_wallet + staked != native_total
        or int(row["liquid_shard0_atto"])
        + int(row["liquid_shard1_atto"])
        != liquid_total
    ):
        raise ValueError(f"native claim arithmetic mismatch at line {line}")
    return native_wallet, staked, native_total


def apply_overlay(row, wone, threshold):
    native_wallet = int(row["wallet_airdrop_atto"])
    native_total = int(row["total_claim_atto"])
    qualification_total = native_total + wone
    wone_airdrop = wone if qualification_total >= threshold else 0
    wallet = native_wallet + wone_airdrop
    total = native_total + wone_airdrop
    price = row["valuation_price_usd_per_one"]
    price_whole, dot, price_fraction = price.partition(".")
    if not price_whole.isdigit() or (dot and not price_fraction.isdigit()):
        raise ValueError(f"invalid price {price!r}")
    price_scale = len(price_fraction)
    price_numerator = int(price_whole + price_fraction)

    result = dict(row)
    result.update(
        {
            "native_wallet_airdrop_atto": str(native_wallet),
            "wone_balance_atto": str(wone),
            "wone_airdrop_atto": str(wone_airdrop),
            "wallet_airdrop_atto": str(wallet),
            "qualification_total_atto": str(qualification_total),
            "native_total_claim_atto": str(native_total),
            "total_claim_atto": str(total),
            "native_wallet_airdrop_one": fixed(native_wallet),
            "wone_balance_one": fixed(wone),
            "wone_airdrop_one": fixed(wone_airdrop),
            "wallet_airdrop_one": fixed(wallet),
            "qualification_total_one": fixed(qualification_total),
            "native_total_claim_one": fixed(native_total),
            "total_claim_one": fixed(total),
            "wallet_airdrop_usd": (
                f"{wallet * price_numerator // (10 ** (18 + price_scale))}."
                f"{wallet * price_numerator % (10 ** (18 + price_scale)):0{18 + price_scale}d}"
            ),
            "total_usd": (
                f"{total * price_numerator // (10 ** (18 + price_scale))}."
                f"{total * price_numerator % (10 ** (18 + price_scale)):0{18 + price_scale}d}"
            ),
        }
    )
    return result


def new_holder_row(address, wone, metadata, template, threshold):
    checksum = lib.to_checksum(address)
    secure_key = "0x" + lib.keccak256(bytes.fromhex(address[2:])).hex()
    result = {field: "" for field in template}
    result.update(
        {
            "secure_key": secure_key,
            "address": checksum,
            "address_or_secure_key": checksum,
            "address_resolved": "true",
            "claims_shard0_block": "93623067",
            "claims_shard1_block": "95882100",
            "valuation_price_reference_shard0_block": "93448483",
            "valuation_price_usd_per_one": "0.00074801",
            "liquid_shard0_atto": "0",
            "liquid_shard1_atto": "0",
            "liquid_total_atto": "0",
            "active_staked_or_delegated_atto": "0",
            "pending_undelegation_atto": "0",
            "unclaimed_staking_reward_atto": "0",
            "pending_cross_shard_atto": "0",
            "wallet_airdrop_atto": "0",
            "staked_to_vault_atto": "0",
            "total_claim_atto": "0",
            "liquid_shard0_one": fixed(0),
            "liquid_shard1_one": fixed(0),
            "liquid_total_one": fixed(0),
            "active_staked_or_delegated_one": fixed(0),
            "pending_undelegation_one": fixed(0),
            "unclaimed_staking_reward_one": fixed(0),
            "pending_cross_shard_one": fixed(0),
            "wallet_airdrop_one": fixed(0),
            "staked_to_vault_one": fixed(0),
            "total_claim_one": fixed(0),
            "wallet_airdrop_usd": "0.00000000000000000000000000",
            "total_usd": "0.00000000000000000000000000",
            "nonce_shard0": metadata["nonce_shard0"],
            "nonce_shard1": "",
            "code_hash_shard0": metadata["code_hash_shard0"],
            "code_hash_shard1": "",
        }
    )
    return secure_key, apply_overlay(result, wone, threshold)


def write_json_atomic(path, value, replace):
    if os.path.exists(path + ".partial"):
        raise FileExistsError(path + ".partial")
    if os.path.exists(path) and not replace:
        raise FileExistsError(path)
    with open(path + ".partial", "x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(path + ".partial", path)


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

    excluded = {
        normalize_address(value, "--exclude-address")
        for value in args.exclude_address
    }
    excluded.add(WONE_ADDRESS)
    with open(args.wone_summary, encoding="utf-8") as source:
        wone_summary = json.load(source)
    if wone_summary.get("status") != "passed":
        raise ValueError("WONE scan summary did not pass")
    if (
        int(wone_summary.get("cutoff_block", -1)) != 93623067
        or normalize_address(
            wone_summary.get("contract_address", ""),
            "WONE summary",
        )
        != WONE_ADDRESS
        or int(wone_summary.get("reserve_minus_supply_atto", -1)) != 0
        or wone_summary.get("contract_total_supply_atto")
        != wone_summary.get("contract_native_reserve_atto")
    ):
        raise ValueError("WONE scan summary has inconsistent cutoff reserve")
    (
        holders,
        holder_rows,
        holder_total,
        excluded_rows,
        excluded_atto,
        excluded_holders,
    ) = load_wone(args.wone_holders, excluded)
    if file_sha256(args.wone_holders) != wone_summary["output_sha256"]:
        raise ValueError("WONE holder CSV hash does not match its summary")
    if holder_rows != int(wone_summary["holder_count"]):
        raise ValueError("WONE holder count does not match its summary")
    if holder_total != int(wone_summary["total_holder_balance_atto"]):
        raise ValueError("WONE holder total does not match its summary")
    reserve = holder_total
    metadata = load_new_holder_metadata(args.new_holder_metadata)

    with open(args.native_claims, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "secure_key",
            "address",
            "wallet_airdrop_atto",
            "staked_to_vault_atto",
            "total_claim_atto",
            "liquid_shard0_atto",
            "liquid_shard1_atto",
            "liquid_total_atto",
            "wallet_airdrop_one",
            "total_claim_one",
            "wallet_airdrop_usd",
            "total_usd",
            "valuation_price_usd_per_one",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"native claims are missing fields: {sorted(missing)}"
            )
        fields = output_fields(reader.fieldnames)
        new_rows = []
        for address, row in metadata.items():
            if address not in holders:
                raise ValueError(f"new holder {address} is absent from WONE input")
            wone = holders[address]
            if int(row["wone_balance_atto"]) != wone:
                raise ValueError(f"new holder {address} WONE mismatch")
            if wone < args.minimum_one:
                raise ValueError(f"new holder {address} is below threshold")
            key, result = new_holder_row(
                address,
                wone,
                row,
                reader.fieldnames,
                args.minimum_one,
            )
            new_rows.append((key, address, result))
        new_rows.sort(key=lambda item: item[0])

        output = open(args.output + ".partial", "x", newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=fields,
            lineterminator="\n",
        )
        writer.writeheader()
        new_index = 0
        native_rows = 0
        output_rows = 0
        baseline_qualified = 0
        qualified_rows = 0
        newly_qualified_existing = 0
        priority_wone_holders = 0
        priority_wone = 0
        native_wallet_total = 0
        native_total_total = 0
        wallet_total = 0
        staked_total = 0
        total_total = 0
        native_threshold_holder_addresses = set()
        wone_contract_seen = False
        previous_key = None

        try:
            for line, row in enumerate(reader, start=2):
                key = row["secure_key"].lower()
                if previous_key is not None and key <= previous_key:
                    raise ValueError(
                        f"native secure keys are not increasing at line {line}"
                    )
                previous_key = key
                while (
                    new_index < len(new_rows)
                    and new_rows[new_index][0] < key
                ):
                    _, address, result = new_rows[new_index]
                    writer.writerow(result)
                    output_rows += 1
                    qualified_rows += 1
                    priority_wone_holders += 1
                    amount = int(result["wone_airdrop_atto"])
                    priority_wone += amount
                    wallet_total += amount
                    total_total += amount
                    new_index += 1
                if new_index < len(new_rows) and new_rows[new_index][0] == key:
                    raise ValueError(
                        f"new-holder secure key already exists at line {line}"
                    )

                address = normalize_address(row["address"], f"native line {line}")
                if holders.get(address, 0) >= args.minimum_one:
                    native_threshold_holder_addresses.add(address)
                native_wallet, staked, native_total = validate_native_row(
                    row, line
                )
                wone = holders.get(address, 0)
                result = apply_overlay(row, wone, args.minimum_one)
                baseline = native_total >= args.minimum_one
                qualifies = (
                    int(result["qualification_total_atto"])
                    >= args.minimum_one
                )
                if baseline:
                    baseline_qualified += 1
                if qualifies:
                    qualified_rows += 1
                if qualifies and not baseline:
                    newly_qualified_existing += 1
                wone_airdrop = int(result["wone_airdrop_atto"])
                if wone_airdrop:
                    priority_wone_holders += 1
                    priority_wone += wone_airdrop
                if address == WONE_ADDRESS:
                    wone_contract_seen = True
                    if int(row["liquid_shard0_atto"]) != reserve:
                        raise ValueError(
                            "WONE contract shard-0 reserve does not match "
                            "holder supply"
                        )
                writer.writerow(result)
                native_rows += 1
                output_rows += 1
                native_wallet_total += native_wallet
                native_total_total += native_total
                wallet_total += int(result["wallet_airdrop_atto"])
                staked_total += staked
                total_total += int(result["total_claim_atto"])

            while new_index < len(new_rows):
                _, address, result = new_rows[new_index]
                writer.writerow(result)
                output_rows += 1
                qualified_rows += 1
                priority_wone_holders += 1
                amount = int(result["wone_airdrop_atto"])
                priority_wone += amount
                wallet_total += amount
                total_total += amount
                new_index += 1
            output.flush()
            os.fsync(output.fileno())
            output.close()
        except BaseException:
            output.close()
            os.remove(args.output + ".partial")
            raise
    try:
        if not wone_contract_seen:
            raise ValueError("native claim ledger is missing the WONE contract")
        if set(metadata) & native_threshold_holder_addresses:
            raise ValueError("new-holder metadata overlaps native claims")
        require_complete_new_holder_metadata(
            holders,
            native_threshold_holder_addresses,
            metadata,
            args.minimum_one,
        )
        if wallet_total + staked_total != total_total:
            raise ValueError("global wallet and vault totals do not close")
        if native_total_total - native_wallet_total != staked_total:
            raise ValueError("native wallet and vault totals do not close")
        if total_total != native_total_total + priority_wone:
            raise ValueError("WONE overlay total does not close")
        if priority_wone > reserve:
            raise ValueError("priority WONE airdrop exceeds reserve")
    except BaseException:
        os.remove(args.output + ".partial")
        raise
    os.replace(args.output + ".partial", args.output)

    retained = reserve - priority_wone
    result = {
        "schema_version": 1,
        "status": "passed",
        "policy": (
            "WONE is added only for addresses in the current inclusive "
            "1,000 ONE batch; the matching reserve is redistributed and "
            "the remainder is retained in the Year 2025 Supply Reserve"
        ),
        "minimum_atto": str(args.minimum_one),
        "native_claims": args.native_claims,
        "native_claims_sha256": file_sha256(args.native_claims),
        "wone_holders": args.wone_holders,
        "wone_holders_sha256": file_sha256(args.wone_holders),
        "wone_summary": args.wone_summary,
        "wone_summary_sha256": file_sha256(args.wone_summary),
        "new_holder_metadata": args.new_holder_metadata,
        "new_holder_metadata_sha256": file_sha256(
            args.new_holder_metadata
        ),
        "native_rows": native_rows,
        "new_wone_only_rows": len(new_rows),
        "output_rows": output_rows,
        "baseline_qualified_rows": baseline_qualified,
        "newly_qualified_existing_native_rows": newly_qualified_existing,
        "newly_qualified_wone_only_rows": len(new_rows),
        "newly_qualified_rows": newly_qualified_existing + len(new_rows),
        "qualified_rows": qualified_rows,
        "wone_holder_rows": holder_rows,
        "excluded_wone_holder_rows": excluded_rows,
        "excluded_wone_atto": str(excluded_atto),
        "excluded_addresses_requested": sorted(excluded),
        "excluded_wone_holders": excluded_holders,
        "priority_wone_holder_rows": priority_wone_holders,
        "wone_reserve_atto": str(reserve),
        "wone_redistributed_to_priority_atto": str(priority_wone),
        "wone_retained_not_issued_atto": str(retained),
        "native_wallet_airdrop_atto": str(native_wallet_total),
        "native_total_claim_atto": str(native_total_total),
        "wallet_airdrop_atto": str(wallet_total),
        "staked_to_vault_atto": str(staked_total),
        "expanded_total_claim_atto": str(total_total),
        "component_totals_atto": {
            "native_wallet_airdrop": str(native_wallet_total),
            "wone_airdrop": str(priority_wone),
            "wallet_airdrop": str(wallet_total),
            "staked_to_vault": str(staked_total),
            "native_total_claim": str(native_total_total),
            "total_claim": str(total_total),
        },
        "conservation": {
            "wone_reserve_equals_redistributed_plus_retained": (
                reserve == priority_wone + retained
            ),
            "expanded_total_equals_native_plus_priority_wone": (
                total_total == native_total_total + priority_wone
            ),
        },
        "output": args.output,
        "output_sha256": file_sha256(args.output),
    }
    write_json_atomic(args.summary, result, args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
