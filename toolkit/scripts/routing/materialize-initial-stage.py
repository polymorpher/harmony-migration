#!/usr/bin/env python3

"""Materialize and verify initial-stage wallet and vault allocations."""

import argparse
import csv
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path


WALLET_FIELDS = (
    "source_secure_key",
    "source_address",
    "destination_id",
    "destination_address",
    "destination_status",
    "amount_atto",
    "delivery_method",
    "reason",
    "evidence",
)
SHARE_FIELDS = (
    "validator_secure_key",
    "validator_address",
    "source_secure_key",
    "source_address",
    "beneficiary_address",
    "destination_id",
    "destination_status",
    "amount_atto",
    "delivery_method",
    "reason",
    "evidence",
)
VAULT_FIELDS = (
    "validator_secure_key",
    "validator_address",
    "initial_assets_atto",
    "governor_address",
    "governor_status",
    "governor_reason",
    "governor_evidence",
)
UNRESOLVED_FIELDS = (
    "item_type",
    "source_address",
    "validator_address",
    "amount_atto",
    "reason",
    "evidence",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-policy", required=True)
    parser.add_argument("--base-priority-shares", required=True)
    parser.add_argument("--routing-exceptions", required=True)
    parser.add_argument("--routing-summary", required=True)
    parser.add_argument("--vault-stages", required=True)
    parser.add_argument("--governor-exceptions", required=True)
    parser.add_argument("--wallet-output", required=True)
    parser.add_argument("--shares-output", required=True)
    parser.add_argument("--vault-output", required=True)
    parser.add_argument("--unresolved-output", required=True)
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


def normalize_address(value, context):
    address = value.strip().lower()
    if (
        len(address) != 42
        or not address.startswith("0x")
        or any(character not in "0123456789abcdef" for character in address[2:])
    ):
        raise ValueError(f"{context}: invalid address")
    return address


def write_csv(path, fields, rows, replace):
    path = Path(path)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output, fieldnames=fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def write_json(path, value, replace):
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


def write_text(path, value, replace):
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


def one(value):
    whole, fraction = divmod(int(value), 10**18)
    return f"{whole:,}.{fraction:018d}"


def render_report(result):
    return f"""# Initial-stage materialization

This plan contains only `migration_stage = initial` and
`issuance_treatment = issue`. It expands implicit same-address wallet and vault
share delivery and excludes every exchange-manual (delivered from the 2050
supply reserve), next-stage, deferred, manual-review, not-issued, and
redistributed amount.

- Status: `{result["status"]}`
- Source wallet addresses: `{result["source_addresses"]:,}`
- Wallet allocation: `{one(result["wallet_allocation_atto"])} ONE`
- Vault-share allocation: `{one(result["vault_share_allocation_atto"])} ONE`
- Validator vaults: `{result["validator_vaults"]:,}`
- Unresolved rows: `{result["unresolved_rows"]:,}`
- Pending initial-stage policy decisions:
  `{", ".join(result["pending_policy_decisions"]) or "none"}`

Outputs:

- Wallet allocations: `{result["outputs"]["wallets"]["path"]}`
- Vault shares: `{result["outputs"]["vault_shares"]["path"]}`
- Validator vaults: `{result["outputs"]["validator_vaults"]["path"]}`
- Initial-stage unresolved queue: `{result["outputs"]["unresolved"]["path"]}`

The plan is not releasable while status is `hold`. Later-stage holds do not
appear here and do not by themselves affect this status.
"""


def load_routing_summary(path, args):
    with open(path, encoding="utf-8") as source:
        summary = json.load(source)
    if summary.get("status") not in {"ready", "hold"}:
        raise ValueError("routing summary has invalid status")
    if summary.get("migration_stages_sha256") != file_sha256(
        args.stage_policy
    ):
        raise ValueError("routing summary does not identify stage policy")
    expected_outputs = {
        "routing_exceptions": args.routing_exceptions,
        "governor_exceptions": args.governor_exceptions,
        "vault_stages": args.vault_stages,
    }
    for label, path_value in expected_outputs.items():
        record = summary["outputs"][label]
        if (
            record["path"] != path_value
            or record["sha256"] != file_sha256(path_value)
        ):
            raise ValueError(
                f"routing summary does not identify {label} output"
            )
    if "initial" not in summary.get("stage_readiness", {}):
        raise ValueError("routing summary has no initial-stage readiness")
    return summary


def load_stages(path):
    all_stages = {}
    initial = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "secure_key",
            "address",
            "account_classification",
            "routing_category",
            "migration_stage",
            "issuance_treatment",
            "migration_wallet_allocation_atto",
            "migration_staked_to_vault_atto",
            "migration_allocation_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            address = normalize_address(row["address"], f"{path}:{line}")
            if address in all_stages:
                raise ValueError(f"{path}:{line}: duplicate address")
            wallet = int(row["migration_wallet_allocation_atto"])
            staked = int(row["migration_staked_to_vault_atto"])
            total = int(row["migration_allocation_atto"])
            if min(wallet, staked, total) < 0 or wallet + staked != total:
                raise ValueError(f"{path}:{line}: allocation mismatch")
            record = {
                "key": row["secure_key"].lower(),
                "address": address,
                "classification": row["account_classification"],
                "routing_category": row["routing_category"],
                "stage": row["migration_stage"],
                "treatment": row["issuance_treatment"],
                "wallet": wallet,
                "staked": staked,
                "total": total,
            }
            all_stages[address] = record
            if record["stage"] == "initial":
                if record["treatment"] != "issue" or total <= 0:
                    raise ValueError(
                        f"{path}:{line}: invalid initial allocation"
                    )
                initial[address] = record
    return all_stages, initial


def load_base_shares(path, initial):
    positions = {}
    by_source = defaultdict(int)
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "validator_secure_key",
            "validator_address",
            "delegator_secure_key",
            "delegator_address",
            "staked_to_vault_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            source_address = normalize_address(
                row["delegator_address"], f"{path}:{line} delegator"
            )
            if source_address not in initial:
                continue
            validator = normalize_address(
                row["validator_address"], f"{path}:{line} validator"
            )
            pair = (source_address, validator)
            if pair in positions:
                raise ValueError(f"{path}:{line}: duplicate position")
            amount = int(row["staked_to_vault_atto"])
            if amount <= 0:
                raise ValueError(f"{path}:{line}: non-positive position")
            positions[pair] = {
                "validator_key": row["validator_secure_key"].lower(),
                "validator_address": validator,
                "source_key": row["delegator_secure_key"].lower(),
                "source_address": source_address,
                "amount": amount,
            }
            by_source[source_address] += amount
    for address, stage in initial.items():
        if by_source[address] < stage["staked"]:
            raise ValueError(
                f"base shares are below initial stage for {address}"
            )
    return positions


def load_exceptions(path, all_stages, initial):
    wallet_issue = defaultdict(list)
    share_routes = defaultdict(list)
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "component",
            "source_secure_key",
            "source_address",
            "validator_secure_key",
            "validator_address",
            "amount_atto",
            "migration_stage",
            "issuance_treatment",
            "destination_id",
            "destination_address",
            "destination_status",
            "reason",
            "evidence",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            source_address = normalize_address(
                row["source_address"], f"{path}:{line} source"
            )
            stage = all_stages.get(source_address)
            exchange_route = (
                row["reason"] == "exchange_manual_reserve_delivery"
            )
            if stage is not None and row["migration_stage"] != stage["stage"]:
                if exchange_route and row["migration_stage"] == "exchange_manual":
                    # Exchange wallets leave the airdrop entirely; they are
                    # delivered manually from the 2050 supply reserve.
                    initial.pop(source_address, None)
                else:
                    raise ValueError(
                        f"{path}:{line}: migration stage mismatch"
                    )
            treatment = row["issuance_treatment"]
            if exchange_route:
                if treatment != "manual_from_reserve" or row[
                    "destination_status"
                ] not in {"exchange_manual", "hold"}:
                    raise ValueError(
                        f"{path}:{line}: exchange route treatment/status mismatch"
                    )
                initial.pop(source_address, None)
            else:
                expected_treatment = {
                    "ready": "issue",
                    "hold": "issue",
                    "not_issuing": "not_issued",
                    "redistributed": "redistributed",
                }.get(row["destination_status"])
                if treatment != expected_treatment:
                    raise ValueError(
                        f"{path}:{line}: treatment/status mismatch"
                    )
            if source_address not in initial:
                continue
            amount = int(row["amount_atto"])
            if amount <= 0:
                raise ValueError(f"{path}:{line}: non-positive amount")
            record = {
                **row,
                "amount": amount,
                "source_address_normalized": source_address,
            }
            if row["component"] == "wallet_airdrop":
                if treatment == "issue":
                    wallet_issue[source_address].append(record)
            elif row["component"] == "vault_shares":
                validator = normalize_address(
                    row["validator_address"], f"{path}:{line} validator"
                )
                share_routes[(source_address, validator)].append(record)
            else:
                raise ValueError(f"{path}:{line}: invalid component")
    return wallet_issue, share_routes


def materialize_wallets(initial, wallet_issue):
    rows = []
    for address, stage in sorted(initial.items()):
        explicit = wallet_issue[address]
        explicit_total = sum(row["amount"] for row in explicit)
        implicit = stage["wallet"] - explicit_total
        if implicit < 0:
            raise ValueError(f"explicit wallet routes exceed {address}")
        for row in explicit:
            rows.append(
                {
                    "source_secure_key": stage["key"],
                    "source_address": address,
                    "destination_id": row["destination_id"],
                    "destination_address": row["destination_address"],
                    "destination_status": row["destination_status"],
                    "amount_atto": str(row["amount"]),
                    "delivery_method": "explicit_route",
                    "reason": row["reason"],
                    "evidence": row["evidence"],
                }
            )
        if implicit:
            if (
                stage["routing_category"] != "automatic_policy"
                or stage["classification"] != "wallet"
            ):
                raise ValueError(
                    f"non-automatic initial wallet has implicit delivery: {address}"
                )
            rows.append(
                {
                    "source_secure_key": stage["key"],
                    "source_address": address,
                    "destination_id": "",
                    "destination_address": address,
                    "destination_status": "ready",
                    "amount_atto": str(implicit),
                    "delivery_method": "implicit_same_address",
                    "reason": "ordinary code-less wallet",
                    "evidence": "",
                }
            )
    rows.sort(
        key=lambda row: (
            row["source_secure_key"],
            row["destination_status"],
            row["destination_address"],
            row["destination_id"],
        )
    )
    return rows


def materialize_shares(initial, positions, share_routes):
    rows = []
    for pair, position in sorted(
        positions.items(),
        key=lambda item: (item[1]["validator_key"], item[1]["source_key"]),
    ):
        source_address, _validator = pair
        routes = share_routes[pair]
        consumed = sum(row["amount"] for row in routes)
        if consumed > position["amount"]:
            raise ValueError(f"share routes exceed base position {pair}")
        issued_explicit = 0
        for row in routes:
            if row["issuance_treatment"] != "issue":
                continue
            issued_explicit += row["amount"]
            rows.append(
                {
                    "validator_secure_key": position["validator_key"],
                    "validator_address": position["validator_address"],
                    "source_secure_key": position["source_key"],
                    "source_address": source_address,
                    "beneficiary_address": row["destination_address"],
                    "destination_id": row["destination_id"],
                    "destination_status": row["destination_status"],
                    "amount_atto": str(row["amount"]),
                    "delivery_method": "explicit_route",
                    "reason": row["reason"],
                    "evidence": row["evidence"],
                }
            )
        implicit = position["amount"] - consumed
        if implicit:
            stage = initial[source_address]
            if stage["routing_category"] != "automatic_policy":
                raise ValueError(
                    f"manual initial share has implicit delivery: {pair}"
                )
            rows.append(
                {
                    "validator_secure_key": position["validator_key"],
                    "validator_address": position["validator_address"],
                    "source_secure_key": position["source_key"],
                    "source_address": source_address,
                    "beneficiary_address": source_address,
                    "destination_id": "",
                    "destination_status": "ready",
                    "amount_atto": str(implicit),
                    "delivery_method": "implicit_same_address",
                    "reason": "ordinary wallet vault shares",
                    "evidence": "",
                }
            )
            issued_explicit += implicit
        expected = sum(
            row["amount"]
            for row in routes
            if row["issuance_treatment"] == "issue"
        ) + implicit
        if expected != issued_explicit:
            raise ValueError(f"issued share arithmetic mismatch for {pair}")
    by_source = defaultdict(int)
    for row in rows:
        by_source[row["source_address"]] += int(row["amount_atto"])
    for address, stage in initial.items():
        if by_source[address] != stage["staked"]:
            raise ValueError(
                f"materialized shares do not match initial stage for {address}"
            )
    return rows


def load_governors(path):
    governors = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            validator = normalize_address(
                row["validator_address"], f"{path}:{line}"
            )
            if validator in governors:
                raise ValueError(f"{path}:{line}: duplicate validator")
            governors[validator] = row
    return governors


def materialize_vaults(path, shares, governors):
    share_totals = defaultdict(int)
    for row in shares:
        share_totals[row["validator_address"]] += int(row["amount_atto"])
    rows = []
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "validator_secure_key",
            "validator_address",
            "initial_assets_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            validator = normalize_address(
                row["validator_address"], f"{path}:{line}"
            )
            assets = int(row["initial_assets_atto"])
            if assets < 0 or share_totals.get(validator, 0) != assets:
                raise ValueError(
                    f"{path}:{line}: initial vault assets do not match shares"
                )
            if assets == 0:
                continue
            governor = governors.get(validator)
            rows.append(
                {
                    "validator_secure_key": row[
                        "validator_secure_key"
                    ].lower(),
                    "validator_address": validator,
                    "initial_assets_atto": str(assets),
                    "governor_address": (
                        governor["destination_address"]
                        if governor
                        else validator
                    ),
                    "governor_status": (
                        governor["destination_status"]
                        if governor
                        else "ready"
                    ),
                    "governor_reason": governor["reason"] if governor else "",
                    "governor_evidence": (
                        governor["evidence"] if governor else ""
                    ),
                }
            )
    missing = set(share_totals) - {
        row["validator_address"] for row in rows
    }
    if missing:
        raise ValueError(
            "initial shares have no positive vault row: "
            + ", ".join(sorted(missing))
        )
    rows.sort(key=lambda row: row["validator_secure_key"])
    return rows


def build_unresolved(wallets, shares, vaults, readiness):
    rows = []
    for row in wallets:
        if row["destination_status"] == "hold":
            rows.append(
                {
                    "item_type": "wallet_delivery",
                    "source_address": row["source_address"],
                    "validator_address": "",
                    "amount_atto": row["amount_atto"],
                    "reason": row["reason"],
                    "evidence": row["evidence"],
                }
            )
    for row in shares:
        if row["destination_status"] == "hold":
            rows.append(
                {
                    "item_type": "vault_share_delivery",
                    "source_address": row["source_address"],
                    "validator_address": row["validator_address"],
                    "amount_atto": row["amount_atto"],
                    "reason": row["reason"],
                    "evidence": row["evidence"],
                }
            )
    for row in vaults:
        if row["governor_status"] == "hold":
            rows.append(
                {
                    "item_type": "validator_governor",
                    "source_address": "",
                    "validator_address": row["validator_address"],
                    "amount_atto": row["initial_assets_atto"],
                    "reason": row["governor_reason"],
                    "evidence": row["governor_evidence"],
                }
            )
    for decision in readiness["pending_policy_decisions"]:
        rows.append(
            {
                "item_type": "policy_gate",
                "source_address": "",
                "validator_address": "",
                "amount_atto": "0",
                "reason": decision,
                "evidence": "",
            }
        )
    rows.sort(
        key=lambda row: (
            row["item_type"],
            row["source_address"],
            row["validator_address"],
            row["reason"],
        )
    )
    return rows


def main():
    args = parse_args()
    routing = load_routing_summary(args.routing_summary, args)
    all_stages, initial = load_stages(args.stage_policy)
    wallet_issue, share_routes = load_exceptions(
        args.routing_exceptions, all_stages, initial
    )
    positions = load_base_shares(args.base_priority_shares, initial)
    wallets = materialize_wallets(initial, wallet_issue)
    shares = materialize_shares(initial, positions, share_routes)
    governors = load_governors(args.governor_exceptions)
    vaults = materialize_vaults(args.vault_stages, shares, governors)
    readiness = routing["stage_readiness"]["initial"]
    unresolved = build_unresolved(wallets, shares, vaults, readiness)

    wallet_total = sum(int(row["amount_atto"]) for row in wallets)
    share_total = sum(int(row["amount_atto"]) for row in shares)
    stage_wallet = sum(row["wallet"] for row in initial.values())
    stage_staked = sum(row["staked"] for row in initial.values())
    if wallet_total != stage_wallet or share_total != stage_staked:
        raise ValueError("materialized initial totals do not match stage policy")
    if (
        str(wallet_total) != readiness["wallet_airdrop_atto"]
        or str(share_total) != readiness["staked_to_vault_atto"]
    ):
        raise ValueError("materialized totals do not match routing readiness")
    held_wallet = sum(
        int(row["amount_atto"])
        for row in wallets
        if row["destination_status"] == "hold"
    )
    held_shares = sum(
        int(row["amount_atto"])
        for row in shares
        if row["destination_status"] == "hold"
    )
    held_governors = sum(
        int(row["initial_assets_atto"])
        for row in vaults
        if row["governor_status"] == "hold"
    )
    if (
        str(held_wallet) != readiness["held_wallet_airdrop_atto"]
        or str(held_shares) != readiness["held_staked_to_vault_atto"]
        or str(held_governors)
        != readiness["held_validator_governor_assets_atto"]
    ):
        raise ValueError("materialized holds do not match stage readiness")
    computed_status = "ready" if not unresolved else "hold"
    if computed_status != readiness["status"]:
        raise ValueError("materialized status does not match stage readiness")

    write_csv(args.wallet_output, WALLET_FIELDS, wallets, args.replace)
    write_csv(args.shares_output, SHARE_FIELDS, shares, args.replace)
    write_csv(args.vault_output, VAULT_FIELDS, vaults, args.replace)
    write_csv(
        args.unresolved_output,
        UNRESOLVED_FIELDS,
        unresolved,
        args.replace,
    )
    result = {
        "schema_version": 1,
        "status": computed_status,
        "migration_stage": "initial",
        "issuance_treatment": "issue",
        "source_addresses": len(initial),
        "wallet_rows": len(wallets),
        "wallet_allocation_atto": str(wallet_total),
        "vault_share_rows": len(shares),
        "vault_share_allocation_atto": str(share_total),
        "validator_vaults": len(vaults),
        "unresolved_rows": len(unresolved),
        "pending_policy_decisions": readiness["pending_policy_decisions"],
        "inputs": {
            "stage_policy": {
                "path": args.stage_policy,
                "sha256": file_sha256(args.stage_policy),
            },
            "base_priority_shares": {
                "path": args.base_priority_shares,
                "sha256": file_sha256(args.base_priority_shares),
            },
            "routing_exceptions": {
                "path": args.routing_exceptions,
                "sha256": file_sha256(args.routing_exceptions),
            },
            "routing_summary": {
                "path": args.routing_summary,
                "sha256": file_sha256(args.routing_summary),
            },
            "vault_stages": {
                "path": args.vault_stages,
                "sha256": file_sha256(args.vault_stages),
            },
            "governor_exceptions": {
                "path": args.governor_exceptions,
                "sha256": file_sha256(args.governor_exceptions),
            },
        },
        "outputs": {
            "wallets": {
                "path": args.wallet_output,
                "sha256": file_sha256(args.wallet_output),
            },
            "vault_shares": {
                "path": args.shares_output,
                "sha256": file_sha256(args.shares_output),
            },
            "validator_vaults": {
                "path": args.vault_output,
                "sha256": file_sha256(args.vault_output),
            },
            "unresolved": {
                "path": args.unresolved_output,
                "sha256": file_sha256(args.unresolved_output),
            },
        },
    }
    write_json(args.summary, result, args.replace)
    write_text(args.report, render_report(result), args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
