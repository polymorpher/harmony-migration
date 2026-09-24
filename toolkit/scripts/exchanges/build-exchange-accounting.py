#!/usr/bin/env python3

"""Build exchange routing inputs, address audits, and private memo reports."""

import argparse
import calendar
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


ATTO_PER_ONE = 10**18
NORMALIZED_FIELDS = (
    "exchange_id",
    "source_file",
    "source_sha256",
    "source_sheet",
    "source_row",
    "source_row_id",
    "source_address_raw",
    "address_hex",
    "address_one",
    "submitted_balance_raw",
    "submitted_balance_unit",
    "submitted_balance_atto",
    "configured_destination",
    "configured_staking_destination",
    "configured_destination_status",
    "authorization_type",
    "authorization_destination",
    "authorization_message_sha256",
    "authorization_signature_sha256",
    "authorization_status",
)
AUDIT_FIELDS = (
    "exchange_id",
    "address_hex",
    "address_one",
    "source_row",
    "claim_status",
    "liquid_shard0_atto",
    "liquid_shard1_atto",
    "pending_cross_shard_atto",
    "pending_undelegation_atto",
    "unclaimed_staking_reward_atto",
    "native_wallet_airdrop_atto",
    "wone_balance_atto",
    "wone_airdrop_atto",
    "wallet_airdrop_atto",
    "staked_to_vault_atto",
    "native_total_claim_atto",
    "qualification_total_atto",
    "total_claim_atto",
    "wallet_component_atto",
    "staking_component_atto",
    "qualification_status",
    "policy_category",
    "migration_stage",
    "issuance_treatment",
    "delivery_policy",
    "destination_mode",
    "delivery_tier",
    "configured_destination",
    "configured_staking_destination",
    "configured_destination_status",
    "planned_wallet_destination",
    "planned_staking_destination",
    "planned_delivery_status",
    "planned_wallet_airdrop_atto",
    "planned_staked_to_vault_atto",
    "planned_total_entitlement_atto",
    "remaining_not_airdropped_atto",
    "last_activity_time_utc",
    "last_activity_block",
    "last_activity_shard",
    "last_activity_type",
    "activity_status",
    "activity_age_bucket",
    "submitted_balance_scope",
    "submitted_balance_atto",
    "submitted_balance_reconciliation",
    "submitted_balance_delta_atto",
    "authorization_type",
    "authorization_status",
)
ROUTE_FIELDS = (
    "route_id",
    "priority",
    "source_address",
    "destination_id",
    "destination_address",
    "status",
    "amount_atto",
    "allocation_method",
    "reason",
    "evidence",
    "notes",
)
DESTINATION_FIELDS = (
    "destination_id",
    "destination_address",
    "status",
    "notes",
)
EXCLUSION_FIELDS = (
    "address",
    "exchange_id",
    "reason",
    "evidence",
)
TIER_LIST_FIELDS = (
    "address_hex",
    "address_one",
    "delivery_tier",
    "planned_wallet_destination",
    "qualification_total_atto",
    "total_claim_atto",
    "wallet_component_atto",
    "staking_component_atto",
    "planned_total_entitlement_atto",
    "qualification_status",
    "migration_stage",
    "issuance_treatment",
    "planned_delivery_status",
    "last_activity_time_utc",
    "activity_status",
)
COMPONENTS = (
    "liquid_shard0",
    "liquid_shard1",
    "pending_cross_shard",
    "pending_undelegation",
    "unclaimed_staking_reward",
    "native_wallet_airdrop",
    "wone_balance",
    "wone_airdrop",
    "wallet_airdrop",
    "staked_to_vault",
    "native_total_claim",
    "qualification_total",
    "total_claim",
    "wallet_component",
    "staking_component",
    "planned_wallet_airdrop",
    "planned_staked_to_vault",
    "planned_total_entitlement",
    "remaining_not_airdropped",
)
EXCHANGE_ROUTE_REASON = "exchange_manual_reserve_delivery"
EXCHANGE_STAGE = "exchange_manual"
EXCHANGE_TREATMENT = "manual_from_reserve"
EXCHANGE_STATUS = "exchange_manual"
ACTIVITY_FIELDS = (
    "last_activity_time_utc",
    "last_activity_block",
    "last_activity_shard",
    "last_activity_type",
)
ACTIVITY_BUCKET_ORDER = (
    "within_3_months",
    "3_to_6_months",
    "6_to_12_months",
    "12_to_24_months",
    "24_to_36_months",
    "36_to_48_months",
    "older_than_48_months",
    "not_found",
    "not_collected",
)
BALANCE_BUCKET_ORDER = (
    "zero_or_no_cutoff_value",
    "over_0_under_1",
    "at_least_1_under_10",
    "at_least_10_under_100",
    "at_least_100_under_1000",
    "at_least_1000",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--normalized-dir", required=True)
    parser.add_argument("--normalization-summary", required=True)
    parser.add_argument("--claims", required=True)
    parser.add_argument("--wone-holders", required=True)
    parser.add_argument("--activity", required=True)
    parser.add_argument("--automatic-claims")
    parser.add_argument("--contract-claims")
    parser.add_argument("--excluded-claims")
    parser.add_argument("--migration-stages")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--routes-output", required=True)
    parser.add_argument("--destinations-output", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_csv(path, fields, rows, replace):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    with partial.open("x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=fields,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def atomic_json(path, value, replace):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    with partial.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def atomic_text(path, value, replace):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    with partial.open("x", encoding="utf-8", newline="") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def normalize_address(value, context):
    address = lib.any_to_hex(str(value or "").strip())
    if address is None:
        raise ValueError(f"{context}: invalid address")
    try:
        raw = bytes.fromhex(address[2:])
    except ValueError as error:
        raise ValueError(f"{context}: invalid address") from error
    if len(raw) != 20:
        raise ValueError(f"{context}: invalid address")
    return address


def load_policy(path):
    with open(path, encoding="utf-8") as source:
        policy = json.load(source)
    if policy.get("schema_version") != 2:
        raise ValueError("unsupported exchange policy schema")
    if policy.get("delivery_source") != "year_2050_supply_reserve":
        raise ValueError("exchange policy must name the 2050 supply reserve")
    threshold = int(policy.get("minimum_atto", 0))
    if threshold <= 0:
        raise ValueError("invalid exchange threshold")
    for config in policy["exchanges"]:
        if config.get("delivery_policy") != "manual_from_reserve":
            raise ValueError(f"{config['id']}: unsupported delivery policy")
        if config.get("destination_mode") not in {
            "aggregate",
            "aggregate_split",
            "same_address",
            "tiered",
        }:
            raise ValueError(f"{config['id']}: unsupported destination mode")
    if policy.get("wallet_destination_components") != [
        "liquid_shard0",
        "liquid_shard1",
        "pending_cross_shard",
        "wone",
    ] or policy.get("staking_destination_components") != [
        "active_staked_or_delegated",
        "pending_undelegation",
        "unclaimed_staking_reward",
    ]:
        raise ValueError("exchange policy component split is not the reviewed one")
    cutoff = parse_utc(policy["cutoff_time_utc"])
    return policy, threshold, cutoff


def load_normalized(policy, directory, summary_path):
    with open(summary_path, encoding="utf-8") as source:
        normalization = json.load(source)
    if normalization.get("schema_version") != 2:
        raise ValueError("unsupported normalization summary schema")
    result = {}
    all_addresses = set()
    for config in policy["exchanges"]:
        exchange_id = config["id"]
        path = Path(directory) / f"{exchange_id}.csv"
        expected = normalization["exchanges"][exchange_id]
        if file_sha256(path) != expected["output_sha256"]:
            raise ValueError(f"{exchange_id}: normalized file hash mismatch")
        rows = []
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != NORMALIZED_FIELDS:
                raise ValueError(
                    f"{path}: unexpected normalized fields {reader.fieldnames}"
                )
            previous = None
            for line, row in enumerate(reader, start=2):
                if row["exchange_id"] != exchange_id:
                    raise ValueError(f"{path}:{line}: exchange id mismatch")
                address = normalize_address(
                    row["address_hex"], f"{path}:{line}"
                )
                raw = bytes.fromhex(address[2:])
                if previous is not None and raw <= previous:
                    raise ValueError(
                        f"{path}:{line}: addresses are not increasing"
                    )
                previous = raw
                if address in all_addresses:
                    raise ValueError(f"{path}:{line}: exchange overlap")
                all_addresses.add(address)
                row["address_normalized"] = address
                rows.append(row)
        if len(rows) != expected["normalized_rows"]:
            raise ValueError(f"{exchange_id}: normalized row count mismatch")
        result[exchange_id] = {
            "config": config,
            "normalization": expected,
            "path": path,
            "rows": rows,
        }
    return result, normalization, all_addresses


def load_claims(path, wanted):
    claims = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "liquid_shard0_atto",
            "liquid_shard1_atto",
            "pending_cross_shard_atto",
            "pending_undelegation_atto",
            "unclaimed_staking_reward_atto",
            "active_staked_or_delegated_atto",
            "native_wallet_airdrop_atto",
            "wone_balance_atto",
            "wone_airdrop_atto",
            "wallet_airdrop_atto",
            "staked_to_vault_atto",
            "native_total_claim_atto",
            "qualification_total_atto",
            "total_claim_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing claim fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            address = normalize_address(row["address"], f"{path}:{line}")
            if address not in wanted:
                continue
            if address in claims:
                raise ValueError(f"{path}:{line}: duplicate claim address")
            native_total = int(row["native_total_claim_atto"])
            wone = int(row["wone_balance_atto"])
            qualification = int(row["qualification_total_atto"])
            wallet = int(row["wallet_airdrop_atto"])
            staked = int(row["staked_to_vault_atto"])
            total = int(row["total_claim_atto"])
            native_wallet = (
                int(row["liquid_shard0_atto"])
                + int(row["liquid_shard1_atto"])
                + int(row["pending_cross_shard_atto"])
                + int(row["pending_undelegation_atto"])
                + int(row["unclaimed_staking_reward_atto"])
            )
            if (
                min(native_total, wone, qualification, wallet, staked, total)
                < 0
                or qualification != native_total + wone
                or total != wallet + staked
                or native_wallet != int(row["native_wallet_airdrop_atto"])
                or int(row["active_staked_or_delegated_atto"]) != staked
                or wallet != native_wallet + int(row["wone_airdrop_atto"])
            ):
                raise ValueError(f"{path}:{line}: claim arithmetic mismatch")
            claims[address] = row
    return claims


def load_wone(path, wanted):
    holders = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"address", "wone_balance_atto"}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError(f"{path}: missing WONE fields")
        for line, row in enumerate(reader, start=2):
            address = normalize_address(row["address"], f"{path}:{line}")
            if address not in wanted:
                continue
            if address in holders:
                raise ValueError(f"{path}:{line}: duplicate WONE holder")
            amount = int(row["wone_balance_atto"])
            if amount <= 0:
                raise ValueError(f"{path}:{line}: non-positive WONE balance")
            holders[address] = amount
    return holders


def load_activity(path, wanted):
    activity = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"address", *ACTIVITY_FIELDS}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError(f"{path}: missing activity fields")
        for line, row in enumerate(reader, start=2):
            address = normalize_address(row["address"], f"{path}:{line}")
            if address not in wanted:
                continue
            if address in activity:
                raise ValueError(f"{path}:{line}: duplicate activity row")
            activity[address] = {field: row[field] for field in ACTIVITY_FIELDS}
    return activity


def load_categories(paths, wanted):
    categories = {}
    for path, category in paths:
        with open(path, newline="") as source:
            reader = csv.DictReader(source)
            if "address" not in set(reader.fieldnames or ()):
                raise ValueError(f"{path}: missing address field")
            for line, row in enumerate(reader, start=2):
                address = normalize_address(row["address"], f"{path}:{line}")
                if address not in wanted:
                    continue
                if address in categories:
                    raise ValueError(f"{path}:{line}: category overlap")
                categories[address] = category
    return categories


def load_migration_stages(path, wanted):
    stages = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "migration_stage",
            "issuance_treatment",
            "migration_allocation_atto",
        }
        if not required <= set(reader.fieldnames or ()):
            raise ValueError(f"{path}: missing migration-stage fields")
        for line, row in enumerate(reader, start=2):
            address = normalize_address(row["address"], f"{path}:{line}")
            if address not in wanted:
                continue
            if address in stages:
                raise ValueError(f"{path}:{line}: duplicate migration stage")
            stage = row["migration_stage"]
            if stage not in {"", "initial", "next_stage", "deferred"}:
                raise ValueError(f"{path}:{line}: invalid migration stage")
            treatment = row["issuance_treatment"]
            if treatment not in {"issue", "not_issued"}:
                raise ValueError(f"{path}:{line}: invalid issuance treatment")
            allocation = int(row["migration_allocation_atto"])
            if allocation < 0:
                raise ValueError(f"{path}:{line}: negative stage allocation")
            if treatment == "issue" and (not stage or allocation <= 0):
                raise ValueError(f"{path}:{line}: invalid issued allocation")
            if treatment == "not_issued" and (stage or allocation):
                raise ValueError(f"{path}:{line}: invalid not-issued allocation")
            stages[address] = {
                "stage": stage,
                "treatment": treatment,
                "allocation": allocation,
            }
    return stages


def parse_utc(value):
    if not value.endswith("Z"):
        raise ValueError(f"UTC timestamp must end in Z: {value}")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"timestamp is not UTC: {value}")
    return parsed


def subtract_months(value, months):
    absolute_month = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def activity_bucket(activity_time, cutoff):
    if not activity_time:
        return ""
    timestamp = parse_utc(activity_time)
    if timestamp > cutoff:
        raise ValueError("activity after cutoff")
    previous = 0
    for months in (3, 6, 12, 24, 36, 48):
        if timestamp >= subtract_months(cutoff, months):
            return (
                f"within_{months}_months"
                if previous == 0
                else f"{previous}_to_{months}_months"
            )
        previous = months
    return "older_than_48_months"


def claim_values(claim, wone):
    values = {component: 0 for component in COMPONENTS}
    if claim is not None:
        for component in (
            "liquid_shard0",
            "liquid_shard1",
            "pending_cross_shard",
            "pending_undelegation",
            "unclaimed_staking_reward",
            "native_wallet_airdrop",
            "wone_airdrop",
            "wallet_airdrop",
            "staked_to_vault",
            "native_total_claim",
            "qualification_total",
            "total_claim",
        ):
            values[component] = int(claim[f"{component}_atto"])
        if int(claim["wone_balance_atto"]) != wone:
            raise ValueError(
                f"WONE census mismatch for {claim['address']}"
            )
    else:
        values["qualification_total"] = wone
    values["wone_balance"] = wone
    if values["qualification_total"] != (
        values["native_total_claim"] + values["wone_balance"]
    ):
        raise ValueError("qualification total does not close")
    # Reviewed destination split: liquid balances, supported cross-shard
    # receipts, and WONE go to the wallet destination; delegated principal,
    # pending undelegation, and unclaimed rewards go to the staking destination.
    values["wallet_component"] = (
        values["liquid_shard0"]
        + values["liquid_shard1"]
        + values["pending_cross_shard"]
        + values["wone_airdrop"]
    )
    values["staking_component"] = (
        values["staked_to_vault"]
        + values["pending_undelegation"]
        + values["unclaimed_staking_reward"]
    )
    if (
        values["wallet_component"] + values["staking_component"]
        != values["total_claim"]
    ):
        raise ValueError("destination components do not close to total claim")
    return values


def submitted_reconciliation(normalized, claim, scope):
    submitted = normalized["submitted_balance_atto"]
    if not submitted:
        return "", "not_provided", ""
    submitted_value = int(submitted)
    if scope == "liquid_shard0":
        cutoff_value = int(claim["liquid_shard0_atto"]) if claim else 0
    elif scope == "liquid_total":
        cutoff_value = (
            int(claim["liquid_shard0_atto"])
            + int(claim["liquid_shard1_atto"])
            if claim
            else 0
        )
    else:
        raise ValueError(f"unsupported submitted balance scope: {scope}")
    delta = cutoff_value - submitted_value
    if claim is None:
        status = "claim_not_found"
    elif delta == 0:
        status = f"exact_{scope}_match"
    elif delta > 0:
        status = f"cutoff_{scope}_higher"
    else:
        status = "submitted_balance_higher"
    return str(submitted_value), status, str(delta)


def build_audit_row(
    exchange,
    normalized,
    claim,
    wone,
    activity,
    category,
    stage_record,
    threshold,
    cutoff,
    categories_complete=True,
    stages_complete=False,
):
    config = exchange["config"]
    values = claim_values(claim, wone)
    qualifies = values["qualification_total"] >= threshold
    if qualifies and category is None and categories_complete:
        raise ValueError(
            f"qualified exchange address missing policy category: "
            f"{normalized['address_hex']}"
        )
    if not qualifies and category is not None:
        raise ValueError(
            f"below-threshold exchange address has policy category: "
            f"{normalized['address_hex']}"
        )
    if qualifies and stages_complete and stage_record is None:
        raise ValueError(
            f"qualified exchange address missing migration stage: "
            f"{normalized['address_hex']}"
        )
    # Every exchange wallet is delivered manually from the 2050 supply reserve
    # and excluded from the airdrop. The stage policy still records whether a
    # wallet met the initial-distribution criteria, which decides Gate's tier.
    ordinary_stage = stage_record["stage"] if stage_record is not None else ""
    if stage_record is not None and stage_record["treatment"] == "not_issued":
        issuance_treatment = "not_issued"
        target_allocation = stage_record["allocation"]
    else:
        issuance_treatment = EXCHANGE_TREATMENT
        target_allocation = values["total_claim"]
    migration_stage = EXCHANGE_STAGE if target_allocation > 0 else ""
    deduction = values["total_claim"] - target_allocation
    if deduction < 0:
        raise ValueError("stage allocation exceeds exchange claim")
    target_wallet = max(values["wallet_airdrop"] - deduction, 0)
    target_staked = target_allocation - target_wallet
    if target_staked < 0 or target_staked > values["staked_to_vault"]:
        raise ValueError("stage allocation does not split across components")
    activity_record = activity or {field: "" for field in ACTIVITY_FIELDS}
    if activity_record["last_activity_time_utc"]:
        activity_status = "found"
    elif activity is not None:
        activity_status = "not_found"
    else:
        activity_status = "not_collected"
    if qualifies and activity is None:
        raise ValueError(
            f"qualified exchange address missing activity row: "
            f"{normalized['address_hex']}"
        )
    delivery_policy = config["delivery_policy"]
    mode = config["destination_mode"]
    destination_status = normalized["configured_destination_status"]
    own_address = normalized["address_hex"]
    wallet_destination = ""
    staking_destination = ""
    if mode == "same_address":
        tier = "same_address"
        wallet_destination = staking_destination = own_address
    elif mode == "aggregate":
        tier = "aggregate"
        wallet_destination = staking_destination = normalized[
            "configured_destination"
        ]
    elif mode == "aggregate_split":
        tier = "aggregate_split"
        wallet_destination = normalized["configured_destination"]
        staking_destination = normalized["configured_staking_destination"]
    else:  # tiered: initial-criteria wallets keep their own address
        if not stages_complete:
            tier = "tier_pending"
        elif ordinary_stage == "initial" and issuance_treatment != "not_issued":
            tier = "same_address_initial"
            wallet_destination = staking_destination = own_address
        else:
            tier = "aggregated_non_initial"
            wallet_destination = staking_destination = normalized[
                "configured_destination"
            ]
    planned_wallet = 0
    planned_staked = 0
    if issuance_treatment == "not_issued":
        planned_status = "not_issued"
        wallet_destination = staking_destination = ""
    elif target_allocation == 0:
        planned_status = (
            "wone_not_in_migration_claim" if wone else "no_cutoff_claim"
        )
        wallet_destination = staking_destination = ""
    else:
        planned_wallet = target_wallet
        planned_staked = target_staked
        if tier == "tier_pending":
            planned_status = "tier_pending"
        elif not wallet_destination or (
            values["staking_component"] > 0 and not staking_destination
        ):
            planned_status = "manual_destination_hold"
        else:
            planned_status = EXCHANGE_STATUS
    planned_total = planned_wallet + planned_staked
    if planned_total != (
        target_allocation
        if planned_status
        in {EXCHANGE_STATUS, "manual_destination_hold", "tier_pending"}
        else 0
    ):
        raise ValueError("planned exchange delivery components do not close")
    remaining = values["qualification_total"] - planned_total
    if remaining < 0:
        raise ValueError("planned exchange delivery exceeds qualification value")
    values["planned_wallet_airdrop"] = planned_wallet
    values["planned_staked_to_vault"] = planned_staked
    values["planned_total_entitlement"] = planned_total
    values["remaining_not_airdropped"] = remaining
    submitted_scope = config.get(
        "submitted_balance_scope",
        "liquid_shard0",
    )
    submitted, reconciliation, delta = submitted_reconciliation(
        normalized,
        claim,
        submitted_scope,
    )
    row = {
        "exchange_id": config["id"],
        "address_hex": normalized["address_hex"],
        "address_one": normalized["address_one"],
        "source_row": normalized["source_row"],
        "claim_status": "present" if claim is not None else "not_found",
        "qualification_status": (
            "qualified" if qualifies else "below_threshold"
        ),
        "policy_category": category or "",
        "migration_stage": migration_stage,
        "issuance_treatment": issuance_treatment,
        "delivery_policy": delivery_policy,
        "destination_mode": mode,
        "delivery_tier": tier,
        "configured_destination": normalized["configured_destination"],
        "configured_staking_destination": normalized[
            "configured_staking_destination"
        ],
        "configured_destination_status": destination_status,
        "planned_wallet_destination": wallet_destination,
        "planned_staking_destination": staking_destination,
        "planned_delivery_status": planned_status,
        "last_activity_time_utc": activity_record["last_activity_time_utc"],
        "last_activity_block": activity_record["last_activity_block"],
        "last_activity_shard": activity_record["last_activity_shard"],
        "last_activity_type": activity_record["last_activity_type"],
        "activity_status": activity_status,
        "activity_age_bucket": (
            activity_bucket(
                activity_record["last_activity_time_utc"], cutoff
            )
            if activity_status == "found"
            else activity_status
        ),
        "submitted_balance_scope": (
            submitted_scope if normalized["submitted_balance_atto"] else ""
        ),
        "submitted_balance_atto": submitted,
        "submitted_balance_reconciliation": reconciliation,
        "submitted_balance_delta_atto": delta,
        "authorization_type": normalized["authorization_type"],
        "authorization_status": normalized["authorization_status"],
    }
    for component in COMPONENTS:
        row[f"{component}_atto"] = str(values[component])
    return row


def empty_component_totals():
    return {component: 0 for component in COMPONENTS}


def add_components(totals, row):
    for component in COMPONENTS:
        totals[component] += int(row[f"{component}_atto"])


def serializable_components(totals):
    result = {}
    for component in COMPONENTS:
        result[f"{component}_atto"] = str(totals[component])
        result[f"{component}_one"] = lib.atto_to_one_str(totals[component])
    return result


def balance_bucket(value):
    if value == 0:
        return "zero_or_no_cutoff_value"
    for upper, label in (
        (1 * ATTO_PER_ONE, "over_0_under_1"),
        (10 * ATTO_PER_ONE, "at_least_1_under_10"),
        (100 * ATTO_PER_ONE, "at_least_10_under_100"),
        (1000 * ATTO_PER_ONE, "at_least_100_under_1000"),
    ):
        if value < upper:
            return label
    return "at_least_1000"


def amount_statistics(values):
    values = sorted(int(value) for value in values)
    if not values:
        return {
            "rows": 0,
            "positive_rows": 0,
            "zero_rows": 0,
            "total_atto": "0",
            "total_one": lib.atto_to_one_str(0),
            "minimum_atto": None,
            "minimum_one": None,
            "minimum_positive_atto": None,
            "minimum_positive_one": None,
            "mean_atto_rounded": None,
            "mean_one": None,
            "median_atto_rounded": None,
            "median_one": None,
            "maximum_atto": None,
            "maximum_one": None,
        }
    if values[0] < 0:
        raise ValueError("negative balance in exchange statistics")
    total = sum(values)
    positive = [value for value in values if value > 0]
    mean = (total + len(values) // 2) // len(values)
    middle = len(values) // 2
    median_numerator = (
        values[middle] * 2
        if len(values) % 2
        else values[middle - 1] + values[middle]
    )
    median = (median_numerator + 1) // 2
    minimum_positive = positive[0] if positive else None
    return {
        "rows": len(values),
        "positive_rows": len(positive),
        "zero_rows": len(values) - len(positive),
        "total_atto": str(total),
        "total_one": lib.atto_to_one_str(total),
        "minimum_atto": str(values[0]),
        "minimum_one": lib.atto_to_one_str(values[0]),
        "minimum_positive_atto": (
            str(minimum_positive) if minimum_positive is not None else None
        ),
        "minimum_positive_one": (
            lib.atto_to_one_str(minimum_positive)
            if minimum_positive is not None
            else None
        ),
        "mean_atto_rounded": str(mean),
        "mean_one": lib.atto_to_one_str(mean),
        "median_atto_rounded": str(median),
        "median_one": lib.atto_to_one_str(median),
        "maximum_atto": str(values[-1]),
        "maximum_one": lib.atto_to_one_str(values[-1]),
    }


def amount_bucket_stats(rows, field, skip_blank=False):
    groups = {}
    for row in rows:
        raw = row[field]
        if skip_blank and raw == "":
            continue
        value = int(raw or 0)
        label = balance_bucket(value)
        group = groups.setdefault(label, {"rows": 0, "total": 0})
        group["rows"] += 1
        group["total"] += value
    return {
        label: {
            "rows": values["rows"],
            "total_atto": str(values["total"]),
            "total_one": lib.atto_to_one_str(values["total"]),
        }
        for label, values in sorted(
            groups.items(),
            key=lambda item: BALANCE_BUCKET_ORDER.index(item[0]),
        )
    }


def grouped_stats(rows, field):
    groups = {}
    for row in rows:
        label = row[field] or "none"
        group = groups.setdefault(
            label,
            {"rows": 0, "components": empty_component_totals()},
        )
        group["rows"] += 1
        add_components(group["components"], row)
    return {
        label: {
            "rows": values["rows"],
            **serializable_components(values["components"]),
        }
        for label, values in sorted(groups.items())
    }


def summarize_exchange(exchange, rows):
    totals = empty_component_totals()
    for row in rows:
        add_components(totals, row)
    remaining_native = sum(
        int(row["native_total_claim_atto"])
        for row in rows
        if int(row["planned_total_entitlement_atto"]) == 0
    )
    remaining_wone = sum(
        (
            int(row["wone_balance_atto"])
            if int(row["planned_total_entitlement_atto"]) == 0
            else int(row["wone_balance_atto"])
            - int(row["wone_airdrop_atto"])
        )
        for row in rows
    )
    planned_wallet = totals["planned_wallet_airdrop"]
    planned_staked = totals["planned_staked_to_vault"]
    if (
        planned_wallet + planned_staked
        != totals["planned_total_entitlement"]
    ):
        raise ValueError("exchange planned delivery components do not close")
    if (
        remaining_native + remaining_wone
        != totals["remaining_not_airdropped"]
    ):
        raise ValueError("exchange residual component split does not close")
    incomplete_wone_rows = [
        row["address_hex"]
        for row in rows
        if int(row["wone_balance_atto"]) != int(row["wone_airdrop_atto"])
        and row["issuance_treatment"] != "not_issued"
    ]
    if incomplete_wone_rows:
        raise ValueError(
            "manual exchange delivery omits WONE for: "
            + ", ".join(incomplete_wone_rows[:10])
        )
    balance_rows = [dict(row) for row in rows]
    for row in balance_rows:
        row["balance_bucket"] = balance_bucket(
            int(row["qualification_total_atto"])
        )
    normalization = exchange["normalization"]
    destination_status = normalization["configured_destination_status"]
    if normalization["inventory_status"] != "received":
        memo_status = "awaiting_wallet_inventory"
    elif (
        exchange["config"]["destination_required"]
        and destination_status != "configured"
    ):
        memo_status = "hold_missing_destination"
    elif any(row["planned_delivery_status"] == "tier_pending" for row in rows):
        memo_status = "stage_policy_pending"
    else:
        memo_status = "ready_for_manual_reserve_delivery"
    tier_groups = {}
    for row in rows:
        if int(row["planned_total_entitlement_atto"]) == 0:
            continue
        key = (row["delivery_tier"], row["planned_wallet_destination"], row["planned_staking_destination"])
        group = tier_groups.setdefault(
            key,
            {
                "delivery_tier": key[0],
                "wallet_destination": key[1],
                "staking_destination": key[2],
                "wallets": 0,
                "wallet_component_atto": 0,
                "staking_component_atto": 0,
            },
        )
        group["wallets"] += 1
        group["wallet_component_atto"] += int(row["wallet_component_atto"])
        group["staking_component_atto"] += int(row["staking_component_atto"])
    delivery_groups = [
        {
            **group,
            "wallet_component_atto": str(group["wallet_component_atto"]),
            "staking_component_atto": str(group["staking_component_atto"]),
            "total_atto": str(
                group["wallet_component_atto"] + group["staking_component_atto"]
            ),
        }
        for _key, group in sorted(tier_groups.items())
    ]
    same_address_rows = sum(
        row["delivery_tier"] in {"same_address", "same_address_initial"}
        and int(row["planned_total_entitlement_atto"]) > 0
        for row in rows
    )
    submitted_values = [
        int(row["submitted_balance_atto"])
        for row in rows
        if row["submitted_balance_atto"] != ""
    ]
    qualification_values = [
        int(row["qualification_total_atto"]) for row in rows
    ]
    routed_values = [
        int(row["planned_total_entitlement_atto"]) for row in rows
    ]
    reconciliation_statuses = {}
    for row in rows:
        status = row["submitted_balance_reconciliation"]
        reconciliation_statuses[status] = (
            reconciliation_statuses.get(status, 0) + 1
        )
    parser_details = normalization.get("parser_details", {})
    rolled_back_claim = None
    if parser_details.get("rolled_back_deposits"):
        rolled_back_total = int(parser_details["rolled_back_deposit_total_atto"])
        if rolled_back_total != sum(
            int(item["amount_atto"])
            for item in parser_details["rolled_back_deposits"]
        ):
            raise ValueError("rolled-back deposit claim does not sum")
        rolled_back_claim = {
            "rows": len(parser_details["rolled_back_deposits"]),
            "total_atto": str(rolled_back_total),
            "total_one": lib.atto_to_one_str(rolled_back_total),
            "transactions": parser_details["rolled_back_deposits"],
            "treatment": "outside_cutoff_wallet_accounting_pending_decision",
        }
    return {
        "display_name": exchange["config"]["display_name"],
        "delivery_policy": exchange["config"]["delivery_policy"],
        "destination_mode": exchange["config"]["destination_mode"],
        "memo_status": memo_status,
        "delivery_groups": delivery_groups,
        "same_address_delivery_rows": same_address_rows,
        "aggregated_delivery_rows": sum(
            row["delivery_tier"]
            in {"aggregate", "aggregate_split", "aggregated_non_initial"}
            and int(row["planned_total_entitlement_atto"]) > 0
            for row in rows
        ),
        "rolled_back_deposit_claim": rolled_back_claim,
        "wallet_rows": len(rows),
        "claim_rows_found": sum(
            row["claim_status"] == "present" for row in rows
        ),
        "claim_rows_not_found": sum(
            row["claim_status"] == "not_found" for row in rows
        ),
        "qualified_rows": sum(
            row["qualification_status"] == "qualified" for row in rows
        ),
        "below_threshold_rows": sum(
            row["qualification_status"] == "below_threshold" for row in rows
        ),
        "manual_delivery_rows": sum(
            row["planned_delivery_status"] == EXCHANGE_STATUS for row in rows
        ),
        "held_delivery_rows": sum(
            row["planned_delivery_status"]
            in {"manual_destination_hold", "tier_pending"}
            for row in rows
        ),
        "destination": normalization["configured_destination"],
        "staking_destination": normalization.get(
            "configured_staking_destination", ""
        ),
        "destination_notes": normalization.get(
            "configured_destination_notes", {}
        ),
        "destination_status": destination_status,
        "authorization_designated_rows": normalization.get(
            "authorization_designated_rows",
            normalization["authorization_verified_rows"],
        ),
        "authorization_failed_rows": normalization.get(
            "authorization_failed_rows",
            0,
        ),
        "authorization_not_designated_rows": normalization.get(
            "authorization_not_designated_rows",
            len(rows) - normalization["authorization_verified_rows"],
        ),
        "authorization_provided_rows": normalization.get(
            "authorization_provided_rows",
            normalization["authorization_verified_rows"],
        ),
        "authorization_verified_rows": normalization[
            "authorization_verified_rows"
        ],
        "authorization_verified_qualified_rows": sum(
            row["qualification_status"] == "qualified"
            and row["authorization_status"] == "verified"
            for row in rows
        ),
        "authorization_unverified_qualified_rows": sum(
            row["qualification_status"] == "qualified"
            and row["authorization_status"] != "verified"
            for row in rows
        ),
        "submitted_balance_rows": normalization["submitted_balance_rows"],
        "submitted_balance_scope": normalization.get(
            "submitted_balance_scope",
            exchange["config"].get(
                "submitted_balance_scope",
                "liquid_shard0",
            ),
        ),
        "submitted_balance_exact_rows": sum(
            row["submitted_balance_reconciliation"].startswith("exact_")
            for row in rows
        ),
        "submitted_balance_reconciliations": {
            label: reconciliation_statuses[label]
            for label in sorted(reconciliation_statuses)
        },
        "submitted_balance_statistics": amount_statistics(submitted_values),
        "qualification_balance_statistics": amount_statistics(
            qualification_values
        ),
        "routed_balance_statistics": amount_statistics(routed_values),
        "remaining_native_not_airdropped_atto": str(remaining_native),
        "remaining_native_not_airdropped_one": lib.atto_to_one_str(
            remaining_native
        ),
        "remaining_wone_not_airdropped_atto": str(remaining_wone),
        "remaining_wone_not_airdropped_one": lib.atto_to_one_str(
            remaining_wone
        ),
        "planned_wallet_airdrop_atto": str(planned_wallet),
        "planned_wallet_airdrop_one": lib.atto_to_one_str(planned_wallet),
        "planned_staked_to_vault_atto": str(planned_staked),
        "planned_staked_to_vault_one": lib.atto_to_one_str(planned_staked),
        "totals": serializable_components(totals),
        "activity": grouped_stats(rows, "activity_status"),
        "activity_age": grouped_stats(rows, "activity_age_bucket"),
        "migration_stages": grouped_stats(rows, "migration_stage"),
        "balance_buckets": grouped_stats(balance_rows, "balance_bucket"),
        "submitted_balance_buckets": amount_bucket_stats(
            rows,
            "submitted_balance_atto",
            skip_blank=True,
        ),
        "planned_delivery_statuses": grouped_stats(
            rows, "planned_delivery_status"
        ),
    }


def one_display(value):
    amount = lib.atto_to_one_str(int(value))
    whole, fraction = amount.split(".")
    return f"{int(whole):,}.{fraction}"


def optional_one_display(value):
    return "n/a" if value is None else one_display(value)


def markdown_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _header in headers) + "|",
    ]
    lines.extend(
        "| " + " | ".join(str(value) for value in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


MODE_TEXT = {
    "aggregate": (
        "Every positive native or WONE claim in the inventory is aggregated "
        "and delivered to the configured exchange destination."
    ),
    "aggregate_split": (
        "Every positive claim is aggregated and split by component: liquid "
        "balances, supported cross-shard receipts, and WONE go to the wallet "
        "destination; delegated principal, pending undelegation, and unclaimed "
        "staking rewards go to the staking destination."
    ),
    "same_address": (
        "Every positive native or WONE claim is delivered to the same wallet "
        "address the exchange holds on Harmony, transferred manually rather "
        "than through the airdrop."
    ),
    "tiered": (
        "Wallets meeting the initial-distribution criteria (at least 1,000 ONE "
        "and indexed activity in the six months before cutoff) are delivered "
        "to their own addresses; every other positive claim is aggregated to "
        "the configured exchange destination."
    ),
}


def render_memo(config, summary, cutoff_text, threshold):
    mode = config["destination_mode"]
    policy_text = (
        "All exchange delivery is manual and drawn directly from the year 2050 "
        "supply reserve; no exchange wallet is included in the airdrop and any "
        "delegated principal is released from the validator vaults. "
        + MODE_TEXT[mode]
    )
    if mode == "same_address":
        destination = "each source wallet's own address"
    else:
        destination = summary["destination"] or "Not yet supplied"
    staking_destination = (
        summary["staking_destination"]
        if mode == "aggregate_split"
        else ""
    )
    notes = summary.get("destination_notes", {})
    wallet_note = notes.get("wallet" if mode == "aggregate_split" else "aggregate", "")
    staking_note = notes.get("staking", "")
    destination_lines = "\n".join(
        line
        for line in (
            f"- Wallet-component destination: `{destination}`",
            f"  - Destination file note: {wallet_note}" if wallet_note else "",
            f"- Staking-component destination: `{staking_destination}`"
            if staking_destination
            else "",
            f"  - Destination file note: {staking_note}"
            if staking_destination and staking_note
            else "",
        )
        if line
    )
    group_rows = [
        (
            group["delivery_tier"],
            f"{group['wallets']:,}",
            one_display(group["wallet_component_atto"]),
            one_display(group["staking_component_atto"]),
            one_display(group["total_atto"]),
            (
                "own address"
                if group["delivery_tier"] in {"same_address", "same_address_initial"}
                else group["wallet_destination"] or "hold"
            )
            + (
                f" / staking → {group['staking_destination']}"
                if group["staking_destination"]
                and group["staking_destination"] != group["wallet_destination"]
                else ""
            ),
        )
        for group in summary["delivery_groups"]
    ]
    delivery_table = (
        markdown_table(
            (
                "Delivery tier",
                "Wallets",
                "Wallet component ONE",
                "Staking component ONE",
                "Total ONE",
                "Destination",
            ),
            group_rows,
        )
        if group_rows
        else "No positive cutoff claim in this inventory."
    )
    totals = summary["totals"]
    submitted_stats = summary["submitted_balance_statistics"]
    qualification_stats = summary["qualification_balance_statistics"]
    balance_stat_rows = [
        (
            "Rows",
            f"{submitted_stats['rows']:,}",
            f"{qualification_stats['rows']:,}",
        ),
        (
            "Positive rows",
            f"{submitted_stats['positive_rows']:,}",
            f"{qualification_stats['positive_rows']:,}",
        ),
        (
            "Zero/no-value rows",
            f"{submitted_stats['zero_rows']:,}",
            f"{qualification_stats['zero_rows']:,}",
        ),
        (
            "Total",
            one_display(submitted_stats["total_atto"]),
            one_display(qualification_stats["total_atto"]),
        ),
        (
            "Minimum",
            optional_one_display(submitted_stats["minimum_atto"]),
            optional_one_display(qualification_stats["minimum_atto"]),
        ),
        (
            "Minimum positive",
            optional_one_display(submitted_stats["minimum_positive_atto"]),
            optional_one_display(
                qualification_stats["minimum_positive_atto"]
            ),
        ),
        (
            "Median",
            optional_one_display(submitted_stats["median_atto_rounded"]),
            optional_one_display(
                qualification_stats["median_atto_rounded"]
            ),
        ),
        (
            "Mean",
            optional_one_display(submitted_stats["mean_atto_rounded"]),
            optional_one_display(qualification_stats["mean_atto_rounded"]),
        ),
        (
            "Maximum",
            optional_one_display(submitted_stats["maximum_atto"]),
            optional_one_display(qualification_stats["maximum_atto"]),
        ),
    ]
    submitted_bucket_rows = [
        (
            label,
            values["rows"],
            one_display(values["total_atto"]),
        )
        for label, values in summary["submitted_balance_buckets"].items()
    ]
    submitted_breakdown = (
        markdown_table(
            ("Submitted balance bucket", "Wallets", "Submitted ONE"),
            submitted_bucket_rows,
        )
        if submitted_bucket_rows
        else "No submitted per-wallet balances were provided."
    )
    balance_rows = [
        (
            label,
            values["rows"],
            one_display(values["qualification_total_atto"]),
            one_display(values["planned_wallet_airdrop_atto"]),
            one_display(values["planned_staked_to_vault_atto"]),
        )
        for label, values in sorted(
            summary["balance_buckets"].items(),
            key=lambda item: BALANCE_BUCKET_ORDER.index(item[0]),
        )
    ]
    activity_rows = [
        (
            label,
            values["rows"],
            one_display(values["qualification_total_atto"]),
            one_display(values["planned_wallet_airdrop_atto"]),
            one_display(values["planned_staked_to_vault_atto"]),
        )
        for label, values in summary["activity"].items()
    ]
    activity_age_rows = [
        (
            label,
            values["rows"],
            one_display(values["qualification_total_atto"]),
            one_display(values["planned_wallet_airdrop_atto"]),
            one_display(values["planned_staked_to_vault_atto"]),
        )
        for label, values in sorted(
            summary["activity_age"].items(),
            key=lambda item: ACTIVITY_BUCKET_ORDER.index(item[0]),
        )
    ]
    blockers = []
    if summary["memo_status"] == "awaiting_wallet_inventory":
        blockers.append("Wallet inventory has not been received.")
    if config["destination_required"] and summary[
        "destination_status"
    ] != "configured":
        blockers.append(
            "The aggregate Ethereum destination is missing or blank; all "
            "planned routes remain on hold."
        )
    if not blockers:
        blockers.append(
            "Review and confirm the address-level audit and destination "
            "out of band before signing any transfer."
        )
    balance_scope = {
        "liquid_shard0": "cutoff shard-0 liquid balance",
        "liquid_total": "combined cutoff shard-0 and shard-1 liquid balance",
    }.get(
        summary["submitted_balance_scope"],
        summary["submitted_balance_scope"],
    )
    reconciliation_rows = [
        (status, rows)
        for status, rows in summary[
            "submitted_balance_reconciliations"
        ].items()
    ]
    threshold_scope = (
        "- Manual delivery threshold: none. The ordinary "
        f"`{one_display(threshold)} ONE` threshold is used only to remove "
        "exchange wallets from the automatic same-address airdrop"
        + (
            " and, with six-month activity, to decide the same-address tier."
            if mode == "tiered"
            else "."
        )
    )
    outside_delivery_label = "Value outside manual delivery (must be zero)"
    threshold_counts = (
        "- Wallets meeting the ordinary threshold: "
        f"{summary['qualified_rows']:,}\n"
        "- Wallets below the ordinary threshold: "
        f"{summary['below_threshold_rows']:,}\n"
        "- Wallets delivered to their own address: "
        f"{summary['same_address_delivery_rows']:,}\n"
        "- Wallets aggregated to an exchange destination: "
        f"{summary['aggregated_delivery_rows']:,}\n"
    )
    activity_policy = (
        "Activity is audit context"
        + (
            " and, together with the threshold, selects the same-address tier."
            if mode == "tiered"
            else " only and does not gate manual exchange delivery."
        )
        + " `not_collected` means the wallet was outside the ordinary "
        "activity scan; it does not mean the wallet was inactive."
    )
    rolled_back_section = ""
    rolled_back = summary.get("rolled_back_deposit_claim")
    if rolled_back:
        rolled_back_section = f"""
## Rolled-back deposit claim (outside wallet accounting)

The submission separately lists {rolled_back['rows']:,} deposit transactions
that the exchange reports as invalidated by the network rollback, totalling
**{one_display(rolled_back['total_atto'])} ONE**. These transactions are not
part of any cutoff wallet balance and are therefore excluded from the proposed
delivery above. Honouring them would require an explicit policy decision and a
separate route; they are recorded here so the exchange's own combined figure
can be reproduced.

{markdown_table(
            ('Source row', 'Transaction', 'Reported ONE'),
            [
                (
                    item['source_row'],
                    f"`{item['transaction_hash']}`",
                    one_display(item['amount_atto']),
                )
                for item in rolled_back['transactions']
            ],
        )}
"""
    return f"""# {config['display_name']} migration allocation memo

Status: `{summary['memo_status']}`

## Scope and policy

- Cutoff: `{cutoff_text}`
- Delivery source: year 2050 supply reserve, manual transfer; excluded from
  the airdrop
- Destination mode: `{mode}`
{threshold_scope}
- Wallet inventory rows: {summary['wallet_rows']:,}
- Cutoff claim rows found: {summary['claim_rows_found']:,}
- Cutoff claim rows not found: {summary['claim_rows_not_found']:,}
{destination_lines}

{policy_text}

Exchange-submitted balances are reconciliation evidence only. Proposed amounts
come from the cutoff-pinned Harmony migration claim ledger.

## Proposed manual delivery

- ONE delivered from the reserve for wallet balances (native liquid, supported
  cross-shard receipts, unclaimed rewards, pending undelegation, and WONE):
  **{one_display(totals['planned_wallet_airdrop_atto'])} ONE**
- Delegated principal released from validator vaults and delivered as ONE:
  {one_display(totals['planned_staked_to_vault_atto'])} ONE
- Total manual delivery:
  **{one_display(totals['planned_total_entitlement_atto'])} ONE**
- {outside_delivery_label}:
  {one_display(totals['remaining_not_airdropped_atto'])} ONE
- Native cutoff claim value: {one_display(totals['native_total_claim_atto'])} ONE
- Cutoff WONE value: {one_display(totals['wone_balance_atto'])} WONE
{threshold_counts}
### Delivery by tier and destination

Wallet component = liquid shard-0 and shard-1 balance, supported pending
cross-shard receipts, and WONE. Staking component = active delegated
principal, pending undelegation, and unclaimed staking reward.

{delivery_table}

## Wallet balance statistics

Submitted values use the exchange-declared `{summary['submitted_balance_scope']}`
scope. Cutoff native-plus-WONE value is
`native_total_claim_atto + wone_balance_atto`; it is the manual-delivery
amount, not an eligibility test. Mean and median are rounded to the nearest
atto-ONE.

{markdown_table(
        ('Statistic', 'Submitted ONE', 'Cutoff native + WONE'),
        balance_stat_rows,
    )}

## Submitted balance breakdown

{submitted_breakdown}

## Cutoff balance breakdown

Buckets use combined cutoff native-plus-WONE value
(`native_total_claim_atto + wone_balance_atto`). The bucket boundary only
removes exchange wallets from the automatic airdrop and selects the tier of a
tiered exchange; it does not limit manual delivery.

{markdown_table(
        (
            'Balance bucket',
            'Wallets',
            'Cutoff native + WONE',
            'Wallet ONE from reserve',
            'Released vault principal',
        ),
        balance_rows,
    )}

## Activity coverage

{activity_policy}

{markdown_table(
        (
            'Activity status',
            'Wallets',
            'Cutoff native + WONE',
            'Wallet ONE from reserve',
            'Released vault principal',
        ),
        activity_rows,
    )}

Activity recency uses exclusive calendar-month buckets relative to the cutoff:

{markdown_table(
        (
            'Last activity',
            'Wallets',
            'Cutoff native + WONE',
            'Wallet ONE from reserve',
            'Released vault principal',
        ),
        activity_age_rows,
    )}

## Submitted-balance reconciliation

- Rows carrying a submitted balance: {summary['submitted_balance_rows']:,}
- Declared reconciliation scope: {balance_scope}
- Exact matches to that cutoff scope:
  {summary['submitted_balance_exact_rows']:,}

{markdown_table(
        ('Reconciliation status', 'Wallets'),
        reconciliation_rows,
    )}
{rolled_back_section}
## Authorization-signature verification

- Submission-designated signature rows:
  {summary['authorization_designated_rows']:,}
- Signature rows provided: {summary['authorization_provided_rows']:,}
- Signatures cryptographically verified:
  {summary['authorization_verified_rows']:,}
- Designated rows missing or failing verification:
  {summary['authorization_failed_rows']:,}
- Inventory rows not designated for a signature:
  {summary['authorization_not_designated_rows']:,}

Normalization fails rather than emitting a row when a designated signature,
recovered signer, signed destination, or signature-source cross-check fails.

## Required review

{''.join(f'- {blocker}\n' for blocker in blockers)}
The address-level audit, normalization hashes, and generated routing input are
retained in the private exchange-accounting artifact bundle.
"""


def render_tier_report(config, summary, same_path, aggregated_path, cutoff_text):
    totals = summary["totals"]
    same_group = [
        group
        for group in summary["delivery_groups"]
        if group["delivery_tier"] == "same_address_initial"
    ]
    aggregated_group = [
        group
        for group in summary["delivery_groups"]
        if group["delivery_tier"] == "aggregated_non_initial"
    ]
    same_total = sum(int(group["total_atto"]) for group in same_group)
    aggregated_total = sum(int(group["total_atto"]) for group in aggregated_group)
    same_wallets = sum(group["wallets"] for group in same_group)
    aggregated_wallets = sum(group["wallets"] for group in aggregated_group)
    return f"""# {config['display_name']} tiered manual delivery

{config['display_name']} asked for wallets that meet the initial-distribution
criteria (at least 1,000 ONE of combined native and WONE value and indexed
activity in the six months before cutoff) to be delivered to their own
addresses, and for every other positive claim to be aggregated to
`{summary['destination']}`. Both tiers are delivered manually from the year
2050 supply reserve; none of these wallets is in the airdrop.

- Cutoff: `{cutoff_text}`
- Submitted wallets (including supplemental additions): {summary['wallet_rows']:,}
- Same-address tier wallets: {same_wallets:,}
- Same-address tier ONE: **{one_display(same_total)} ONE**
- Aggregated tier wallets (positive claims): {aggregated_wallets:,}
- Aggregated tier ONE: **{one_display(aggregated_total)} ONE**
- Total manual delivery: **{one_display(totals['planned_total_entitlement_atto'])} ONE**
- Of which delegated principal released from validator vaults:
  {one_display(totals['planned_staked_to_vault_atto'])} ONE
- Native cutoff claim value across the submitted set:
  {one_display(totals['native_total_claim_atto'])} ONE
- Cutoff WONE across the submitted set:
  {one_display(totals['wone_balance_atto'])} WONE
- Wallets with no positive cutoff claim:
  {summary['wallet_rows'] - same_wallets - aggregated_wallets:,}

Address lists:

- `{same_path.name}` — same-address tier; SHA-256 `{file_sha256(same_path)}`.
- `{aggregated_path.name}` — aggregated tier (positive claims only); SHA-256
  `{file_sha256(aggregated_path)}`.

The detailed `audits/{config['id']}.csv` file retains cutoff components,
policy category, stage, activity evidence, and each wallet's planned
destination. Missing activity means no qualifying indexed transaction was
found or activity was not previously collected; it is not proof that the
wallet was never used.
"""


def render_summary_report(policy, summaries, cutoff_text):
    delivery_rows = []
    statistics_rows = []
    signature_rows = []
    breakdown_rows = []
    pending_inventories = []
    held_destinations = []
    rolled_back_claims = []
    for config in policy["exchanges"]:
        summary = summaries[config["id"]]
        if summary.get("rolled_back_deposit_claim"):
            claim = summary["rolled_back_deposit_claim"]
            rolled_back_claims.append(
                f"{config['display_name']} "
                f"({one_display(claim['total_atto'])} ONE across "
                f"{claim['rows']:,} transactions)"
            )
        if summary["destination_mode"] == "same_address":
            destination_text = "same addresses"
        elif summary["destination_mode"] == "tiered":
            destination_text = (
                f"initial-criteria wallets: same addresses; others: "
                f"{summary['destination'] or 'hold'}"
            )
        elif summary["destination_mode"] == "aggregate_split":
            destination_text = (
                f"wallet: {summary['destination'] or 'hold'}; staking: "
                f"{summary['staking_destination'] or 'hold'}"
            )
        else:
            destination_text = summary["destination"] or "hold"
        delivery_rows.append(
            (
                config["display_name"],
                summary["destination_mode"],
                summary["memo_status"],
                f"{summary['wallet_rows']:,}",
                one_display(
                    summary["totals"]["wallet_component_atto"]
                ),
                one_display(
                    summary["totals"]["staking_component_atto"]
                ),
                one_display(
                    summary["totals"]["planned_total_entitlement_atto"]
                ),
                destination_text,
            )
        )
        statistics = summary["qualification_balance_statistics"]
        submitted = summary["submitted_balance_statistics"]
        statistics_rows.append(
            (
                config["display_name"],
                f"{summary['wallet_rows']:,}",
                summary["submitted_balance_scope"],
                f"{submitted['rows']:,}",
                one_display(submitted["total_atto"]),
                one_display(statistics["total_atto"]),
                optional_one_display(statistics["mean_atto_rounded"]),
                optional_one_display(statistics["median_atto_rounded"]),
                optional_one_display(statistics["maximum_atto"]),
            )
        )
        signature_rows.append(
            (
                config["display_name"],
                f"{summary['authorization_designated_rows']:,}",
                f"{summary['authorization_provided_rows']:,}",
                f"{summary['authorization_verified_rows']:,}",
                f"{summary['authorization_failed_rows']:,}",
                f"{summary['authorization_not_designated_rows']:,}",
            )
        )
        for label, values in sorted(
            summary["balance_buckets"].items(),
            key=lambda item: BALANCE_BUCKET_ORDER.index(item[0]),
        ):
            breakdown_rows.append(
                (
                    config["display_name"],
                    label,
                    f"{values['rows']:,}",
                    one_display(values["qualification_total_atto"]),
                )
            )
        if summary["memo_status"] == "awaiting_wallet_inventory":
            pending_inventories.append(config["display_name"])
        if (
            config["destination_required"]
            and summary["destination_status"] != "configured"
        ):
            held_destinations.append(config["display_name"])
    completion_notes = []
    if pending_inventories:
        completion_notes.append(
            "- Pending wallet inventories: "
            + ", ".join(pending_inventories)
            + "."
        )
    if held_destinations:
        completion_notes.append(
            "- Missing or blank aggregate destinations: "
            + ", ".join(held_destinations)
            + "."
        )
    if not completion_notes:
        completion_notes.append(
            "- All configured inventories and required destinations are present."
        )
    if rolled_back_claims:
        completion_notes.append(
            "- Separately reported rolled-back deposit claims, excluded from "
            "wallet accounting pending a policy decision: "
            + "; ".join(rolled_back_claims)
            + "."
        )
    return f"""# Exchange migration accounting summary

This private finding applies exchange-provided wallet inventories to the
cutoff-pinned migration ledger without re-deriving chain state.

- Cutoff: `{cutoff_text}`
- Every exchange wallet is excluded from the airdrop. All exchange migration is
  delivered manually, directly from the year 2050 supply reserve, in the
  `exchange_manual` stage; delegated principal is released from the validator
  vaults and delivered as ONE.
- Destination modes: `aggregate` (one destination), `aggregate_split` (wallet
  and staking components to separate destinations), `same_address` (each
  exchange wallet's own address), and `tiered` (initial-criteria wallets to
  their own addresses, all other positive claims aggregated).
- A blank destination produces a hold; it never falls back to a source wallet.
- Exchange-submitted balances are checked but never replace chain accounting.

## Delivery summary

The ordinary threshold does not limit manual delivery; it only removes
exchange wallets from the automatic same-address airdrop (recorded in the
generated exclusion artifact) and, with six-month activity, selects the
same-address tier of a tiered exchange. Wallet component = liquid balances,
supported cross-shard receipts, and WONE; staking component = delegated
principal, pending undelegation, and unclaimed rewards.

{markdown_table(
        (
            'Exchange',
            'Mode',
            'Status',
            'Wallets',
            'Wallet component ONE',
            'Staking component ONE',
            'Manual delivery ONE',
            'Destination(s)',
        ),
        delivery_rows,
    )}

## Wallet and balance statistics

Submitted totals reproduce exchange-provided balance evidence in the declared
scope. Cutoff statistics use combined native-plus-WONE value
`native_total_claim_atto + wone_balance_atto`, including zero/no-claim
inventory rows. This is the manual-delivery amount, not an eligibility test.
Mean and median are rounded to the nearest atto-ONE.

{markdown_table(
        (
            'Exchange',
            'Wallets',
            'Submitted scope',
            'Submitted rows',
            'Submitted ONE',
            'Cutoff native + WONE',
            'Mean ONE',
            'Median ONE',
            'Maximum ONE',
        ),
        statistics_rows,
    )}

## Cutoff balance breakdown

{markdown_table(
        ('Exchange', 'Balance bucket', 'Wallets', 'Cutoff native + WONE'),
        breakdown_rows,
    )}

## Signature verification

`Designated` means the exchange submission explicitly identified a row for
signature proof. A successful normalization emits no invalid designated row.

{markdown_table(
        (
            'Exchange',
            'Designated',
            'Provided',
            'Verified',
            'Missing/failed',
            'Not designated',
        ),
        signature_rows,
    )}

Full per-exchange memos, normalized inputs, address audits, tiered-exchange
address lists, airdrop exclusions, and manual route inputs are hash-pinned in
the private artifact bundle.

{chr(10).join(completion_notes)}

After route compilation,
`verify-exchange-routing.py` independently compares every positive memo amount
and planned destination with the compiled `exchange_manual` exception rows.
"""


def tier_list_row(row):
    return {field: row[field] for field in TIER_LIST_FIELDS}


def exchange_routes(exchange_id, row, route_priority):
    """Return the manual reserve-delivery route(s) for one positive audit row."""
    address = row["address_hex"].lower()
    evidence = f"exchanges/wallets-standardized/{exchange_id}.csv"
    wallet_destination = row["planned_wallet_destination"]
    staking_destination = row["planned_staking_destination"]
    held = row["planned_delivery_status"] != EXCHANGE_STATUS
    same_address = row["delivery_tier"] in {"same_address", "same_address_initial"}
    base = {
        "priority": str(route_priority),
        "source_address": address,
        "status": "hold" if held else EXCHANGE_STATUS,
        "reason": EXCHANGE_ROUTE_REASON,
        "evidence": evidence,
    }
    if same_address:
        return [
            {
                **base,
                "route_id": f"exchange-{exchange_id}-{address[2:]}",
                "destination_id": "",
                "destination_address": wallet_destination,
                "amount_atto": "ALL",
                "allocation_method": "wallet_first_pro_rata_vault",
                "notes": (
                    f"{row['delivery_tier']}: manual reserve delivery to the "
                    "exchange's own wallet address; excluded from the airdrop"
                ),
            }
        ]
    wallet_component = int(row["wallet_component_atto"])
    staking_component = int(row["staking_component_atto"])
    split = (
        row["destination_mode"] == "aggregate_split"
        and wallet_component > 0
        and staking_component > 0
    )
    if split:
        return [
            {
                **base,
                "route_id": f"exchange-{exchange_id}-{address[2:]}-wallet",
                "destination_id": f"exchange-{exchange_id}",
                "destination_address": "",
                "amount_atto": str(wallet_component),
                "allocation_method": "wallet_only",
                "notes": (
                    "liquid, cross-shard receipt, and WONE component to the "
                    "wallet destination; manual reserve delivery"
                ),
            },
            {
                **base,
                "priority": str(route_priority + 1),
                "route_id": f"exchange-{exchange_id}-{address[2:]}-staking",
                "destination_id": f"exchange-{exchange_id}-staking",
                "destination_address": "",
                "amount_atto": "ALL",
                "allocation_method": "wallet_first_pro_rata_vault",
                "notes": (
                    "delegated principal, pending undelegation, and unclaimed "
                    "reward component to the staking destination; released "
                    "from validator vaults"
                ),
            },
        ]
    destination_suffix = (
        "-staking"
        if row["destination_mode"] == "aggregate_split" and wallet_component == 0
        else ""
    )
    return [
        {
            **base,
            "route_id": f"exchange-{exchange_id}-{address[2:]}",
            "destination_id": f"exchange-{exchange_id}{destination_suffix}",
            "destination_address": "",
            "amount_atto": "ALL",
            "allocation_method": "wallet_first_pro_rata_vault",
            "notes": (
                f"{row['delivery_tier']}: manual reserve delivery to the "
                "configured exchange destination; excluded from the airdrop"
            ),
        }
    ]


def main():
    args = parse_args()
    policy, threshold, cutoff = load_policy(args.policy)
    exchanges, normalization, wanted = load_normalized(
        policy,
        args.normalized_dir,
        args.normalization_summary,
    )
    if normalization.get("policy_sha256") != file_sha256(args.policy):
        raise ValueError("normalization policy hash is stale")
    if int(normalization.get("minimum_atto", 0)) != threshold:
        raise ValueError("normalization threshold does not match policy")
    claims = load_claims(args.claims, wanted)
    holders = load_wone(args.wone_holders, wanted)
    activity = load_activity(args.activity, wanted)
    category_arguments = (
        args.automatic_claims,
        args.contract_claims,
        args.excluded_claims,
    )
    if any(category_arguments) and not all(category_arguments):
        raise ValueError(
            "automatic, contract, and excluded category files must be "
            "provided together"
        )
    categories_complete = all(category_arguments)
    categories = (
        load_categories(
            (
                (args.automatic_claims, "automatic"),
                (args.contract_claims, "contract_review"),
                (args.excluded_claims, "excluded_address"),
            ),
            wanted,
        )
        if categories_complete
        else {}
    )
    stages_complete = bool(args.migration_stages)
    stages = (
        load_migration_stages(args.migration_stages, wanted)
        if stages_complete
        else {}
    )
    output_dir = Path(args.output_dir)
    audits_dir = output_dir / "audits"
    memos_dir = output_dir / "memos"
    all_audits = {}
    summaries = {}
    exclusions = []
    routes = []
    destinations = []
    route_priority = int(policy["manual_route_priority"])
    if route_priority <= 0:
        raise ValueError("manual route priority must be positive")

    for config in policy["exchanges"]:
        exchange_id = config["id"]
        exchange = exchanges[exchange_id]
        rows = []
        for normalized in exchange["rows"]:
            address = normalized["address_normalized"]
            row = build_audit_row(
                exchange,
                normalized,
                claims.get(address),
                holders.get(address, 0),
                activity.get(address),
                categories.get(address),
                stages.get(address),
                threshold,
                cutoff,
                categories_complete,
                stages_complete,
            )
            rows.append(row)
            if (
                categories_complete
                and row["qualification_status"] == "qualified"
                and row["policy_category"] != "excluded_address"
            ):
                raise ValueError(
                    "qualifying exchange wallet remains outside the "
                    f"policy-routed category: {address}"
                )
            if (
                stages_complete
                and int(row["planned_total_entitlement_atto"]) > 0
            ):
                routes.extend(exchange_routes(exchange_id, row, route_priority))
            if row["qualification_status"] == "qualified":
                exclusions.append(
                    {
                        "address": address,
                        "exchange_id": exchange_id,
                        "reason": EXCHANGE_ROUTE_REASON,
                        "evidence": (
                            f"exchanges/wallets-standardized/"
                            f"{exchange_id}.csv"
                        ),
                    }
                )
        rows.sort(key=lambda row: bytes.fromhex(row["address_hex"][2:]))
        all_audits[exchange_id] = rows
        summaries[exchange_id] = summarize_exchange(exchange, rows)
        atomic_csv(
            audits_dir / f"{exchange_id}.csv",
            AUDIT_FIELDS,
            rows,
            args.replace,
        )
        atomic_text(
            memos_dir / f"{exchange_id}.md",
            render_memo(
                config,
                summaries[exchange_id],
                policy["cutoff_time_utc"],
                threshold,
            ),
            args.replace,
        )
        if config["destination_mode"] != "same_address":
            normalization_record = exchange["normalization"]
            state = normalization_record["configured_destination_status"]
            configured_notes = normalization_record.get(
                "configured_destination_notes", {}
            )
            if state == "configured":
                expected_roles = (
                    {"wallet", "staking"}
                    if config["destination_mode"] == "aggregate_split"
                    else {"aggregate"}
                )
                if set(configured_notes) != expected_roles or not all(
                    configured_notes.values()
                ):
                    raise ValueError(
                        f"{exchange_id}: configured destination roles "
                        f"{sorted(configured_notes)} lack the English notes "
                        f"required for {sorted(expected_roles)}"
                    )
            wallet_role = (
                "wallet" if config["destination_mode"] == "aggregate_split" else "aggregate"
            )
            destinations.append(
                {
                    "destination_id": f"exchange-{exchange_id}",
                    "destination_address": normalization_record[
                        "configured_destination"
                    ],
                    "status": EXCHANGE_STATUS if state == "configured" else "hold",
                    "notes": (
                        f"{config['display_name']} manual delivery destination "
                        "(wallet component) funded from the 2050 supply "
                        "reserve; confirm out of band before transfer. "
                        f"Destination file note: {configured_notes.get(wallet_role, '')}"
                    ),
                }
            )
            if config["destination_mode"] == "aggregate_split":
                destinations.append(
                    {
                        "destination_id": f"exchange-{exchange_id}-staking",
                        "destination_address": normalization_record[
                            "configured_staking_destination"
                        ],
                        "status": (
                            EXCHANGE_STATUS if state == "configured" else "hold"
                        ),
                        "notes": (
                            f"{config['display_name']} manual delivery "
                            "destination (delegated principal, pending "
                            "undelegation, unclaimed reward) funded from the "
                            "2050 supply reserve. Destination file note: "
                            f"{configured_notes.get('staking', '')}"
                        ),
                    }
                )

    exclusions.sort(
        key=lambda row: (bytes.fromhex(row["address"][2:]), row["exchange_id"])
    )
    routes.sort(key=lambda row: (int(row["priority"]), row["route_id"]))
    destinations.sort(key=lambda row: row["destination_id"])
    if len({row["address"] for row in exclusions}) != len(exclusions):
        raise ValueError("duplicate exchange eligibility exclusion")
    if len({row["route_id"] for row in routes}) != len(routes):
        raise ValueError("duplicate exchange route id")
    exclusion_path = output_dir / "qualified-exchange-exclusions.csv"
    artifact_routes = output_dir / "exchange-routes.csv"
    artifact_destinations = output_dir / "exchange-destinations.csv"
    atomic_csv(
        exclusion_path,
        EXCLUSION_FIELDS,
        exclusions,
        args.replace,
    )
    atomic_csv(artifact_routes, ROUTE_FIELDS, routes, args.replace)
    atomic_csv(
        artifact_destinations,
        DESTINATION_FIELDS,
        destinations,
        args.replace,
    )
    atomic_csv(args.routes_output, ROUTE_FIELDS, routes, args.replace)
    atomic_csv(
        args.destinations_output,
        DESTINATION_FIELDS,
        destinations,
        args.replace,
    )

    tier_outputs = {}
    tier_reports = {}
    for config in policy["exchanges"]:
        if config["destination_mode"] != "tiered":
            continue
        exchange_id = config["id"]
        tier_rows = all_audits[exchange_id]
        same_rows = [
            tier_list_row(row)
            for row in tier_rows
            if row["delivery_tier"] == "same_address_initial"
            and int(row["planned_total_entitlement_atto"]) > 0
        ]
        aggregated_rows = [
            tier_list_row(row)
            for row in tier_rows
            if row["delivery_tier"] == "aggregated_non_initial"
            and int(row["planned_total_entitlement_atto"]) > 0
        ]
        positive = {
            row["address_hex"].lower()
            for row in tier_rows
            if int(row["planned_total_entitlement_atto"]) > 0
        }
        listed = {row["address_hex"].lower() for row in same_rows} | {
            row["address_hex"].lower() for row in aggregated_rows
        }
        if stages_complete and listed != positive:
            raise ValueError(
                f"{exchange_id}: tier lists do not partition positive claims"
            )
        same_path = output_dir / f"{exchange_id}-same-address-initial.csv"
        aggregated_path = (
            output_dir / f"{exchange_id}-aggregated-non-initial.csv"
        )
        atomic_csv(same_path, TIER_LIST_FIELDS, same_rows, args.replace)
        atomic_csv(
            aggregated_path, TIER_LIST_FIELDS, aggregated_rows, args.replace
        )
        report_path = (
            output_dir / f"{exchange_id.upper()}_DELIVERY_TIERS_2026-09-22.md"
        )
        atomic_text(
            report_path,
            render_tier_report(
                config,
                summaries[exchange_id],
                same_path,
                aggregated_path,
                policy["cutoff_time_utc"],
            ),
            args.replace,
        )
        tier_outputs[f"{exchange_id}_same_address_initial"] = same_path
        tier_outputs[f"{exchange_id}_aggregated_non_initial"] = aggregated_path
        tier_reports[f"{exchange_id}_tier_report"] = report_path
        summaries[exchange_id]["tier_rows"] = {
            "same_address_initial": len(same_rows),
            "aggregated_non_initial": len(aggregated_rows),
        }
    aggregate_report = (
        output_dir / "EXCHANGE_MIGRATION_ACCOUNTING_2026-09-17.md"
    )
    atomic_text(
        aggregate_report,
        render_summary_report(
            policy,
            summaries,
            policy["cutoff_time_utc"],
        ),
        args.replace,
    )

    outputs = {
        "aggregate_report": aggregate_report,
        **tier_reports,
        **tier_outputs,
        "eligibility_exclusions": exclusion_path,
        "exchange_routes": artifact_routes,
        "exchange_destinations": artifact_destinations,
        "routing_input": Path(args.routes_output),
        "destination_input": Path(args.destinations_output),
    }
    result = {
        "schema_version": 2,
        "status": (
            "hold"
            if any(
                summary["memo_status"].startswith(
                    ("hold_", "awaiting_", "stage_policy_pending")
                )
                for summary in summaries.values()
            )
            else "passed"
        ),
        "delivery_policy": "manual_from_reserve",
        "delivery_source": policy["delivery_source"],
        "exchange_stage": EXCHANGE_STAGE,
        "routes_emitted": stages_complete,
        "cutoff_time_utc": policy["cutoff_time_utc"],
        "minimum_atto": str(threshold),
        "manual_route_priority": route_priority,
        "policy": args.policy,
        "policy_sha256": file_sha256(args.policy),
        "normalization_summary": args.normalization_summary,
        "normalization_summary_sha256": file_sha256(
            args.normalization_summary
        ),
        "claims": args.claims,
        "claims_sha256": file_sha256(args.claims),
        "wone_holders": args.wone_holders,
        "wone_holders_sha256": file_sha256(args.wone_holders),
        "activity": args.activity,
        "activity_sha256": file_sha256(args.activity),
        "migration_stages": args.migration_stages,
        "migration_stages_sha256": (
            file_sha256(args.migration_stages)
            if args.migration_stages
            else None
        ),
        "qualified_exchange_exclusions": len(exclusions),
        "manual_routes": len(routes),
        "exchanges": summaries,
        "outputs": {
            label: {
                "path": str(path),
                "sha256": file_sha256(path),
            }
            for label, path in outputs.items()
        },
        "audit_outputs": {
            exchange_id: {
                "path": str(audits_dir / f"{exchange_id}.csv"),
                "sha256": file_sha256(
                    audits_dir / f"{exchange_id}.csv"
                ),
            }
            for exchange_id in all_audits
        },
        "memo_outputs": {
            exchange_id: {
                "path": str(memos_dir / f"{exchange_id}.md"),
                "sha256": file_sha256(
                    memos_dir / f"{exchange_id}.md"
                ),
            }
            for exchange_id in all_audits
        },
    }
    atomic_json(output_dir / "summary.json", result, args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
