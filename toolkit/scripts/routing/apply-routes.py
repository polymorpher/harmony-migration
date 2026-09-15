#!/usr/bin/env python3

"""Apply explicit routes to direct-wallet and validator-vault claim delivery."""

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


EMPTY_CODE_HASH = (
    "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
)
REQUIRED_POLICY_DECISIONS = {
    "contract-recovery-custody",
    "layerzero-nativeoft-reconciliation",
    "rollback-exploit-proceeds",
    "wone-reserve-custody",
}
DESTINATION_STATUSES = {"ready", "hold", "not_issuing"}
NOT_ISSUING_DESTINATION_ID = "not-issuing"
ROUTING_EXCEPTION_FIELDS = (
    "component",
    "source_secure_key",
    "source_address",
    "source_category",
    "source_code_bearing",
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
GOVERNOR_EXCEPTION_FIELDS = (
    "validator_address",
    "validator_secure_key",
    "vault_assets_atto",
    "exception_type",
    "destination_id",
    "destination_address",
    "destination_status",
    "reason",
    "evidence",
)
UNRESOLVED_FIELDS = (
    "component",
    "source_address",
    "source_category",
    "validator_address",
    "amount_atto",
    "exception_type",
    "route_id",
    "destination_id",
    "destination_address",
    "destination_status",
    "reason",
    "evidence",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-claims", required=True)
    parser.add_argument("--automatic-claims", required=True)
    parser.add_argument("--contract-claims", required=True)
    parser.add_argument("--excluded-claims", required=True)
    parser.add_argument("--priority-shares", required=True)
    parser.add_argument("--deferred-shares", required=True)
    parser.add_argument("--base-vault-deposits", required=True)
    parser.add_argument("--validator-accounts", required=True)
    parser.add_argument("--routes", action="append", default=[])
    parser.add_argument("--destinations", required=True)
    parser.add_argument("--governors", required=True)
    parser.add_argument("--policy-decisions", required=True)
    parser.add_argument("--exceptions-output", required=True)
    parser.add_argument("--governor-exceptions-output", required=True)
    parser.add_argument("--unresolved-output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="atomically replace existing canonical output files",
    )
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_csv(path, fields, rows, replace=False):
    if os.path.exists(path + ".partial"):
        raise FileExistsError(path)
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


def code_bearing(row):
    if not row.get("code_hash_shard0", "").strip():
        return None
    hashes = [
        row.get(field, "").strip().lower()
        for field in ("code_hash_shard0", "code_hash_shard1")
        if row.get(field, "").strip()
    ]
    if not hashes:
        return None
    return any(value != EMPTY_CODE_HASH for value in hashes)


def load_claims(paths):
    claims = {}
    by_address = {}
    for path, category in paths:
        with open(path, newline="") as source:
            reader = csv.DictReader(source)
            required = {
                "secure_key",
                "address",
                "wallet_airdrop_atto",
                "staked_to_vault_atto",
                "total_claim_atto",
                "code_hash_shard0",
                "code_hash_shard1",
            }
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{path}: missing fields {sorted(missing)}")
            for line, row in enumerate(reader, start=2):
                key = row["secure_key"].lower()
                address = lib.require_address_secure_key(
                    row["address"], key, f"{path}:{line}"
                )
                wallet = int(row["wallet_airdrop_atto"])
                staked = int(row["staked_to_vault_atto"])
                total = int(row["total_claim_atto"])
                has_code = code_bearing(row)
                if min(wallet, staked, total) < 0 or wallet + staked != total:
                    raise ValueError(f"{path}:{line}: claim mismatch")
                if has_code is None:
                    raise ValueError(
                        f"{path}:{line}: unresolved code metadata"
                    )
                if key in claims or address in by_address:
                    raise ValueError(f"{path}:{line}: duplicate claim")
                claims[key] = {
                    "key": key,
                    "address": address,
                    "wallet": wallet,
                    "staked": staked,
                    "total": total,
                    "liquid_shard0": (
                        int(row["liquid_shard0_atto"])
                        if row.get("liquid_shard0_atto", "") != ""
                        else None
                    ),
                    "category": category,
                    "code_bearing": has_code,
                }
                by_address[address] = key
    return claims, by_address


def load_routed_deferred_claims(path, route_sources, claims, by_address):
    wanted = route_sources - set(by_address)
    if not wanted:
        return
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            address = lib.any_to_hex(row["address"])
            if address not in wanted:
                continue
            key = row["secure_key"].lower()
            address = lib.require_address_secure_key(
                row["address"], key, f"{path}:{line}"
            )
            wallet = int(row["wallet_airdrop_atto"])
            staked = int(row["staked_to_vault_atto"])
            total = int(row["total_claim_atto"])
            has_code = code_bearing(row)
            if min(wallet, staked, total) < 0 or wallet + staked != total:
                raise ValueError(f"{path}:{line}: deferred claim mismatch")
            if has_code is None:
                raise ValueError(
                    f"{path}:{line}: unresolved deferred code metadata"
                )
            claims[key] = {
                "key": key,
                "address": address,
                "wallet": wallet,
                "staked": staked,
                "total": total,
                "liquid_shard0": (
                    int(row["liquid_shard0_atto"])
                    if row.get("liquid_shard0_atto", "") != ""
                    else None
                ),
                "category": "deferred",
                "code_bearing": has_code,
            }
            by_address[address] = key
            wanted.remove(address)
            if not wanted:
                break


def load_validator_accounts(path, claims, by_address):
    validators = set()
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        if "address" not in set(reader.fieldnames or ()):
            raise ValueError(f"{path}: missing address field")
        for line, row in enumerate(reader, start=2):
            address = lib.any_to_hex(row["address"])
            if address in validators:
                raise ValueError(f"{path}:{line}: duplicate validator")
            key = by_address.get(address)
            if key is None or claims[key]["category"] != "automatic":
                raise ValueError(
                    f"{path}:{line}: validator is not an automatic claim"
                )
            if claims[key]["code_bearing"] is not True:
                raise ValueError(
                    f"{path}:{line}: validator is not code-bearing"
                )
            validators.add(address)

    for claim in claims.values():
        if claim["category"] != "automatic":
            continue
        if claim["code_bearing"] is None:
            raise ValueError(
                "automatic claim has unresolved code metadata: "
                f"{claim['address']}"
            )
        if claim["code_bearing"] and claim["address"] not in validators:
            raise ValueError(
                "code-bearing automatic claim is not a verified validator: "
                f"{claim['address']}"
            )
    return validators


def load_destinations(path):
    destinations = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            destination_id = row["destination_id"].strip()
            if not destination_id or destination_id in destinations:
                raise ValueError(f"{path}:{line}: invalid destination id")
            address = row["destination_address"].strip()
            if address:
                address = lib.to_checksum(lib.normalize_address(address))
            status = row["status"].strip()
            if status not in DESTINATION_STATUSES:
                raise ValueError(f"{path}:{line}: invalid destination status")
            if status == "ready" and not address:
                raise ValueError(
                    f"{path}:{line}: ready destination has no address"
                )
            if status == "not_issuing" and (
                address or destination_id != NOT_ISSUING_DESTINATION_ID
            ):
                raise ValueError(
                    f"{path}:{line}: not_issuing must use the "
                    "not-issuing id with no address"
                )
            if (
                destination_id == NOT_ISSUING_DESTINATION_ID
                and status != "not_issuing"
            ):
                raise ValueError(
                    f"{path}:{line}: not-issuing destination must have "
                    "not_issuing status"
                )
            destinations[destination_id] = {
                "address": address,
                "status": status,
            }
    return destinations


def load_policy_decisions(path):
    pending = []
    seen = set()
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"decision_id", "status", "decision"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            decision_id = row["decision_id"].strip()
            status = row["status"].strip()
            if not decision_id or decision_id in seen:
                raise ValueError(f"{path}:{line}: invalid decision id")
            seen.add(decision_id)
            if status not in {"pending", "resolved"}:
                raise ValueError(f"{path}:{line}: invalid decision status")
            if status == "resolved" and not row["decision"].strip():
                raise ValueError(
                    f"{path}:{line}: resolved decision has no decision text"
                )
            if status == "pending":
                pending.append(decision_id)
    missing = REQUIRED_POLICY_DECISIONS - seen
    if missing:
        raise ValueError(
            f"{path}: missing required policy decisions {sorted(missing)}"
        )
    return pending


def csv_policy_state(path):
    states = set()
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        fields = {
            "policy_state_block",
            "policy_state_block_hash",
            "policy_state_root",
        }
        if not fields <= set(reader.fieldnames or ()):
            return None
        for line, row in enumerate(reader, start=2):
            try:
                state = (
                    int(row["policy_state_block"]),
                    row["policy_state_block_hash"],
                    row["policy_state_root"],
                )
            except ValueError as error:
                raise ValueError(
                    f"{path}:{line}: invalid policy state"
                ) from error
            if not state[1] or not state[2]:
                raise ValueError(f"{path}:{line}: incomplete policy state")
            states.add(state)
    if len(states) != 1:
        raise ValueError(f"{path}: policy state is not unique")
    block, block_hash, state_root = states.pop()
    return {
        "block": block,
        "block_hash": block_hash,
        "state_root": state_root,
    }


def resolve_destination(row, destinations):
    direct = row.get("destination_address", "").strip()
    destination_id = row.get("destination_id", "").strip()
    explicit_status = row.get("status", "").strip()
    if explicit_status and explicit_status not in DESTINATION_STATUSES:
        raise ValueError(f"invalid explicit destination status: {explicit_status}")
    if direct:
        if explicit_status == "not_issuing":
            raise ValueError("not_issuing destination cannot have an address")
        return (
            destination_id,
            lib.to_checksum(lib.normalize_address(direct)),
            explicit_status or "ready",
        )
    if not destination_id:
        if explicit_status in {"ready", "not_issuing"}:
            raise ValueError(
                f"{explicit_status} destination has no address or id"
            )
        return "", "", "hold"
    if destination_id not in destinations:
        if explicit_status in {"ready", "not_issuing"}:
            raise ValueError(
                f"{explicit_status} destination id is undefined: "
                f"{destination_id}"
            )
        return destination_id, "", "hold"
    destination = destinations[destination_id]
    if (
        explicit_status == "not_issuing"
        and destination["status"] != "not_issuing"
    ):
        raise ValueError(
            "explicit not_issuing status requires the not-issuing destination"
        )
    status = (
        "hold"
        if explicit_status == "hold"
        else destination["status"]
    )
    return destination_id, destination["address"], status


def load_routes(paths, destinations):
    routes = []
    seen = set()
    for path in paths:
        with open(path, newline="") as source:
            reader = csv.DictReader(source)
            for line, row in enumerate(reader, start=2):
                route_id = row["route_id"].strip()
                if not route_id or route_id in seen:
                    raise ValueError(f"{path}:{line}: duplicate route id")
                seen.add(route_id)
                method = row["allocation_method"].strip()
                if method not in {
                    "wallet_first_pro_rata_vault",
                    "wallet_only",
                    "vault_only_pro_rata",
                }:
                    raise ValueError(
                        f"{path}:{line}: invalid allocation method"
                    )
                amount_text = row["amount_atto"].strip().upper()
                if amount_text in {"ALL", "SHARD0_LIQUID"}:
                    amount = amount_text
                else:
                    amount = int(amount_text)
                if isinstance(amount, int) and amount <= 0:
                    raise ValueError(f"{path}:{line}: invalid route amount")
                destination_id, destination, status = resolve_destination(
                    row, destinations
                )
                routes.append(
                    {
                        "route_id": route_id,
                        "priority": int(row["priority"]),
                        "source_address": lib.any_to_hex(
                            row["source_address"]
                        ),
                        "destination_id": destination_id,
                        "destination_address": destination,
                        "destination_status": status,
                        "amount": amount,
                        "method": method,
                        "exception_type": "explicit_route",
                        "reason": row.get("reason", ""),
                        "evidence": row.get("evidence", ""),
                        "policy_state": (
                            {
                                "block": int(row["policy_state_block"]),
                                "block_hash": row[
                                    "policy_state_block_hash"
                                ],
                                "state_root": row["policy_state_root"],
                            }
                            if row.get("policy_state_block", "").strip()
                            else None
                        ),
                    }
                )
    routes.sort(key=lambda row: (row["priority"], row["route_id"]))
    return routes


def pro_rata(amount, positions):
    available = [
        (index, position)
        for index, position in enumerate(positions)
        if position["remaining"] > 0
    ]
    total = sum(position["remaining"] for _index, position in available)
    if amount < 0 or amount > total:
        raise ValueError("pro-rata amount exceeds vault position")
    if amount == 0:
        return []
    allocations = []
    assigned = 0
    remainders = []
    for index, position in available:
        numerator = amount * position["remaining"]
        share, remainder = divmod(numerator, total)
        allocations.append([index, share])
        assigned += share
        remainders.append(
            (
                -remainder,
                position["validator_secure_key"],
                index,
            )
        )
    extras = amount - assigned
    allocation_by_index = {index: share for index, share in allocations}
    for _negative_remainder, _validator, index in sorted(remainders)[:extras]:
        allocation_by_index[index] += 1
    result = []
    for index, share in sorted(allocation_by_index.items()):
        if share:
            if share > positions[index]["remaining"]:
                raise ValueError("pro-rata allocation exceeds position")
            positions[index]["remaining"] -= share
            result.append((positions[index], share))
    if sum(share for _position, share in result) != amount:
        raise ValueError("pro-rata allocation does not close")
    return result


def append_wallet(rows, claim, amount, route):
    if amount == 0:
        return
    rows.append(
        {
            "source_secure_key": claim["key"],
            "source_address": lib.to_checksum(claim["address"]),
            "source_category": claim["routing_category"],
            "source_code_bearing": str(claim["code_bearing"]).lower(),
            "wallet_airdrop_atto": str(amount),
            "exception_type": route["exception_type"],
            "route_id": route["route_id"],
            "route_priority": str(route["priority"]),
            "destination_id": route["destination_id"],
            "destination_address": route["destination_address"],
            "destination_status": route["destination_status"],
            "reason": route["reason"],
            "evidence": route["evidence"],
        }
    )


def append_shares(rows, allocations, route):
    for position, amount in allocations:
        rows.append(
            {
                "validator_address": position["validator_address"],
                "validator_secure_key": position["validator_secure_key"],
                "delegator_address": position["delegator_address"],
                "delegator_secure_key": position["delegator_secure_key"],
                "source_category": position["source_category"],
                "source_code_bearing": str(
                    position["source_code_bearing"]
                ).lower(),
                "staked_to_vault_atto": str(amount),
                "exception_type": route["exception_type"],
                "route_id": route["route_id"],
                "route_priority": str(route["priority"]),
                "destination_id": route["destination_id"],
                "destination_address": route["destination_address"],
                "destination_status": route["destination_status"],
                "reason": route["reason"],
                "evidence": route["evidence"],
            }
        )


def main():
    args = parse_args()
    outputs = (
        args.exceptions_output,
        args.governor_exceptions_output,
        args.unresolved_output,
        args.summary,
    )
    for path in outputs:
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path)
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)

    claims, by_address = load_claims(
        (
            (args.automatic_claims, "automatic"),
            (args.contract_claims, "contract_review"),
            (args.excluded_claims, "excluded"),
        )
    )
    destinations = load_destinations(args.destinations)
    pending_policy_decisions = load_policy_decisions(args.policy_decisions)
    routes = load_routes(args.routes, destinations)
    custody_states = {
        tuple(route["policy_state"].values())
        for route in routes
        if route["reason"]
        == "non_multisig_contract_recovery_custody"
        and route["policy_state"] is not None
    }
    custody_without_state = [
        route["route_id"]
        for route in routes
        if route["reason"]
        == "non_multisig_contract_recovery_custody"
        and route["policy_state"] is None
    ]
    invalid_custody_destinations = [
        route["route_id"]
        for route in routes
        if route["reason"]
        == "non_multisig_contract_recovery_custody"
        and route["destination_id"] != "contract-recovery-custody"
    ]
    if invalid_custody_destinations:
        raise ValueError(
            "generated contract-holding routes must use "
            "contract-recovery-custody, not treasury"
        )
    if custody_without_state or len(custody_states) > 1:
        raise ValueError(
            "contract-holding routes must share one cutoff block, hash, "
            "and state root"
        )
    contract_policy_state = None
    if custody_states:
        block, block_hash, state_root = custody_states.pop()
        contract_policy_state = {
            "block": block,
            "block_hash": block_hash,
            "state_root": state_root,
        }
        if csv_policy_state(args.validator_accounts) != contract_policy_state:
            raise ValueError(
                "contract-holding routes and validator accounts must use "
                "the same cutoff block, hash, and state root"
            )
    load_routed_deferred_claims(
        args.all_claims,
        {route["source_address"] for route in routes},
        claims,
        by_address,
    )
    validator_accounts = load_validator_accounts(
        args.validator_accounts, claims, by_address
    )
    for claim in claims.values():
        if claim["category"] == "automatic":
            claim["routing_category"] = (
                "validator_account"
                if claim["address"] in validator_accounts
                else "ordinary_eoa"
            )
        else:
            claim["routing_category"] = claim["category"]
    routes_by_key = defaultdict(list)
    inactive_routes = []
    explicitly_routed_validators = set()
    for route in routes:
        explicitly_routed_validators.add(route["source_address"])
        key = by_address.get(route["source_address"])
        if key is None:
            inactive_routes.append(route["route_id"])
            continue
        routes_by_key[key].append(route)

    positions = defaultdict(list)
    seen_positions = set()
    for path, required in (
        (args.priority_shares, True),
        (args.deferred_shares, False),
    ):
        with open(path, newline="") as source:
            reader = csv.DictReader(source)
            for line, row in enumerate(reader, start=2):
                key = row["delegator_secure_key"].lower()
                lib.require_address_secure_key(
                    row["delegator_address"],
                    key,
                    f"{path}:{line} delegator",
                )
                if key not in claims:
                    if required:
                        raise ValueError(
                            f"{path}:{line}: unknown delegator"
                        )
                    continue
                validator_key = row["validator_secure_key"].lower()
                lib.require_address_secure_key(
                    row["validator_address"],
                    validator_key,
                    f"{path}:{line} validator",
                )
                pair = (validator_key, key)
                if pair in seen_positions:
                    raise ValueError(f"{path}:{line}: duplicate position")
                seen_positions.add(pair)
                amount = int(row["staked_to_vault_atto"])
                if amount <= 0:
                    raise ValueError(
                        f"{path}:{line}: non-positive shares"
                    )
                positions[key].append(
                    {
                        "validator_address": row["validator_address"],
                        "validator_secure_key": validator_key,
                        "delegator_address": row["delegator_address"],
                        "delegator_secure_key": key,
                        "source_category": claims[key]["routing_category"],
                        "source_code_bearing": claims[key]["code_bearing"],
                        "remaining": amount,
                    }
                )
    for key, claim in claims.items():
        positions[key].sort(
            key=lambda row: (
                row["validator_secure_key"],
                row["delegator_secure_key"],
            )
        )
        if sum(row["remaining"] for row in positions[key]) != claim["staked"]:
            raise ValueError(f"staked-to-vault mismatch for {key}")

    wallet_rows = []
    share_rows = []
    for key in sorted(claims):
        claim = claims[key]
        wallet_remaining = claim["wallet"]
        for route in routes_by_key.get(key, []):
            vault_remaining = sum(
                position["remaining"] for position in positions[key]
            )
            remaining_total = wallet_remaining + vault_remaining
            if route["amount"] == "ALL":
                amount = remaining_total
            elif route["amount"] == "SHARD0_LIQUID":
                if claim["liquid_shard0"] is None:
                    raise ValueError(
                        f"route {route['route_id']} requires "
                        "liquid_shard0_atto"
                    )
                amount = claim["liquid_shard0"]
            else:
                amount = route["amount"]
            if amount > remaining_total:
                raise ValueError(
                    f"route {route['route_id']} exceeds remaining claim"
                )
            wallet_amount = 0
            vault_amount = 0
            if route["method"] == "wallet_first_pro_rata_vault":
                wallet_amount = min(wallet_remaining, amount)
                vault_amount = amount - wallet_amount
            elif route["method"] == "wallet_only":
                if amount > wallet_remaining:
                    raise ValueError(
                        f"route {route['route_id']} exceeds wallet amount"
                    )
                wallet_amount = amount
            else:
                vault_amount = amount
            wallet_remaining -= wallet_amount
            allocations = pro_rata(vault_amount, positions[key])
            append_wallet(wallet_rows, claim, wallet_amount, route)
            append_shares(share_rows, allocations, route)

        automatic = claim["category"] == "automatic"
        validator = claim["routing_category"] == "validator_account"
        default_status = "ready" if automatic else "hold"
        default_destination = (
            lib.to_checksum(claim["address"])
            if default_status == "ready"
            else ""
        )
        if validator:
            exception_type = "validator_wrapper_same_address"
            reason = "verified validator wrapper same-address"
            evidence = args.validator_accounts
        elif automatic:
            exception_type = ""
            reason = "implicit code-less EOA same-address"
            evidence = ""
        else:
            exception_type = f"{claim['category']}_hold"
            reason = claim["category"]
            evidence = ""
        default_route = {
            "route_id": f"default-{key[2:]}",
            "priority": 1000000,
            "destination_id": "",
            "destination_address": default_destination,
            "destination_status": default_status,
            "exception_type": exception_type,
            "reason": reason,
            "evidence": evidence,
        }
        append_wallet(wallet_rows, claim, wallet_remaining, default_route)
        append_shares(
            share_rows,
            pro_rata(
                sum(position["remaining"] for position in positions[key]),
                positions[key],
            ),
            default_route,
        )

    wallet_rows.sort(
        key=lambda row: (
            row["source_secure_key"],
            int(row["route_priority"]),
            row["route_id"],
        )
    )
    share_rows.sort(
        key=lambda row: (
            row["validator_secure_key"],
            row["delegator_secure_key"],
            int(row["route_priority"]),
            row["route_id"],
        )
    )

    routing_exceptions = []
    for row in wallet_rows:
        if not row["exception_type"]:
            continue
        routing_exceptions.append(
            {
                "component": "wallet_airdrop",
                "source_secure_key": row["source_secure_key"],
                "source_address": row["source_address"],
                "source_category": row["source_category"],
                "source_code_bearing": row["source_code_bearing"],
                "validator_secure_key": "",
                "validator_address": "",
                "amount_atto": row["wallet_airdrop_atto"],
                "exception_type": row["exception_type"],
                "route_id": row["route_id"],
                "route_priority": row["route_priority"],
                "destination_id": row["destination_id"],
                "destination_address": row["destination_address"],
                "destination_status": row["destination_status"],
                "reason": row["reason"],
                "evidence": row["evidence"],
            }
        )
    for row in share_rows:
        if not row["exception_type"]:
            continue
        routing_exceptions.append(
            {
                "component": "vault_shares",
                "source_secure_key": row["delegator_secure_key"],
                "source_address": row["delegator_address"],
                "source_category": row["source_category"],
                "source_code_bearing": row["source_code_bearing"],
                "validator_secure_key": row["validator_secure_key"],
                "validator_address": row["validator_address"],
                "amount_atto": row["staked_to_vault_atto"],
                "exception_type": row["exception_type"],
                "route_id": row["route_id"],
                "route_priority": row["route_priority"],
                "destination_id": row["destination_id"],
                "destination_address": row["destination_address"],
                "destination_status": row["destination_status"],
                "reason": row["reason"],
                "evidence": row["evidence"],
            }
        )
    routing_exceptions.sort(
        key=lambda row: (
            row["source_secure_key"],
            row["component"],
            row["validator_secure_key"],
            int(row["route_priority"]),
            row["route_id"],
        )
    )
    represented_code_bearing = {
        lib.any_to_hex(row["source_address"])
        for row in routing_exceptions
        if row["source_code_bearing"] == "true"
    }
    missing_code_bearing = {
        claim["address"]
        for claim in claims.values()
        if claim["code_bearing"]
    } - represented_code_bearing
    if missing_code_bearing:
        raise ValueError(
            "code-bearing claims missing from routing exceptions: "
            + ", ".join(sorted(missing_code_bearing))
        )
    unresolved = [
        {field: row[field] for field in UNRESOLVED_FIELDS}
        for row in routing_exceptions
        if row["destination_status"] == "hold"
    ]

    governor_overrides = {}
    with open(args.governors, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            validator = lib.any_to_hex(row["validator_address"])
            if validator in governor_overrides:
                raise ValueError(
                    f"{args.governors}:{line}: duplicate validator"
                )
            destination_id, destination, status = resolve_destination(
                row, destinations
            )
            if status == "not_issuing":
                raise ValueError(
                    "not-issuing is not a validator-governor destination"
                )
            governor_overrides[validator] = {
                "destination_id": destination_id,
                "destination": destination,
                "status": status,
                "exception_type": "explicit_governor_route",
                "reason": row.get("reason", ""),
                "evidence": row.get("evidence", ""),
            }

    governor_exceptions = []
    seen_vaults = set()
    with open(args.base_vault_deposits, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            lib.require_address_secure_key(
                row["validator_address"],
                row["validator_secure_key"],
                f"{args.base_vault_deposits}:{line} validator",
            )
            validator = lib.any_to_hex(row["validator_address"])
            seen_vaults.add(validator)
            governor = governor_overrides.get(validator)
            if governor is None:
                hold = validator in explicitly_routed_validators
                governor = {
                    "destination_id": "",
                    "destination": (
                        "" if hold else lib.to_checksum(validator)
                    ),
                    "status": "hold" if hold else "ready",
                    "exception_type": (
                        "claim_route_governor_hold" if hold else ""
                    ),
                    "reason": (
                        "explicit claim route requires governor decision"
                        if hold
                        else "same validator key"
                    ),
                    "evidence": "",
                }
            if governor["exception_type"]:
                governor_exceptions.append(
                    {
                        "validator_address": row["validator_address"],
                        "validator_secure_key": row["validator_secure_key"],
                        "vault_assets_atto": row["vault_assets_atto"],
                        "exception_type": governor["exception_type"],
                        "destination_id": governor["destination_id"],
                        "destination_address": governor["destination"],
                        "destination_status": governor["status"],
                        "reason": governor["reason"],
                        "evidence": governor["evidence"],
                    }
                )
            if governor["status"] == "hold":
                unresolved.append(
                    {
                        "component": "validator_governor",
                        "source_address": validator,
                        "source_category": "validator_account",
                        "validator_address": validator,
                        "amount_atto": row["vault_assets_atto"],
                        "exception_type": governor["exception_type"],
                        "route_id": "",
                        "destination_id": governor["destination_id"],
                        "destination_address": governor["destination"],
                        "destination_status": governor["status"],
                        "reason": governor["reason"],
                        "evidence": governor["evidence"],
                    }
                )
    inactive_governors = set(governor_overrides) - seen_vaults
    if inactive_governors:
        raise ValueError(
            "governor overrides have no validator vault: "
            + ", ".join(sorted(inactive_governors))
        )
    governor_exceptions.sort(
        key=lambda row: row["validator_secure_key"].lower()
    )
    unresolved.sort(
        key=lambda row: (
            row["component"],
            row["source_address"].lower(),
            row["validator_address"].lower(),
            row["route_id"],
        )
    )

    write_csv(
        args.exceptions_output,
        ROUTING_EXCEPTION_FIELDS,
        routing_exceptions,
        replace=args.replace,
    )
    write_csv(
        args.governor_exceptions_output,
        GOVERNOR_EXCEPTION_FIELDS,
        governor_exceptions,
        replace=args.replace,
    )
    write_csv(
        args.unresolved_output,
        UNRESOLVED_FIELDS,
        unresolved,
        replace=args.replace,
    )

    source_wallet = sum(claim["wallet"] for claim in claims.values())
    source_staked = sum(claim["staked"] for claim in claims.values())
    routed_wallet = sum(
        int(row["wallet_airdrop_atto"]) for row in wallet_rows
    )
    routed_staked = sum(
        int(row["staked_to_vault_atto"]) for row in share_rows
    )
    if source_wallet != routed_wallet or source_staked != routed_staked:
        raise ValueError("routed components do not close to source claims")
    exception_wallet = sum(
        int(row["amount_atto"])
        for row in routing_exceptions
        if row["component"] == "wallet_airdrop"
    )
    exception_staked = sum(
        int(row["amount_atto"])
        for row in routing_exceptions
        if row["component"] == "vault_shares"
    )
    unresolved_wallet = sum(
        int(row["amount_atto"])
        for row in unresolved
        if row["component"] == "wallet_airdrop"
    )
    unresolved_staked = sum(
        int(row["amount_atto"])
        for row in unresolved
        if row["component"] == "vault_shares"
    )
    unresolved_governors = sum(
        row["component"] == "validator_governor" for row in unresolved
    )
    not_issued_wallet = sum(
        int(row["amount_atto"])
        for row in routing_exceptions
        if row["component"] == "wallet_airdrop"
        and row["destination_status"] == "not_issuing"
    )
    not_issued_staked = sum(
        int(row["amount_atto"])
        for row in routing_exceptions
        if row["component"] == "vault_shares"
        and row["destination_status"] == "not_issuing"
    )
    result = {
        "status": (
            "ready"
            if not unresolved_wallet
            and not unresolved_staked
            and not unresolved_governors
            and not inactive_routes
            and not pending_policy_decisions
            else "hold"
        ),
        "claims": len(claims),
        "priority_claims": sum(
            claim["category"] != "deferred" for claim in claims.values()
        ),
        "explicitly_routed_deferred_claims": sum(
            claim["category"] == "deferred" for claim in claims.values()
        ),
        "route_files": args.routes,
        "validator_accounts": args.validator_accounts,
        "contract_review_policy_state": contract_policy_state,
        "active_routes": sum(len(rows) for rows in routes_by_key.values()),
        "inactive_routes": inactive_routes,
        "pending_policy_decisions": pending_policy_decisions,
        "routing_exception_rows": len(routing_exceptions),
        "validator_account_exceptions": len(validator_accounts),
        "governor_exception_rows": len(governor_exceptions),
        "source_wallet_airdrop_atto": str(source_wallet),
        "source_staked_to_vault_atto": str(source_staked),
        "source_total_claim_atto": str(source_wallet + source_staked),
        "routed_wallet_airdrop_atto": str(routed_wallet),
        "routed_staked_to_vault_atto": str(routed_staked),
        "not_issued_wallet_airdrop_atto": str(not_issued_wallet),
        "not_issued_staked_to_vault_atto": str(not_issued_staked),
        "not_issued_total_claim_atto": str(
            not_issued_wallet + not_issued_staked
        ),
        "issuable_wallet_airdrop_atto": str(
            source_wallet - not_issued_wallet
        ),
        "issuable_staked_to_vault_atto": str(
            source_staked - not_issued_staked
        ),
        "issuable_total_claim_atto": str(
            source_wallet
            + source_staked
            - not_issued_wallet
            - not_issued_staked
        ),
        "exception_wallet_airdrop_atto": str(exception_wallet),
        "exception_staked_to_vault_atto": str(exception_staked),
        "implicit_wallet_airdrop_atto": str(
            routed_wallet - exception_wallet
        ),
        "implicit_staked_to_vault_atto": str(
            routed_staked - exception_staked
        ),
        "unresolved_wallet_airdrop_atto": str(unresolved_wallet),
        "unresolved_staked_to_vault_atto": str(unresolved_staked),
        "unresolved_governors": unresolved_governors,
        "outputs": {
            "routing_exceptions": {
                "path": args.exceptions_output,
                "sha256": file_sha256(args.exceptions_output),
            },
            "governor_exceptions": {
                "path": args.governor_exceptions_output,
                "sha256": file_sha256(args.governor_exceptions_output),
            },
            "unresolved": {
                "path": args.unresolved_output,
                "sha256": file_sha256(args.unresolved_output),
            },
        },
    }
    with open(args.summary + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
