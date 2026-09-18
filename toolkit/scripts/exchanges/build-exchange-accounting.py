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
    "native_wallet_airdrop_atto",
    "wone_balance_atto",
    "wone_airdrop_atto",
    "wallet_airdrop_atto",
    "staked_to_vault_atto",
    "native_total_claim_atto",
    "qualification_total_atto",
    "total_claim_atto",
    "qualification_status",
    "policy_category",
    "delivery_policy",
    "configured_destination",
    "configured_destination_status",
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
GATE_LIST_FIELDS = (
    "address_hex",
    "address_one",
    "qualification_total_atto",
    "total_claim_atto",
    "planned_wallet_airdrop_atto",
    "planned_staked_to_vault_atto",
    "planned_total_entitlement_atto",
    "remaining_not_airdropped_atto",
    "qualification_status",
    "planned_delivery_status",
    "last_activity_time_utc",
    "activity_status",
)
COMPONENTS = (
    "liquid_shard0",
    "liquid_shard1",
    "native_wallet_airdrop",
    "wone_balance",
    "wone_airdrop",
    "wallet_airdrop",
    "staked_to_vault",
    "native_total_claim",
    "qualification_total",
    "total_claim",
    "planned_wallet_airdrop",
    "planned_staked_to_vault",
    "planned_total_entitlement",
    "remaining_not_airdropped",
)
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
    if policy.get("schema_version") != 1:
        raise ValueError("unsupported exchange policy schema")
    threshold = int(policy.get("minimum_atto", 0))
    if threshold <= 0:
        raise ValueError("invalid exchange threshold")
    cutoff = parse_utc(policy["cutoff_time_utc"])
    return policy, threshold, cutoff


def load_normalized(policy, directory, summary_path):
    with open(summary_path, encoding="utf-8") as source:
        normalization = json.load(source)
    if normalization.get("schema_version") != 1:
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
            if (
                min(native_total, wone, qualification, wallet, staked, total)
                < 0
                or qualification != native_total + wone
                or total != wallet + staked
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
    return values


def submitted_reconciliation(normalized, claim):
    submitted = normalized["submitted_balance_atto"]
    if not submitted:
        return "", "not_provided", ""
    submitted_value = int(submitted)
    cutoff_value = int(claim["liquid_shard0_atto"]) if claim else 0
    delta = cutoff_value - submitted_value
    if claim is None:
        status = "claim_not_found"
    elif delta == 0:
        status = "exact_liquid_shard0_match"
    elif delta > 0:
        status = "cutoff_liquid_higher"
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
    threshold,
    cutoff,
    categories_complete=True,
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
    destination_status = normalized["configured_destination_status"]
    planned_wallet = 0
    planned_staked = 0
    if delivery_policy == "automatic_threshold":
        if not qualifies:
            planned_status = "below_threshold_not_airdropped"
        elif not categories_complete:
            planned_status = "policy_category_pending"
        elif category == "automatic":
            planned_wallet = values["wallet_airdrop"]
            planned_staked = values["staked_to_vault"]
            planned_status = "automatic_same_address"
        elif category == "contract_review":
            planned_status = "contract_review_hold"
        else:
            planned_status = "higher_priority_policy_route"
    else:
        planned_wallet = values["wallet_airdrop"]
        planned_staked = values["staked_to_vault"]
        if not planned_wallet and not planned_staked:
            planned_status = (
                "wone_below_threshold_not_in_current_claim"
                if wone
                else "no_cutoff_claim"
            )
        elif destination_status == "configured":
            planned_status = "manual_destination_configured"
        else:
            planned_status = "manual_destination_hold"
    planned_total = planned_wallet + planned_staked
    if planned_total != (
        values["total_claim"]
        if planned_status
        in {
            "automatic_same_address",
            "manual_destination_configured",
            "manual_destination_hold",
        }
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
    submitted, reconciliation, delta = submitted_reconciliation(
        normalized, claim
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
        "delivery_policy": delivery_policy,
        "configured_destination": normalized["configured_destination"],
        "configured_destination_status": destination_status,
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
    elif exchange["config"]["delivery_policy"] == "automatic_threshold":
        memo_status = "automatic_threshold_policy"
    else:
        memo_status = "ready_for_exchange_review"
    return {
        "display_name": exchange["config"]["display_name"],
        "delivery_policy": exchange["config"]["delivery_policy"],
        "memo_status": memo_status,
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
        "automatic_airdrop_rows": sum(
            row["planned_delivery_status"] == "automatic_same_address"
            for row in rows
        ),
        "manual_delivery_rows": sum(
            row["planned_delivery_status"].startswith("manual_destination_")
            for row in rows
        ),
        "destination": normalization["configured_destination"],
        "destination_status": destination_status,
        "authorization_verified_rows": normalization[
            "authorization_verified_rows"
        ],
        "submitted_balance_rows": normalization["submitted_balance_rows"],
        "submitted_balance_exact_rows": sum(
            row["submitted_balance_reconciliation"]
            == "exact_liquid_shard0_match"
            for row in rows
        ),
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
        "balance_buckets": grouped_stats(balance_rows, "balance_bucket"),
        "planned_delivery_statuses": grouped_stats(
            rows, "planned_delivery_status"
        ),
    }


def one_display(value):
    amount = lib.atto_to_one_str(int(value))
    whole, fraction = amount.split(".")
    return f"{int(whole):,}.{fraction}"


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


def render_memo(config, summary, cutoff_text, threshold):
    manual = config["delivery_policy"] == "manual_current_claim"
    policy_text = (
        "Manual aggregate delivery to the configured exchange destination. "
        "Positive current migration claims are explicitly routed even when "
        "the source wallet is below the ordinary automatic threshold."
        if manual
        else (
            "Ordinary same-address automatic delivery applies only to wallets "
            "meeting the inclusive threshold and passing the normal account "
            "policy classification. Gate requested no aggregate reroute."
        )
    )
    destination = summary["destination"] or "Not yet supplied"
    totals = summary["totals"]
    balance_rows = [
        (
            label,
            values["rows"],
            one_display(values["qualification_total_atto"]),
            one_display(values["planned_wallet_airdrop_atto"]),
            one_display(values["planned_staked_to_vault_atto"]),
        )
        for label, values in summary["balance_buckets"].items()
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
    return f"""# {config['display_name']} migration allocation memo

Status: `{summary['memo_status']}`

## Scope and policy

- Cutoff: `{cutoff_text}`
- Inclusive ordinary automatic threshold: `{one_display(threshold)} ONE`
- Wallet inventory rows: {summary['wallet_rows']:,}
- Cutoff claim rows found: {summary['claim_rows_found']:,}
- Cutoff claim rows not found: {summary['claim_rows_not_found']:,}
- Configured Ethereum destination: `{destination}`

{policy_text}

Exchange-submitted balances are reconciliation evidence only. Proposed amounts
come from the cutoff-pinned Harmony migration claim ledger.

## Proposed delivery

- ERC-20 ONE proposed for direct airdrop:
  **{one_display(totals['planned_wallet_airdrop_atto'])} ONE**
- Validator-vault share principal:
  {one_display(totals['planned_staked_to_vault_atto'])} ONE
- Total routed ONE-equivalent entitlement:
  {one_display(totals['planned_total_entitlement_atto'])} ONE
- Remaining combined qualification value not in this delivery:
  **{one_display(totals['remaining_not_airdropped_atto'])} ONE**
- Remaining native ONE: {one_display(summary['remaining_native_not_airdropped_atto'])} ONE
- Remaining WONE: {one_display(summary['remaining_wone_not_airdropped_atto'])} WONE
- Native cutoff claim value: {one_display(totals['native_total_claim_atto'])} ONE
- Cutoff WONE value: {one_display(totals['wone_balance_atto'])} WONE
- Wallets meeting the ordinary threshold: {summary['qualified_rows']:,}
- Wallets below the ordinary threshold: {summary['below_threshold_rows']:,}

## Balance breakdown

Buckets use combined cutoff qualification value
(`native_total_claim_atto + wone_balance_atto`).

{markdown_table(
        (
            'Balance bucket',
            'Wallets',
            'Qualification ONE',
            'ERC-20 ONE',
            'Vault principal',
        ),
        balance_rows,
    )}

## Activity coverage

Last-activity data was previously collected for the ordinary qualifying set.
`not_collected` means the source wallet was outside that set; it does not mean
the wallet was inactive.

{markdown_table(
        (
            'Activity status',
            'Wallets',
            'Qualification ONE',
            'ERC-20 ONE',
            'Vault principal',
        ),
        activity_rows,
    )}

Activity recency uses exclusive calendar-month buckets relative to the cutoff:

{markdown_table(
        (
            'Last activity',
            'Wallets',
            'Qualification ONE',
            'ERC-20 ONE',
            'Vault principal',
        ),
        activity_age_rows,
    )}

## Submitted-balance reconciliation

- Rows carrying a submitted balance: {summary['submitted_balance_rows']:,}
- Exact matches to cutoff shard-0 liquid balance:
  {summary['submitted_balance_exact_rows']:,}
- Verified authorization-signature rows:
  {summary['authorization_verified_rows']:,}

## Required review

{''.join(f'- {blocker}\n' for blocker in blockers)}
The address-level audit, normalization hashes, and generated routing input are
retained in the private exchange-accounting artifact bundle.
"""


def render_gate_report(summary, output_dir, cutoff_text):
    totals = summary["totals"]
    airdropped_path = output_dir / "gate-airdropped.csv"
    not_airdropped_path = output_dir / "gate-not-airdropped.csv"
    return f"""# Gate automatic-airdrop audit

Gate did not request aggregate rerouting. Its submitted wallets therefore
remain under the ordinary inclusive threshold and account-policy rules.

- Cutoff: `{cutoff_text}`
- Submitted Gate wallets: {summary['wallet_rows']:,}
- Wallets in the automatic same-address batch:
  {summary['automatic_airdrop_rows']:,}
- Automatic ERC-20 ONE airdrop:
  **{one_display(totals['planned_wallet_airdrop_atto'])} ONE**
- Validator-vault share principal:
  {one_display(totals['planned_staked_to_vault_atto'])} ONE
- Total automatic ONE-equivalent entitlement:
  {one_display(totals['planned_total_entitlement_atto'])} ONE
- Wallets not in the current automatic batch:
  {summary['wallet_rows'] - summary['automatic_airdrop_rows']:,}
- Combined native ONE plus WONE value not airdropped:
  **{one_display(totals['remaining_not_airdropped_atto'])} ONE**
- Native ONE not airdropped:
  {one_display(summary['remaining_native_not_airdropped_atto'])} ONE
- WONE not airdropped:
  {one_display(summary['remaining_wone_not_airdropped_atto'])} WONE
- Native cutoff claim value across the submitted set:
  {one_display(totals['native_total_claim_atto'])} ONE
- Cutoff WONE across the submitted set:
  {one_display(totals['wone_balance_atto'])} WONE

Address lists:

- `{airdropped_path.name}` — wallets receiving a current automatic airdrop;
  SHA-256 `{file_sha256(airdropped_path)}`.
- `{not_airdropped_path.name}` — wallets not receiving a current automatic
  airdrop, including zero/no-cutoff-claim rows; SHA-256
  `{file_sha256(not_airdropped_path)}`.

The detailed `audits/gate.csv` file retains cutoff components, policy category,
activity evidence, and the reason for each disposition. Missing activity means
no qualifying indexed transaction was found or activity was not previously
collected; it is not proof that the wallet was never used.
"""


def render_summary_report(policy, summaries, cutoff_text):
    rows = []
    for config in policy["exchanges"]:
        summary = summaries[config["id"]]
        rows.append(
            (
                config["display_name"],
                summary["memo_status"],
                f"{summary['wallet_rows']:,}",
                f"{summary['qualified_rows']:,}",
                one_display(
                    summary["totals"]["planned_wallet_airdrop_atto"]
                ),
                one_display(
                    summary["totals"]["planned_staked_to_vault_atto"]
                ),
                one_display(
                    summary["totals"]["planned_total_entitlement_atto"]
                ),
                one_display(
                    summary["totals"]["remaining_not_airdropped_atto"]
                ),
            )
        )
    return f"""# Exchange migration accounting summary

This private finding applies exchange-provided wallet inventories to the
cutoff-pinned migration ledger without re-deriving chain state.

- Cutoff: `{cutoff_text}`
- Gate: ordinary same-address automatic processing at the inclusive threshold.
- Other exchanges: excluded from automatic same-address delivery and routed
  manually to their configured aggregate destination.
- A blank destination produces a hold; it never falls back to a source wallet.
- Exchange-submitted balances are checked but never replace chain accounting.

{markdown_table(
        (
            'Exchange',
            'Status',
            'Wallets',
            'Threshold wallets',
            'ERC-20 ONE',
            'Vault principal',
            'Total entitlement',
            'Remaining ONE/WONE value',
        ),
        rows,
    )}

Full per-exchange memos, normalized inputs, address audits, Gate split lists,
eligibility exclusions, and manual route inputs are hash-pinned in the private
artifact bundle. Binance and KuCoin remain incomplete until their missing
inputs are received; blank destination files keep the corresponding manual
delivery on hold. After route compilation,
`verify-exchange-routing.py` independently compares every positive memo amount
with the resulting exchange exception rows.
"""


def gate_list_row(row):
    return {field: row[field] for field in GATE_LIST_FIELDS}


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
                threshold,
                cutoff,
                categories_complete,
            )
            rows.append(row)
            if (
                categories_complete
                and config["delivery_policy"] == "manual_current_claim"
                and row["qualification_status"] == "qualified"
                and row["policy_category"] != "excluded_address"
            ):
                raise ValueError(
                    "qualifying non-Gate exchange wallet remains outside "
                    f"the policy-routed category: {address}"
                )
            if (
                config["delivery_policy"] == "manual_current_claim"
                and int(row["total_claim_atto"]) > 0
            ):
                routes.append(
                    {
                        "route_id": f"exchange-{exchange_id}-{address[2:]}",
                        "priority": str(route_priority),
                        "source_address": address,
                        "destination_id": f"exchange-{exchange_id}",
                        "destination_address": "",
                        "amount_atto": "ALL",
                        "allocation_method": "wallet_first_pro_rata_vault",
                        "reason": "exchange_requested_aggregate_reroute",
                        "evidence": (
                            f"exchanges/wallets-standardized/"
                            f"{exchange_id}.csv"
                        ),
                        "notes": (
                            "manual current-claim delivery; may explicitly "
                            "activate a threshold-deferred native claim"
                        ),
                    }
                )
            if (
                config["delivery_policy"] == "manual_current_claim"
                and row["qualification_status"] == "qualified"
            ):
                exclusions.append(
                    {
                        "address": address,
                        "exchange_id": exchange_id,
                        "reason": "exchange_requested_aggregate_reroute",
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
        if config["delivery_policy"] == "manual_current_claim":
            state = exchange["normalization"][
                "configured_destination_status"
            ]
            destinations.append(
                {
                    "destination_id": f"exchange-{exchange_id}",
                    "destination_address": exchange["normalization"][
                        "configured_destination"
                    ],
                    "status": "ready" if state == "configured" else "hold",
                    "notes": (
                        f"{config['display_name']} aggregate migration "
                        "destination; confirm out of band before transfer"
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
    exclusion_path = output_dir / "qualified-non-gate-exclusions.csv"
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

    gate_rows = all_audits["gate"]
    gate_airdropped = [
        gate_list_row(row)
        for row in gate_rows
        if int(row["planned_total_entitlement_atto"]) > 0
    ]
    gate_not_airdropped = [
        gate_list_row(row)
        for row in gate_rows
        if int(row["planned_total_entitlement_atto"]) == 0
    ]
    gate_airdropped_addresses = {
        row["address_hex"].lower() for row in gate_airdropped
    }
    gate_not_airdropped_addresses = {
        row["address_hex"].lower() for row in gate_not_airdropped
    }
    gate_addresses = {row["address_hex"].lower() for row in gate_rows}
    if (
        gate_airdropped_addresses & gate_not_airdropped_addresses
        or gate_airdropped_addresses | gate_not_airdropped_addresses
        != gate_addresses
    ):
        raise ValueError(
            "Gate airdropped and not-airdropped lists do not partition "
            "the normalized inventory"
        )
    atomic_csv(
        output_dir / "gate-airdropped.csv",
        GATE_LIST_FIELDS,
        gate_airdropped,
        args.replace,
    )
    atomic_csv(
        output_dir / "gate-not-airdropped.csv",
        GATE_LIST_FIELDS,
        gate_not_airdropped,
        args.replace,
    )
    gate_report = output_dir / "GATE_AUTOMATIC_AIRDROP_AUDIT_2026-09-17.md"
    atomic_text(
        gate_report,
        render_gate_report(
            summaries["gate"],
            output_dir,
            policy["cutoff_time_utc"],
        ),
        args.replace,
    )
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
        "gate_report": gate_report,
        "gate_airdropped": output_dir / "gate-airdropped.csv",
        "gate_not_airdropped": output_dir / "gate-not-airdropped.csv",
        "eligibility_exclusions": exclusion_path,
        "exchange_routes": artifact_routes,
        "exchange_destinations": artifact_destinations,
        "routing_input": Path(args.routes_output),
        "destination_input": Path(args.destinations_output),
    }
    result = {
        "schema_version": 1,
        "status": (
            "hold"
            if any(
                summary["memo_status"].startswith(("hold_", "awaiting_"))
                for summary in summaries.values()
            )
            else "passed"
        ),
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
        "qualified_non_gate_exclusions": len(exclusions),
        "manual_routes": len(routes),
        "gate_airdropped_rows": len(gate_airdropped),
        "gate_not_airdropped_rows": len(gate_not_airdropped),
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
