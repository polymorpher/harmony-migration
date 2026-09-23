#!/usr/bin/env python3

"""Build the manual exchange delivery worksheet and validator vault releases.

Every exchange wallet is excluded from the airdrop. Its cutoff entitlement is
delivered manually, directly from the year 2050 supply reserve, to the
destination(s) the exchange confirmed. Delegated principal owned by exchange
wallets is released from the validator vaults so that the vault ledger and the
manual delivery worksheet describe the same ONE exactly once.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


DISPLAY_NAMES = {
    "binance": "Binance",
    "binance-us": "Binance.US",
    "bybit": "Bybit",
    "gate": "Gate",
    "htx": "HTX",
    "mexc": "MEXC",
    "okx": "OKX",
    "kucoin": "KuCoin",
    "digitalx": "DigitalX",
}
DELIVERY_SOURCE = "year_2050_supply_reserve"
DELIVERY_FIELDS = (
    "exchange_id",
    "delivery_tier",
    "destination_address",
    "destination_one",
    "delivery_status",
    "source_wallets",
    "native_wallet_atto",
    "wone_atto",
    "staked_released_atto",
    "total_delivery_atto",
)
WALLET_FIELDS = (
    "exchange_id",
    "source_address",
    "address_one",
    "delivery_tier",
    "delivery_status",
    "liquid_shard0_atto",
    "liquid_shard1_atto",
    "pending_cross_shard_atto",
    "pending_undelegation_atto",
    "unclaimed_staking_reward_atto",
    "native_wallet_atto",
    "wone_atto",
    "wallet_component_atto",
    "staked_released_atto",
    "total_delivery_atto",
    "wallet_destination_address",
    "staking_destination_address",
)
DELEGATION_FIELDS = (
    "exchange_id",
    "validator_address",
    "validator_secure_key",
    "delegator_address",
    "delegator_secure_key",
    "staked_released_atto",
    "destination_address",
)
VAULT_FIELDS = (
    "validator_address",
    "validator_secure_key",
    "base_vault_assets_atto",
    "exchange_withdrawal_atto",
    "adjusted_vault_assets_atto",
    "delegation_rows_removed",
    "exchanges",
)
TIER_TEXT = {
    "aggregate": "aggregate destination",
    "aggregate_split": "wallet and staking destinations",
    "same_address": "same address",
    "same_address_initial": "same address (meets initial criteria)",
    "aggregated_non_initial": "aggregate (below initial criteria)",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--audits-dir", required=True)
    parser.add_argument("--normalization-summary", required=True)
    parser.add_argument("--native-claims", required=True)
    parser.add_argument("--delegations", required=True)
    parser.add_argument("--vaults", required=True)
    parser.add_argument("--gate-supplemental", required=True)
    parser.add_argument("--gate-reported-total", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_policy(path):
    with open(path, encoding="utf-8") as source:
        policy = json.load(source)
    if policy.get("schema_version") != 2:
        raise ValueError("unsupported exchange policy schema")
    if policy.get("delivery_source") != DELIVERY_SOURCE:
        raise ValueError("exchange policy delivery source mismatch")
    for config in policy["exchanges"]:
        if config.get("delivery_policy") != "manual_from_reserve":
            raise ValueError(f"{config['id']}: unexpected delivery policy")
        if config["id"] not in DISPLAY_NAMES:
            raise ValueError(f"{config['id']}: missing display name")
    return policy


def load_address_list(path):
    addresses = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        address = lib.any_to_hex(text)
        if address is None:
            raise ValueError(f"{path}: invalid address {text!r}")
        addresses.append(address.lower())
    if not addresses:
        raise ValueError(f"{path}: address list is empty")
    return addresses


def load_reported_total(path):
    text = Path(path).read_text(encoding="utf-8").strip()
    value = Decimal(text) * 10**18
    if value < 0 or value != value.to_integral_value():
        raise ValueError("Gate reported total is not exact atto-ONE")
    return int(value)


def load_digitalx_submission(path):
    """Return DigitalX's submitted wallet total and rolled-back deposit claim."""
    with open(path, encoding="utf-8") as source:
        normalization = json.load(source)
    if normalization.get("schema_version") != 2:
        raise ValueError("unsupported normalization summary schema")
    summary = normalization["exchanges"]["digitalx"]
    details = summary["parser_details"]
    rolled_back = details["rolled_back_deposits"]
    rolled_back_total = int(details["rolled_back_deposit_total_atto"])
    if rolled_back_total != sum(int(item["amount_atto"]) for item in rolled_back):
        raise ValueError("DigitalX rolled-back deposit claim does not sum")
    submitted_wallet_total = int(summary["submitted_balance_atto"])
    if submitted_wallet_total + rolled_back_total != int(
        details["submitted_wallet_and_rolled_back_total_atto"]
    ):
        raise ValueError("DigitalX submitted combined total does not close")
    return {
        "submitted_wallet_total_atto": submitted_wallet_total,
        "submitted_wallet_rows": summary["submitted_balance_rows"],
        "rolled_back_deposit_total_atto": rolled_back_total,
        "rolled_back_deposit_rows": len(rolled_back),
        "rolled_back_deposits": rolled_back,
        "submitted_combined_total_atto": submitted_wallet_total
        + rolled_back_total,
        "displayed_totals": details["source_summary_checks"],
    }


def one(value, commas=False):
    whole, fraction = divmod(int(value), 10**18)
    return f"{whole:,}.{fraction:018d}" if commas else f"{whole}.{fraction:018d}"


def signed_one(value, commas=False):
    value = int(value)
    text = one(abs(value), commas)
    return f"-{text}" if value < 0 else f"+{text}"


def atomic_csv(path, fields, rows, replace):
    path = Path(path)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def atomic_json(path, value, replace):
    path = Path(path)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def atomic_text(path, value, replace):
    path = Path(path)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("x", encoding="utf-8") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


AUDIT_REQUIRED = {
    "exchange_id",
    "address_hex",
    "address_one",
    "claim_status",
    "liquid_shard0_atto",
    "liquid_shard1_atto",
    "pending_cross_shard_atto",
    "pending_undelegation_atto",
    "unclaimed_staking_reward_atto",
    "native_wallet_airdrop_atto",
    "wone_airdrop_atto",
    "wallet_airdrop_atto",
    "staked_to_vault_atto",
    "native_total_claim_atto",
    "total_claim_atto",
    "wallet_component_atto",
    "staking_component_atto",
    "migration_stage",
    "issuance_treatment",
    "delivery_policy",
    "destination_mode",
    "delivery_tier",
    "planned_wallet_destination",
    "planned_staking_destination",
    "planned_delivery_status",
    "planned_total_entitlement_atto",
}


def load_audit(path, exchange_id):
    rows = []
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        if not AUDIT_REQUIRED <= set(reader.fieldnames or ()):
            raise ValueError(f"{path}: missing exchange audit fields")
        for line, row in enumerate(reader, start=2):
            if row["exchange_id"] != exchange_id:
                raise ValueError(f"{path}:{line}: exchange id mismatch")
            if row["delivery_policy"] != "manual_from_reserve":
                raise ValueError(f"{path}:{line}: unexpected delivery policy")
            native_wallet = int(row["native_wallet_airdrop_atto"])
            wone = int(row["wone_airdrop_atto"])
            staked = int(row["staked_to_vault_atto"])
            native_total = int(row["native_total_claim_atto"])
            total = int(row["total_claim_atto"])
            wallet_component = int(row["wallet_component_atto"])
            staking_component = int(row["staking_component_atto"])
            planned = int(row["planned_total_entitlement_atto"])
            if min(native_wallet, wone, staked, native_total, total, planned) < 0:
                raise ValueError(f"{path}:{line}: negative component")
            if native_wallet + staked != native_total or native_total + wone != total:
                raise ValueError(f"{path}:{line}: native arithmetic mismatch")
            if wallet_component + staking_component != total:
                raise ValueError(f"{path}:{line}: component split mismatch")
            if planned not in {0, total}:
                raise ValueError(f"{path}:{line}: partial planned entitlement")
            if planned and row["migration_stage"] != "exchange_manual":
                raise ValueError(f"{path}:{line}: planned row is not exchange_manual")
            if planned and row["issuance_treatment"] != "manual_from_reserve":
                raise ValueError(f"{path}:{line}: planned row is not manual")
            rows.append(row)
    return rows


def load_native_components(path, address_to_exchange):
    component_fields = (
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
    )
    records = {}
    totals = defaultdict(lambda: defaultdict(int))
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"address", *component_fields}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError("native claim ledger is missing component fields")
        for line, row in enumerate(reader, start=2):
            address = row["address"].lower()
            exchange_id = address_to_exchange.get(address)
            if exchange_id is None:
                continue
            if address in records:
                raise ValueError(f"{path}:{line}: duplicate native claim address")
            values = {field: int(row[field]) for field in component_fields}
            if min(values.values()) < 0:
                raise ValueError(f"{path}:{line}: negative native component")
            if (
                values["liquid_shard0_atto"] + values["liquid_shard1_atto"]
                != values["liquid_total_atto"]
                or values["liquid_total_atto"]
                + values["pending_undelegation_atto"]
                + values["unclaimed_staking_reward_atto"]
                + values["pending_cross_shard_atto"]
                != values["wallet_airdrop_atto"]
                or values["active_staked_or_delegated_atto"]
                != values["staked_to_vault_atto"]
                or values["wallet_airdrop_atto"] + values["staked_to_vault_atto"]
                != values["total_claim_atto"]
            ):
                raise ValueError(f"{path}:{line}: native component arithmetic mismatch")
            records[address] = values
            for field, value in values.items():
                totals[exchange_id][field] += value
    return records, totals


def cross_check_native(audits, native_records, address_to_exchange, native_totals):
    for exchange_id, rows in audits.items():
        # WONE-only wallets have a migration claim row but no native row.
        expected = {
            row["address_hex"].lower()
            for row in rows
            if row["claim_status"] == "present" and int(row["native_total_claim_atto"]) > 0
        }
        actual = {
            address
            for address in native_records
            if address_to_exchange[address] == exchange_id
        }
        if actual != expected:
            raise ValueError(f"{exchange_id}: native claim address set mismatch")
        for row in rows:
            if row["address_hex"].lower() not in actual and (
                int(row["native_total_claim_atto"]) or int(row["native_wallet_airdrop_atto"])
            ):
                raise ValueError(f"{exchange_id}: {row['address_hex']} has native value without a native row")
        components = native_totals[exchange_id]
        if (
            components["wallet_airdrop_atto"]
            != sum(int(row["native_wallet_airdrop_atto"]) for row in rows)
            or components["staked_to_vault_atto"]
            != sum(int(row["staked_to_vault_atto"]) for row in rows)
            or components["total_claim_atto"]
            != sum(int(row["native_total_claim_atto"]) for row in rows)
        ):
            raise ValueError(f"{exchange_id}: native component totals mismatch")
        for row in rows:
            record = native_records.get(row["address_hex"].lower())
            if record is None:
                continue
            for field in (
                "liquid_shard0_atto",
                "liquid_shard1_atto",
                "pending_cross_shard_atto",
                "pending_undelegation_atto",
                "unclaimed_staking_reward_atto",
            ):
                if record[field] != int(row[field]):
                    raise ValueError(
                        f"{exchange_id}: {row['address_hex']} {field} mismatch"
                    )


def load_delegations(path, wallet_lookup):
    rows = []
    by_wallet = defaultdict(int)
    by_validator = defaultdict(int)
    count_by_validator = defaultdict(int)
    exchanges_by_validator = defaultdict(set)
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "validator_address",
            "validator_secure_key",
            "delegator_address",
            "delegator_secure_key",
            "staked_to_vault_atto",
        }
        if not required <= set(reader.fieldnames or ()):
            raise ValueError("delegation ledger is missing required fields")
        for row in reader:
            delegator = row["delegator_address"].lower()
            wallet = wallet_lookup.get(delegator)
            if wallet is None:
                continue
            amount = int(row["staked_to_vault_atto"])
            validator = row["validator_address"].lower()
            by_wallet[delegator] += amount
            by_validator[validator] += amount
            count_by_validator[validator] += 1
            exchanges_by_validator[validator].add(wallet["exchange_id"])
            rows.append(
                {
                    "exchange_id": wallet["exchange_id"],
                    "validator_address": row["validator_address"],
                    "validator_secure_key": row["validator_secure_key"],
                    "delegator_address": row["delegator_address"],
                    "delegator_secure_key": row["delegator_secure_key"],
                    "staked_released_atto": str(amount),
                    "destination_address": wallet["planned_staking_destination"]
                    or wallet["planned_wallet_destination"],
                }
            )
    for address, wallet in wallet_lookup.items():
        if by_wallet[address] != int(wallet["staked_to_vault_atto"]):
            raise ValueError(f"exchange wallet delegation mismatch: {address}")
    return rows, by_validator, count_by_validator, exchanges_by_validator


def load_vault_adjustments(path, by_validator, count_by_validator, exchanges_by_validator):
    rows = []
    seen = set()
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"validator_address", "validator_secure_key", "vault_assets_atto"}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError("vault ledger is missing required fields")
        for row in reader:
            validator = row["validator_address"].lower()
            withdrawal = by_validator.get(validator, 0)
            if not withdrawal:
                continue
            base = int(row["vault_assets_atto"])
            adjusted = base - withdrawal
            if adjusted < 0:
                raise ValueError(f"exchange withdrawal exceeds vault: {validator}")
            seen.add(validator)
            rows.append(
                {
                    "validator_address": row["validator_address"],
                    "validator_secure_key": row["validator_secure_key"],
                    "base_vault_assets_atto": str(base),
                    "exchange_withdrawal_atto": str(withdrawal),
                    "adjusted_vault_assets_atto": str(adjusted),
                    "delegation_rows_removed": str(count_by_validator[validator]),
                    "exchanges": ";".join(sorted(exchanges_by_validator[validator])),
                }
            )
    if seen != set(by_validator):
        raise ValueError("exchange delegation references an unknown vault")
    return rows


def build_deliveries(exchange_id, rows):
    """Aggregate planned wallets into per-destination manual transfers."""
    groups = {}
    wallet_rows = []
    for row in sorted(rows, key=lambda item: item["address_hex"].lower()):
        planned = int(row["planned_total_entitlement_atto"])
        if planned == 0:
            continue
        native_wallet = int(row["native_wallet_airdrop_atto"])
        wone = int(row["wone_airdrop_atto"])
        staked = int(row["staked_to_vault_atto"])
        wallet_component = int(row["wallet_component_atto"])
        wallet_destination = row["planned_wallet_destination"]
        staking_destination = row["planned_staking_destination"]
        status = row["planned_delivery_status"]
        tier = row["delivery_tier"]
        wallet_rows.append(
            {
                "exchange_id": exchange_id,
                "source_address": row["address_hex"],
                "address_one": row["address_one"],
                "delivery_tier": tier,
                "delivery_status": status,
                "liquid_shard0_atto": row["liquid_shard0_atto"],
                "liquid_shard1_atto": row["liquid_shard1_atto"],
                "pending_cross_shard_atto": row["pending_cross_shard_atto"],
                "pending_undelegation_atto": row["pending_undelegation_atto"],
                "unclaimed_staking_reward_atto": row["unclaimed_staking_reward_atto"],
                "native_wallet_atto": str(native_wallet),
                "wone_atto": str(wone),
                "wallet_component_atto": str(wallet_component),
                "staked_released_atto": str(staked),
                "total_delivery_atto": str(planned),
                "wallet_destination_address": wallet_destination,
                "staking_destination_address": staking_destination,
            }
        )
        # Wallet components go to the wallet destination; staking components
        # (active principal, pending undelegation, unclaimed reward) go to the
        # staking destination when one is configured.
        liquid_native = (
            int(row["liquid_shard0_atto"])
            + int(row["liquid_shard1_atto"])
            + int(row["pending_cross_shard_atto"])
        )
        staking_native = int(row["pending_undelegation_atto"]) + int(
            row["unclaimed_staking_reward_atto"]
        )
        if liquid_native + wone != wallet_component or liquid_native + staking_native != native_wallet:
            raise ValueError(f"{exchange_id}: component split mismatch for {row['address_hex']}")
        parts = []
        if staking_destination and staking_destination != wallet_destination:
            parts.append((wallet_destination, liquid_native, wone, 0))
            parts.append((staking_destination, staking_native, 0, staked))
        else:
            parts.append((wallet_destination, native_wallet, wone, staked))
        for destination, native, wone_part, staked_part in parts:
            if native + wone_part + staked_part == 0:
                continue
            if native < 0:
                raise ValueError(f"{exchange_id}: negative native split")
            key = (tier, destination, status)
            group = groups.setdefault(
                key,
                {
                    "exchange_id": exchange_id,
                    "delivery_tier": tier,
                    "destination_address": destination,
                    "destination_one": lib.hex_to_bech32(destination) if destination else "",
                    "delivery_status": status,
                    "source_wallets": set(),
                    "native_wallet_atto": 0,
                    "wone_atto": 0,
                    "staked_released_atto": 0,
                },
            )
            group["source_wallets"].add(row["address_hex"].lower())
            group["native_wallet_atto"] += native
            group["wone_atto"] += wone_part
            group["staked_released_atto"] += staked_part
    delivery_rows = []
    for key in sorted(groups):
        group = groups[key]
        total = group["native_wallet_atto"] + group["wone_atto"] + group["staked_released_atto"]
        delivery_rows.append(
            {
                **{k: v for k, v in group.items() if k != "source_wallets"},
                "source_wallets": str(len(group["source_wallets"])),
                "native_wallet_atto": str(group["native_wallet_atto"]),
                "wone_atto": str(group["wone_atto"]),
                "staked_released_atto": str(group["staked_released_atto"]),
                "total_delivery_atto": str(total),
            }
        )
    return delivery_rows, wallet_rows


def exchange_totals(rows):
    planned_rows = [row for row in rows if int(row["planned_total_entitlement_atto"]) > 0]
    return {
        "inventory_wallets": len(rows),
        "claim_rows": sum(row["claim_status"] == "present" for row in rows),
        "positive_claims": sum(int(row["total_claim_atto"]) > 0 for row in rows),
        "planned_wallets": len(planned_rows),
        "held_wallets": sum(
            row["planned_delivery_status"] != "exchange_manual" for row in planned_rows
        ),
        "native_wallet_atto": sum(int(row["native_wallet_airdrop_atto"]) for row in rows),
        "wone_atto": sum(int(row["wone_airdrop_atto"]) for row in rows),
        "staked_to_vault_atto": sum(int(row["staked_to_vault_atto"]) for row in rows),
        "native_total_claim_atto": sum(int(row["native_total_claim_atto"]) for row in rows),
        "total_claim_atto": sum(int(row["total_claim_atto"]) for row in rows),
        "manual_delivery_atto": sum(
            int(row["planned_total_entitlement_atto"]) for row in rows
        ),
        "destination_mode": rows[0]["destination_mode"] if rows else "",
        "tiers": {
            tier: {
                "wallets": len(group),
                "total_delivery_atto": sum(
                    int(row["planned_total_entitlement_atto"]) for row in group
                ),
            }
            for tier, group in sorted(
                group_by(planned_rows, lambda row: row["delivery_tier"]).items()
            )
        },
    }


def group_by(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def gate_reconciliation(rows, supplemental, reported_total):
    supplemental = set(supplemental)
    inventory = [row for row in rows if row["address_hex"].lower() not in supplemental]
    extra = [row for row in rows if row["address_hex"].lower() in supplemental]
    if len(extra) != len(supplemental):
        raise ValueError("Gate supplemental addresses are not all in the audit")
    inventory_shard0 = sum(int(row["liquid_shard0_atto"]) for row in inventory)
    inventory_shard1 = sum(int(row["liquid_shard1_atto"]) for row in inventory)
    supplemental_total = sum(int(row["native_total_claim_atto"]) for row in extra)
    reconstructed = inventory_shard0 + supplemental_total
    if reconstructed != reported_total:
        raise ValueError(
            f"Gate reported total does not reconcile: {reconstructed} != {reported_total}"
        )
    all_shards = sum(int(row["native_total_claim_atto"]) for row in rows)
    return {
        "submitted_inventory_wallets": len(inventory),
        "supplemental_wallets": len(extra),
        "inventory_shard0_atto": str(inventory_shard0),
        "inventory_shard1_atto": str(inventory_shard1),
        "inventory_shard1_wallets": sum(int(row["liquid_shard1_atto"]) > 0 for row in inventory),
        "supplemental_native_claim_atto": str(supplemental_total),
        "supplemental_addresses": [
            {
                "address": row["address_hex"],
                "native_total_claim_atto": row["native_total_claim_atto"],
                "delivery_tier": row["delivery_tier"],
            }
            for row in sorted(extra, key=lambda row: row["address_hex"].lower())
        ],
        "reported_total_atto": str(reported_total),
        "reported_reconstructed_atto": str(reconstructed),
        "all_shards_native_atto": str(all_shards),
        "all_shards_minus_reported_atto": str(all_shards - reported_total),
    }


def render_report(result):
    exchanges = result["exchanges"]
    table = [
        "| Exchange | Mode | Inventory wallets | Delivered wallets | Native wallet ONE | "
        "WONE | Delegated ONE released | Total manual delivery ONE |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for exchange_id, item in exchanges.items():
        table.append(
            "| "
            + " | ".join(
                (
                    item["display_name"],
                    item["destination_mode"],
                    f"{item['inventory_wallets']:,}",
                    f"{item['planned_wallets']:,}",
                    one(item["native_wallet_atto"], True),
                    one(item["wone_atto"], True),
                    one(item["staked_to_vault_atto"], True),
                    one(item["manual_delivery_atto"], True),
                )
            )
            + " |"
        )
    totals = result["totals"]
    table.append(
        "| **All exchanges** | | "
        f"{totals['inventory_wallets']:,} | {totals['planned_wallets']:,} | "
        f"{one(totals['native_wallet_atto'], True)} | {one(totals['wone_atto'], True)} | "
        f"{one(totals['staked_to_vault_atto'], True)} | "
        f"{one(totals['manual_delivery_atto'], True)} |"
    )
    delivery_table = [
        "| Exchange | Tier | Destination | Wallets | Native ONE | WONE | Released delegation | Total ONE |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in result["deliveries"]:
        if row["delivery_tier"] in {"same_address", "same_address_initial"}:
            continue
        delivery_table.append(
            f"| {DISPLAY_NAMES[row['exchange_id']]} | {TIER_TEXT[row['delivery_tier']]} | "
            f"`{row['destination_address']}` | {int(row['source_wallets']):,} | "
            f"{one(row['native_wallet_atto'], True)} | {one(row['wone_atto'], True)} | "
            f"{one(row['staked_released_atto'], True)} | {one(row['total_delivery_atto'], True)} |"
        )
    same_address = defaultdict(lambda: [0, 0, 0, 0, 0])
    for row in result["deliveries"]:
        if row["delivery_tier"] in {"same_address", "same_address_initial"}:
            group = same_address[row["exchange_id"]]
            group[0] += int(row["source_wallets"])
            group[1] += int(row["native_wallet_atto"])
            group[2] += int(row["wone_atto"])
            group[3] += int(row["staked_released_atto"])
            group[4] += int(row["total_delivery_atto"])
    for exchange_id, (wallets, native, wone, staked, total) in same_address.items():
        delivery_table.append(
            f"| {DISPLAY_NAMES[exchange_id]} | same address | "
            f"each source wallet | {wallets:,} | {one(native, True)} | "
            f"{one(wone, True)} | {one(staked, True)} | {one(total, True)} |"
        )
    vault = result["vault_release"]
    gate = result["gate"]
    digitalx = result["digitalx"]
    return f"""# Exchange manual delivery from the 2050 supply reserve

Every exchange wallet in a confirmed inventory is excluded from the airdrop.
Its full cutoff entitlement (native ONE, WONE, and delegated principal) is
delivered manually, directly from the year 2050 supply reserve, to the
destination(s) the exchange confirmed. All amounts are independently summed
from row-level cutoff accounting; exchange-submitted totals are reconciled but
never trusted.

## Manual delivery by exchange

{chr(10).join(table)}

## Manual transfer worksheet

Same-address deliveries are one transfer per source wallet; the per-wallet
rows are in `exchange-wallet-deliveries.csv`. Aggregate destinations receive
one transfer each.

{chr(10).join(delivery_table)}

## Delegated principal released from validator vaults

Exchange wallets delegated **{one(vault['released_atto'], True)} ONE** across
{vault['delegation_rows']:,} delegation rows on {vault['validators']:,}
validators. That principal is removed from the validator vaults and delivered
manually with the rest of each exchange's entitlement, so it is never issued
as vault shares. Adjusted vault balances are in
`exchange-validator-vault-adjustments.csv`; row-level releases are in
`exchange-delegation-withdrawals.csv`.

| Exchange | Delegation rows | Validators | Released ONE |
|---|---:|---:|---:|
{chr(10).join(
        f"| {DISPLAY_NAMES[exchange_id]} | {item['delegation_rows']:,} | "
        f"{item['validators']:,} | {one(item['released_atto'], True)} |"
        for exchange_id, item in vault['by_exchange'].items()
    )}

## Gate — tiered delivery and reported-total reconciliation

Gate confirmed that wallets meeting the initial distribution criteria
(at least the minimum balance and indexed activity in the six months before
cutoff) receive their entitlement at their own addresses, and that every other
wallet's entitlement is aggregated to
`{gate['aggregate_destination']}`. The two addresses Gate supplied outside its
workbook are part of the inventory and follow the same tiers.

| Gate tier | Wallets | Total ONE |
|---|---:|---:|
{chr(10).join(
        f"| {TIER_TEXT.get(tier, tier)} | {item['wallets']:,} | "
        f"{one(item['total_delivery_atto'], True)} |"
        for tier, item in exchanges['gate']['tiers'].items()
    )}

| Component used by Gate | Native ONE |
|---|---:|
| Submitted workbook inventory — shard 0 ({gate['submitted_inventory_wallets']:,} wallets) | {one(gate['inventory_shard0_atto'], True)} |
| Supplemental addresses ({gate['supplemental_wallets']:,}) — full native claim | {one(gate['supplemental_native_claim_atto'], True)} |
| Gate-reported total | {one(gate['reported_total_atto'], True)} |

This closes exactly. Gate's report excluded
**{one(gate['inventory_shard1_atto'], True)} ONE** held on shard 1 across
{gate['inventory_shard1_wallets']:,} workbook wallets; the complete native
position is **{one(gate['all_shards_native_atto'], True)} ONE**, which is what
the tiers above deliver (plus WONE).

## Binance — split destinations

Binance receives wallet components (liquid balances, pending cross-shard
receipts, and WONE) at `{result['binance']['wallet_destination']}` and staking
components (delegated principal, pending undelegation, and unclaimed rewards)
at `{result['binance']['staking_destination']}`.

| Binance destination | ONE |
|---|---:|
| Wallet destination | {one(result['binance']['wallet_delivery_atto'], True)} |
| Staking destination | {one(result['binance']['staking_delivery_atto'], True)} |
| Total | {one(exchanges['binance']['manual_delivery_atto'], True)} |

## DigitalX — submitted total reconciliation

DigitalX's workbook total of
**{one(digitalx['submitted_combined_total_atto'], True)} ONE** combines its
current wallet balances with {digitalx['rolled_back_deposit_rows']:,} deposit
transactions it reports as invalidated by the network rollback. Only the
wallet component is cutoff chain state; the rolled-back deposits are not held
by any DigitalX wallet at cutoff and are **excluded** from the manual delivery
pending an explicit policy decision.

| DigitalX component | Native ONE |
|---|---:|
| Submitted current wallet balances ({digitalx['submitted_wallet_rows']:,} wallets) | {one(digitalx['submitted_wallet_total_atto'], True)} |
| Independent cutoff native claim (delivered) | {one(digitalx['native_delivery_atto'], True)} |
| Submitted minus cutoff (exchange rounding) | {signed_one(digitalx['submitted_minus_delivery_atto'], True)} |
| Rolled-back deposit claim (excluded) | {one(digitalx['rolled_back_deposit_total_atto'], True)} |
| DigitalX combined total | {one(digitalx['submitted_combined_total_atto'], True)} |

| Rolled-back deposit transaction | Reported ONE |
|---|---:|
{chr(10).join(
        f"| `{item['transaction_hash']}` | {one(item['amount_atto'], True)} |"
        for item in digitalx['rolled_back_deposits']
    )}
"""


def main():
    args = parse_args()
    policy = load_policy(args.policy)
    audits_dir = Path(args.audits_dir)
    output_dir = Path(args.output_dir)
    gate_supplemental = load_address_list(args.gate_supplemental)
    gate_reported_total = load_reported_total(args.gate_reported_total)
    digitalx_submission = load_digitalx_submission(args.normalization_summary)

    exchange_ids = [config["id"] for config in policy["exchanges"]]
    audits = {}
    address_to_exchange = {}
    wallet_lookup = {}
    for exchange_id in exchange_ids:
        rows = load_audit(audits_dir / f"{exchange_id}.csv", exchange_id)
        audits[exchange_id] = rows
        for row in rows:
            address = row["address_hex"].lower()
            if address in address_to_exchange:
                raise ValueError(f"cross-exchange audit overlap: {address}")
            address_to_exchange[address] = exchange_id
            wallet_lookup[address] = row
    native_records, native_totals = load_native_components(
        args.native_claims, address_to_exchange
    )
    cross_check_native(audits, native_records, address_to_exchange, native_totals)

    delegation_rows, by_validator, count_by_validator, exchanges_by_validator = (
        load_delegations(args.delegations, wallet_lookup)
    )
    vault_rows = load_vault_adjustments(
        args.vaults, by_validator, count_by_validator, exchanges_by_validator
    )

    delivery_rows = []
    wallet_rows = []
    exchange_result = {}
    for exchange_id in exchange_ids:
        rows = audits[exchange_id]
        deliveries, wallets = build_deliveries(exchange_id, rows)
        delivery_rows.extend(deliveries)
        wallet_rows.extend(wallets)
        totals = exchange_totals(rows)
        if sum(int(row["total_delivery_atto"]) for row in deliveries) != totals[
            "manual_delivery_atto"
        ]:
            raise ValueError(f"{exchange_id}: delivery worksheet does not close")
        exchange_result[exchange_id] = {
            "display_name": DISPLAY_NAMES[exchange_id],
            **{
                key: (str(value) if key.endswith("_atto") else value)
                for key, value in totals.items()
            },
            "tiers": {
                tier: {
                    "wallets": item["wallets"],
                    "total_delivery_atto": str(item["total_delivery_atto"]),
                }
                for tier, item in totals["tiers"].items()
            },
            "native_components": {
                key: str(value) for key, value in native_totals[exchange_id].items()
            },
            "destinations": sorted(
                {row["destination_address"] for row in deliveries}
                - {row["source_address"] for row in wallets}
            ),
        }

    released_by_exchange = defaultdict(lambda: {"rows": 0, "validators": set(), "atto": 0})
    for row in delegation_rows:
        item = released_by_exchange[row["exchange_id"]]
        item["rows"] += 1
        item["validators"].add(row["validator_address"].lower())
        item["atto"] += int(row["staked_released_atto"])
    released_total = sum(int(row["staked_released_atto"]) for row in delegation_rows)
    if released_total != sum(
        int(item["staked_to_vault_atto"]) for item in exchange_result.values()
    ):
        raise ValueError("released delegation does not equal exchange staked total")
    vault_release = {
        "released_atto": str(released_total),
        "delegation_rows": len(delegation_rows),
        "validators": len(by_validator),
        "by_exchange": {
            exchange_id: {
                "delegation_rows": item["rows"],
                "validators": len(item["validators"]),
                "released_atto": str(item["atto"]),
            }
            for exchange_id, item in sorted(released_by_exchange.items())
        },
    }

    gate_config = next(config for config in policy["exchanges"] if config["id"] == "gate")
    gate_aggregate = {
        row["destination_address"]
        for row in delivery_rows
        if row["exchange_id"] == "gate" and row["delivery_tier"] == "aggregated_non_initial"
    }
    if len(gate_aggregate) > 1:
        raise ValueError("Gate aggregate tier has multiple destinations")
    gate_result = {
        **gate_reconciliation(audits["gate"], gate_supplemental, gate_reported_total),
        "aggregate_destination": next(iter(gate_aggregate), ""),
        "destination_mode": gate_config["destination_mode"],
    }

    binance_deliveries = [row for row in delivery_rows if row["exchange_id"] == "binance"]
    binance_wallet_destination = {
        row["planned_wallet_destination"] for row in audits["binance"]
    } - {""}
    binance_staking_destination = {
        row["planned_staking_destination"] for row in audits["binance"]
    } - {""}
    if len(binance_wallet_destination) != 1 or len(binance_staking_destination) != 1:
        raise ValueError("Binance destinations are not uniquely configured")
    binance_wallet_destination = next(iter(binance_wallet_destination))
    binance_staking_destination = next(iter(binance_staking_destination))
    binance_result = {
        "wallet_destination": binance_wallet_destination,
        "staking_destination": binance_staking_destination,
        "wallet_delivery_atto": str(
            sum(
                int(row["total_delivery_atto"])
                for row in binance_deliveries
                if row["destination_address"] == binance_wallet_destination
            )
        ),
        "staking_delivery_atto": str(
            sum(
                int(row["total_delivery_atto"])
                for row in binance_deliveries
                if row["destination_address"] == binance_staking_destination
            )
        ),
    }

    digitalx_native = int(exchange_result["digitalx"]["native_total_claim_atto"])
    if (
        exchange_result["digitalx"]["inventory_wallets"]
        != digitalx_submission["submitted_wallet_rows"]
    ):
        raise ValueError("DigitalX submitted wallet rows do not match audit")
    digitalx_result = {
        **{
            key: str(value) if key.endswith("_atto") else value
            for key, value in digitalx_submission.items()
        },
        "native_delivery_atto": str(digitalx_native),
        "submitted_minus_delivery_atto": str(
            digitalx_submission["submitted_wallet_total_atto"] - digitalx_native
        ),
        "rolled_back_treatment": (
            "excluded from manual delivery; not cutoff wallet state; "
            "requires explicit policy decision"
        ),
    }

    outputs = {
        "deliveries": output_dir / "exchange-manual-deliveries.csv",
        "wallets": output_dir / "exchange-wallet-deliveries.csv",
        "delegations": output_dir / "exchange-delegation-withdrawals.csv",
        "vaults": output_dir / "exchange-validator-vault-adjustments.csv",
    }
    atomic_csv(outputs["deliveries"], DELIVERY_FIELDS, delivery_rows, args.replace)
    atomic_csv(outputs["wallets"], WALLET_FIELDS, wallet_rows, args.replace)
    atomic_csv(outputs["delegations"], DELEGATION_FIELDS, delegation_rows, args.replace)
    atomic_csv(outputs["vaults"], VAULT_FIELDS, vault_rows, args.replace)

    totals = {
        key: (
            str(sum(int(item[key]) for item in exchange_result.values()))
            if key.endswith("_atto")
            else sum(item[key] for item in exchange_result.values())
        )
        for key in (
            "inventory_wallets",
            "planned_wallets",
            "held_wallets",
            "native_wallet_atto",
            "wone_atto",
            "staked_to_vault_atto",
            "total_claim_atto",
            "manual_delivery_atto",
        )
    }
    result = {
        "schema_version": 2,
        "status": "passed",
        "policy": (
            "all exchange wallets excluded from the airdrop; full entitlement "
            "delivered manually from the year 2050 supply reserve; delegated "
            "principal released from validator vaults"
        ),
        "delivery_source": DELIVERY_SOURCE,
        "exchanges": exchange_result,
        "totals": totals,
        "deliveries": delivery_rows,
        "vault_release": vault_release,
        "gate": gate_result,
        "binance": binance_result,
        "digitalx": digitalx_result,
        "inputs": {
            "policy": {"path": args.policy, "sha256": file_sha256(args.policy)},
            "audits": {
                exchange_id: {
                    "path": str(audits_dir / f"{exchange_id}.csv"),
                    "sha256": file_sha256(audits_dir / f"{exchange_id}.csv"),
                }
                for exchange_id in exchange_ids
            },
            "delegations": {
                "path": args.delegations,
                "sha256": file_sha256(args.delegations),
            },
            "gate_supplemental": {
                "path": args.gate_supplemental,
                "sha256": file_sha256(args.gate_supplemental),
            },
            "gate_reported_total": {
                "path": args.gate_reported_total,
                "sha256": file_sha256(args.gate_reported_total),
            },
            "native_claims": {
                "path": args.native_claims,
                "sha256": file_sha256(args.native_claims),
            },
            "normalization_summary": {
                "path": args.normalization_summary,
                "sha256": file_sha256(args.normalization_summary),
            },
            "vaults": {"path": args.vaults, "sha256": file_sha256(args.vaults)},
        },
        "outputs": {
            label: {"path": str(path), "sha256": file_sha256(path)}
            for label, path in outputs.items()
        },
    }
    atomic_json(args.summary, result, args.replace)
    atomic_text(args.report, render_report(result), args.replace)
    print(json.dumps({key: value for key, value in result.items() if key != "deliveries"}, sort_keys=True))


if __name__ == "__main__":
    main()
