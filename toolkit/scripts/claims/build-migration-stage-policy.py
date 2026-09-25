#!/usr/bin/env python3

"""Build the address-level migration-stage policy and exact reconciliation."""

import argparse
import calendar
import csv
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ATTO_PER_ONE = 10**18
MINIMUM_ATTO = 1000 * ATTO_PER_ONE
WONE_ADDRESS = "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a"
LAYERZERO_ADDRESSES = {
    "0x5b18a4e73f9a4fe337a072516b317863ad3046aa",
    "0x905582f21fb9855c809d5b8933272a292dfbb138",
}
OUTPUT_FIELDS = (
    "secure_key",
    "address",
    "account_classification",
    "contract_category",
    "policy_group",
    "routing_category",
    "migration_stage",
    "issuance_treatment",
    "qualification_total_atto",
    "gross_allocation_atto",
    "existing_non_issuance_atto",
    "historical_retained_cap_atto",
    "wone_source_offset_atto",
    "reviewed_contract_non_issuance_atto",
    "reviewed_contract_wallet_non_issuance_atto",
    "reviewed_contract_staked_non_issuance_atto",
    "migration_wallet_allocation_atto",
    "migration_staked_to_vault_atto",
    "migration_allocation_atto",
    "last_activity_time_utc",
    "stage_reason",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualified-activity", required=True)
    parser.add_argument("--activity-summary", required=True)
    parser.add_argument("--migration-summary", required=True)
    parser.add_argument("--contract-review", required=True)
    parser.add_argument(
        "--existing-non-issuance",
        action="append",
        required=True,
        help="reviewed non-issuance inventory (not_issued_atto); repeatable, an address may appear in only one",
    )
    parser.add_argument(
        "--historical-retention",
        action="append",
        required=True,
        help="retained-cap export (retained_cap_atto); repeatable, amounts for one address add up",
    )
    parser.add_argument("--manual-wallets", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--initial-months", type=int, default=6)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.initial_months <= 0:
        parser.error("--initial-months must be positive")
    return args


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc(value):
    if not value.endswith("Z"):
        raise ValueError(f"UTC timestamp must end in Z: {value}")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"timestamp is not UTC: {value}")
    return parsed


def subtract_calendar_months(value, months):
    absolute_month = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def one(value):
    whole, fraction = divmod(int(value), ATTO_PER_ONE)
    return f"{whole}.{fraction:018d}"


def percentage(value, total):
    if not total:
        return "0.000000000000"
    scaled = (int(value) * 100 * 10**12 + int(total) // 2) // int(total)
    whole, fraction = divmod(scaled, 10**12)
    return f"{whole}.{fraction:012d}"


def split_final_components(wallet, staked, final_allocation):
    gross = wallet + staked
    if min(wallet, staked, final_allocation) < 0 or final_allocation > gross:
        raise ValueError("invalid final allocation component input")
    deduction = gross - final_allocation
    final_wallet = max(wallet - deduction, 0)
    final_staked = final_allocation - final_wallet
    if final_staked < 0 or final_staked > staked:
        raise ValueError("final allocation components do not close")
    return final_wallet, final_staked


def wallet_stage(allocation, net_qualification, activity_time, initial_since, months):
    """Stage, reason and threshold membership of a wallet row.

    Incident and reviewed non-issuance deductions apply before the 1,000 ONE
    test, so exploit or theft funds cannot qualify a wallet whose legitimate
    remainder is smaller.
    """
    if allocation == 0:
        return "", "prior reviewed non-issuance consumes allocation", False
    if net_qualification < MINIMUM_ATTO:
        return "deferred", "below 1,000 ONE after incident deductions", False
    if activity_time is not None and activity_time >= initial_since:
        return "initial", f"wallet activity within {months} months", True
    if activity_time is not None:
        return "deferred", "wallet activity predates initial window", True
    return "deferred", "no indexed wallet activity", True


def read_json(path, label):
    with open(path, encoding="utf-8") as source:
        value = json.load(source)
    if value.get("status") not in {None, "passed"}:
        raise ValueError(f"{label} did not pass")
    return value


def load_amounts(path, amount_field, label):
    values = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"address_hex", amount_field}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            address = row["address_hex"].strip().lower()
            if (
                len(address) != 42
                or not address.startswith("0x")
                or any(character not in "0123456789abcdef" for character in address[2:])
            ):
                raise ValueError(f"{path}:{line}: invalid address")
            if address in values:
                raise ValueError(f"{path}:{line}: duplicate address")
            amount = int(row[amount_field])
            if amount < 0:
                raise ValueError(f"{path}:{line}: negative {label}")
            values[address] = amount
    return values


def load_address_set(path):
    addresses = set()
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        if "address" not in set(reader.fieldnames or ()):
            raise ValueError(f"{path}: missing address field")
        for line, row in enumerate(reader, start=2):
            address = row["address"].strip().lower()
            if (
                len(address) != 42
                or not address.startswith("0x")
                or any(character not in "0123456789abcdef" for character in address[2:])
            ):
                raise ValueError(f"{path}:{line}: invalid address")
            if address in addresses:
                raise ValueError(f"{path}:{line}: duplicate address")
            addresses.add(address)
    return addresses


def load_contract_review(path):
    identities = {}
    policy_state = None
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "primary_category",
            "subcategory",
            "policy_state_block",
            "policy_state_block_hash",
            "policy_state_root",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            address = row["address"].strip().lower()
            if address in identities:
                raise ValueError(f"{path}:{line}: duplicate address")
            state = {
                "block": int(row["policy_state_block"]),
                "block_hash": row["policy_state_block_hash"],
                "state_root": row["policy_state_root"],
            }
            if not state["block_hash"] or not state["state_root"]:
                raise ValueError(f"{path}:{line}: incomplete policy state")
            if policy_state is None:
                policy_state = state
            elif state != policy_state:
                raise ValueError(f"{path}:{line}: mixed policy state")
            identities[address] = {
                "primary_category": row["primary_category"],
                "subcategory": row["subcategory"],
            }
    return identities, policy_state


def contract_policy(address, identity):
    category = identity["primary_category"]
    if category == "validator-account":
        return None
    if category == "multisig-wallet":
        return "multisig", "next_stage"
    if category == "onewallet":
        return "onewallet", "next_stage"
    if address in LAYERZERO_ADDRESSES:
        return "layerzero_bridge_collateral", "next_stage"
    if category == "smartvault-wallet":
        return "smartvault", ""
    return "other_reviewed_contract", ""


def write_csv(path, rows, replace):
    if os.path.exists(path + ".partial"):
        raise FileExistsError(path + ".partial")
    if os.path.exists(path) and not replace:
        raise FileExistsError(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=OUTPUT_FIELDS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(path + ".partial", path)


def write_text(path, text, replace):
    if os.path.exists(path + ".partial"):
        raise FileExistsError(path + ".partial")
    if os.path.exists(path) and not replace:
        raise FileExistsError(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path + ".partial", "x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(path + ".partial", path)


def markdown_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend(
        "| " + " | ".join(str(value) for value in row) + " |" for row in rows
    )
    return "\n".join(lines)


def render_report(result):
    contracts = result["contracts"]
    windows = result["wallet_activity_windows"]
    deferred = result["deferred_wallets"]
    allocation = result["allocation"]
    return f"""# Ordinary migration-stage policy reconciliation

Generated from the inclusive snapshot-qualified population. Account
classification, migration stage, destination readiness, and issuance treatment
are separate decisions. Exchange routes later override every exchange
wallet's ordinary stage with `exchange_manual`: all exchange migration is
delivered manually from the year 2050 supply reserve and excluded from the
airdrop.

## Confirmed stage policy

- Threshold: at least 1,000 ONE of combined wallet, staking and WONE balance
  after incident deductions (reviewed non-issuance and retained exploit caps),
  so incident funds never qualify a wallet. The snapshot-qualified population
  below is the gross review scope.
- Initial stage: positive eligible wallets with indexed activity on or after
  `{result["initial_window"]["since_time_utc"]}`.
- Exchange override at route compilation: every positive entitlement in an
  exchange inventory moves to `exchange_manual`, regardless of the ordinary
  threshold or activity result shown here; for a tiered exchange the
  `initial` result shown here selects same-address delivery.
- Next stage regardless of activity: reviewed multisigs, the two reviewed
  LayerZero collateral contracts, and reviewed 1wallet allocations.
- Not issued: SmartVault and the remaining reviewed genuine contracts.
- Destination evidence remains required for every nonterminal contract route.

## Reviewed genuine contracts

{markdown_table(
    (
        "Internal group",
        "Addresses",
        "Migration allocation ONE",
        "Migration stage",
        "Issuance treatment",
    ),
    (
        ("Multisigs", f'{contracts["groups"]["multisig"]["addresses"]:,}', one(contracts["groups"]["multisig"]["allocation_atto"]), "next stage", "issue after per-address destination approval"),
        ("LayerZero bridge collateral", f'{contracts["groups"]["layerzero_bridge_collateral"]["addresses"]:,}', one(contracts["groups"]["layerzero_bridge_collateral"]["allocation_atto"]), "next stage", "issue after reconciliation"),
        ("1wallet", f'{contracts["groups"]["onewallet"]["addresses"]:,}', one(contracts["groups"]["onewallet"]["allocation_atto"]), "next stage", "issue after destination approval"),
        ("SmartVault", f'{contracts["groups"]["smartvault"]["addresses"]:,}', one(contracts["groups"]["smartvault"]["not_issued_atto"]), "—", "not issued"),
        ("Other reviewed genuine contracts", f'{contracts["groups"]["other_reviewed_contract"]["addresses"]:,}', one(contracts["groups"]["other_reviewed_contract"]["not_issued_atto"]), "—", "not issued"),
    ),
)}

The public “Abandoned contracts” aggregate is a reporting label only:
`{one(allocation["reviewed_contract_non_issuance_atto"])} + `
`{one(allocation["wone_retained_not_issued_atto"])} = `
`{one(allocation["abandoned_contracts_public_aggregate_atto"])} ONE`.
It is not an inactivity detector or a finding about beneficial ownership.

## Allocation by stage

{markdown_table(
    ("Allocation bucket", "Addresses", "ONE"),
    (
        ("Ordinary initial wallets before exchange override", f'{result["initial_wallets"]["addresses"]:,}', one(allocation["initial_wallets_atto"])),
        ("Next-stage reviewed contracts", f'{contracts["approved"]["addresses"]:,}', one(allocation["next_stage_contracts_atto"])),
        ("Deferred wallets", "not a recipient manifest", one(allocation["deferred_wallets_atto"])),
        ("Total migration allocation", "", one(allocation["total_migration_atto"])),
    ),
)}

Before explicit routing, the ordinary initial wallet cohort contains
`{result["initial_wallets"]["automatic_policy_addresses"]:,}` automatic-policy
rows and `{result["initial_wallets"]["manual_routing_addresses"]:,}`
exchange/manual rows. Confirmed exchange rows are removed from this cohort when
routing compiles the separate `exchange_manual` stage, which is delivered by
hand from the 2050 supply reserve and never airdropped. This report is not an
unconditional same-address distribution manifest.

## Wallet activity windows

{markdown_table(
    ("Window", "Positive wallets", "ONE", "Share of qualifying wallet allocation"),
    tuple(
        (
            f'{entry["months"]} months',
            f'{entry["addresses"]:,}',
            one(entry["allocation_atto"]),
            entry["share_of_qualifying_wallet_percent"] + "%",
        )
        for entry in windows
    )
    + (
        (
            "No activity limit",
            f'{result["qualifying_wallets"]["positive_addresses"]:,}',
            one(result["qualifying_wallets"]["allocation_atto"]),
            "100.000000000000%",
        ),
    ),
)}

Ordinary deferred value before exchange overrides reconciles as:

- below 1,000 ONE: `{one(deferred["below_threshold_atto"])} ONE`, including
  `{deferred["below_threshold_after_incident_deductions_addresses"]:,}` snapshot-qualified
  wallets (`{one(deferred["below_threshold_after_incident_deductions_atto"])} ONE`) that fall
  below the threshold after incident deductions;
- qualified, older than six months:
  `{one(deferred["older_than_initial_window_atto"])} ONE`;
- qualified, no indexed activity:
  `{one(deferred["no_indexed_activity_atto"])} ONE`.

The compiled routing summary moves all exchange value out of these ordinary
buckets and into `exchange_manual`.

## Conservation

```text
gross native snapshot                      {one(allocation["gross_native_snapshot_atto"])}
- existing non-issuance                    {one(allocation["existing_non_issuance_atto"])}
- historical retained-fund caps            {one(allocation["historical_retained_caps_atto"])}
- retained WONE backing                     {one(allocation["wone_retained_not_issued_atto"])}
- reviewed-contract non-issuance            {one(allocation["reviewed_contract_non_issuance_atto"])}
= total migration allocation               {one(allocation["total_migration_atto"])}
```

WONE holder additions are redistribution, not new supply. The WONE source
backing is removed once; the source address's shard-1 native remainder is part
of reviewed-contract non-issuance.
Not-issued amounts remain unallocated within the fixed 26.271 billion ONE
premint (`12.6 billion + 441 million × 31 years`); no burn, new treasury
transfer, or total-supply change is implied.

## Evidence and limitations

- Address policy CSV: `{result["output"]}`
- Contract classification: `{result["sources"]["contract_review"]["path"]}`
- Activity metric: `{result["activity_definition"]}`
- Verified validator-wrapper rows remain wallet accounts.
- The reviewed contract partition covers the snapshot-qualified review set.
  Unclassified below-threshold code-bearing accounts remain deferred and are
  not silently added to the contract exclusion.
"""


def main():
    args = parse_args()
    activity_summary = read_json(args.activity_summary, "activity summary")
    migration_summary = read_json(args.migration_summary, "migration summary")
    qualified_activity_sha256 = file_sha256(args.qualified_activity)
    if activity_summary.get("input_sha256") != qualified_activity_sha256:
        raise ValueError(
            "activity summary does not identify the qualified activity CSV"
        )
    cutoff_text = activity_summary.get("cutoff_time_utc")
    if not cutoff_text:
        raise ValueError("activity summary is missing cutoff_time_utc")
    cutoff = parse_utc(cutoff_text)
    initial_since = subtract_calendar_months(cutoff, args.initial_months)
    windows = {
        int(entry["months"]): entry for entry in activity_summary["windows"]
    }
    if args.initial_months not in windows:
        raise ValueError("initial activity window is absent from activity summary")
    if parse_utc(windows[args.initial_months]["since_time_utc"]) != initial_since:
        raise ValueError("initial activity window does not match cutoff")

    existing = {}
    for path in args.existing_non_issuance:
        for address, amount in load_amounts(
            path, "not_issued_atto", "non-issuance"
        ).items():
            if address in existing:
                raise ValueError(f"{path}: {address} is in more than one non-issuance inventory")
            existing[address] = amount
    historical = defaultdict(int)
    for path in args.historical_retention:
        for address, amount in load_amounts(
            path, "retained_cap_atto", "retained cap"
        ).items():
            historical[address] += amount
    overlap = set(existing) & set(historical)
    if overlap:
        raise ValueError(
            "existing non-issuance and historical retained caps overlap: "
            + ", ".join(sorted(overlap))
        )
    manual_wallets = load_address_set(args.manual_wallets)
    identities, policy_state = load_contract_review(args.contract_review)
    reserve = int(migration_summary["wone_reserve_atto"])
    redistributed = int(
        migration_summary.get(
            "wone_redistributed_to_recipients_atto",
            migration_summary["wone_redistributed_to_priority_atto"],
        )
    )
    retained_wone = int(
        migration_summary["wone_retained_not_issued_atto"]
    )
    if reserve != redistributed + retained_wone:
        raise ValueError("WONE reserve split does not close")

    output_rows = []
    seen = set()
    contract_seen = set()
    validators_seen = set()
    stage_counts = Counter()
    treatment_counts = Counter()
    contract_groups = defaultdict(
        lambda: {
            "addresses": 0,
            "allocation_atto": 0,
            "wallet_allocation_atto": 0,
            "staked_allocation_atto": 0,
            "not_issued_atto": 0,
            "not_issued_wallet_atto": 0,
            "not_issued_staked_atto": 0,
        }
    )
    wallet_rows = []
    below_after_deductions = {"addresses": 0, "allocation_atto": 0}
    exact_threshold_rows = 0
    previous_key = None
    with open(args.qualified_activity, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "secure_key",
            "address",
            "qualification_total_atto",
            "wallet_airdrop_atto",
            "staked_to_vault_atto",
            "total_claim_atto",
            "last_activity_time_utc",
            "last_activity_timestamp_unix",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{args.qualified_activity}: missing fields {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous_key is not None and key <= previous_key:
                raise ValueError(
                    f"{args.qualified_activity}:{line}: keys not increasing"
                )
            previous_key = key
            address = row["address"].lower()
            if address in seen:
                raise ValueError(
                    f"{args.qualified_activity}:{line}: duplicate address"
                )
            seen.add(address)
            qualification = int(row["qualification_total_atto"])
            if qualification < MINIMUM_ATTO:
                raise ValueError(
                    f"{args.qualified_activity}:{line}: below threshold"
                )
            exact_threshold_rows += int(qualification == MINIMUM_ATTO)
            gross = int(row["total_claim_atto"])
            wallet = int(row["wallet_airdrop_atto"])
            staked = int(row["staked_to_vault_atto"])
            if min(gross, wallet, staked) < 0 or wallet + staked != gross:
                raise ValueError(
                    f"{args.qualified_activity}:{line}: allocation mismatch"
                )
            existing_amount = existing.get(address, 0)
            historical_amount = historical.get(address, 0)
            wone_offset = reserve if address == WONE_ADDRESS else 0
            before_contract_policy = (
                gross - existing_amount - historical_amount - wone_offset
            )
            if before_contract_policy < 0:
                raise ValueError(
                    f"{args.qualified_activity}:{line}: deductions exceed claim"
                )

            identity = identities.get(address)
            policy = contract_policy(address, identity) if identity else None
            contract_non_issuance = 0
            contract_wallet_non_issuance = 0
            contract_staked_non_issuance = 0
            before_contract_wallet, before_contract_staked = (
                split_final_components(wallet, staked, before_contract_policy)
            )
            if policy is None:
                account_classification = (
                    "validator_wallet"
                    if identity
                    and identity["primary_category"] == "validator-account"
                    else "wallet"
                )
                contract_category = ""
                policy_group = (
                    "validator_wallet"
                    if account_classification == "validator_wallet"
                    else "wallet"
                )
                if account_classification == "validator_wallet":
                    validators_seen.add(address)
                activity_time = (
                    parse_utc(row["last_activity_time_utc"])
                    if row["last_activity_time_utc"]
                    else None
                )
                stage, reason, meets_threshold = wallet_stage(
                    before_contract_policy,
                    qualification - existing_amount - historical_amount,
                    activity_time,
                    initial_since,
                    args.initial_months,
                )
                migration_allocation = before_contract_policy
                routing_category = (
                    "exchange_or_manual"
                    if address in manual_wallets
                    else "automatic_policy"
                )
                if migration_allocation > 0 and not meets_threshold:
                    below_after_deductions["addresses"] += 1
                    below_after_deductions["allocation_atto"] += (
                        migration_allocation
                    )
                if migration_allocation > 0 and meets_threshold:
                    wallet_rows.append(
                        {
                            "address": address,
                            "allocation": migration_allocation,
                            "time": activity_time,
                            "stage": stage,
                            "routing_category": routing_category,
                            "validator": account_classification
                            == "validator_wallet",
                        }
                    )
            else:
                policy_group, stage = policy
                account_classification = "genuine_contract"
                contract_category = identity["primary_category"]
                contract_seen.add(address)
                routing_category = "contract_policy"
                if not stage:
                    contract_non_issuance = before_contract_policy
                    contract_wallet_non_issuance = before_contract_wallet
                    contract_staked_non_issuance = before_contract_staked
                    migration_allocation = 0
                    reason = (
                        "reviewed contract allocation retained in 2050 premint reserve"
                    )
                else:
                    migration_allocation = before_contract_policy
                    reason = "reviewed contract allocation reserved for next stage"
                group = contract_groups[policy_group]
                group["addresses"] += 1
                group["allocation_atto"] += migration_allocation
                if stage:
                    group["wallet_allocation_atto"] += before_contract_wallet
                    group["staked_allocation_atto"] += before_contract_staked
                group["not_issued_atto"] += contract_non_issuance
                group["not_issued_wallet_atto"] += (
                    contract_wallet_non_issuance
                )
                group["not_issued_staked_atto"] += (
                    contract_staked_non_issuance
                )

            issuance_treatment = (
                "issue" if migration_allocation > 0 else "not_issued"
            )
            migration_wallet, migration_staked = split_final_components(
                wallet, staked, migration_allocation
            )
            if stage:
                stage_counts[stage] += 1
            treatment_counts[issuance_treatment] += 1
            output_rows.append(
                {
                    "secure_key": key,
                    "address": address,
                    "account_classification": account_classification,
                    "contract_category": contract_category,
                    "policy_group": policy_group,
                    "routing_category": routing_category,
                    "migration_stage": stage,
                    "issuance_treatment": issuance_treatment,
                    "qualification_total_atto": str(qualification),
                    "gross_allocation_atto": str(gross),
                    "existing_non_issuance_atto": str(existing_amount),
                    "historical_retained_cap_atto": str(historical_amount),
                    "wone_source_offset_atto": str(wone_offset),
                    "reviewed_contract_non_issuance_atto": str(
                        contract_non_issuance
                    ),
                    "reviewed_contract_wallet_non_issuance_atto": str(
                        contract_wallet_non_issuance
                    ),
                    "reviewed_contract_staked_non_issuance_atto": str(
                        contract_staked_non_issuance
                    ),
                    "migration_wallet_allocation_atto": str(
                        migration_wallet
                    ),
                    "migration_staked_to_vault_atto": str(
                        migration_staked
                    ),
                    "migration_allocation_atto": str(migration_allocation),
                    "last_activity_time_utc": row["last_activity_time_utc"],
                    "stage_reason": reason,
                }
            )

    if len(output_rows) != int(activity_summary["candidates"]):
        raise ValueError("qualified row count does not match activity summary")
    if exact_threshold_rows != int(activity_summary["exact_threshold_rows"]):
        raise ValueError("exact-threshold count does not match activity summary")
    reviewed_contracts = {
        address
        for address, identity in identities.items()
        if identity["primary_category"] != "validator-account"
    }
    reviewed_validators = {
        address
        for address, identity in identities.items()
        if identity["primary_category"] == "validator-account"
    }
    if contract_seen != reviewed_contracts:
        raise ValueError("contract review does not match qualified input")
    if validators_seen != reviewed_validators:
        raise ValueError("validator review does not match qualified input")
    if not LAYERZERO_ADDRESSES <= reviewed_contracts:
        raise ValueError("reviewed contract set is missing LayerZero collateral")
    if manual_wallets & reviewed_contracts:
        raise ValueError("manual wallet set overlaps genuine contracts")
    if not manual_wallets <= seen:
        raise ValueError("manual wallet set is not a subset of threshold rows")

    window_results = []
    for months, entry in sorted(windows.items()):
        since = parse_utc(entry["since_time_utc"])
        selected = [
            row
            for row in wallet_rows
            if row["time"] is not None and row["time"] >= since
        ]
        window_results.append(
            {
                "months": months,
                "since_time_utc": entry["since_time_utc"],
                "addresses": len(selected),
                "allocation_atto": sum(
                    row["allocation"] for row in selected
                ),
            }
        )
    qualifying_wallet_allocation = sum(
        row["allocation"] for row in wallet_rows
    )
    for entry in window_results:
        entry["share_of_qualifying_wallet_percent"] = percentage(
            entry["allocation_atto"], qualifying_wallet_allocation
        )
    initial_window = next(
        entry
        for entry in window_results
        if entry["months"] == args.initial_months
    )
    initial_rows = [row for row in wallet_rows if row["stage"] == "initial"]
    if (
        len(initial_rows) != initial_window["addresses"]
        or sum(row["allocation"] for row in initial_rows)
        != initial_window["allocation_atto"]
    ):
        raise ValueError("initial stage does not match activity window")

    existing_total = sum(existing.values())
    historical_total = sum(historical.values())
    contract_non_issuance_total = sum(
        group["not_issued_atto"] for group in contract_groups.values()
    )
    approved_contract_total = sum(
        group["allocation_atto"] for group in contract_groups.values()
    )
    gross_native = int(migration_summary["native_total_claim_atto"])
    total_exclusions = (
        existing_total
        + historical_total
        + retained_wone
        + contract_non_issuance_total
    )
    total_migration = gross_native - total_exclusions
    threshold_allocation = (
        qualifying_wallet_allocation + approved_contract_total
    )
    below_threshold = total_migration - threshold_allocation
    if below_threshold < 0:
        raise ValueError("threshold allocation exceeds total migration")
    older = sum(
        row["allocation"]
        for row in wallet_rows
        if row["time"] is not None and row["time"] < initial_since
    )
    no_activity = sum(
        row["allocation"] for row in wallet_rows if row["time"] is None
    )
    deferred_wallets = below_threshold + older + no_activity
    initial_total = sum(row["allocation"] for row in initial_rows)
    if (
        initial_total + approved_contract_total + deferred_wallets
        != total_migration
    ):
        raise ValueError("migration stage allocation does not close")
    if sum(int(row["gross_allocation_atto"]) for row in output_rows) != int(
        activity_summary["all_candidates"]["total_claim_atto"]
    ):
        raise ValueError("qualified gross allocation mismatch")
    for group, values in contract_groups.items():
        if (
            values["wallet_allocation_atto"]
            + values["staked_allocation_atto"]
            != values["allocation_atto"]
            or values["not_issued_wallet_atto"]
            + values["not_issued_staked_atto"]
            != values["not_issued_atto"]
        ):
            raise ValueError(f"{group} contract components do not close")

    approved_addresses = sum(
        contract_groups[group]["addresses"]
        for group in ("multisig", "layerzero_bridge_collateral", "onewallet")
    )
    excluded_addresses = sum(
        contract_groups[group]["addresses"]
        for group in ("smartvault", "other_reviewed_contract")
    )
    sources = {
        "qualified_activity": {
            "path": args.qualified_activity,
            "sha256": qualified_activity_sha256,
        },
        "activity_summary": {
            "path": args.activity_summary,
            "sha256": file_sha256(args.activity_summary),
        },
        "migration_summary": {
            "path": args.migration_summary,
            "sha256": file_sha256(args.migration_summary),
        },
        "contract_review": {
            "path": args.contract_review,
            "sha256": file_sha256(args.contract_review),
        },
        "existing_non_issuance": [
            {"path": path, "sha256": file_sha256(path)}
            for path in args.existing_non_issuance
        ],
        "historical_retention": [
            {"path": path, "sha256": file_sha256(path)}
            for path in args.historical_retention
        ],
        "manual_wallets": {
            "path": args.manual_wallets,
            "sha256": file_sha256(args.manual_wallets),
        },
    }
    result = {
        "schema_version": 1,
        "status": "passed",
        "policy": (
            "inclusive 1,000 ONE threshold after incident deductions; "
            "six-month initial wallet stage; "
            "confirmed exchange inventories override ordinary stages with "
            "manual delivery from the 2050 supply reserve (exchange_manual); "
            "reviewed multisig, LayerZero, and 1wallet allocations next stage; "
            "SmartVault and other reviewed contracts not issued"
        ),
        "activity_definition": activity_summary["activity_definition"],
        "cutoff_time_utc": cutoff_text,
        "initial_window": {
            "months": args.initial_months,
            "since_time_utc": initial_since.isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z"),
        },
        "sources": sources,
        "output": args.output,
        "output_sha256": "",
        "qualified_rows": len(output_rows),
        "exact_threshold_rows": exact_threshold_rows,
        "stage_rows": dict(sorted(stage_counts.items())),
        "issuance_treatment_rows": dict(sorted(treatment_counts.items())),
        "policy_state": policy_state,
        "validator_wrappers": {
            "reviewed_addresses": len(validators_seen),
            "initial_addresses": sum(row["validator"] for row in initial_rows),
        },
        "initial_wallets": {
            "addresses": len(initial_rows),
            "automatic_policy_addresses": sum(
                row["routing_category"] == "automatic_policy"
                for row in initial_rows
            ),
            "manual_routing_addresses": sum(
                row["routing_category"] == "exchange_or_manual"
                for row in initial_rows
            ),
            "allocation_atto": initial_total,
            "allocation_one": one(initial_total),
        },
        "qualifying_wallets": {
            "positive_addresses": len(wallet_rows),
            "allocation_atto": qualifying_wallet_allocation,
            "allocation_one": one(qualifying_wallet_allocation),
        },
        "contracts": {
            "reviewed_genuine_addresses": len(contract_seen),
            "approved": {
                "addresses": approved_addresses,
                "allocation_atto": approved_contract_total,
                "allocation_one": one(approved_contract_total),
                "wallet_allocation_atto": sum(
                    contract_groups[group]["wallet_allocation_atto"]
                    for group in (
                        "multisig",
                        "layerzero_bridge_collateral",
                        "onewallet",
                    )
                ),
                "staked_allocation_atto": sum(
                    contract_groups[group]["staked_allocation_atto"]
                    for group in (
                        "multisig",
                        "layerzero_bridge_collateral",
                        "onewallet",
                    )
                ),
            },
            "not_issued": {
                "addresses": excluded_addresses,
                "allocation_atto": contract_non_issuance_total,
                "allocation_one": one(contract_non_issuance_total),
                "wallet_allocation_atto": sum(
                    contract_groups[group]["not_issued_wallet_atto"]
                    for group in ("smartvault", "other_reviewed_contract")
                ),
                "staked_allocation_atto": sum(
                    contract_groups[group]["not_issued_staked_atto"]
                    for group in ("smartvault", "other_reviewed_contract")
                ),
            },
            "groups": {
                name: {
                    **values,
                    "allocation_one": one(values["allocation_atto"]),
                    "wallet_allocation_one": one(
                        values["wallet_allocation_atto"]
                    ),
                    "staked_allocation_one": one(
                        values["staked_allocation_atto"]
                    ),
                    "not_issued_one": one(values["not_issued_atto"]),
                    "not_issued_wallet_one": one(
                        values["not_issued_wallet_atto"]
                    ),
                    "not_issued_staked_one": one(
                        values["not_issued_staked_atto"]
                    ),
                }
                for name, values in sorted(contract_groups.items())
            },
        },
        "wallet_activity_windows": window_results,
        "deferred_wallets": {
            "below_threshold_atto": below_threshold,
            "below_threshold_one": one(below_threshold),
            "below_threshold_after_incident_deductions_addresses": (
                below_after_deductions["addresses"]
            ),
            "below_threshold_after_incident_deductions_atto": (
                below_after_deductions["allocation_atto"]
            ),
            "below_threshold_after_incident_deductions_one": one(
                below_after_deductions["allocation_atto"]
            ),
            "older_than_initial_window_atto": older,
            "older_than_initial_window_one": one(older),
            "no_indexed_activity_atto": no_activity,
            "no_indexed_activity_one": one(no_activity),
            "total_atto": deferred_wallets,
            "total_one": one(deferred_wallets),
        },
        "allocation": {
            "gross_native_snapshot_atto": gross_native,
            "gross_native_snapshot_one": one(gross_native),
            "existing_non_issuance_atto": existing_total,
            "existing_non_issuance_one": one(existing_total),
            "historical_retained_caps_atto": historical_total,
            "historical_retained_caps_one": one(historical_total),
            "wone_retained_not_issued_atto": retained_wone,
            "wone_retained_not_issued_one": one(retained_wone),
            "reviewed_contract_non_issuance_atto": contract_non_issuance_total,
            "reviewed_contract_non_issuance_one": one(
                contract_non_issuance_total
            ),
            "abandoned_contracts_public_aggregate_atto": (
                contract_non_issuance_total + retained_wone
            ),
            "abandoned_contracts_public_aggregate_one": one(
                contract_non_issuance_total + retained_wone
            ),
            "total_exclusions_atto": total_exclusions,
            "total_exclusions_one": one(total_exclusions),
            "initial_wallets_atto": initial_total,
            "initial_wallets_one": one(initial_total),
            "next_stage_contracts_atto": approved_contract_total,
            "next_stage_contracts_one": one(approved_contract_total),
            "deferred_wallets_atto": deferred_wallets,
            "deferred_wallets_one": one(deferred_wallets),
            "total_migration_atto": total_migration,
            "total_migration_one": one(total_migration),
            "outside_initial_atto": total_migration - initial_total,
            "outside_initial_one": one(total_migration - initial_total),
            "outside_initial_percent": percentage(
                total_migration - initial_total, total_migration
            ),
        },
    }
    twelve = next(
        (entry for entry in window_results if entry["months"] == 12), None
    )
    if twelve is not None:
        result["twelve_month_increment_atto"] = (
            twelve["allocation_atto"] - initial_total
        )
        result["twelve_month_increment_one"] = one(
            result["twelve_month_increment_atto"]
        )

    write_csv(args.output, output_rows, args.replace)
    result["output_sha256"] = file_sha256(args.output)
    write_text(
        args.summary,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        args.replace,
    )
    write_text(args.report, render_report(result), args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
