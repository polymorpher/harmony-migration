#!/usr/bin/env python3

"""Merge reviewed wallet-theft additions into the non-issuance inventory."""

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


COMPONENTS = (
    "liquid_shard0",
    "liquid_shard1",
    "active_staked_or_delegated",
    "pending_undelegation",
    "unclaimed_staking_reward",
    "pending_cross_shard",
    "total_claim",
)
PERPETRATOR_FIELDS = (
    "case_id",
    "role",
    "address_bech32",
    "address_hex",
    "cutoff_row_present",
    "secure_key",
    *(f"{component}_atto" for component in COMPONENTS),
    *(f"{component}_one" for component in COMPONENTS),
    "evidence_basis",
    "source_transaction_hash",
    "source_transaction_eth_hash",
    "migration_action",
)
NON_ISSUANCE_FIELDS = (
    "address_bech32",
    "address_hex",
    "category",
    "cutoff_claim_atto",
    "cutoff_claim_one",
    "not_issued_atto",
    "not_issued_one",
    "remaining_original_address_allocation_atto",
    "remaining_original_address_allocation_one",
    "evidence",
    "decision",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--historical-inventory", required=True)
    parser.add_argument("--historical-perpetrators", required=True)
    parser.add_argument("--additions", required=True)
    parser.add_argument("--victims", required=True)
    parser.add_argument("--all-claims", required=True)
    parser.add_argument("--perpetrator-output", required=True)
    parser.add_argument("--perpetrator-summary", required=True)
    parser.add_argument("--victim-summary", required=True)
    parser.add_argument("--non-issuance-output", required=True)
    parser.add_argument("--non-issuance-summary", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def one_to_atto(value):
    return lib.one_str_to_atto(value)


def write_csv(path, fields, rows, replace):
    if os.path.exists(path + ".partial"):
        raise FileExistsError(path + ".partial")
    if os.path.exists(path) and not replace:
        raise FileExistsError(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(path + ".partial", path)


def write_json(path, value, replace):
    if os.path.exists(path + ".partial"):
        raise FileExistsError(path + ".partial")
    if os.path.exists(path) and not replace:
        raise FileExistsError(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path + ".partial", "x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(path + ".partial", path)


def load_additions(path):
    rows = []
    addresses = set()
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "case_id",
            "role",
            "address_bech32",
            "address_hex",
            "cutoff_claim_atto",
            "evidence_basis",
            "source_transaction_hash",
            "source_transaction_eth_hash",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"additions are missing fields: {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            address = lib.any_to_hex(row["address_hex"])
            if address != lib.bech32_to_hex(row["address_bech32"]):
                raise ValueError(
                    f"addition line {line} address forms differ"
                )
            if address in addresses:
                raise ValueError(f"duplicate addition at line {line}")
            if row["role"] not in {
                "reported_perpetrator",
                "report_linked_theft_recipient",
            }:
                raise ValueError(f"invalid addition role at line {line}")
            expected = int(row["cutoff_claim_atto"])
            if lib.atto_to_one_str(expected) != row["cutoff_claim_one"]:
                raise ValueError(
                    f"addition line {line} amount rendering differs"
                )
            addresses.add(address)
            rows.append({**row, "address_hex": address})
    return rows


def load_victims(path):
    rows = []
    addresses = set()
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "case_id",
            "victim",
            "address_bech32",
            "total_native_one",
            "migration_treatment",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"victims are missing fields: {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            address = lib.bech32_to_hex(row["address_bech32"])
            if address is None or address in addresses:
                raise ValueError(
                    f"invalid or duplicate victim at line {line}"
                )
            expected = one_to_atto(row["total_native_one"])
            addresses.add(address)
            rows.append(
                {
                    **row,
                    "address_hex": address,
                    "expected_total_claim_atto": expected,
                }
            )
    return rows


def load_claim_rows(path, targets):
    rows = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "secure_key",
            "address",
            *(f"{component}_atto" for component in COMPONENTS),
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"all-claims CSV is missing fields: {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            address = lib.any_to_hex(row["address"])
            if address not in targets:
                continue
            if address in rows:
                raise ValueError(f"duplicate claim row at line {line}")
            lib.require_address_secure_key(
                row["address"],
                row["secure_key"],
                f"all-claims line {line}",
            )
            rows[address] = row
    return rows


def component_values(row):
    return {
        component: int(row[f"{component}_atto"])
        for component in COMPONENTS
    }


def serializable_components(totals):
    result = {}
    for component in COMPONENTS:
        result[f"{component}_atto"] = str(totals[component])
        result[f"{component}_one"] = lib.atto_to_one_str(
            totals[component]
        )
    return result


def main():
    args = parse_args()
    additions = load_additions(args.additions)
    victims = load_victims(args.victims)
    addition_addresses = {row["address_hex"] for row in additions}
    victim_addresses = {row["address_hex"] for row in victims}
    if addition_addresses & victim_addresses:
        raise ValueError("victim and addition address sets overlap")
    targets = addition_addresses | victim_addresses
    claims = load_claim_rows(args.all_claims, targets)

    addition_totals = defaultdict(int)
    for row in additions:
        address = row["address_hex"]
        if address not in claims:
            raise ValueError(f"addition is absent from claims: {address}")
        values = component_values(claims[address])
        if values["total_claim"] != int(row["cutoff_claim_atto"]):
            raise ValueError(f"addition claim differs for {address}")
        if any(
            values[component]
            for component in COMPONENTS
            if component not in {"liquid_shard0", "total_claim"}
        ):
            raise ValueError(f"addition has unexpected component: {address}")
        if values["liquid_shard0"] != values["total_claim"]:
            raise ValueError(f"addition is not shard-0 liquid: {address}")
        for component, value in values.items():
            addition_totals[component] += value

    victim_total = 0
    victim_positive = 0
    for row in victims:
        expected = row["expected_total_claim_atto"]
        claim = claims.get(row["address_hex"])
        actual = int(claim["total_claim_atto"]) if claim else 0
        if actual != expected:
            raise ValueError(
                f"victim claim differs for {row['address_bech32']}"
            )
        victim_total += actual
        victim_positive += int(actual > 0)

    perpetrator_rows = []
    base_addresses = set()
    base_totals = defaultdict(int)
    with open(args.historical_perpetrators, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            address = lib.any_to_hex(row["address_hex"])
            if address in base_addresses or address in addition_addresses:
                raise ValueError(
                    f"duplicate perpetrator address at line {line}"
                )
            base_addresses.add(address)
            values = {
                component: int(row[f"{component}_atto"] or 0)
                for component in COMPONENTS
            }
            for component, value in values.items():
                base_totals[component] += value
            perpetrator_rows.append(
                {
                    "case_id": f"historical-{row['input_order']}",
                    "role": "reported_perpetrator",
                    "address_bech32": row["address_bech32"],
                    "address_hex": address,
                    "cutoff_row_present": row["cutoff_row_present"],
                    "secure_key": row["secure_key"],
                    **{
                        f"{component}_atto": str(values[component])
                        for component in COMPONENTS
                    },
                    **{
                        f"{component}_one": lib.atto_to_one_str(
                            values[component]
                        )
                        for component in COMPONENTS
                    },
                    "evidence_basis": "historical 17-address report inventory",
                    "source_transaction_hash": "",
                    "source_transaction_eth_hash": "",
                    "migration_action": "not_issuing",
                }
            )
    for row in additions:
        claim = claims[row["address_hex"]]
        values = component_values(claim)
        perpetrator_rows.append(
            {
                "case_id": row["case_id"],
                "role": row["role"],
                "address_bech32": row["address_bech32"],
                "address_hex": row["address_hex"],
                "cutoff_row_present": "true",
                "secure_key": claim["secure_key"].lower(),
                **{
                    f"{component}_atto": str(values[component])
                    for component in COMPONENTS
                },
                **{
                    f"{component}_one": lib.atto_to_one_str(
                        values[component]
                    )
                    for component in COMPONENTS
                },
                "evidence_basis": row["evidence_basis"],
                "source_transaction_hash": row[
                    "source_transaction_hash"
                ],
                "source_transaction_eth_hash": row[
                    "source_transaction_eth_hash"
                ],
                "migration_action": "not_issuing",
            }
        )
    if base_addresses & victim_addresses:
        raise ValueError(
            "historical perpetrator and victim address sets overlap"
        )
    perpetrator_rows.sort(
        key=lambda row: (row["role"], row["address_hex"])
    )

    total_components = {
        component: base_totals[component] + addition_totals[component]
        for component in COMPONENTS
    }
    role_stats = {}
    for role in ("reported_perpetrator", "report_linked_theft_recipient"):
        rows = [row for row in perpetrator_rows if row["role"] == role]
        totals = {
            component: sum(
                int(row[f"{component}_atto"]) for row in rows
            )
            for component in COMPONENTS
        }
        role_stats[role] = {
            "addresses": len(rows),
            "positive_claim_addresses": sum(
                int(row["total_claim_atto"]) > 0 for row in rows
            ),
            **serializable_components(totals),
        }

    non_issuance_rows = []
    non_issuance_addresses = set()
    historical_total = 0
    with open(args.historical_inventory, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            address = lib.any_to_hex(row["address_hex"])
            if address in non_issuance_addresses:
                raise ValueError(
                    f"duplicate historical inventory at line {line}"
                )
            if address in victim_addresses:
                raise ValueError(
                    "reported victim already has a non-issuance route: "
                    f"{address}"
                )
            non_issuance_addresses.add(address)
            amount = int(row["treasury_reclaim_atto"])
            historical_total += amount
            non_issuance_rows.append(
                {
                    "address_bech32": row["address_bech32"],
                    "address_hex": address,
                    "category": row["category"],
                    "cutoff_claim_atto": row["cutoff_claim_atto"],
                    "cutoff_claim_one": row["cutoff_claim_one"],
                    "not_issued_atto": str(amount),
                    "not_issued_one": lib.atto_to_one_str(amount),
                    "remaining_original_address_allocation_atto": row[
                        "remaining_original_address_allocation_atto"
                    ],
                    "remaining_original_address_allocation_one": row[
                        "remaining_original_address_allocation_one"
                    ],
                    "evidence": (
                        "historical treasury-reclaim-inventory.csv"
                    ),
                    "decision": (
                        "preserve exact previously selected amount; "
                        "change destination outcome to not_issuing"
                    ),
                }
            )
    for row in additions:
        address = row["address_hex"]
        if address in non_issuance_addresses:
            raise ValueError(f"addition already in inventory: {address}")
        non_issuance_addresses.add(address)
        amount = int(row["cutoff_claim_atto"])
        category = (
            "reported_wallet_theft_perpetrator"
            if row["role"] == "reported_perpetrator"
            else "report_linked_theft_recipient"
        )
        non_issuance_rows.append(
            {
                "address_bech32": row["address_bech32"],
                "address_hex": address,
                "category": category,
                "cutoff_claim_atto": str(amount),
                "cutoff_claim_one": lib.atto_to_one_str(amount),
                "not_issued_atto": str(amount),
                "not_issued_one": lib.atto_to_one_str(amount),
                "remaining_original_address_allocation_atto": "0",
                "remaining_original_address_allocation_one": (
                    "0.000000000000000000"
                ),
                "evidence": (
                    f"wallet-theft-inventory-additions-20260916.csv:"
                    f"{row['case_id']}"
                ),
                "decision": (
                    "do not issue the full existing cutoff claim; "
                    "no balance is added to gross supply"
                ),
            }
        )
    non_issuance_rows.sort(
        key=lambda row: (row["category"], row["address_hex"])
    )
    category_stats = {}
    for category in sorted(
        {row["category"] for row in non_issuance_rows}
    ):
        rows = [
            row for row in non_issuance_rows
            if row["category"] == category
        ]
        category_stats[category] = {
            "inventory_rows": len(rows),
            "routes": sum(int(row["not_issued_atto"]) > 0 for row in rows),
            "not_issued_atto": str(
                sum(int(row["not_issued_atto"]) for row in rows)
            ),
        }
    new_total = sum(
        int(row["not_issued_atto"]) for row in non_issuance_rows
    )

    write_csv(
        args.perpetrator_output,
        PERPETRATOR_FIELDS,
        perpetrator_rows,
        args.replace,
    )
    perpetrator_summary = {
        "status": "passed",
        "cutoff": {"shard0": 93623067, "shard1": 95882100},
        "historical_perpetrators": args.historical_perpetrators,
        "historical_perpetrators_sha256": file_sha256(
            args.historical_perpetrators
        ),
        "additions": args.additions,
        "additions_sha256": file_sha256(args.additions),
        "addresses": len(perpetrator_rows),
        "positive_claim_addresses": sum(
            int(row["total_claim_atto"]) > 0
            for row in perpetrator_rows
        ),
        "roles": role_stats,
        "addition_delta": serializable_components(addition_totals),
        "totals": serializable_components(total_components),
        "output": args.perpetrator_output,
        "output_sha256": file_sha256(args.perpetrator_output),
    }
    write_json(
        args.perpetrator_summary,
        perpetrator_summary,
        args.replace,
    )
    victim_summary = {
        "status": "passed",
        "policy": (
            "separate victim population; no automatic non-issuance route"
        ),
        "input": args.victims,
        "input_sha256": file_sha256(args.victims),
        "addresses": len(victims),
        "positive_claim_addresses": victim_positive,
        "total_claim_atto": str(victim_total),
        "total_claim_one": lib.atto_to_one_str(victim_total),
    }
    write_json(args.victim_summary, victim_summary, args.replace)
    non_issuance_summary = {
        "status": "passed",
        "policy": (
            "preserve historical exact amounts and add the full existing "
            "claims of four reviewed perpetrator-related addresses"
        ),
        "historical_inventory": args.historical_inventory,
        "historical_inventory_sha256": file_sha256(
            args.historical_inventory
        ),
        "wallet_theft_additions": args.additions,
        "wallet_theft_additions_sha256": file_sha256(args.additions),
        "wallet_theft_perpetrator_output": args.perpetrator_output,
        "wallet_theft_perpetrator_summary": args.perpetrator_summary,
        "wallet_theft_victims": args.victims,
        "wallet_theft_victim_summary": args.victim_summary,
        "historical_not_issued_atto": str(historical_total),
        "wallet_theft_addition_atto": str(
            addition_totals["total_claim"]
        ),
        "victim_addresses_not_routed": len(victims),
        "victim_total_claim_atto_not_routed": str(victim_total),
        "inventory_rows": len(non_issuance_rows),
        "categories": category_stats,
        "totals_atto": {"not_issued": str(new_total)},
        "totals_one": {
            "not_issued": lib.atto_to_one_str(new_total)
        },
        "global_claim_supply_changed": False,
        "output": args.non_issuance_output,
        "output_sha256": "",
    }
    write_csv(
        args.non_issuance_output,
        NON_ISSUANCE_FIELDS,
        non_issuance_rows,
        args.replace,
    )
    # Recompute the output hash after the atomic CSV write.
    non_issuance_summary["output_sha256"] = file_sha256(
        args.non_issuance_output
    )
    write_json(
        args.non_issuance_summary,
        non_issuance_summary,
        args.replace,
    )
    print(
        json.dumps(
            {
                "perpetrator_summary": perpetrator_summary,
                "victim_summary": victim_summary,
                "non_issuance_summary": non_issuance_summary,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
