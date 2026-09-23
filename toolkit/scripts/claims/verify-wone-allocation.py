#!/usr/bin/env python3

"""Independently verify the post-WONE allocation and routing artifacts."""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


ATTO_PER_ONE = 10**18
MINIMUM_ATTO = 1000 * ATTO_PER_ONE
WONE_ADDRESS = "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a"
LAYERZERO_ADDRESSES = (
    "0x5b18a4e73f9a4fe337a072516b317863ad3046aa",
    "0x905582f21fb9855c809d5b8933272a292dfbb138",
)
EMPTY_CODE_HASH = (
    "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
)

HOLDER_FIELDS = ("address", "wone_balance_atto", "wone_balance")
METADATA_FIELDS = (
    "address",
    "wone_balance_atto",
    "combined_total_atto",
    "claim_row_present",
    "nonce_shard0",
    "code_hash_shard0",
    "code_status",
)
NATIVE_FIELDS = (
    "secure_key",
    "address",
    "address_or_secure_key",
    "address_resolved",
    "claims_shard0_block",
    "claims_shard1_block",
    "valuation_price_reference_shard0_block",
    "valuation_price_usd_per_one",
    "liquid_shard0_atto",
    "liquid_shard1_atto",
    "liquid_total_atto",
    "active_staked_or_delegated_atto",
    "pending_undelegation_atto",
    "unclaimed_staking_reward_atto",
    "pending_cross_shard_atto",
    "wallet_airdrop_atto",
    "staked_to_vault_atto",
    "total_claim_atto",
    "liquid_shard0_one",
    "liquid_shard1_one",
    "liquid_total_one",
    "active_staked_or_delegated_one",
    "pending_undelegation_one",
    "unclaimed_staking_reward_one",
    "pending_cross_shard_one",
    "wallet_airdrop_one",
    "staked_to_vault_one",
    "total_claim_one",
    "wallet_airdrop_usd",
    "total_usd",
    "nonce_shard0",
    "nonce_shard1",
    "code_hash_shard0",
    "code_hash_shard1",
)
OVERLAY_FIELDS = (
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
NATIVE_COMPONENTS = (
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
MUTATED_NATIVE_FIELDS = {
    "wallet_airdrop_atto",
    "total_claim_atto",
    "wallet_airdrop_one",
    "total_claim_one",
    "wallet_airdrop_usd",
    "total_usd",
}
ROUTE_FIELDS = (
    "route_id",
    "priority",
    "source_address",
    "destination_id",
    "destination_address",
    "amount_atto",
    "allocation_method",
    "reason",
    "evidence",
    "notes",
)
ROUTING_EXCEPTION_FIELDS = (
    "component",
    "source_secure_key",
    "source_address",
    "source_category",
    "source_code_bearing",
    "migration_stage",
    "issuance_treatment",
    "validator_secure_key",
    "validator_address",
    "amount_atto",
    "exception_type",
    "route_id",
    "route_priority",
    "destination_id",
    "destination_address",
    "destination_status",
    "reason",
    "evidence",
)
WONE_REDISTRIBUTION_ROUTE = "wone-priority-holder-redistribution"
WONE_RETAINED_ROUTE = "wone-reserve-remainder-not-issued"
LAYERZERO_ROUTES = {
    LAYERZERO_ADDRESSES[0]: "layerzero-nativeoft-bsc-custody",
    LAYERZERO_ADDRESSES[1]: "layerzero-nativeoft-ethereum-custody",
}

UINT_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
KEY_RE = re.compile(r"0x[0-9a-f]{64}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
HEX_HASH_RE = re.compile(r"0x[0-9a-f]{64}\Z")
PRICE_RE = re.compile(r"(?:0|[1-9][0-9]*)\.[0-9]+\Z")


def migration_fields():
    fields = []
    for field in NATIVE_FIELDS:
        if field == "wallet_airdrop_atto":
            fields.extend(OVERLAY_FIELDS[:3])
        if field == "total_claim_atto":
            fields.extend(OVERLAY_FIELDS[3:5])
        if field == "wallet_airdrop_one":
            fields.extend(OVERLAY_FIELDS[5:8])
        if field == "total_claim_one":
            fields.extend(OVERLAY_FIELDS[8:])
        fields.append(field)
    return tuple(fields)


MIGRATION_FIELDS = migration_fields()


def default_path(relative):
    return ROOT / relative


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Verify WONE holders, claim overlay, ordinary-threshold and "
            "aggregate-exchange selection, and reserve routing without "
            "changing source artifacts."
        )
    )
    parser.add_argument(
        "--wone-holders",
        type=Path,
        default=default_path(
            "artifacts/wone-holder-accounting-20260917/"
            "wone-holders-cutoff-excluding-layerzero.csv"
        ),
    )
    parser.add_argument(
        "--wone-summary",
        type=Path,
        default=default_path(
            "artifacts/wone-holder-accounting-20260917/"
            "wone-holders-cutoff-summary.json"
        ),
    )
    parser.add_argument(
        "--wone-only-metadata",
        type=Path,
        default=default_path(
            "artifacts/wone-holder-accounting-20260917/"
            "wone-only-qualified-metadata.csv"
        ),
    )
    parser.add_argument(
        "--native-claims",
        type=Path,
        default=default_path(
            "artifacts/cutoff-20260910/claims/"
            "all-address-native-claims-cutoff.csv"
        ),
    )
    parser.add_argument(
        "--migration-claims",
        type=Path,
        default=default_path(
            "artifacts/cutoff-20260910/claims/"
            "all-address-migration-claims-cutoff.csv"
        ),
    )
    parser.add_argument(
        "--migration-summary",
        type=Path,
        default=default_path(
            "artifacts/cutoff-20260910/claims/"
            "all-address-migration-claims-cutoff-summary.json"
        ),
    )
    parser.add_argument(
        "--threshold-claims",
        type=Path,
        default=default_path(
            "artifacts/cutoff-20260910/claims/"
            "migration-claims-at-least-1000-one.csv"
        ),
    )
    parser.add_argument(
        "--threshold-summary",
        type=Path,
        default=default_path(
            "artifacts/cutoff-20260910/claims/"
            "migration-claims-at-least-1000-one-summary.json"
        ),
    )
    parser.add_argument(
        "--bridge-routes",
        type=Path,
        default=default_path("routing/local/bridge-reserves.csv"),
    )
    parser.add_argument(
        "--bridge-summary",
        type=Path,
        default=default_path("routing/local/bridge-reserves-summary.json"),
    )
    parser.add_argument(
        "--routing-exceptions",
        type=Path,
        default=default_path(
            "routing/local/generated/routing-exceptions.csv"
        ),
    )
    parser.add_argument(
        "--routing-summary",
        type=Path,
        default=default_path("routing/local/generated/routing-summary.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional JSON verification artifact; stdout is always emitted",
    )
    parser.add_argument(
        "--check-output",
        type=Path,
        help="require an existing JSON artifact to equal the recomputed result",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing --output artifact",
    )
    args = parser.parse_args(argv)
    if args.output is not None and args.check_output is not None:
        parser.error("--output and --check-output are mutually exclusive")
    if args.replace and args.output is None:
        parser.error("--replace requires --output")
    return args


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path):
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def load_json(path, label):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label}: duplicate JSON key {key!r}")
            value[key] = item
        return value

    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source, object_pairs_hook=unique_object)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label}: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label}: top-level JSON value must be an object")
    return value


def require_key(value, key, context):
    if key not in value:
        raise ValueError(f"{context}: missing {key}")
    return value[key]


def canonical_uint(value, context):
    if not isinstance(value, str) or UINT_RE.fullmatch(value) is None:
        raise ValueError(f"{context}: expected a canonical unsigned integer")
    return int(value)


def json_amount(value, key, context):
    return canonical_uint(require_key(value, key, context), f"{context}.{key}")


def json_count(value, key, context):
    item = require_key(value, key, context)
    if type(item) is not int or item < 0:
        raise ValueError(f"{context}.{key}: expected a non-negative integer")
    return item


def json_bool(value, key, context):
    item = require_key(value, key, context)
    if type(item) is not bool:
        raise ValueError(f"{context}.{key}: expected a boolean")
    return item


def normalize_address(value, context):
    if not isinstance(value, str) or ADDRESS_RE.fullmatch(value) is None:
        raise ValueError(f"{context}: invalid address {value!r}")
    return value.lower()


def normalize_key(value, context):
    if not isinstance(value, str) or KEY_RE.fullmatch(value) is None:
        raise ValueError(f"{context}: invalid secure key {value!r}")
    return value


def normalize_hex_hash(value, context):
    if not isinstance(value, str):
        raise ValueError(f"{context}: invalid hash")
    normalized = value.lower()
    if HEX_HASH_RE.fullmatch(normalized) is None:
        raise ValueError(f"{context}: invalid hash {value!r}")
    return normalized


def fixed(value):
    whole, fraction = divmod(value, ATTO_PER_ONE)
    return f"{whole}.{fraction:018d}"


def parse_price(value, context):
    if not isinstance(value, str) or PRICE_RE.fullmatch(value) is None:
        raise ValueError(f"{context}: invalid valuation price {value!r}")
    whole, fraction = value.split(".", 1)
    return int(whole + fraction), len(fraction)


def usd_value(amount, price, context):
    numerator, scale = parse_price(price, context)
    denominator = 10 ** (18 + scale)
    whole, fraction = divmod(amount * numerator, denominator)
    return f"{whole}.{fraction:0{18 + scale}d}"


def require_row_shape(row, fields, context):
    if None in row or any(row.get(field) is None for field in fields):
        raise ValueError(f"{context}: malformed CSV row")


def recorded_path(value, context):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}: expected a non-empty path")
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve()


def expect_path(summary, key, actual, context):
    expected = recorded_path(require_key(summary, key, context), f"{context}.{key}")
    if expected != actual.resolve():
        raise ValueError(
            f"{context}.{key}: recorded path {expected} does not match "
            f"{actual.resolve()}"
        )


def expect_hash(summary, key, actual_hash, context):
    value = require_key(summary, key, context)
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{context}.{key}: invalid SHA-256")
    if value != actual_hash:
        raise ValueError(f"{context}.{key}: SHA-256 mismatch")


def expect_amount(summary, key, expected, context):
    actual = json_amount(summary, key, context)
    if actual != expected:
        raise ValueError(
            f"{context}.{key}: expected {expected}, found {actual}"
        )


def expect_count(summary, key, expected, context):
    actual = json_count(summary, key, context)
    if actual != expected:
        raise ValueError(
            f"{context}.{key}: expected {expected}, found {actual}"
        )


def parse_exclusions(summary):
    context = "migration summary"
    requested = require_key(summary, "excluded_addresses_requested", context)
    if not isinstance(requested, list) or not requested:
        raise ValueError(
            "migration summary: excluded_addresses_requested must be explicit"
        )
    normalized = [
        normalize_address(value, f"{context}.excluded_addresses_requested")
        for value in requested
    ]
    if len(normalized) != len(set(normalized)):
        raise ValueError("migration summary: duplicate excluded address")
    if normalized != sorted(normalized):
        raise ValueError("migration summary: excluded addresses are not sorted")
    required = {WONE_ADDRESS, *LAYERZERO_ADDRESSES}
    if not required <= set(normalized):
        missing = sorted(required - set(normalized))
        raise ValueError(
            "migration summary: required WONE/LayerZero exclusions missing: "
            + ", ".join(missing)
        )

    details = require_key(summary, "excluded_wone_holders", context)
    if not isinstance(details, list):
        raise ValueError(
            "migration summary: excluded_wone_holders must be explicit"
        )
    parsed_details = []
    detail_addresses = set()
    for index, detail in enumerate(details):
        item_context = f"{context}.excluded_wone_holders[{index}]"
        if not isinstance(detail, dict) or set(detail) != {
            "address",
            "wone_balance_atto",
        }:
            raise ValueError(
                f"{item_context}: expected address and wone_balance_atto"
            )
        address = normalize_address(
            detail["address"], f"{item_context}.address"
        )
        amount = canonical_uint(
            detail["wone_balance_atto"],
            f"{item_context}.wone_balance_atto",
        )
        if amount <= 0:
            raise ValueError(f"{item_context}: excluded balance must be positive")
        if address not in normalized:
            raise ValueError(f"{item_context}: address was not requested")
        if address in detail_addresses:
            raise ValueError(f"{item_context}: duplicate detail")
        detail_addresses.add(address)
        parsed_details.append((address, amount))
    if [address for address, _ in parsed_details] != sorted(detail_addresses):
        raise ValueError(
            "migration summary: excluded WONE holder details are not sorted"
        )
    return set(normalized), parsed_details


def load_holders(path):
    holders = {}
    total = 0
    previous = None
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != HOLDER_FIELDS:
            raise ValueError(
                f"WONE holders: unexpected fields {reader.fieldnames}"
            )
        for line, row in enumerate(reader, start=2):
            context = f"WONE holders line {line}"
            require_row_shape(row, HOLDER_FIELDS, context)
            address = normalize_address(row["address"], f"{context}.address")
            raw = bytes.fromhex(address[2:])
            if previous is not None and raw <= previous:
                raise ValueError(
                    f"{context}: addresses must be unique and increasing"
                )
            previous = raw
            amount = canonical_uint(
                row["wone_balance_atto"],
                f"{context}.wone_balance_atto",
            )
            if amount <= 0:
                raise ValueError(f"{context}: WONE balance must be positive")
            if row["wone_balance"] != fixed(amount):
                raise ValueError(f"{context}: fixed-decimal balance mismatch")
            holders[address] = amount
            total += amount
    return holders, total


def validate_holder_summary(summary, holders, total, paths, hashes):
    context = "WONE holder summary"
    if require_key(summary, "status", context) != "passed":
        raise ValueError(f"{context}: status is not passed")
    if json_count(summary, "schema_version", context) != 1:
        raise ValueError(f"{context}: unsupported schema_version")
    contract = normalize_address(
        require_key(summary, "contract_address", context),
        f"{context}.contract_address",
    )
    if contract != WONE_ADDRESS:
        raise ValueError(f"{context}: wrong WONE contract")
    expect_path(summary, "output_path", paths["wone_holders"], context)
    expect_hash(summary, "output_sha256", hashes["wone_holders"], context)
    expect_count(summary, "holder_count", len(holders), context)
    expect_amount(summary, "total_holder_balance_atto", total, context)
    supply = json_amount(summary, "contract_total_supply_atto", context)
    reserve = json_amount(summary, "contract_native_reserve_atto", context)
    if supply != reserve or reserve != total:
        raise ValueError(
            f"{context}: totalSupply, native reserve, and holder sum differ"
        )
    expect_amount(summary, "reserve_minus_supply_atto", 0, context)
    return reserve


def validate_exclusion_details(
    summary,
    requested,
    recorded_details,
    holders,
):
    actual_details = [
        (address, amount)
        for address, amount in holders.items()
        if address in requested
    ]
    if recorded_details != actual_details:
        raise ValueError(
            "migration summary: excluded WONE holder details do not exactly "
            "match the holder ledger"
        )
    excluded_total = sum(amount for _, amount in actual_details)
    expect_count(
        summary,
        "excluded_wone_holder_rows",
        len(actual_details),
        "migration summary",
    )
    expect_amount(
        summary,
        "excluded_wone_atto",
        excluded_total,
        "migration summary",
    )
    return excluded_total


def load_aggregate_delivery_addresses(
    migration_summary,
    exclusions,
    paths,
    hashes,
):
    summary_value = migration_summary.get("aggregate_delivery_summary")
    if summary_value in (None, ""):
        return set(), {
            "requested": 0,
            "active": 0,
            "suppressed": [],
            "sources": [],
        }
    expect_path(
        migration_summary,
        "aggregate_delivery_summary",
        paths["aggregate_delivery_summary"],
        "migration summary",
    )
    expect_hash(
        migration_summary,
        "aggregate_delivery_summary_sha256",
        hashes["aggregate_delivery_summary"],
        "migration summary",
    )
    normalization = load_json(
        paths["aggregate_delivery_summary"],
        "exchange normalization summary",
    )
    if json_count(
        normalization,
        "schema_version",
        "exchange normalization summary",
    ) != 2:
        raise ValueError("unsupported exchange normalization schema")
    exchanges = require_key(
        normalization,
        "exchanges",
        "exchange normalization summary",
    )
    if not isinstance(exchanges, dict) or not exchanges:
        raise ValueError("exchange normalization summary has no exchanges")
    recorded_sources = require_key(
        migration_summary,
        "aggregate_delivery_sources",
        "migration summary",
    )
    if not isinstance(recorded_sources, list):
        raise ValueError(
            "migration summary.aggregate_delivery_sources must be a list"
        )
    expected_exchange_ids = [
        exchange_id
        for exchange_id, exchange in sorted(exchanges.items())
        if exchange.get("delivery_policy") == "manual_from_reserve"
    ]
    if len(recorded_sources) != len(expected_exchange_ids):
        raise ValueError("aggregate delivery source count mismatch")
    addresses = set()
    verified_sources = []
    for index, (exchange_id, recorded) in enumerate(
        zip(expected_exchange_ids, recorded_sources)
    ):
        context = f"migration summary.aggregate_delivery_sources[{index}]"
        if not isinstance(recorded, dict) or set(recorded) != {
            "exchange_id",
            "path",
            "sha256",
            "addresses",
        }:
            raise ValueError(f"{context}: invalid source record")
        if recorded["exchange_id"] != exchange_id:
            raise ValueError(f"{context}: exchange id mismatch")
        exchange = exchanges[exchange_id]
        source_path = paths[f"aggregate_delivery_source_{index}"]
        expected_path = recorded_path(
            require_key(exchange, "output", f"exchange {exchange_id}"),
            f"exchange {exchange_id}.output",
        )
        if source_path != expected_path:
            raise ValueError(f"{context}: normalized source path mismatch")
        expected_hash = require_key(
            exchange,
            "output_sha256",
            f"exchange {exchange_id}",
        )
        if (
            recorded["sha256"] != expected_hash
            or hashes[f"aggregate_delivery_source_{index}"] != expected_hash
        ):
            raise ValueError(f"{context}: normalized source hash mismatch")
        source_addresses = set()
        with source_path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if not {"exchange_id", "address_hex"} <= set(
                reader.fieldnames or ()
            ):
                raise ValueError(
                    f"{source_path}: missing normalized exchange fields"
                )
            for line, row in enumerate(reader, start=2):
                if row["exchange_id"] != exchange_id:
                    raise ValueError(
                        f"{source_path}:{line}: exchange id mismatch"
                    )
                address = normalize_address(
                    row["address_hex"],
                    f"{source_path}:{line}.address_hex",
                )
                if address in source_addresses or address in addresses:
                    raise ValueError(
                        f"{source_path}:{line}: duplicate exchange address"
                    )
                source_addresses.add(address)
                addresses.add(address)
        expected_rows = json_count(
            exchange,
            "normalized_rows",
            f"exchange {exchange_id}",
        )
        if (
            len(source_addresses) != expected_rows
            or json_count(recorded, "addresses", context) != expected_rows
        ):
            raise ValueError(f"{context}: normalized row count mismatch")
        verified_sources.append(exchange_id)
    suppressed = sorted(addresses & exclusions)
    active = addresses - exclusions
    expect_count(
        migration_summary,
        "aggregate_delivery_addresses_requested",
        len(addresses),
        "migration summary",
    )
    expect_count(
        migration_summary,
        "aggregate_delivery_addresses_active",
        len(active),
        "migration summary",
    )
    if migration_summary.get("aggregate_delivery_addresses_suppressed") != (
        suppressed
    ):
        raise ValueError("aggregate delivery suppressed-address mismatch")
    return active, {
        "requested": len(addresses),
        "active": len(active),
        "suppressed": suppressed,
        "sources": verified_sources,
    }


def load_metadata(path, holders, exclusions, aggregate_delivery):
    metadata = {}
    previous = None
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != METADATA_FIELDS:
            raise ValueError(
                f"WONE-only metadata: unexpected fields {reader.fieldnames}"
            )
        for line, row in enumerate(reader, start=2):
            context = f"WONE-only metadata line {line}"
            require_row_shape(row, METADATA_FIELDS, context)
            address = normalize_address(row["address"], f"{context}.address")
            if previous is not None and address <= previous:
                raise ValueError(
                    f"{context}: addresses must be unique and increasing"
                )
            previous = address
            if row["claim_row_present"] != "false":
                raise ValueError(
                    f"{context}: claim_row_present must be false"
                )
            amount = canonical_uint(
                row["wone_balance_atto"],
                f"{context}.wone_balance_atto",
            )
            combined = canonical_uint(
                row["combined_total_atto"],
                f"{context}.combined_total_atto",
            )
            if (
                amount < MINIMUM_ATTO
                and address not in aggregate_delivery
            ) or combined != amount:
                raise ValueError(
                    f"{context}: invalid WONE-only delivery amount"
                )
            if address in exclusions:
                raise ValueError(f"{context}: excluded address has metadata")
            if holders.get(address) != amount:
                raise ValueError(f"{context}: holder balance mismatch")
            canonical_uint(row["nonce_shard0"], f"{context}.nonce_shard0")
            code_hash = normalize_hex_hash(
                row["code_hash_shard0"],
                f"{context}.code_hash_shard0",
            )
            expected_status = (
                "code_less"
                if code_hash == EMPTY_CODE_HASH
                else "code_bearing"
            )
            if row["code_status"] != expected_status:
                raise ValueError(f"{context}: code status does not match hash")
            metadata[address] = {
                "row": row,
                "secure_key": (
                    "0x" + lib.keccak256(bytes.fromhex(address[2:])).hex()
                ),
            }
    return metadata


class SortedClaimReader:
    def __init__(self, path, fields, label):
        self.path = path
        self.fields = fields
        self.label = label
        self.source = None
        self.reader = None
        self.previous = None
        self.line = 1

    def __enter__(self):
        self.source = self.path.open(newline="")
        self.reader = csv.DictReader(self.source)
        if tuple(self.reader.fieldnames or ()) != self.fields:
            self.source.close()
            raise ValueError(
                f"{self.label}: unexpected fields {self.reader.fieldnames}"
            )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.source.close()

    def pop(self):
        try:
            row = next(self.reader)
        except StopIteration:
            return None
        self.line += 1
        context = f"{self.label} line {self.line}"
        require_row_shape(row, self.fields, context)
        key = normalize_key(row["secure_key"], f"{context}.secure_key")
        if self.previous is not None and key <= self.previous:
            raise ValueError(
                f"{context}: secure keys must be unique and increasing"
            )
        self.previous = key
        return {"row": row, "key": key, "line": self.line, "context": context}


class ThresholdCursor:
    def __init__(self, reader):
        self.reader = reader
        self.current = reader.pop()

    def compare(self, migration_item, qualifies):
        key = migration_item["key"]
        if self.current is not None and self.current["key"] < key:
            raise ValueError(
                f"{self.current['context']}: threshold output has an extra row"
            )
        if qualifies:
            if self.current is None or self.current["key"] != key:
                raise ValueError(
                    f"{migration_item['context']}: qualified row is missing "
                    "from threshold output"
                )
            if self.current["row"] != migration_item["row"]:
                raise ValueError(
                    f"{migration_item['context']}: threshold row is not an "
                    "exact copy"
                )
            self.current = self.reader.pop()
        elif self.current is not None and self.current["key"] == key:
            raise ValueError(
                f"{migration_item['context']}: below-threshold row was selected"
            )

    def finish(self):
        if self.current is not None:
            raise ValueError(
                f"{self.current['context']}: threshold output has an extra row"
            )


def validate_native_row(row, context):
    address = normalize_address(row["address"], f"{context}.address")
    key = normalize_key(row["secure_key"], f"{context}.secure_key")
    lib.require_address_secure_key(address, key, context)
    if row["address_or_secure_key"] != row["address"]:
        raise ValueError(f"{context}: address_or_secure_key mismatch")
    if row["address_resolved"] != "true":
        raise ValueError(f"{context}: address is not resolved")
    amounts = {}
    for component in NATIVE_COMPONENTS:
        amount = canonical_uint(
            row[f"{component}_atto"],
            f"{context}.{component}_atto",
        )
        if row[f"{component}_one"] != fixed(amount):
            raise ValueError(
                f"{context}.{component}_one: fixed-decimal mismatch"
            )
        amounts[component] = amount
    if (
        amounts["liquid_shard0"] + amounts["liquid_shard1"]
        != amounts["liquid_total"]
    ):
        raise ValueError(f"{context}: liquid shard total mismatch")
    expected_wallet = sum(
        amounts[component]
        for component in (
            "liquid_shard0",
            "liquid_shard1",
            "pending_undelegation",
            "unclaimed_staking_reward",
            "pending_cross_shard",
        )
    )
    if amounts["wallet_airdrop"] != expected_wallet:
        raise ValueError(f"{context}: native wallet arithmetic mismatch")
    if (
        amounts["staked_to_vault"]
        != amounts["active_staked_or_delegated"]
    ):
        raise ValueError(f"{context}: native vault arithmetic mismatch")
    if (
        amounts["wallet_airdrop"] + amounts["staked_to_vault"]
        != amounts["total_claim"]
    ):
        raise ValueError(f"{context}: native total arithmetic mismatch")
    price = row["valuation_price_usd_per_one"]
    if row["wallet_airdrop_usd"] != usd_value(
        amounts["wallet_airdrop"], price, f"{context}.price"
    ):
        raise ValueError(f"{context}: native wallet USD mismatch")
    if row["total_usd"] != usd_value(
        amounts["total_claim"], price, f"{context}.price"
    ):
        raise ValueError(f"{context}: native total USD mismatch")
    return {"address": address, "key": key, **amounts}


def validate_fixed_overlay(row, values, context):
    for field, amount in (
        ("native_wallet_airdrop_one", values["native_wallet"]),
        ("wone_balance_one", values["wone_balance"]),
        ("wone_airdrop_one", values["wone_airdrop"]),
        ("wallet_airdrop_one", values["wallet"]),
        ("staked_to_vault_one", values["staked"]),
        ("qualification_total_one", values["qualification"]),
        ("native_total_claim_one", values["native_total"]),
        ("total_claim_one", values["total"]),
    ):
        if row[field] != fixed(amount):
            raise ValueError(f"{context}.{field}: fixed-decimal mismatch")
    price = row["valuation_price_usd_per_one"]
    if row["wallet_airdrop_usd"] != usd_value(
        values["wallet"], price, f"{context}.price"
    ):
        raise ValueError(f"{context}: expanded wallet USD mismatch")
    if row["total_usd"] != usd_value(
        values["total"], price, f"{context}.price"
    ):
        raise ValueError(f"{context}: expanded total USD mismatch")


def validate_wone_only_static(row, metadata, cutoff_block, context):
    address = normalize_address(row["address"], f"{context}.address")
    if row["address"] != lib.to_checksum(address):
        raise ValueError(f"{context}: WONE-only address is not checksummed")
    if row["address_or_secure_key"] != row["address"]:
        raise ValueError(f"{context}: address_or_secure_key mismatch")
    if row["address_resolved"] != "true":
        raise ValueError(f"{context}: WONE-only address is not resolved")
    if row["secure_key"] != metadata["secure_key"]:
        raise ValueError(f"{context}: WONE-only address/secure-key mismatch")
    if canonical_uint(
        row["claims_shard0_block"], f"{context}.claims_shard0_block"
    ) != cutoff_block:
        raise ValueError(f"{context}: WONE-only cutoff block mismatch")
    canonical_uint(
        row["claims_shard1_block"], f"{context}.claims_shard1_block"
    )
    canonical_uint(
        row["valuation_price_reference_shard0_block"],
        f"{context}.valuation_price_reference_shard0_block",
    )
    parse_price(
        row["valuation_price_usd_per_one"],
        f"{context}.valuation_price_usd_per_one",
    )
    metadata_row = metadata["row"]
    if (
        row["nonce_shard0"] != metadata_row["nonce_shard0"]
        or row["code_hash_shard0"].lower()
        != metadata_row["code_hash_shard0"].lower()
        or row["nonce_shard1"] != ""
        or row["code_hash_shard1"] != ""
    ):
        raise ValueError(f"{context}: WONE-only cutoff metadata mismatch")
    zero_components = (
        "liquid_shard0",
        "liquid_shard1",
        "liquid_total",
        "active_staked_or_delegated",
        "pending_undelegation",
        "unclaimed_staking_reward",
        "pending_cross_shard",
    )
    for component in zero_components:
        if canonical_uint(
            row[f"{component}_atto"], f"{context}.{component}_atto"
        ) != 0:
            raise ValueError(f"{context}: WONE-only native component is nonzero")
        if row[f"{component}_one"] != fixed(0):
            raise ValueError(f"{context}: WONE-only decimal component mismatch")


def holder_amount_for_native(holders, address, context):
    signed = holders.get(address)
    if signed is None:
        return 0
    if signed < 0:
        raise ValueError(f"{context}: duplicate native claim address")
    holders[address] = -signed
    return signed


def validate_migration_row(
    migration_item,
    native_item,
    holders,
    exclusions,
    aggregate_delivery,
    metadata,
    seen_metadata,
    cutoff_block,
):
    row = migration_item["row"]
    context = migration_item["context"]
    address = normalize_address(row["address"], f"{context}.address")
    if native_item is None:
        metadata_item = metadata.get(address)
        if metadata_item is None:
            raise ValueError(
                "WONE-only metadata does not equal the complete delivery set: "
                f"{address} is missing"
            )
        validate_wone_only_static(row, metadata_item, cutoff_block, context)
        if address in seen_metadata:
            raise ValueError(f"{context}: duplicate WONE-only metadata row")
        seen_metadata.add(address)
        native_wallet = 0
        staked = 0
        native_total = 0
        actual_wone = holders[address]
    else:
        native_row = native_item["row"]
        native = validate_native_row(native_row, native_item["context"])
        if migration_item["key"] != native["key"]:
            raise ValueError(f"{context}: native secure key mismatch")
        if address != native["address"]:
            raise ValueError(f"{context}: native address mismatch")
        for field in NATIVE_FIELDS:
            if field not in MUTATED_NATIVE_FIELDS and row[field] != native_row[field]:
                raise ValueError(
                    f"{context}.{field}: native field was not preserved"
                )
        native_wallet = native["wallet_airdrop"]
        staked = native["staked_to_vault"]
        native_total = native["total_claim"]
        actual_wone = holder_amount_for_native(
            holders, address, native_item["context"]
        )

    effective_wone = 0 if address in exclusions else actual_wone
    qualification = native_total + effective_wone
    wone_airdrop = (
        effective_wone
        if (
            qualification >= MINIMUM_ATTO
            or address in aggregate_delivery
        )
        else 0
    )
    wallet = native_wallet + wone_airdrop
    total = native_total + wone_airdrop
    expected = {
        "native_wallet_airdrop_atto": native_wallet,
        "wone_balance_atto": effective_wone,
        "wone_airdrop_atto": wone_airdrop,
        "wallet_airdrop_atto": wallet,
        "staked_to_vault_atto": staked,
        "qualification_total_atto": qualification,
        "native_total_claim_atto": native_total,
        "total_claim_atto": total,
    }
    actual = {
        field: canonical_uint(row[field], f"{context}.{field}")
        for field in expected
    }
    for field, expected_value in expected.items():
        if actual[field] != expected_value:
            raise ValueError(
                f"{context}.{field}: expected {expected_value}, "
                f"found {actual[field]}"
            )
    if (
        actual["native_wallet_airdrop_atto"]
        + actual["staked_to_vault_atto"]
        != actual["native_total_claim_atto"]
        or actual["qualification_total_atto"]
        != actual["native_total_claim_atto"]
        + actual["wone_balance_atto"]
        or actual["wallet_airdrop_atto"]
        != actual["native_wallet_airdrop_atto"]
        + actual["wone_airdrop_atto"]
        or actual["wallet_airdrop_atto"]
        + actual["staked_to_vault_atto"]
        != actual["total_claim_atto"]
    ):
        raise ValueError(f"{context}: WONE allocation arithmetic mismatch")
    values = {
        "native_wallet": native_wallet,
        "wone_balance": effective_wone,
        "wone_airdrop": wone_airdrop,
        "wallet": wallet,
        "staked": staked,
        "qualification": qualification,
        "native_total": native_total,
        "total": total,
    }
    validate_fixed_overlay(row, values, context)
    return {
        "address": address,
        "key": migration_item["key"],
        "native_present": native_item is not None,
        "actual_wone": actual_wone,
        "aggregate_delivery": address in aggregate_delivery,
        **values,
        "liquid_shard0": canonical_uint(
            row["liquid_shard0_atto"], f"{context}.liquid_shard0_atto"
        ),
        "liquid_shard1": canonical_uint(
            row["liquid_shard1_atto"], f"{context}.liquid_shard1_atto"
        ),
    }


def verify_claim_ledgers(
    paths,
    holders,
    exclusions,
    aggregate_delivery,
    metadata,
    cutoff_block,
):
    metrics = {
        "native_rows": 0,
        "migration_rows": 0,
        "wone_only_rows": 0,
        "baseline_qualified_rows": 0,
        "newly_qualified_existing_native_rows": 0,
        "qualified_rows": 0,
        "exact_threshold_rows": 0,
        "priority_wone_holder_rows": 0,
        "priority_wone": 0,
        "ordinary_threshold_wone_holder_rows": 0,
        "ordinary_threshold_wone": 0,
        "aggregate_delivery_wone_holder_rows": 0,
        "aggregate_delivery_wone": 0,
        "newly_qualified_wone_only_rows": 0,
        "native_wallet": 0,
        "native_total": 0,
        "wallet": 0,
        "staked": 0,
        "total": 0,
        "threshold_wallet": 0,
        "threshold_staked": 0,
        "threshold_total": 0,
        "threshold_wone": 0,
    }
    seen_metadata = set()
    wone_recipients = set()
    special = {}
    with SortedClaimReader(
        paths["native_claims"], NATIVE_FIELDS, "native claims"
    ) as native_reader, SortedClaimReader(
        paths["migration_claims"], MIGRATION_FIELDS, "migration claims"
    ) as migration_reader, SortedClaimReader(
        paths["threshold_claims"], MIGRATION_FIELDS, "threshold claims"
    ) as threshold_reader:
        threshold = ThresholdCursor(threshold_reader)
        native_item = native_reader.pop()
        migration_item = migration_reader.pop()
        while native_item is not None or migration_item is not None:
            if migration_item is None or (
                native_item is not None
                and native_item["key"] < migration_item["key"]
            ):
                raise ValueError(
                    f"{native_item['context']}: native row is missing from "
                    "migration claims"
                )
            if native_item is None or migration_item["key"] < native_item["key"]:
                matched_native = None
            else:
                matched_native = native_item
            values = validate_migration_row(
                migration_item,
                matched_native,
                holders,
                exclusions,
                aggregate_delivery,
                metadata,
                seen_metadata,
                cutoff_block,
            )
            qualifies = values["qualification"] >= MINIMUM_ATTO
            threshold.compare(migration_item, qualifies)
            metrics["migration_rows"] += 1
            metrics["wallet"] += values["wallet"]
            metrics["staked"] += values["staked"]
            metrics["total"] += values["total"]
            metrics["qualified_rows"] += int(qualifies)
            metrics["exact_threshold_rows"] += int(
                values["qualification"] == MINIMUM_ATTO
            )
            if values["wone_airdrop"]:
                metrics["priority_wone_holder_rows"] += 1
                metrics["priority_wone"] += values["wone_airdrop"]
                wone_recipients.add(values["address"])
                if qualifies:
                    metrics["ordinary_threshold_wone_holder_rows"] += 1
                    metrics["ordinary_threshold_wone"] += values[
                        "wone_airdrop"
                    ]
                elif values["aggregate_delivery"]:
                    metrics["aggregate_delivery_wone_holder_rows"] += 1
                    metrics["aggregate_delivery_wone"] += values[
                        "wone_airdrop"
                    ]
                else:
                    raise ValueError(
                        "below-threshold WONE recipient is not an aggregate "
                        f"exchange wallet: {values['address']}"
                    )
            if qualifies:
                metrics["threshold_wallet"] += values["wallet"]
                metrics["threshold_staked"] += values["staked"]
                metrics["threshold_total"] += values["total"]
                metrics["threshold_wone"] += values["wone_airdrop"]
            if matched_native is None:
                metrics["wone_only_rows"] += 1
                metrics["newly_qualified_wone_only_rows"] += int(qualifies)
            else:
                metrics["native_rows"] += 1
                metrics["native_wallet"] += values["native_wallet"]
                metrics["native_total"] += values["native_total"]
                baseline = values["native_total"] >= MINIMUM_ATTO
                metrics["baseline_qualified_rows"] += int(baseline)
                metrics["newly_qualified_existing_native_rows"] += int(
                    qualifies and not baseline
                )
                native_item = native_reader.pop()
            if values["address"] in {WONE_ADDRESS, *LAYERZERO_ADDRESSES}:
                if values["address"] in special:
                    raise ValueError(
                        f"duplicate special claim {values['address']}"
                    )
                if not values["native_present"]:
                    raise ValueError(
                        f"special claim {values['address']} is WONE-only"
                    )
                special[values["address"]] = values
            migration_item = migration_reader.pop()
        threshold.finish()

    expected_metadata = {
        address
        for address, signed_amount in holders.items()
        if signed_amount > 0
        and (
            signed_amount >= MINIMUM_ATTO
            or address in aggregate_delivery
        )
        and address not in exclusions
    }
    if set(metadata) != expected_metadata or seen_metadata != expected_metadata:
        missing = sorted(expected_metadata - set(metadata))
        extra = sorted(set(metadata) - expected_metadata)
        unmerged = sorted(set(metadata) - seen_metadata)
        raise ValueError(
            "WONE-only metadata does not equal the complete delivery set: "
            f"missing={missing[:5]} extra={extra[:5]} "
            f"unmerged={unmerged[:5]}"
        )
    required_special = {WONE_ADDRESS, *LAYERZERO_ADDRESSES}
    if set(special) != required_special:
        raise ValueError(
            "claim ledgers are missing WONE/LayerZero rows: "
            + ", ".join(sorted(required_special - set(special)))
        )
    return metrics, special, wone_recipients


def validate_claim_summaries(
    holder_count,
    reserve,
    excluded_total,
    metrics,
    paths,
    hashes,
    migration,
    threshold,
    aggregate_stats,
):
    context = "migration summary"
    if require_key(migration, "status", context) != "passed":
        raise ValueError(f"{context}: status is not passed")
    if json_count(migration, "schema_version", context) != 1:
        raise ValueError(f"{context}: unsupported schema_version")
    expect_path(migration, "native_claims", paths["native_claims"], context)
    expect_hash(
        migration,
        "native_claims_sha256",
        hashes["native_claims"],
        context,
    )
    expect_path(
        migration, "wone_holders", paths["wone_holders"], context
    )
    expect_hash(
        migration,
        "wone_holders_sha256",
        hashes["wone_holders"],
        context,
    )
    expect_path(migration, "wone_summary", paths["wone_summary"], context)
    expect_hash(
        migration,
        "wone_summary_sha256",
        hashes["wone_summary"],
        context,
    )
    expect_path(
        migration,
        "new_holder_metadata",
        paths["wone_only_metadata"],
        context,
    )
    expect_hash(
        migration,
        "new_holder_metadata_sha256",
        hashes["wone_only_metadata"],
        context,
    )
    expect_path(migration, "output", paths["migration_claims"], context)
    expect_hash(
        migration, "output_sha256", hashes["migration_claims"], context
    )
    expect_amount(migration, "minimum_atto", MINIMUM_ATTO, context)
    expect_count(migration, "native_rows", metrics["native_rows"], context)
    expect_count(
        migration, "output_rows", metrics["migration_rows"], context
    )
    expect_count(
        migration, "new_wone_only_rows", metrics["wone_only_rows"], context
    )
    expect_count(
        migration,
        "newly_qualified_wone_only_rows",
        metrics["newly_qualified_wone_only_rows"],
        context,
    )
    expect_count(
        migration,
        "baseline_qualified_rows",
        metrics["baseline_qualified_rows"],
        context,
    )
    expect_count(
        migration,
        "newly_qualified_existing_native_rows",
        metrics["newly_qualified_existing_native_rows"],
        context,
    )
    newly_qualified = (
        metrics["newly_qualified_existing_native_rows"]
        + metrics["newly_qualified_wone_only_rows"]
    )
    expect_count(
        migration, "newly_qualified_rows", newly_qualified, context
    )
    expect_count(
        migration, "qualified_rows", metrics["qualified_rows"], context
    )
    expect_count(
        migration,
        "priority_wone_holder_rows",
        metrics["priority_wone_holder_rows"],
        context,
    )
    if migration.get("aggregate_delivery_summary"):
        expect_count(
            migration,
            "ordinary_threshold_wone_holder_rows",
            metrics["ordinary_threshold_wone_holder_rows"],
            context,
        )
        expect_amount(
            migration,
            "ordinary_threshold_wone_atto",
            metrics["ordinary_threshold_wone"],
            context,
        )
        expect_count(
            migration,
            "aggregate_delivery_wone_holder_rows",
            metrics["aggregate_delivery_wone_holder_rows"],
            context,
        )
        expect_amount(
            migration,
            "aggregate_delivery_wone_atto",
            metrics["aggregate_delivery_wone"],
            context,
        )
        expect_count(
            migration,
            "wone_recipient_rows",
            metrics["priority_wone_holder_rows"],
            context,
        )
        expect_amount(
            migration,
            "wone_redistributed_to_recipients_atto",
            metrics["priority_wone"],
            context,
        )
        if aggregate_stats["active"] != json_count(
            migration,
            "aggregate_delivery_addresses_active",
            context,
        ):
            raise ValueError("aggregate delivery address total mismatch")
    expect_count(migration, "wone_holder_rows", holder_count, context)
    expect_amount(migration, "wone_reserve_atto", reserve, context)
    expect_amount(
        migration,
        "wone_redistributed_to_priority_atto",
        metrics["priority_wone"],
        context,
    )
    retained = reserve - metrics["priority_wone"]
    if retained < 0:
        raise ValueError("migration summary: WONE redistribution exceeds reserve")
    expect_amount(
        migration, "wone_retained_not_issued_atto", retained, context
    )
    expect_amount(
        migration, "excluded_wone_atto", excluded_total, context
    )
    expect_amount(
        migration,
        "native_wallet_airdrop_atto",
        metrics["native_wallet"],
        context,
    )
    expect_amount(
        migration,
        "native_total_claim_atto",
        metrics["native_total"],
        context,
    )
    expect_amount(
        migration, "wallet_airdrop_atto", metrics["wallet"], context
    )
    expect_amount(
        migration, "staked_to_vault_atto", metrics["staked"], context
    )
    expect_amount(
        migration, "expanded_total_claim_atto", metrics["total"], context
    )
    components = require_key(migration, "component_totals_atto", context)
    if not isinstance(components, dict):
        raise ValueError(f"{context}.component_totals_atto must be an object")
    expected_components = {
        "native_wallet_airdrop": metrics["native_wallet"],
        "wone_airdrop": metrics["priority_wone"],
        "wallet_airdrop": metrics["wallet"],
        "staked_to_vault": metrics["staked"],
        "native_total_claim": metrics["native_total"],
        "total_claim": metrics["total"],
    }
    for key, expected in expected_components.items():
        expect_amount(
            components, key, expected, f"{context}.component_totals_atto"
        )
    conservation = require_key(migration, "conservation", context)
    if not isinstance(conservation, dict):
        raise ValueError(f"{context}.conservation must be an object")
    for key in (
        "wone_reserve_equals_redistributed_plus_retained",
        "expanded_total_equals_native_plus_priority_wone",
    ):
        if not json_bool(conservation, key, f"{context}.conservation"):
            raise ValueError(f"{context}.conservation.{key} is false")
    if migration.get("aggregate_delivery_summary") and not json_bool(
        conservation,
        "expanded_total_equals_native_plus_delivered_wone",
        f"{context}.conservation",
    ):
        raise ValueError(
            f"{context}.conservation."
            "expanded_total_equals_native_plus_delivered_wone is false"
        )
    if (
        reserve != metrics["priority_wone"] + retained
        or metrics["total"]
        != metrics["native_total"] + metrics["priority_wone"]
    ):
        raise ValueError(f"{context}: conservation arithmetic failed")

    context = "threshold summary"
    if require_key(threshold, "comparison", context) != "ge":
        raise ValueError(f"{context}: comparison is not ge")
    if require_key(threshold, "threshold_field", context) != (
        "qualification_total_atto"
    ):
        raise ValueError(f"{context}: wrong threshold field")
    expect_path(threshold, "input", paths["migration_claims"], context)
    expect_hash(
        threshold, "input_sha256", hashes["migration_claims"], context
    )
    expect_path(threshold, "output", paths["threshold_claims"], context)
    expect_hash(
        threshold, "output_sha256", hashes["threshold_claims"], context
    )
    expect_amount(threshold, "minimum_atto", MINIMUM_ATTO, context)
    expect_count(
        threshold, "input_rows", metrics["migration_rows"], context
    )
    expect_count(
        threshold, "output_rows", metrics["qualified_rows"], context
    )
    expect_count(
        threshold,
        "exact_threshold_rows",
        metrics["exact_threshold_rows"],
        context,
    )
    expect_amount(
        threshold,
        "wallet_airdrop_atto",
        metrics["threshold_wallet"],
        context,
    )
    expect_amount(
        threshold,
        "staked_to_vault_atto",
        metrics["threshold_staked"],
        context,
    )
    expect_amount(
        threshold,
        "total_claim_atto",
        metrics["threshold_total"],
        context,
    )
    expect_amount(
        threshold,
        "wone_airdrop_atto",
        metrics["threshold_wone"],
        context,
    )
    return retained


def exact_route(row, expected, context):
    for field, value in expected.items():
        if row[field] != value:
            raise ValueError(
                f"{context}.{field}: expected {value!r}, found {row[field]!r}"
            )


def verify_bridge_routes(
    path,
    bridge_summary,
    migration_summary,
    reserve,
    redistributed,
    retained,
    paths,
    hashes,
):
    rows = {}
    previous = None
    count = 0
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != ROUTE_FIELDS:
            raise ValueError(
                f"bridge routes: unexpected fields {reader.fieldnames}"
            )
        for line, row in enumerate(reader, start=2):
            context = f"bridge routes line {line}"
            require_row_shape(row, ROUTE_FIELDS, context)
            route_id = row["route_id"]
            if not route_id or route_id in rows:
                raise ValueError(f"{context}: duplicate or empty route_id")
            priority = canonical_uint(row["priority"], f"{context}.priority")
            order = (priority, route_id)
            if previous is not None and order <= previous:
                raise ValueError(
                    f"{context}: routes are not ordered by priority and id"
                )
            previous = order
            address = normalize_address(
                row["source_address"], f"{context}.source_address"
            )
            row["_source_address"] = address
            rows[route_id] = row
            count += 1

    base_rows = {}
    previous = None
    with paths["bridge_base"].open(newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != ROUTE_FIELDS:
            raise ValueError(
                f"bridge route base: unexpected fields {reader.fieldnames}"
            )
        for line, row in enumerate(reader, start=2):
            context = f"bridge route base line {line}"
            require_row_shape(row, ROUTE_FIELDS, context)
            route_id = row["route_id"]
            if not route_id or route_id in base_rows:
                raise ValueError(f"{context}: duplicate or empty route_id")
            priority = canonical_uint(row["priority"], f"{context}.priority")
            order = (priority, route_id)
            if previous is not None and order <= previous:
                raise ValueError(
                    f"{context}: routes are not ordered by priority and id"
                )
            previous = order
            source_address = normalize_address(
                row["source_address"], f"{context}.source_address"
            )
            if source_address == WONE_ADDRESS:
                raise ValueError(
                    f"{context}: immutable base contains a WONE route"
                )
            base_rows[route_id] = row
    retained_base_rows = {
        route_id: {field: row[field] for field in ROUTE_FIELDS}
        for route_id, row in rows.items()
        if route_id
        not in {WONE_REDISTRIBUTION_ROUTE, WONE_RETAINED_ROUTE}
    }
    if retained_base_rows != base_rows:
        raise ValueError(
            "bridge routes: non-WONE rows differ from immutable base"
        )

    redistribution = rows.get(WONE_REDISTRIBUTION_ROUTE)
    if redistribution is None:
        raise ValueError("bridge routes: missing WONE redistribution route")
    exact_route(
        redistribution,
        {
            "_source_address": WONE_ADDRESS,
            "priority": "400",
            "destination_id": "wone-holder-redistribution",
            "destination_address": "",
            "amount_atto": str(redistributed),
            "allocation_method": "wallet_only",
            "reason": "wone_holder_delivery_redistribution",
        },
        "bridge WONE redistribution route",
    )
    retained_route = rows.get(WONE_RETAINED_ROUTE)
    if retained_route is None:
        raise ValueError("bridge routes: missing WONE retained route")
    exact_route(
        retained_route,
        {
            "_source_address": WONE_ADDRESS,
            "priority": "401",
            "destination_id": "not-issuing",
            "destination_address": "",
            "amount_atto": str(retained),
            "allocation_method": "wallet_only",
            "reason": "wone_reserve_remainder_retained_not_issued",
        },
        "bridge WONE retained route",
    )
    wone_rows = [
        row for row in rows.values() if row["_source_address"] == WONE_ADDRESS
    ]
    if {row["route_id"] for row in wone_rows} != {
        WONE_REDISTRIBUTION_ROUTE,
        WONE_RETAINED_ROUTE,
    }:
        raise ValueError("bridge routes: unexpected WONE source route")
    if WONE_REDISTRIBUTION_ROUTE == "wone-reserve-custody" or (
        "wone-reserve-custody" in rows
    ):
        raise ValueError("bridge routes: legacy whole-reserve route remains")

    for address, route_id in LAYERZERO_ROUTES.items():
        row = rows.get(route_id)
        if row is None:
            raise ValueError(f"bridge routes: missing {route_id}")
        exact_route(
            row,
            {
                "_source_address": address,
                "priority": "400",
                "destination_id": "layerzero-nativeoft-custody",
                "destination_address": "",
                "amount_atto": "SHARD0_LIQUID",
                "allocation_method": "wallet_first_pro_rata_vault",
                "reason": "layerzero_nativeoft_reconciliation_hold",
            },
            f"bridge route {route_id}",
        )
        address_rows = [
            item
            for item in rows.values()
            if item["_source_address"] == address
        ]
        if len(address_rows) != 1:
            raise ValueError(
                f"bridge routes: unexpected extra LayerZero route for {address}"
            )
        if row["destination_id"] in {
            "wone-holder-redistribution",
            "not-issuing",
        }:
            raise ValueError(
                f"bridge routes: LayerZero address {address} uses WONE policy"
            )

    context = "bridge summary"
    if require_key(bridge_summary, "status", context) != "passed":
        raise ValueError(f"{context}: status is not passed")
    if json_count(bridge_summary, "schema_version", context) != 1:
        raise ValueError(f"{context}: unsupported schema_version")
    expect_path(bridge_summary, "output", paths["bridge_routes"], context)
    expect_hash(
        bridge_summary, "output_sha256", hashes["bridge_routes"], context
    )
    expect_path(
        bridge_summary, "wone_summary", paths["migration_summary"], context
    )
    expect_hash(
        bridge_summary,
        "wone_summary_sha256",
        hashes["migration_summary"],
        context,
    )
    expect_path(bridge_summary, "input", paths["bridge_base"], context)
    expect_hash(
        bridge_summary, "input_sha256", hashes["bridge_base"], context
    )
    expect_count(bridge_summary, "route_rows", count, context)
    expect_amount(
        bridge_summary, "wone_reserve_atto", reserve, context
    )
    expect_amount(
        bridge_summary,
        "wone_redistributed_to_holders_atto",
        redistributed,
        context,
    )
    expect_amount(
        bridge_summary,
        "wone_retained_not_issued_atto",
        retained,
        context,
    )
    if reserve != redistributed + retained:
        raise ValueError("bridge routes: reserve split does not close")
    return count


def exception_matches(row, expected, context):
    for field, value in expected.items():
        if row[field] != value:
            raise ValueError(
                f"{context}.{field}: expected {value!r}, found {row[field]!r}"
            )


def verify_routing_exceptions(
    path,
    special,
    reserve,
    redistributed,
    retained,
    wone_recipients,
):
    rows = []
    special_rows = {address: [] for address in special}
    totals = {
        "rows": 0,
        "exception_wallet": 0,
        "exception_staked": 0,
        "not_issued_wallet": 0,
        "not_issued_staked": 0,
        "redistributed_wallet": 0,
        "redistributed_staked": 0,
        "exchange_manual_wallet": 0,
        "exchange_manual_staked": 0,
    }
    recipient_not_issued = set()
    stages = {
        "initial",
        "exchange_manual",
        "next_stage",
        "deferred",
        "manual_review",
    }
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != ROUTING_EXCEPTION_FIELDS:
            raise ValueError(
                f"routing exceptions: unexpected fields {reader.fieldnames}"
            )
        for line, row in enumerate(reader, start=2):
            context = f"routing exceptions line {line}"
            require_row_shape(row, ROUTING_EXCEPTION_FIELDS, context)
            address = normalize_address(
                row["source_address"], f"{context}.source_address"
            )
            source_key = normalize_key(
                row["source_secure_key"], f"{context}.source_secure_key"
            )
            lib.require_address_secure_key(address, source_key, context)
            amount = canonical_uint(
                row["amount_atto"], f"{context}.amount_atto"
            )
            if amount <= 0:
                raise ValueError(f"{context}: amount must be positive")
            if row["component"] not in {"wallet_airdrop", "vault_shares"}:
                raise ValueError(f"{context}: invalid component")
            exchange_route = (
                row["reason"] == "exchange_manual_reserve_delivery"
            )
            if exchange_route:
                if row["issuance_treatment"] != "manual_from_reserve" or row[
                    "destination_status"
                ] not in {"exchange_manual", "hold"}:
                    raise ValueError(
                        f"{context}: exchange route treatment mismatch"
                    )
                if row["migration_stage"] != "exchange_manual":
                    raise ValueError(f"{context}: exchange route stage mismatch")
            else:
                expected_treatment = {
                    "ready": "issue",
                    "hold": "issue",
                    "not_issuing": "not_issued",
                    "redistributed": "redistributed",
                }.get(row["destination_status"])
                if row["issuance_treatment"] != expected_treatment:
                    raise ValueError(f"{context}: issuance treatment mismatch")
                if row["issuance_treatment"] == "issue":
                    if row["migration_stage"] not in stages - {"exchange_manual"}:
                        raise ValueError(f"{context}: issued row has no stage")
                elif row["migration_stage"] and row["migration_stage"] not in stages:
                    raise ValueError(f"{context}: invalid associated stage")
            canonical_uint(
                row["route_priority"], f"{context}.route_priority"
            )
            component = (
                "wallet" if row["component"] == "wallet_airdrop" else "staked"
            )
            totals[f"exception_{component}"] += amount
            if exchange_route:
                totals[f"exchange_manual_{component}"] += amount
            if row["destination_status"] == "not_issuing":
                totals[f"not_issued_{component}"] += amount
                if (
                    component == "wallet"
                    and address in wone_recipients
                ):
                    recipient_not_issued.add(address)
            if row["destination_status"] == "redistributed":
                totals[f"redistributed_{component}"] += amount
                if (
                    address != WONE_ADDRESS
                    or row["route_id"] != WONE_REDISTRIBUTION_ROUTE
                    or row["destination_id"]
                    != "wone-holder-redistribution"
                ):
                    raise ValueError(
                        f"{context}: non-WONE redistribution route"
                    )
            if address in LAYERZERO_ADDRESSES and (
                row["destination_status"] in {
                    "redistributed",
                    "not_issuing",
                }
                or row["destination_id"]
                in {"wone-holder-redistribution", "not-issuing"}
            ):
                raise ValueError(
                    f"{context}: LayerZero address uses WONE reserve policy"
                )
            if address in special_rows:
                special_rows[address].append(row)
            rows.append(row)
            totals["rows"] += 1

    wone = special[WONE_ADDRESS]
    if wone["staked"] != 0:
        raise ValueError("WONE claim unexpectedly has a vault component")
    if wone["liquid_shard0"] != reserve:
        raise ValueError(
            "WONE contract shard-0 liquid balance does not equal reserve"
        )
    if wone["wone_balance"] != 0 or wone["wone_airdrop"] != 0:
        raise ValueError("WONE contract received its own WONE allocation")
    non_backing_residual = wone["native_wallet"] - reserve
    if non_backing_residual < wone["liquid_shard1"]:
        raise ValueError(
            "WONE non-backing residual does not preserve shard-1 native ONE"
        )
    wone_rows = special_rows[WONE_ADDRESS]
    redistribution_rows = [
        row
        for row in wone_rows
        if row["route_id"] == WONE_REDISTRIBUTION_ROUTE
    ]
    retained_rows = [
        row for row in wone_rows if row["route_id"] == WONE_RETAINED_ROUTE
    ]
    if len(redistribution_rows) != 1 or len(retained_rows) != 1:
        raise ValueError(
            "routing exceptions: WONE reserve split routes are not unique"
        )
    exception_matches(
        redistribution_rows[0],
        {
            "component": "wallet_airdrop",
            "source_secure_key": wone["key"],
            "amount_atto": str(redistributed),
            "route_priority": "400",
            "destination_id": "wone-holder-redistribution",
            "destination_address": "",
            "destination_status": "redistributed",
            "migration_stage": "",
            "issuance_treatment": "redistributed",
            "reason": "wone_holder_delivery_redistribution",
        },
        "WONE redistribution exception",
    )
    exception_matches(
        retained_rows[0],
        {
            "component": "wallet_airdrop",
            "source_secure_key": wone["key"],
            "amount_atto": str(retained),
            "route_priority": "401",
            "destination_id": "not-issuing",
            "destination_address": "",
            "destination_status": "not_issuing",
            "migration_stage": "",
            "issuance_treatment": "not_issued",
            "reason": "wone_reserve_remainder_retained_not_issued",
        },
        "WONE retained exception",
    )
    residual_rows = [
        row
        for row in wone_rows
        if row["route_id"]
        not in {WONE_REDISTRIBUTION_ROUTE, WONE_RETAINED_ROUTE}
    ]
    residual_total = 0
    for row in residual_rows:
        exception_matches(
            row,
            {
                "component": "wallet_airdrop",
                "source_secure_key": wone["key"],
                "destination_id": "not-issuing",
                "destination_status": "not_issuing",
                "migration_stage": "",
                "issuance_treatment": "not_issued",
                "reason": "reviewed_contract_allocation_not_issued",
            },
            "WONE reviewed-contract residual exception",
        )
        if canonical_uint(
            row["route_priority"], "WONE residual route priority"
        ) <= 401:
            raise ValueError(
                "WONE residual route does not follow the reserve split"
            )
        residual_total += canonical_uint(
            row["amount_atto"], "WONE residual amount"
        )
    if residual_total != non_backing_residual:
        raise ValueError(
            "WONE non-backing residual is not exactly excluded by reviewed "
            "contract policy"
        )
    if sum(canonical_uint(row["amount_atto"], "WONE exception") for row in wone_rows) != (
        wone["native_wallet"]
    ):
        raise ValueError("WONE routing exceptions do not close to native wallet")

    layerzero_metrics = {}
    for address, route_id in LAYERZERO_ROUTES.items():
        claim = special[address]
        if (
            claim["actual_wone"] != 0
            or claim["wone_balance"] != 0
            or claim["wone_airdrop"] != 0
        ):
            raise ValueError(f"LayerZero address {address} has WONE")
        address_rows = special_rows[address]
        primary = [row for row in address_rows if row["route_id"] == route_id]
        if len(primary) != 1:
            raise ValueError(
                f"routing exceptions: LayerZero route {route_id} is not unique"
            )
        exception_matches(
            primary[0],
            {
                "component": "wallet_airdrop",
                "source_secure_key": claim["key"],
                "amount_atto": str(claim["liquid_shard0"]),
                "route_priority": "400",
                "destination_id": "layerzero-nativeoft-custody",
                "destination_address": "",
                "destination_status": "hold",
                "migration_stage": "next_stage",
                "issuance_treatment": "issue",
                "reason": "layerzero_nativeoft_reconciliation_hold",
            },
            f"LayerZero exception {route_id}",
        )
        remainder_rows = [
            row for row in address_rows if row["route_id"] != route_id
        ]
        remainder_total = 0
        for row in remainder_rows:
            exception_matches(
                row,
                {
                    "component": "wallet_airdrop",
                    "source_secure_key": claim["key"],
                    "destination_id": "layerzero-nativeoft-custody",
                    "destination_status": "hold",
                    "migration_stage": "next_stage",
                    "issuance_treatment": "issue",
                    "reason": "layerzero_nativeoft_reconciliation_hold",
                },
                f"LayerZero residual exception {address}",
            )
            remainder_total += canonical_uint(
                row["amount_atto"], f"LayerZero residual {address}"
            )
        expected_remainder = claim["native_wallet"] - claim["liquid_shard0"]
        if remainder_total != expected_remainder:
            raise ValueError(
                f"LayerZero residual routing mismatch for {address}"
            )
        layerzero_metrics[address] = {
            "liquid_shard0_atto": str(claim["liquid_shard0"]),
            "liquid_shard1_atto": str(claim["liquid_shard1"]),
            "next_stage_residual_atto": str(remainder_total),
        }
    totals["wone_recipient_not_issued_rows"] = len(
        recipient_not_issued
    )
    totals["wone_non_backing_residual"] = non_backing_residual
    totals["wone_reviewed_contract_non_issuance"] = residual_total
    totals["layerzero"] = layerzero_metrics
    return totals


def validate_routing_summary(
    summary,
    metrics,
    exception_metrics,
    reserve,
    redistributed,
    retained,
    paths,
    hashes,
):
    context = "routing summary"
    if require_key(summary, "status", context) not in {"ready", "hold"}:
        raise ValueError(f"{context}: invalid status")
    outputs = require_key(summary, "outputs", context)
    if not isinstance(outputs, dict):
        raise ValueError(f"{context}.outputs must be an object")
    for name, input_name in (
        ("routing_exceptions", "routing_exceptions"),
        ("governor_exceptions", "routing_governor_exceptions"),
        ("unresolved", "routing_unresolved"),
    ):
        record = require_key(outputs, name, f"{context}.outputs")
        if not isinstance(record, dict):
            raise ValueError(f"{context}.outputs.{name} must be an object")
        expect_path(
            record, "path", paths[input_name], f"{context}.outputs.{name}"
        )
        expect_hash(
            record,
            "sha256",
            hashes[input_name],
            f"{context}.outputs.{name}",
        )
    route_files = require_key(summary, "route_files", context)
    if not isinstance(route_files, list):
        raise ValueError(f"{context}.route_files must be a list")
    bridge_matches = [
        value
        for value in route_files
        if recorded_path(value, f"{context}.route_files")
        == paths["bridge_routes"].resolve()
    ]
    if len(bridge_matches) != 1:
        raise ValueError(
            f"{context}: bridge-reserves.csv is not included exactly once"
        )

    amounts = {}
    amount_fields = (
        "source_wallet_airdrop_atto",
        "source_wone_airdrop_atto",
        "source_staked_to_vault_atto",
        "source_total_claim_atto",
        "routed_wallet_airdrop_atto",
        "routed_staked_to_vault_atto",
        "exception_wallet_airdrop_atto",
        "exception_staked_to_vault_atto",
        "implicit_wallet_airdrop_atto",
        "implicit_staked_to_vault_atto",
        "not_issued_wallet_airdrop_atto",
        "not_issued_staked_to_vault_atto",
        "not_issued_total_claim_atto",
        "redistributed_wallet_airdrop_atto",
        "redistributed_staked_to_vault_atto",
        "redistributed_total_claim_atto",
        "exchange_manual_wallet_airdrop_atto",
        "exchange_manual_staked_to_vault_atto",
        "exchange_manual_total_claim_atto",
        "issuable_wallet_airdrop_atto",
        "issuable_staked_to_vault_atto",
        "issuable_total_claim_atto",
        "unresolved_wallet_airdrop_atto",
        "unresolved_staked_to_vault_atto",
        "wone_reserve_source_atto",
        "wone_redistributed_to_holders_atto",
        "wone_retained_not_issued_atto",
    )
    for field in amount_fields:
        amounts[field] = json_amount(summary, field, context)
    source_wallet = amounts["source_wallet_airdrop_atto"]
    source_staked = amounts["source_staked_to_vault_atto"]
    source_total = amounts["source_total_claim_atto"]
    not_issued_wallet = amounts["not_issued_wallet_airdrop_atto"]
    not_issued_staked = amounts["not_issued_staked_to_vault_atto"]
    redistributed_wallet = amounts["redistributed_wallet_airdrop_atto"]
    redistributed_staked = amounts["redistributed_staked_to_vault_atto"]
    exchange_wallet = amounts["exchange_manual_wallet_airdrop_atto"]
    exchange_staked = amounts["exchange_manual_staked_to_vault_atto"]
    if amounts["exchange_manual_total_claim_atto"] != (
        exchange_wallet + exchange_staked
    ):
        raise ValueError(f"{context}: exchange manual components do not close")
    if source_total != source_wallet + source_staked:
        raise ValueError(f"{context}: source components do not close")
    if (
        amounts["routed_wallet_airdrop_atto"] != source_wallet
        or amounts["routed_staked_to_vault_atto"] != source_staked
    ):
        raise ValueError(f"{context}: routed components do not close")
    if (
        amounts["exception_wallet_airdrop_atto"]
        + amounts["implicit_wallet_airdrop_atto"]
        != source_wallet
        or amounts["exception_staked_to_vault_atto"]
        + amounts["implicit_staked_to_vault_atto"]
        != source_staked
    ):
        raise ValueError(f"{context}: exception/implicit components do not close")
    if amounts["not_issued_total_claim_atto"] != (
        not_issued_wallet + not_issued_staked
    ):
        raise ValueError(f"{context}: not-issued components do not close")
    if amounts["redistributed_total_claim_atto"] != (
        redistributed_wallet + redistributed_staked
    ):
        raise ValueError(f"{context}: redistributed components do not close")
    if (
        amounts["issuable_wallet_airdrop_atto"]
        != source_wallet - not_issued_wallet - redistributed_wallet - exchange_wallet
        or amounts["issuable_staked_to_vault_atto"]
        != source_staked - not_issued_staked - redistributed_staked - exchange_staked
        or amounts["issuable_total_claim_atto"]
        != source_total
        - not_issued_wallet
        - not_issued_staked
        - redistributed_wallet
        - redistributed_staked
        - exchange_wallet
        - exchange_staked
    ):
        raise ValueError(f"{context}: issuable components do not close")
    if (
        amounts["unresolved_wallet_airdrop_atto"]
        > amounts["issuable_wallet_airdrop_atto"]
        or amounts["unresolved_staked_to_vault_atto"]
        > amounts["issuable_staked_to_vault_atto"]
    ):
        raise ValueError(f"{context}: unresolved amount exceeds issuable amount")
    expected_exception_fields = {
        "exception_wallet_airdrop_atto": exception_metrics[
            "exception_wallet"
        ],
        "exception_staked_to_vault_atto": exception_metrics[
            "exception_staked"
        ],
        "not_issued_wallet_airdrop_atto": exception_metrics[
            "not_issued_wallet"
        ],
        "not_issued_staked_to_vault_atto": exception_metrics[
            "not_issued_staked"
        ],
        "redistributed_wallet_airdrop_atto": exception_metrics[
            "redistributed_wallet"
        ],
        "redistributed_staked_to_vault_atto": exception_metrics[
            "redistributed_staked"
        ],
        "exchange_manual_wallet_airdrop_atto": exception_metrics[
            "exchange_manual_wallet"
        ],
        "exchange_manual_staked_to_vault_atto": exception_metrics[
            "exchange_manual_staked"
        ],
    }
    for field, expected in expected_exception_fields.items():
        if amounts[field] != expected:
            raise ValueError(
                f"{context}.{field}: routing exception total mismatch"
            )
    if (
        amounts["source_wone_airdrop_atto"] != redistributed
        or redistributed_wallet != redistributed
        or redistributed_staked != 0
        or amounts["wone_reserve_source_atto"] != reserve
        or amounts["wone_redistributed_to_holders_atto"] != redistributed
        or amounts["wone_retained_not_issued_atto"] != retained
        or reserve != redistributed + retained
    ):
        raise ValueError(f"{context}: WONE reserve/conservation mismatch")
    expect_count(
        summary,
        "routing_exception_rows",
        exception_metrics["rows"],
        context,
    )
    expect_count(
        summary,
        "wone_recipient_not_issued_rows",
        exception_metrics["wone_recipient_not_issued_rows"],
        context,
    )
    expect_count(
        summary, "priority_claims", metrics["qualified_rows"], context
    )
    claims = json_count(summary, "claims", context)
    deferred = json_count(
        summary, "explicitly_routed_deferred_claims", context
    )
    if claims != metrics["qualified_rows"] + deferred:
        raise ValueError(f"{context}: claim population does not close")
    if (
        source_wallet < metrics["threshold_wallet"]
        or source_staked < metrics["threshold_staked"]
        or source_total < metrics["threshold_total"]
    ):
        raise ValueError(f"{context}: routed source omits threshold claims")
    return amounts


def discover_inputs(args, summaries):
    paths = {
        "wone_holders": args.wone_holders,
        "wone_summary": args.wone_summary,
        "wone_only_metadata": args.wone_only_metadata,
        "native_claims": args.native_claims,
        "migration_claims": args.migration_claims,
        "migration_summary": args.migration_summary,
        "threshold_claims": args.threshold_claims,
        "threshold_summary": args.threshold_summary,
        "bridge_routes": args.bridge_routes,
        "bridge_summary": args.bridge_summary,
        "routing_exceptions": args.routing_exceptions,
        "routing_summary": args.routing_summary,
    }
    migration = summaries["migration"]
    aggregate_summary = migration.get("aggregate_delivery_summary")
    if aggregate_summary:
        paths["aggregate_delivery_summary"] = recorded_path(
            aggregate_summary,
            "migration summary.aggregate_delivery_summary",
        )
        aggregate_sources = require_key(
            migration,
            "aggregate_delivery_sources",
            "migration summary",
        )
        if not isinstance(aggregate_sources, list):
            raise ValueError(
                "migration summary.aggregate_delivery_sources must be a list"
            )
        for index, source in enumerate(aggregate_sources):
            context = (
                f"migration summary.aggregate_delivery_sources[{index}]"
            )
            if not isinstance(source, dict):
                raise ValueError(f"{context}: expected an object")
            paths[f"aggregate_delivery_source_{index}"] = recorded_path(
                require_key(source, "path", context),
                f"{context}.path",
            )
    bridge = summaries["bridge"]
    paths["bridge_base"] = recorded_path(
        require_key(bridge, "input", "bridge summary"),
        "bridge summary.input",
    )
    outputs = require_key(
        summaries["routing"], "outputs", "routing summary"
    )
    if not isinstance(outputs, dict):
        raise ValueError("routing summary.outputs must be an object")
    output_names = {
        "routing_exceptions": "routing_exceptions",
        "governor_exceptions": "routing_governor_exceptions",
        "unresolved": "routing_unresolved",
    }
    for output_name, input_name in output_names.items():
        record = require_key(
            outputs, output_name, "routing summary.outputs"
        )
        if not isinstance(record, dict):
            raise ValueError(
                f"routing summary.outputs.{output_name} must be an object"
            )
        path = recorded_path(
            require_key(
                record, "path", f"routing summary.outputs.{output_name}"
            ),
            f"routing summary.outputs.{output_name}.path",
        )
        if input_name == "routing_exceptions":
            if path != args.routing_exceptions.resolve():
                raise ValueError(
                    "routing summary: routing exception path mismatch"
                )
        else:
            paths[input_name] = path
    for name, path in paths.items():
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"{name}: input file does not exist: {resolved}")
        paths[name] = resolved
    return paths


def verify(args):
    summary_paths = {
        "holder": args.wone_summary,
        "migration": args.migration_summary,
        "threshold": args.threshold_summary,
        "bridge": args.bridge_summary,
        "routing": args.routing_summary,
    }
    summaries = {
        name: load_json(path, f"{name} summary")
        for name, path in summary_paths.items()
    }
    paths = discover_inputs(args, summaries)
    if args.output is not None:
        output = args.output.resolve()
        if output in set(paths.values()):
            raise ValueError("--output may not replace an input artifact")
        partial = Path(str(output) + ".partial")
        if partial.exists():
            raise FileExistsError(partial)
        if output.exists() and not args.replace:
            raise FileExistsError(output)
    hashes = {name: file_sha256(path) for name, path in paths.items()}

    migration_summary = summaries["migration"]
    exclusions, recorded_details = parse_exclusions(migration_summary)
    aggregate_delivery, aggregate_stats = (
        load_aggregate_delivery_addresses(
            migration_summary,
            exclusions,
            paths,
            hashes,
        )
    )
    holders, holder_total = load_holders(paths["wone_holders"])
    reserve = validate_holder_summary(
        summaries["holder"], holders, holder_total, paths, hashes
    )
    for address in LAYERZERO_ADDRESSES:
        if address in holders:
            raise ValueError(
                f"LayerZero address {address} has a positive WONE balance"
            )
    excluded_total = validate_exclusion_details(
        migration_summary,
        exclusions,
        recorded_details,
        holders,
    )
    metadata = load_metadata(
        paths["wone_only_metadata"],
        holders,
        exclusions,
        aggregate_delivery,
    )
    cutoff_block = json_count(
        summaries["holder"], "cutoff_block", "WONE holder summary"
    )
    claim_metrics, special, wone_recipients = verify_claim_ledgers(
        paths,
        holders,
        exclusions,
        aggregate_delivery,
        metadata,
        cutoff_block,
    )
    retained = validate_claim_summaries(
        len(holders),
        reserve,
        excluded_total,
        claim_metrics,
        paths,
        hashes,
        migration_summary,
        summaries["threshold"],
        aggregate_stats,
    )
    redistributed = claim_metrics["priority_wone"]
    bridge_rows = verify_bridge_routes(
        paths["bridge_routes"],
        summaries["bridge"],
        migration_summary,
        reserve,
        redistributed,
        retained,
        paths,
        hashes,
    )
    exception_metrics = verify_routing_exceptions(
        paths["routing_exceptions"],
        special,
        reserve,
        redistributed,
        retained,
        wone_recipients,
    )
    routing_amounts = validate_routing_summary(
        summaries["routing"],
        claim_metrics,
        exception_metrics,
        reserve,
        redistributed,
        retained,
        paths,
        hashes,
    )

    result = {
        "schema_version": 1,
        "status": "passed",
        "inputs": {
            name: {
                "path": display_path(path),
                "sha256": hashes[name],
            }
            for name, path in paths.items()
        },
        "checks": {
            "wone_holder_ledger": True,
            "explicit_exclusions": True,
            "claim_overlay_exact_merge": True,
            "wone_only_metadata_complete": True,
            "inclusive_threshold_exact_subset": True,
            "reserve_split_exact_routes": True,
            "routing_conservation": True,
            "layerzero_wone_isolation": True,
            "wone_shard1_residual_preserved": True,
            "aggregate_exchange_wone_delivery": True,
        },
        "holders": {
            "holder_rows": len(holders),
            "holder_balance_atto": str(holder_total),
            "excluded_addresses": sorted(exclusions),
            "excluded_holder_rows": len(recorded_details),
            "excluded_wone_atto": str(excluded_total),
        },
        "qualification": {
            "minimum_atto": str(MINIMUM_ATTO),
            "native_rows": claim_metrics["native_rows"],
            "migration_rows": claim_metrics["migration_rows"],
            "wone_only_rows": claim_metrics["wone_only_rows"],
            "baseline_qualified_rows": claim_metrics[
                "baseline_qualified_rows"
            ],
            "newly_qualified_existing_native_rows": claim_metrics[
                "newly_qualified_existing_native_rows"
            ],
            "qualified_rows": claim_metrics["qualified_rows"],
            "exact_threshold_rows": claim_metrics["exact_threshold_rows"],
            "priority_wone_holder_rows": claim_metrics[
                "priority_wone_holder_rows"
            ],
            "ordinary_threshold_wone_holder_rows": claim_metrics[
                "ordinary_threshold_wone_holder_rows"
            ],
            "ordinary_threshold_wone_atto": str(
                claim_metrics["ordinary_threshold_wone"]
            ),
            "aggregate_delivery_wone_holder_rows": claim_metrics[
                "aggregate_delivery_wone_holder_rows"
            ],
            "aggregate_delivery_wone_atto": str(
                claim_metrics["aggregate_delivery_wone"]
            ),
            "native_total_claim_atto": str(claim_metrics["native_total"]),
            "wone_airdrop_atto": str(redistributed),
            "expanded_total_claim_atto": str(claim_metrics["total"]),
        },
        "aggregate_delivery": aggregate_stats,
        "routing": {
            "bridge_route_rows": bridge_rows,
            "routing_exception_rows": exception_metrics["rows"],
            "wone_reserve_atto": str(reserve),
            "wone_redistributed_atto": str(redistributed),
            "wone_retained_not_issued_atto": str(retained),
            "not_issued_total_claim_atto": str(
                routing_amounts["not_issued_total_claim_atto"]
            ),
            "issuable_total_claim_atto": str(
                routing_amounts["issuable_total_claim_atto"]
            ),
        },
        "shard1": {
            "wone_liquid_shard0_atto": str(
                special[WONE_ADDRESS]["liquid_shard0"]
            ),
            "wone_liquid_shard1_atto": str(
                special[WONE_ADDRESS]["liquid_shard1"]
            ),
            "wone_non_backing_residual_atto": str(
                exception_metrics["wone_non_backing_residual"]
            ),
            "wone_reviewed_contract_non_issuance_atto": str(
                exception_metrics["wone_reviewed_contract_non_issuance"]
            ),
            "layerzero": exception_metrics["layerzero"],
        },
    }
    return result


def write_output(path, result, replace):
    output = path.resolve()
    partial = Path(str(output) + ".partial")
    if partial.exists():
        raise FileExistsError(partial)
    if output.exists() and not replace:
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with partial.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, output)
    except BaseException:
        if partial.exists():
            partial.unlink()
        raise


def main(argv=None):
    args = parse_args(argv)
    result = verify(args)
    if args.output is not None:
        write_output(args.output, result, args.replace)
    if args.check_output is not None:
        expected = load_json(args.check_output, "expected verification output")
        if expected != result:
            raise ValueError(
                "expected verification output does not equal recomputed result"
            )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
