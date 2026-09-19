#!/usr/bin/env python3

"""Add ordinary-threshold and aggregate-exchange WONE to migration claims."""

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
    parser.add_argument(
        "--aggregate-delivery-summary",
        help=(
            "exchange normalization summary; every manual-delivery wallet "
            "receives its WONE regardless of the ordinary wallet threshold"
        ),
    )
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


def load_aggregate_delivery_addresses(path):
    if not path:
        return set(), []
    with open(path, encoding="utf-8") as source:
        summary = json.load(source)
    if summary.get("schema_version") != 1:
        raise ValueError("unsupported exchange normalization summary")
    exchanges = summary.get("exchanges")
    if not isinstance(exchanges, dict) or not exchanges:
        raise ValueError("exchange normalization summary has no exchanges")
    addresses = set()
    sources = []
    for exchange_id, exchange in sorted(exchanges.items()):
        if exchange.get("delivery_policy") != "manual_current_claim":
            continue
        output = exchange.get("output")
        expected_hash = exchange.get("output_sha256")
        expected_rows = exchange.get("normalized_rows")
        if (
            not isinstance(output, str)
            or not output
            or not isinstance(expected_hash, str)
            or not isinstance(expected_rows, int)
        ):
            raise ValueError(
                f"{exchange_id}: incomplete normalized exchange source"
            )
        output_path = Path(output)
        if file_sha256(output_path) != expected_hash:
            raise ValueError(
                f"{exchange_id}: normalized exchange source hash mismatch"
            )
        source_addresses = set()
        with output_path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            required = {"exchange_id", "address_hex"}
            if not required <= set(reader.fieldnames or ()):
                raise ValueError(
                    f"{output_path}: missing normalized exchange fields"
                )
            for line, row in enumerate(reader, start=2):
                if row["exchange_id"] != exchange_id:
                    raise ValueError(
                        f"{output_path}:{line}: exchange id mismatch"
                    )
                address = normalize_address(
                    row["address_hex"],
                    f"{output_path}:{line}",
                )
                if address in source_addresses:
                    raise ValueError(
                        f"{output_path}:{line}: duplicate address"
                    )
                if address in addresses:
                    raise ValueError(
                        f"{output_path}:{line}: aggregate exchange overlap"
                    )
                source_addresses.add(address)
                addresses.add(address)
        if len(source_addresses) != expected_rows:
            raise ValueError(
                f"{exchange_id}: normalized exchange row count mismatch"
            )
        sources.append(
            {
                "exchange_id": exchange_id,
                "path": str(output_path),
                "sha256": expected_hash,
                "addresses": len(source_addresses),
            }
        )
    return addresses, sources


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
    native_candidate_addresses,
    metadata_addresses,
    threshold,
    aggregate_delivery_addresses=None,
):
    aggregate_delivery_addresses = aggregate_delivery_addresses or set()
    expected = {
        address
        for address, amount in holders.items()
        if (
            amount >= threshold or address in aggregate_delivery_addresses
        )
        and address not in native_candidate_addresses
    }
    actual = set(metadata_addresses)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            "WONE-only metadata does not equal the complete delivery set: "
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


def apply_overlay(
    row,
    wone,
    threshold,
    aggregate_delivery=False,
):
    native_wallet = int(row["wallet_airdrop_atto"])
    native_total = int(row["total_claim_atto"])
    qualification_total = native_total + wone
    wone_airdrop = (
        wone
        if qualification_total >= threshold or aggregate_delivery
        else 0
    )
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


def new_holder_row(
    address,
    wone,
    metadata,
    template,
    threshold,
    aggregate_delivery=False,
):
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
    return secure_key, apply_overlay(
        result,
        wone,
        threshold,
        aggregate_delivery,
    )


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
    (
        aggregate_delivery_requested,
        aggregate_delivery_sources,
    ) = load_aggregate_delivery_addresses(args.aggregate_delivery_summary)
    aggregate_delivery_suppressed = aggregate_delivery_requested & excluded
    aggregate_delivery_addresses = aggregate_delivery_requested - excluded
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
            aggregate_delivery = address in aggregate_delivery_addresses
            if wone < args.minimum_one and not aggregate_delivery:
                raise ValueError(
                    f"new holder {address} is neither threshold-qualified "
                    "nor selected for aggregate delivery"
                )
            key, result = new_holder_row(
                address,
                wone,
                row,
                reader.fieldnames,
                args.minimum_one,
                aggregate_delivery,
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
        newly_qualified_wone_only = 0
        newly_qualified_existing = 0
        delivered_wone_holders = 0
        delivered_wone = 0
        threshold_wone_holders = 0
        threshold_wone = 0
        aggregate_delivery_wone_holders = 0
        aggregate_delivery_wone = 0
        native_wallet_total = 0
        native_total_total = 0
        wallet_total = 0
        staked_total = 0
        total_total = 0
        native_candidate_holder_addresses = set()
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
                    qualifies = (
                        int(result["qualification_total_atto"])
                        >= args.minimum_one
                    )
                    qualified_rows += int(qualifies)
                    newly_qualified_wone_only += int(qualifies)
                    delivered_wone_holders += 1
                    amount = int(result["wone_airdrop_atto"])
                    delivered_wone += amount
                    if qualifies:
                        threshold_wone_holders += 1
                        threshold_wone += amount
                    else:
                        aggregate_delivery_wone_holders += 1
                        aggregate_delivery_wone += amount
                    wallet_total += amount
                    total_total += amount
                    new_index += 1
                if new_index < len(new_rows) and new_rows[new_index][0] == key:
                    raise ValueError(
                        f"new-holder secure key already exists at line {line}"
                    )

                address = normalize_address(row["address"], f"native line {line}")
                wone = holders.get(address, 0)
                if (
                    wone >= args.minimum_one
                    or address in aggregate_delivery_addresses
                ):
                    native_candidate_holder_addresses.add(address)
                native_wallet, staked, native_total = validate_native_row(
                    row, line
                )
                aggregate_delivery = address in aggregate_delivery_addresses
                result = apply_overlay(
                    row,
                    wone,
                    args.minimum_one,
                    aggregate_delivery,
                )
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
                    delivered_wone_holders += 1
                    delivered_wone += wone_airdrop
                    if qualifies:
                        threshold_wone_holders += 1
                        threshold_wone += wone_airdrop
                    elif aggregate_delivery:
                        aggregate_delivery_wone_holders += 1
                        aggregate_delivery_wone += wone_airdrop
                    else:
                        raise ValueError(
                            "below-threshold WONE delivery lacks aggregate "
                            f"authorization: {address}"
                        )
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
                qualifies = (
                    int(result["qualification_total_atto"])
                    >= args.minimum_one
                )
                qualified_rows += int(qualifies)
                newly_qualified_wone_only += int(qualifies)
                delivered_wone_holders += 1
                amount = int(result["wone_airdrop_atto"])
                delivered_wone += amount
                if qualifies:
                    threshold_wone_holders += 1
                    threshold_wone += amount
                else:
                    aggregate_delivery_wone_holders += 1
                    aggregate_delivery_wone += amount
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
        if set(metadata) & native_candidate_holder_addresses:
            raise ValueError("new-holder metadata overlaps native claims")
        require_complete_new_holder_metadata(
            holders,
            native_candidate_holder_addresses,
            metadata,
            args.minimum_one,
            aggregate_delivery_addresses,
        )
        if wallet_total + staked_total != total_total:
            raise ValueError("global wallet and vault totals do not close")
        if native_total_total - native_wallet_total != staked_total:
            raise ValueError("native wallet and vault totals do not close")
        if total_total != native_total_total + delivered_wone:
            raise ValueError("WONE overlay total does not close")
        if delivered_wone > reserve:
            raise ValueError("delivered WONE airdrop exceeds reserve")
    except BaseException:
        os.remove(args.output + ".partial")
        raise
    os.replace(args.output + ".partial", args.output)

    retained = reserve - delivered_wone
    result = {
        "schema_version": 1,
        "status": "passed",
        "policy": (
            "WONE is added for addresses in the current inclusive 1,000 ONE "
            "batch and for non-Gate exchange wallets using aggregate manual "
            "delivery; the matching reserve is redistributed and the "
            "remainder is retained in the 2050 premint reserve"
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
        "aggregate_delivery_summary": args.aggregate_delivery_summary,
        "aggregate_delivery_summary_sha256": (
            file_sha256(args.aggregate_delivery_summary)
            if args.aggregate_delivery_summary
            else None
        ),
        "aggregate_delivery_sources": aggregate_delivery_sources,
        "aggregate_delivery_addresses_requested": len(
            aggregate_delivery_requested
        ),
        "aggregate_delivery_addresses_active": len(
            aggregate_delivery_addresses
        ),
        "aggregate_delivery_addresses_suppressed": sorted(
            aggregate_delivery_suppressed
        ),
        "native_rows": native_rows,
        "new_wone_only_rows": len(new_rows),
        "output_rows": output_rows,
        "baseline_qualified_rows": baseline_qualified,
        "newly_qualified_existing_native_rows": newly_qualified_existing,
        "newly_qualified_wone_only_rows": newly_qualified_wone_only,
        "newly_qualified_rows": (
            newly_qualified_existing + newly_qualified_wone_only
        ),
        "qualified_rows": qualified_rows,
        "wone_holder_rows": holder_rows,
        "excluded_wone_holder_rows": excluded_rows,
        "excluded_wone_atto": str(excluded_atto),
        "excluded_addresses_requested": sorted(excluded),
        "excluded_wone_holders": excluded_holders,
        "ordinary_threshold_wone_holder_rows": threshold_wone_holders,
        "ordinary_threshold_wone_atto": str(threshold_wone),
        "aggregate_delivery_wone_holder_rows": (
            aggregate_delivery_wone_holders
        ),
        "aggregate_delivery_wone_atto": str(aggregate_delivery_wone),
        "wone_recipient_rows": delivered_wone_holders,
        "wone_redistributed_to_recipients_atto": str(delivered_wone),
        "priority_wone_holder_rows": delivered_wone_holders,
        "wone_reserve_atto": str(reserve),
        "wone_redistributed_to_priority_atto": str(delivered_wone),
        "wone_retained_not_issued_atto": str(retained),
        "native_wallet_airdrop_atto": str(native_wallet_total),
        "native_total_claim_atto": str(native_total_total),
        "wallet_airdrop_atto": str(wallet_total),
        "staked_to_vault_atto": str(staked_total),
        "expanded_total_claim_atto": str(total_total),
        "component_totals_atto": {
            "native_wallet_airdrop": str(native_wallet_total),
            "wone_airdrop": str(delivered_wone),
            "wallet_airdrop": str(wallet_total),
            "staked_to_vault": str(staked_total),
            "native_total_claim": str(native_total_total),
            "total_claim": str(total_total),
        },
        "conservation": {
            "wone_reserve_equals_redistributed_plus_retained": (
                reserve == delivered_wone + retained
            ),
            "expanded_total_equals_native_plus_priority_wone": (
                total_total == native_total_total + delivered_wone
            ),
            "expanded_total_equals_native_plus_delivered_wone": (
                total_total == native_total_total + delivered_wone
            ),
        },
        "output": args.output,
        "output_sha256": file_sha256(args.output),
    }
    write_json_atomic(args.summary, result, args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
