#!/usr/bin/env python3

"""Independently verify migration-stage policy and compiled routing closure."""

import argparse
import csv
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ATTO_PER_ONE = 10**18


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-policy", required=True)
    parser.add_argument("--stage-summary", required=True)
    parser.add_argument("--routing-summary", required=True)
    parser.add_argument("--output")
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


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


def load_json(path, label):
    with open(path, encoding="utf-8") as source:
        value = json.load(source)
    if value.get("status") not in {"passed", "ready", "hold"}:
        raise ValueError(f"{label} has invalid status")
    return value


def resolve_source(path, stage_summary_path):
    source = Path(path)
    candidates = [
        source,
        Path(stage_summary_path).resolve().parents[2] / source,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(path)


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


def main():
    args = parse_args()
    summary = load_json(args.stage_summary, "stage summary")
    routing = load_json(args.routing_summary, "routing summary")
    stage_hash = file_sha256(args.stage_policy)
    if summary["output_sha256"] != stage_hash:
        raise ValueError("stage summary does not identify stage policy")
    if routing["migration_stages_sha256"] != stage_hash:
        raise ValueError("routing summary does not identify stage policy")
    for name, record in summary["sources"].items():
        source = resolve_source(record["path"], args.stage_summary)
        if file_sha256(source) != record["sha256"]:
            raise ValueError(f"stage source hash mismatch: {name}")

    stages = defaultdict(lambda: {"addresses": 0, "allocation_atto": 0})
    treatments = Counter()
    groups = defaultdict(
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
    initial_routing = Counter()
    initial_validators = 0
    totals = Counter()
    seen = set()
    initial_since = parse_utc(summary["initial_window"]["since_time_utc"])
    with open(args.stage_policy, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "account_classification",
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
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"stage policy missing fields: {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            address = row["address"].lower()
            if address in seen:
                raise ValueError(f"duplicate stage address at line {line}")
            seen.add(address)
            if int(row["qualification_total_atto"]) < 1000 * ATTO_PER_ONE:
                raise ValueError(f"below-threshold row at line {line}")
            amounts = {
                field: int(row[field])
                for field in (
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
                )
            }
            if min(amounts.values()) < 0:
                raise ValueError(f"negative amount at line {line}")
            if (
                amounts["gross_allocation_atto"]
                - amounts["existing_non_issuance_atto"]
                - amounts["historical_retained_cap_atto"]
                - amounts["wone_source_offset_atto"]
                - amounts["reviewed_contract_non_issuance_atto"]
                != amounts["migration_allocation_atto"]
            ):
                raise ValueError(f"deduction closure failed at line {line}")
            if (
                amounts["migration_wallet_allocation_atto"]
                + amounts["migration_staked_to_vault_atto"]
                != amounts["migration_allocation_atto"]
            ):
                raise ValueError(f"component closure failed at line {line}")
            if (
                amounts["reviewed_contract_wallet_non_issuance_atto"]
                + amounts["reviewed_contract_staked_non_issuance_atto"]
                != amounts["reviewed_contract_non_issuance_atto"]
            ):
                raise ValueError(
                    f"contract component closure failed at line {line}"
                )
            stage = row["migration_stage"]
            treatment = row["issuance_treatment"]
            if treatment == "issue":
                if stage not in {"initial", "next_stage", "deferred"}:
                    raise ValueError(f"issued row has invalid stage at line {line}")
                if amounts["migration_allocation_atto"] <= 0:
                    raise ValueError(f"issued row has no amount at line {line}")
            elif treatment == "not_issued":
                if stage or amounts["migration_allocation_atto"]:
                    raise ValueError(f"not-issued row has stage at line {line}")
            else:
                raise ValueError(f"invalid treatment at line {line}")
            if stage:
                stages[stage]["addresses"] += 1
                stages[stage]["allocation_atto"] += amounts[
                    "migration_allocation_atto"
                ]
            treatments[treatment] += 1
            for field, amount in amounts.items():
                totals[field] += amount
            activity = (
                parse_utc(row["last_activity_time_utc"])
                if row["last_activity_time_utc"]
                else None
            )
            if stage == "initial":
                if (
                    row["account_classification"]
                    not in {"wallet", "validator_wallet"}
                    or activity is None
                    or activity < initial_since
                ):
                    raise ValueError(f"invalid initial row at line {line}")
                initial_routing[row["routing_category"]] += 1
                initial_validators += int(
                    row["account_classification"] == "validator_wallet"
                )
            elif (
                stage == "deferred"
                and row["account_classification"] in {"wallet", "validator_wallet"}
                and activity is not None
                and activity >= initial_since
            ):
                raise ValueError(f"active wallet deferred at line {line}")
            if row["account_classification"] == "genuine_contract":
                group = groups[row["policy_group"]]
                group["addresses"] += 1
                group["allocation_atto"] += amounts[
                    "migration_allocation_atto"
                ]
                group["wallet_allocation_atto"] += amounts[
                    "migration_wallet_allocation_atto"
                ]
                group["staked_allocation_atto"] += amounts[
                    "migration_staked_to_vault_atto"
                ]
                group["not_issued_atto"] += amounts[
                    "reviewed_contract_non_issuance_atto"
                ]
                group["not_issued_wallet_atto"] += amounts[
                    "reviewed_contract_wallet_non_issuance_atto"
                ]
                group["not_issued_staked_atto"] += amounts[
                    "reviewed_contract_staked_non_issuance_atto"
                ]
                if stage == "initial":
                    raise ValueError("genuine contract entered initial stage")

    if len(seen) != summary["qualified_rows"]:
        raise ValueError("qualified row count mismatch")
    if set(stages) != set(summary["stage_rows"]):
        raise ValueError("stage labels mismatch")
    for stage, values in stages.items():
        if values["addresses"] != summary["stage_rows"][stage]:
            raise ValueError(f"{stage} row count mismatch")
    if dict(sorted(treatments.items())) != summary["issuance_treatment_rows"]:
        raise ValueError("issuance-treatment count mismatch")
    for group, values in groups.items():
        expected = summary["contracts"]["groups"][group]
        if any(
            values[field] != expected[field]
            for field in (
                "addresses",
                "allocation_atto",
                "wallet_allocation_atto",
                "staked_allocation_atto",
                "not_issued_atto",
                "not_issued_wallet_atto",
                "not_issued_staked_atto",
            )
        ):
            raise ValueError(f"{group} totals mismatch")
    if (
        initial_routing["automatic_policy"]
        != summary["initial_wallets"]["automatic_policy_addresses"]
        or initial_routing["exchange_or_manual"]
        != summary["initial_wallets"]["manual_routing_addresses"]
        or initial_validators
        != summary["validator_wrappers"]["initial_addresses"]
    ):
        raise ValueError("initial composition mismatch")

    allocation = summary["allocation"]
    global_total = int(allocation["total_migration_atto"])
    if (
        int(allocation["gross_native_snapshot_atto"])
        - int(allocation["total_exclusions_atto"])
        != global_total
    ):
        raise ValueError("global allocation does not close")
    if (
        int(allocation["initial_wallets_atto"])
        + int(allocation["next_stage_contracts_atto"])
        + int(allocation["deferred_wallets_atto"])
        != global_total
    ):
        raise ValueError("stage allocation does not close")
    override_totals = Counter()
    exchange_override_total = 0
    for label, override in routing.get("stage_overrides", {}).items():
        source_stage = override["source_stage"]
        target_stage = override["target_stage"]
        wallet = int(override["wallet_airdrop_atto"])
        staked = int(override["staked_to_vault_atto"])
        total = int(override["total_claim_atto"])
        if (
            target_stage != "exchange_manual"
            or min(wallet, staked, total) < 0
            or wallet + staked != total
            or label != f"{source_stage}_to_{target_stage}"
        ):
            raise ValueError("invalid exchange stage override")
        override_totals[source_stage] += total
        exchange_override_total += total
    exchange_stage = routing["stage_totals"].get("exchange_manual")
    if (
        exchange_stage is None
        or int(exchange_stage["total_claim_atto"])
        != exchange_override_total
    ):
        raise ValueError("compiled exchange manual stage mismatch")
    exchange_readiness = routing["stage_readiness"].get(
        "exchange_manual"
    )
    if (
        exchange_readiness is None
        or exchange_readiness["status"] != "ready"
        or not exchange_readiness["release_authorized"]
    ):
        raise ValueError("exchange manual delivery stage is not ready")
    for stage in ("initial", "next_stage"):
        expected = int(
            allocation[
                "initial_wallets_atto"
                if stage == "initial"
                else "next_stage_contracts_atto"
            ]
        )
        if stages[stage]["allocation_atto"] != expected:
            raise ValueError(f"{stage} allocation mismatch")
        compiled_expected = expected - override_totals[stage]
        if (
            int(routing["stage_totals"][stage]["total_claim_atto"])
            != compiled_expected
        ):
            raise ValueError(f"compiled {stage} allocation mismatch")
    if (
        int(routing["stage_totals"]["deferred"]["total_claim_atto"])
        != stages["deferred"]["allocation_atto"]
        - override_totals["deferred"]
    ):
        raise ValueError("compiled qualified-deferred allocation mismatch")
    if (
        int(routing["not_issued_total_claim_atto"])
        != int(allocation["total_exclusions_atto"])
    ):
        raise ValueError("compiled non-issuance mismatch")
    if (
        routing["issuance_treatment_totals"]["not_issued"][
            "total_claim_atto"
        ]
        != routing["not_issued_total_claim_atto"]
    ):
        raise ValueError("compiled treatment total mismatch")
    if (
        routing["vault_stage_totals"]["not_issued_assets_atto"]
        != routing["not_issued_staked_to_vault_atto"]
    ):
        raise ValueError("compiled vault non-issuance mismatch")

    result = {
        "schema_version": 1,
        "status": "passed",
        "method": (
            "independent row-level stage, treatment, component, activity, "
            "contract, routing, and vault closure"
        ),
        "inputs": {
            "stage_policy": {
                "path": args.stage_policy,
                "sha256": stage_hash,
            },
            "stage_summary": {
                "path": args.stage_summary,
                "sha256": file_sha256(args.stage_summary),
            },
            "routing_summary": {
                "path": args.routing_summary,
                "sha256": file_sha256(args.routing_summary),
            },
        },
        "qualified_rows": len(seen),
        "stage_rows": {
            stage: values["addresses"]
            for stage, values in sorted(stages.items())
        },
        "issuance_treatment_rows": dict(sorted(treatments.items())),
        "initial_wallets": {
            "addresses": stages["initial"]["addresses"],
            "automatic_policy_addresses": initial_routing[
                "automatic_policy"
            ],
            "manual_routing_addresses": initial_routing[
                "exchange_or_manual"
            ],
            "validator_wrapper_addresses": initial_validators,
            "allocation_atto": str(stages["initial"]["allocation_atto"]),
        },
        "compiled_initial_wallets": {
            "addresses": (
                stages["initial"]["addresses"]
                - int(
                    routing.get("stage_overrides", {})
                    .get("initial_to_exchange_manual", {})
                    .get("addresses", 0)
                )
            ),
            "allocation_atto": routing["stage_totals"]["initial"][
                "total_claim_atto"
            ],
        },
        "exchange_manual": {
            "addresses": sum(
                int(value["addresses"])
                for value in routing.get("stage_overrides", {}).values()
            ),
            "allocation_atto": str(exchange_override_total),
            "status": exchange_readiness["status"],
            "release_authorized": exchange_readiness[
                "release_authorized"
            ],
        },
        "stage_overrides": routing.get("stage_overrides", {}),
        "next_stage_contract_allocation_atto": str(
            stages["next_stage"]["allocation_atto"]
        ),
        "qualified_deferred_wallet_allocation_atto": str(
            stages["deferred"]["allocation_atto"]
        ),
        "total_migration_allocation_atto": str(global_total),
        "total_exclusions_atto": allocation["total_exclusions_atto"],
        "initial_stage_status": routing["initial_stage_status"],
    }
    if args.output:
        write_json(args.output, result, args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
