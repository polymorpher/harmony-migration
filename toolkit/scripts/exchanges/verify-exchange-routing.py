#!/usr/bin/env python3

"""Independently verify exchange plans against compiled routing exceptions."""

import argparse
import csv
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--exchange-summary", required=True)
    parser.add_argument("--audits-dir", required=True)
    parser.add_argument("--routes", required=True)
    parser.add_argument("--routing-exceptions", required=True)
    parser.add_argument("--routing-summary", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def load_policy(path):
    with open(path, encoding="utf-8") as source:
        policy = json.load(source)
    if policy.get("schema_version") != 2:
        raise ValueError("unsupported exchange policy schema")
    return policy


EXCHANGE_ROUTE_REASON = "exchange_manual_reserve_delivery"
EXCHANGE_STAGE = "exchange_manual"
EXCHANGE_TREATMENT = "manual_from_reserve"
EXCHANGE_STATUS = "exchange_manual"


def load_expected(policy, audits_dir):
    expected = {}
    for config in policy["exchanges"]:
        exchange_id = config["id"]
        path = Path(audits_dir) / f"{exchange_id}.csv"
        sources = {}
        with path.open(newline="") as source:
            reader = csv.DictReader(source)
            required = {
                "address_hex",
                "migration_stage",
                "issuance_treatment",
                "planned_total_entitlement_atto",
                "wallet_component_atto",
                "staking_component_atto",
                "planned_wallet_destination",
                "planned_staking_destination",
                "planned_delivery_status",
                "delivery_tier",
                "delivery_policy",
            }
            if not required <= set(reader.fieldnames or ()):
                raise ValueError(f"{path}: missing audit fields")
            for line, row in enumerate(reader, start=2):
                address = row["address_hex"].lower()
                if address in sources:
                    raise ValueError(f"{path}:{line}: duplicate address")
                amount = int(row["planned_total_entitlement_atto"])
                if amount < 0:
                    raise ValueError(f"{path}:{line}: negative planned amount")
                if row["delivery_policy"] != "manual_from_reserve":
                    raise ValueError(f"{path}:{line}: unexpected delivery policy")
                sources[address] = {
                    "amount": amount,
                    "wallet_component": int(row["wallet_component_atto"]),
                    "staking_component": int(row["staking_component_atto"]),
                    "stage": row["migration_stage"],
                    "treatment": row["issuance_treatment"],
                    "tier": row["delivery_tier"],
                    "status": row["planned_delivery_status"],
                    "destinations": {
                        row["planned_wallet_destination"].lower(),
                        row["planned_staking_destination"].lower(),
                    }
                    - {""},
                }
        expected[exchange_id] = {
            "config": config,
            "path": path,
            "sources": sources,
        }
    return expected


def load_routes(path, policy, expected):
    routes = {}
    base_priority = int(policy["manual_route_priority"])
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            route_id = row["route_id"]
            if not route_id.startswith("exchange-"):
                raise ValueError(f"{path}:{line}: non-exchange route")
            address = row["source_address"].lower()
            exchange_id = route_id[len("exchange-") : route_id.index(address[2:]) - 1]
            record = expected.get(exchange_id)
            if record is None:
                raise ValueError(f"{path}:{line}: unknown exchange {exchange_id}")
            planned = record["sources"].get(address)
            if planned is None or planned["amount"] <= 0:
                raise ValueError(f"{path}:{line}: source has no planned amount")
            if planned["treatment"] != EXCHANGE_TREATMENT:
                raise ValueError(f"{path}:{line}: routed source is not manual")
            if row["reason"] != EXCHANGE_ROUTE_REASON:
                raise ValueError(f"{path}:{line}: invalid route reason")
            held = planned["status"] != EXCHANGE_STATUS
            if row["status"] != ("hold" if held else EXCHANGE_STATUS):
                raise ValueError(f"{path}:{line}: route status contradicts audit")
            suffix = route_id[len(f"exchange-{exchange_id}-{address[2:]}") :]
            destination_id = row["destination_id"]
            direct = row["destination_address"].lower()
            if suffix == "":
                if int(row["priority"]) != base_priority or row["amount_atto"] != "ALL":
                    raise ValueError(f"{path}:{line}: invalid whole-claim route")
                if row["allocation_method"] != "wallet_first_pro_rata_vault":
                    raise ValueError(f"{path}:{line}: invalid allocation method")
                if planned["tier"] in {"same_address", "same_address_initial"}:
                    if destination_id or direct != address:
                        raise ValueError(
                            f"{path}:{line}: same-address route must target the source"
                        )
                elif destination_id not in {
                    f"exchange-{exchange_id}",
                    f"exchange-{exchange_id}-staking",
                } or direct:
                    raise ValueError(f"{path}:{line}: invalid aggregate destination")
            elif suffix == "-wallet":
                if (
                    int(row["priority"]) != base_priority
                    or row["allocation_method"] != "wallet_only"
                    or int(row["amount_atto"]) != planned["wallet_component"]
                    or destination_id != f"exchange-{exchange_id}"
                    or direct
                ):
                    raise ValueError(f"{path}:{line}: invalid split wallet route")
            elif suffix == "-staking":
                if (
                    int(row["priority"]) != base_priority + 1
                    or row["allocation_method"] != "wallet_first_pro_rata_vault"
                    or row["amount_atto"] != "ALL"
                    or destination_id != f"exchange-{exchange_id}-staking"
                    or direct
                ):
                    raise ValueError(f"{path}:{line}: invalid split staking route")
            else:
                raise ValueError(f"{path}:{line}: unexpected route id shape")
            if route_id in routes:
                raise ValueError(f"{path}:{line}: duplicate route")
            routes[route_id] = {
                "exchange_id": exchange_id,
                "address": address,
                "suffix": suffix,
                "planned": planned,
            }
    expected_pairs = {
        (exchange_id, address)
        for exchange_id, record in expected.items()
        for address, planned in record["sources"].items()
        if planned["amount"] > 0
    }
    actual_pairs = {(row["exchange_id"], row["address"]) for row in routes.values()}
    if actual_pairs != expected_pairs:
        raise ValueError(
            "exchange route set does not equal positive manual claims"
        )
    return routes


def load_compiled(path, routes):
    amounts = defaultdict(int)
    statuses = defaultdict(set)
    stages = defaultdict(set)
    treatments = defaultdict(set)
    destinations = defaultdict(set)
    by_source = defaultdict(int)
    unknown = []
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            route_id = row["route_id"]
            if not route_id.startswith("exchange-"):
                continue
            if route_id not in routes:
                unknown.append((line, route_id))
                continue
            amount = int(row["amount_atto"])
            if amount <= 0:
                raise ValueError(f"{path}:{line}: non-positive compiled amount")
            amounts[route_id] += amount
            by_source[(routes[route_id]["exchange_id"], routes[route_id]["address"])] += amount
            statuses[route_id].add(row["destination_status"])
            stages[route_id].add(row["migration_stage"])
            treatments[route_id].add(row["issuance_treatment"])
            if row["destination_address"]:
                destinations[route_id].add(row["destination_address"].lower())
    if unknown:
        raise ValueError(f"unknown compiled exchange routes: {unknown[:10]}")
    missing = set(routes) - set(amounts)
    if missing:
        raise ValueError(
            f"exchange routes missing from compiled exceptions: "
            f"{sorted(missing)[:10]}"
        )
    for route_id, route in routes.items():
        planned = route["planned"]
        if route["suffix"] == "-wallet" and amounts[route_id] != planned["wallet_component"]:
            raise ValueError(f"{route_id}: compiled wallet component mismatch")
        if route["suffix"] == "-staking" and amounts[route_id] != planned["staking_component"]:
            raise ValueError(f"{route_id}: compiled staking component mismatch")
        if by_source[(route["exchange_id"], route["address"])] != planned["amount"]:
            raise ValueError(
                f"{route_id}: compiled amount does not match memo plan"
            )
        if statuses[route_id] - {EXCHANGE_STATUS, "hold"}:
            raise ValueError(f"{route_id}: invalid compiled destination status")
        if stages[route_id] != {EXCHANGE_STAGE}:
            raise ValueError(f"{route_id}: compiled exchange stage mismatch")
        if treatments[route_id] != {EXCHANGE_TREATMENT}:
            raise ValueError(f"{route_id}: compiled issuance treatment mismatch")
        if destinations[route_id] - planned["destinations"]:
            raise ValueError(f"{route_id}: compiled destination not in the memo plan")
    return amounts, statuses, stages


def main():
    args = parse_args()
    policy = load_policy(args.policy)
    expected = load_expected(policy, args.audits_dir)
    routes = load_routes(args.routes, policy, expected)
    amounts, statuses, stages = load_compiled(args.routing_exceptions, routes)
    with open(args.exchange_summary, encoding="utf-8") as source:
        exchange_summary = json.load(source)
    if exchange_summary.get("schema_version") != 2:
        raise ValueError("unsupported exchange summary schema")
    if not exchange_summary.get("routes_emitted"):
        raise ValueError("exchange summary was built without stage policy routes")
    if exchange_summary["outputs"]["routing_input"]["sha256"] != file_sha256(
        args.routes
    ):
        raise ValueError("exchange summary does not identify route input")
    with open(args.routing_summary, encoding="utf-8") as source:
        routing_summary = json.load(source)
    if routing_summary.get("inactive_routes"):
        raise ValueError("compiled routing contains inactive routes")
    route_inputs = {
        row["path"]: row["sha256"]
        for row in routing_summary.get("route_inputs", [])
    }
    if route_inputs.get(args.routes) != file_sha256(args.routes):
        raise ValueError("routing summary does not hash-pin exchange routes")
    by_exchange = {}
    for config in policy["exchanges"]:
        exchange_id = config["id"]
        exchange_routes = [
            route_id
            for route_id, route in routes.items()
            if route["exchange_id"] == exchange_id
        ]
        by_exchange[exchange_id] = {
            "delivery_policy": config["delivery_policy"],
            "destination_mode": config["destination_mode"],
            "routes": len(exchange_routes),
            "compiled_routes": len(exchange_routes),
            "planned_total_and_compiled_atto": str(
                sum(amounts[route_id] for route_id in exchange_routes)
            ),
            "destination_statuses": sorted(
                {
                    status
                    for route_id in exchange_routes
                    for status in statuses[route_id]
                }
            ),
            "migration_stages": sorted(
                {
                    stage
                    for route_id in exchange_routes
                    for stage in stages[route_id]
                }
            ),
        }
    result = {
        "schema_version": 1,
        "status": "passed",
        "policy": args.policy,
        "policy_sha256": file_sha256(args.policy),
        "exchange_summary": args.exchange_summary,
        "exchange_summary_sha256": file_sha256(args.exchange_summary),
        "routes": args.routes,
        "routes_sha256": file_sha256(args.routes),
        "routing_exceptions": args.routing_exceptions,
        "routing_exceptions_sha256": file_sha256(args.routing_exceptions),
        "routing_summary": args.routing_summary,
        "routing_summary_sha256": file_sha256(args.routing_summary),
        "manual_routes": len(routes),
        "exchange_stage": EXCHANGE_STAGE,
        "exchange_treatment": EXCHANGE_TREATMENT,
        "delivery_source": "year_2050_supply_reserve",
        "exchanges": by_exchange,
    }
    atomic_json(args.output, result, args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
