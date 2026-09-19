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
    if policy.get("schema_version") != 1:
        raise ValueError("unsupported exchange policy schema")
    return policy


def load_expected(policy, audits_dir):
    expected = {}
    gate_addresses = set()
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
                sources[address] = {
                    "amount": amount,
                    "stage": row["migration_stage"],
                    "treatment": row["issuance_treatment"],
                }
                if exchange_id == "gate":
                    gate_addresses.add(address)
        expected[exchange_id] = {
            "config": config,
            "path": path,
            "sources": sources,
        }
    return expected, gate_addresses


def load_routes(path, policy, expected, gate_addresses):
    routes = {}
    expected_priority = int(policy["manual_route_priority"])
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            route_id = row["route_id"]
            destination_id = row["destination_id"]
            if not destination_id.startswith("exchange-"):
                raise ValueError(f"{path}:{line}: non-exchange destination")
            exchange_id = destination_id[len("exchange-") :]
            record = expected.get(exchange_id)
            if (
                record is None
                or record["config"]["delivery_policy"]
                != "manual_current_claim"
            ):
                raise ValueError(f"{path}:{line}: invalid exchange route")
            address = row["source_address"].lower()
            planned = record["sources"].get(address)
            if planned is None or planned["amount"] <= 0:
                raise ValueError(f"{path}:{line}: source has no planned amount")
            if planned["treatment"] != "issue":
                raise ValueError(f"{path}:{line}: routed source is not issued")
            if address in gate_addresses:
                raise ValueError(f"{path}:{line}: Gate source was rerouted")
            if (
                route_id != f"exchange-{exchange_id}-{address[2:]}"
                or int(row["priority"]) != expected_priority
                or row["amount_atto"] != "ALL"
                or row["allocation_method"]
                != "wallet_first_pro_rata_vault"
                or row["destination_address"]
            ):
                raise ValueError(f"{path}:{line}: invalid route policy")
            if route_id in routes:
                raise ValueError(f"{path}:{line}: duplicate route")
            routes[route_id] = {
                "exchange_id": exchange_id,
                "address": address,
                "planned": planned["amount"],
                "stage": planned["stage"],
            }
    expected_pairs = {
        (exchange_id, address)
        for exchange_id, record in expected.items()
        if record["config"]["delivery_policy"] == "manual_current_claim"
        for address, planned in record["sources"].items()
        if planned["amount"] > 0
    }
    actual_pairs = {
        (row["exchange_id"], row["address"]) for row in routes.values()
    }
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
            statuses[route_id].add(row["destination_status"])
            stages[route_id].add(row["migration_stage"])
            treatments[route_id].add(row["issuance_treatment"])
    if unknown:
        raise ValueError(f"unknown compiled exchange routes: {unknown[:10]}")
    missing = set(routes) - set(amounts)
    if missing:
        raise ValueError(
            f"exchange routes missing from compiled exceptions: "
            f"{sorted(missing)[:10]}"
        )
    for route_id, route in routes.items():
        if amounts[route_id] != route["planned"]:
            raise ValueError(
                f"{route_id}: compiled amount does not match memo plan"
            )
        if statuses[route_id] - {"ready", "hold"}:
            raise ValueError(f"{route_id}: invalid compiled destination status")
        if stages[route_id] != {"exchange_aggregate"}:
            raise ValueError(
                f"{route_id}: compiled aggregate-exchange stage mismatch"
            )
        if treatments[route_id] != {"issue"}:
            raise ValueError(f"{route_id}: compiled issuance treatment mismatch")
    return amounts, statuses, stages


def main():
    args = parse_args()
    policy = load_policy(args.policy)
    expected, gate_addresses = load_expected(policy, args.audits_dir)
    routes = load_routes(args.routes, policy, expected, gate_addresses)
    amounts, statuses, stages = load_compiled(args.routing_exceptions, routes)
    with open(args.exchange_summary, encoding="utf-8") as source:
        exchange_summary = json.load(source)
    if exchange_summary.get("schema_version") != 1:
        raise ValueError("unsupported exchange summary schema")
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
        "gate_exchange_routes": 0,
        "exchanges": by_exchange,
    }
    atomic_json(args.output, result, args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
