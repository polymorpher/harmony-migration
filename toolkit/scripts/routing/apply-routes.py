#!/usr/bin/env python3

"""Apply explicit routes to direct-wallet and validator-vault claim delivery."""

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


EMPTY_CODE_HASH = (
    "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
)
REQUIRED_POLICY_DECISIONS = {
    "initial-wallet-activity-stage",
    "layerzero-nativeoft-reconciliation",
    "reviewed-contract-migration-policy",
    "rollback-exploit-proceeds",
    "wone-holder-redistribution",
}
DESTINATION_STATUSES = {"ready", "hold", "not_issuing", "redistributed"}
NOT_ISSUING_DESTINATION_ID = "not-issuing"
REDISTRIBUTED_DESTINATION_ID = "wone-holder-redistribution"
CONTRACT_POLICY_DESTINATIONS = {
    "reviewed_contract_allocation_not_issued": "not-issuing",
    "next_stage_multisig_recovery": "",
    "next_stage_onewallet_recovery_multisig": (
        "onewallet-recovery-multisig"
    ),
}
POLICY_DECISION_STAGE_SCOPE = {
    "layerzero-nativeoft-reconciliation": {"next_stage"},
    "rollback-exploit-proceeds": {
        "initial",
        "next_stage",
        "deferred",
        "manual_review",
    },
}
ROUTING_EXCEPTION_FIELDS = (
    "component",
    "source_secure_key",
    "source_address",
    "source_category",
    "source_code_bearing",
    "migration_stage",
    "issuance_treatment",
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
VAULT_STAGE_FIELDS = (
    "validator_address",
    "validator_secure_key",
    "base_vault_assets_atto",
    "initial_assets_atto",
    "next_stage_assets_atto",
    "qualified_deferred_assets_atto",
    "manual_review_assets_atto",
    "uncompiled_deferred_assets_atto",
    "not_issued_assets_atto",
    "post_policy_assets_atto",
)
UNRESOLVED_FIELDS = (
    "component",
    "source_address",
    "source_category",
    "migration_stage",
    "issuance_treatment",
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
    parser.add_argument("--migration-stages", required=True)
    parser.add_argument("--routes", action="append", default=[])
    parser.add_argument(
        "--destinations",
        action="append",
        required=True,
        help="destination CSV; repeatable",
    )
    parser.add_argument("--governors", required=True)
    parser.add_argument("--policy-decisions", required=True)
    parser.add_argument("--exceptions-output", required=True)
    parser.add_argument("--governor-exceptions-output", required=True)
    parser.add_argument("--vault-stage-output", required=True)
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
                wone = int(row.get("wone_airdrop_atto", "0") or 0)
                has_code = code_bearing(row)
                if (
                    min(wallet, wone, staked, total) < 0
                    or wone > wallet
                    or wallet + staked != total
                ):
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
                    "wone": wone,
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


def load_migration_stages(path):
    stages = {}
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "account_classification",
            "migration_stage",
            "issuance_treatment",
            "migration_wallet_allocation_atto",
            "migration_staked_to_vault_atto",
            "migration_allocation_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{path}: missing migration-stage fields {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            address = lib.any_to_hex(row["address"])
            if address in stages:
                raise ValueError(
                    f"{path}:{line}: duplicate migration-stage address"
                )
            stage = row["migration_stage"]
            if stage not in {"", "initial", "next_stage", "deferred"}:
                raise ValueError(
                    f"{path}:{line}: invalid migration stage {stage!r}"
                )
            treatment = row["issuance_treatment"]
            if treatment not in {"issue", "not_issued"}:
                raise ValueError(
                    f"{path}:{line}: invalid issuance treatment {treatment!r}"
                )
            allocation = int(row["migration_allocation_atto"])
            wallet = int(row["migration_wallet_allocation_atto"])
            staked = int(row["migration_staked_to_vault_atto"])
            if min(allocation, wallet, staked) < 0 or wallet + staked != allocation:
                raise ValueError(
                    f"{path}:{line}: invalid migration allocation components"
                )
            if treatment == "issue" and (not stage or allocation <= 0):
                raise ValueError(
                    f"{path}:{line}: issued allocation has no stage or amount"
                )
            if treatment == "not_issued" and (stage or allocation):
                raise ValueError(
                    f"{path}:{line}: not-issued allocation has stage or amount"
                )
            stages[address] = {
                "stage": stage,
                "treatment": treatment,
                "allocation": allocation,
                "wallet": wallet,
                "staked": staked,
                "classification": row["account_classification"],
            }
    return stages


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
            wone = int(row.get("wone_airdrop_atto", "0") or 0)
            has_code = code_bearing(row)
            if (
                min(wallet, wone, staked, total) < 0
                or wone > wallet
                or wallet + staked != total
            ):
                raise ValueError(f"{path}:{line}: deferred claim mismatch")
            if has_code is None:
                raise ValueError(
                    f"{path}:{line}: unresolved deferred code metadata"
                )
            claims[key] = {
                "key": key,
                "address": address,
                "wallet": wallet,
                "wone": wone,
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


def load_destinations(paths):
    destinations = {}
    for path in paths:
        with open(path, newline="") as source:
            reader = csv.DictReader(source)
            for line, row in enumerate(reader, start=2):
                destination_id = row["destination_id"].strip()
                if not destination_id or destination_id in destinations:
                    raise ValueError(
                        f"{path}:{line}: invalid or duplicate destination id"
                    )
                address = row["destination_address"].strip()
                if address:
                    normalized = lib.normalize_address(address)
                    if normalized is None:
                        raise ValueError(
                            f"{path}:{line}: invalid destination address"
                        )
                    try:
                        if len(bytes.fromhex(normalized[2:])) != 20:
                            raise ValueError
                    except ValueError as error:
                        raise ValueError(
                            f"{path}:{line}: invalid destination address"
                        ) from error
                    address = lib.to_checksum(normalized)
                status = row["status"].strip()
                if status not in DESTINATION_STATUSES:
                    raise ValueError(
                        f"{path}:{line}: invalid destination status"
                    )
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
                if status == "redistributed" and (
                    address or destination_id != REDISTRIBUTED_DESTINATION_ID
                ):
                    raise ValueError(
                        f"{path}:{line}: redistributed must use the "
                        "wone-holder-redistribution id with no address"
                    )
                if (
                    destination_id == REDISTRIBUTED_DESTINATION_ID
                    and status != "redistributed"
                ):
                    raise ValueError(
                        f"{path}:{line}: WONE redistribution destination must "
                        "have redistributed status"
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
        if explicit_status in {"not_issuing", "redistributed"}:
            raise ValueError(
                f"{explicit_status} destination cannot have an address"
            )
        return (
            destination_id,
            lib.to_checksum(lib.normalize_address(direct)),
            explicit_status or "ready",
        )
    if not destination_id:
        if explicit_status in {"ready", "not_issuing", "redistributed"}:
            raise ValueError(
                f"{explicit_status} destination has no address or id"
            )
        return "", "", "hold"
    if destination_id not in destinations:
        if explicit_status in {"ready", "not_issuing", "redistributed"}:
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
    if (
        explicit_status == "redistributed"
        and destination["status"] != "redistributed"
    ):
        raise ValueError(
            "explicit redistributed status requires the WONE redistribution "
            "destination"
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


def route_stage(claim, route):
    return claim["migration_stage"]


def route_treatment(route):
    if route["destination_status"] == "not_issuing":
        return "not_issued"
    if route["destination_status"] == "redistributed":
        return "redistributed"
    return "issue"


def append_wallet(rows, claim, amount, route):
    if amount == 0:
        return
    rows.append(
        {
            "source_secure_key": claim["key"],
            "source_address": lib.to_checksum(claim["address"]),
            "source_category": claim["routing_category"],
            "source_code_bearing": str(claim["code_bearing"]).lower(),
            "migration_stage": route["migration_stage"],
            "issuance_treatment": route["issuance_treatment"],
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
                "migration_stage": route["migration_stage"],
                "issuance_treatment": route["issuance_treatment"],
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
        args.vault_stage_output,
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
    migration_stages = load_migration_stages(args.migration_stages)
    destinations = load_destinations(args.destinations)
    pending_policy_decisions = load_policy_decisions(args.policy_decisions)
    routes = load_routes(args.routes, destinations)
    contract_policy_states = {
        tuple(route["policy_state"].values())
        for route in routes
        if route["reason"] in CONTRACT_POLICY_DESTINATIONS
        and route["policy_state"] is not None
    }
    contract_policy_without_state = [
        route["route_id"]
        for route in routes
        if route["reason"] in CONTRACT_POLICY_DESTINATIONS
        and route["policy_state"] is None
    ]
    invalid_contract_policy_destinations = [
        route["route_id"]
        for route in routes
        if route["reason"] in CONTRACT_POLICY_DESTINATIONS
        and route["destination_id"]
        != CONTRACT_POLICY_DESTINATIONS[route["reason"]]
    ]
    if invalid_contract_policy_destinations:
        raise ValueError(
            "generated reviewed-contract route uses the wrong destination"
        )
    if contract_policy_without_state or len(contract_policy_states) > 1:
        raise ValueError(
            "reviewed-contract routes must share one cutoff block, hash, "
            "and state root"
        )
    contract_policy_state = None
    if contract_policy_states:
        block, block_hash, state_root = contract_policy_states.pop()
        contract_policy_state = {
            "block": block,
            "block_hash": block_hash,
            "state_root": state_root,
        }
        if csv_policy_state(args.validator_accounts) != contract_policy_state:
            raise ValueError(
                "reviewed-contract routes and validator accounts must use "
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
    priority_addresses = {
        claim["address"]
        for claim in claims.values()
        if claim["category"] != "deferred"
    }
    if set(migration_stages) != priority_addresses:
        missing = sorted(priority_addresses - set(migration_stages))
        extra = sorted(set(migration_stages) - priority_addresses)
        raise ValueError(
            "migration-stage policy does not equal threshold claim set: "
            f"missing={missing}, extra={extra}"
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
        stage = migration_stages.get(claim["address"])
        if stage is None:
            claim["migration_stage"] = "manual_review"
            claim["issuance_treatment"] = "issue"
            claim["expected_migration_allocation"] = None
            claim["expected_migration_wallet"] = None
            claim["expected_migration_staked"] = None
        else:
            claim["migration_stage"] = stage["stage"]
            claim["issuance_treatment"] = stage["treatment"]
            claim["expected_migration_allocation"] = stage["allocation"]
            claim["expected_migration_wallet"] = stage["wallet"]
            claim["expected_migration_staked"] = stage["staked"]
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
        for source_route in routes_by_key.get(key, []):
            route = dict(source_route)
            route["migration_stage"] = route_stage(claim, route)
            route["issuance_treatment"] = route_treatment(route)
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
            "migration_stage": claim["migration_stage"],
            "issuance_treatment": "issue",
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
                "migration_stage": row["migration_stage"],
                "issuance_treatment": row["issuance_treatment"],
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
                "migration_stage": row["migration_stage"],
                "issuance_treatment": row["issuance_treatment"],
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
            if status in {"not_issuing", "redistributed"}:
                raise ValueError(
                    f"{status} is not a validator-governor destination"
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
    base_vaults = {}
    with open(args.base_vault_deposits, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            lib.require_address_secure_key(
                row["validator_address"],
                row["validator_secure_key"],
                f"{args.base_vault_deposits}:{line} validator",
            )
            validator = lib.any_to_hex(row["validator_address"])
            if validator in base_vaults:
                raise ValueError(
                    f"{args.base_vault_deposits}:{line}: duplicate validator"
                )
            seen_vaults.add(validator)
            base_vaults[validator] = {
                "address": row["validator_address"],
                "key": row["validator_secure_key"].lower(),
                "assets": int(row["vault_assets_atto"]),
            }
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
                        "migration_stage": "manual_review",
                        "issuance_treatment": "issue",
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
    represented_vault_stages = defaultdict(Counter)
    for row in share_rows:
        validator = lib.any_to_hex(row["validator_address"])
        if validator not in base_vaults:
            raise ValueError(
                f"routed share references unknown validator {validator}"
            )
        bucket = (
            "not_issued"
            if row["issuance_treatment"] == "not_issued"
            else row["migration_stage"]
        )
        if not bucket or row["issuance_treatment"] == "redistributed":
            raise ValueError("invalid vault stage/treatment combination")
        represented_vault_stages[validator][bucket] += int(
            row["staked_to_vault_atto"]
        )
    vault_stage_rows = []
    for validator, base in sorted(
        base_vaults.items(), key=lambda item: item[1]["key"]
    ):
        stages = represented_vault_stages[validator]
        represented = sum(stages.values())
        uncompiled_deferred = base["assets"] - represented
        if uncompiled_deferred < 0:
            raise ValueError(
                f"routed vault shares exceed base assets for {validator}"
            )
        not_issued = stages["not_issued"]
        post_policy = base["assets"] - not_issued
        if (
            stages["initial"]
            + stages["next_stage"]
            + stages["deferred"]
            + stages["manual_review"]
            + uncompiled_deferred
            != post_policy
        ):
            raise ValueError(
                f"vault stage assets do not close for {validator}"
            )
        vault_stage_rows.append(
            {
                "validator_address": base["address"],
                "validator_secure_key": base["key"],
                "base_vault_assets_atto": str(base["assets"]),
                "initial_assets_atto": str(stages["initial"]),
                "next_stage_assets_atto": str(stages["next_stage"]),
                "qualified_deferred_assets_atto": str(stages["deferred"]),
                "manual_review_assets_atto": str(stages["manual_review"]),
                "uncompiled_deferred_assets_atto": str(uncompiled_deferred),
                "not_issued_assets_atto": str(not_issued),
                "post_policy_assets_atto": str(post_policy),
            }
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
        args.vault_stage_output,
        VAULT_STAGE_FIELDS,
        vault_stage_rows,
        replace=args.replace,
    )
    write_csv(
        args.unresolved_output,
        UNRESOLVED_FIELDS,
        unresolved,
        replace=args.replace,
    )

    source_wallet = sum(claim["wallet"] for claim in claims.values())
    source_wone = sum(claim["wone"] for claim in claims.values())
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
    redistributed_wallet = sum(
        int(row["amount_atto"])
        for row in routing_exceptions
        if row["component"] == "wallet_airdrop"
        and row["destination_status"] == "redistributed"
    )
    redistributed_staked = sum(
        int(row["amount_atto"])
        for row in routing_exceptions
        if row["component"] == "vault_shares"
        and row["destination_status"] == "redistributed"
    )
    wone_retained_not_issued = sum(
        int(row["amount_atto"])
        for row in routing_exceptions
        if row["route_id"] == "wone-reserve-remainder-not-issued"
        and row["destination_status"] == "not_issuing"
    )
    wone_recipient_not_issued = sorted(
        {
            lib.any_to_hex(row["source_address"])
            for row in routing_exceptions
            if row["destination_status"] == "not_issuing"
            and row["component"] == "wallet_airdrop"
            and claims[
                by_address[lib.any_to_hex(row["source_address"])]
            ]["wone"]
            > 0
        }
    )
    invalid_wone_recipient_non_issuance = [
        address
        for address in wone_recipient_not_issued
        if address not in migration_stages
        or migration_stages[address]["classification"] != "genuine_contract"
        or migration_stages[address]["treatment"] != "not_issued"
    ]
    if invalid_wone_recipient_non_issuance:
        raise ValueError(
            "component-aware WONE non-issuance is required for: "
            + ", ".join(invalid_wone_recipient_non_issuance)
        )
    if redistributed_staked:
        raise ValueError("WONE redistribution cannot consume vault shares")
    if redistributed_wallet != source_wone:
        raise ValueError(
            "redistributed WONE reserve does not equal holder WONE airdrops"
        )
    wone_reserve_source = redistributed_wallet + wone_retained_not_issued
    compiled_by_address = defaultdict(int)
    compiled_wallet_by_address = defaultdict(int)
    compiled_staked_by_address = defaultdict(int)
    stage_wallet = Counter()
    stage_staked = Counter()
    ready_stage_wallet = Counter()
    ready_stage_staked = Counter()
    held_stage_wallet = Counter()
    held_stage_staked = Counter()
    treatment_wallet = Counter()
    treatment_staked = Counter()
    for row in wallet_rows:
        amount = int(row["wallet_airdrop_atto"])
        stage = row["migration_stage"]
        treatment = row["issuance_treatment"]
        if treatment != route_treatment(row):
            raise ValueError("wallet issuance treatment contradicts destination")
        treatment_wallet[treatment] += amount
        if treatment == "issue":
            if not stage:
                raise ValueError("issued wallet row has no migration stage")
            stage_wallet[stage] += amount
            if row["destination_status"] == "ready":
                ready_stage_wallet[stage] += amount
            elif row["destination_status"] == "hold":
                held_stage_wallet[stage] += amount
            else:
                raise ValueError("issued wallet row has terminal status")
            address = lib.any_to_hex(row["source_address"])
            compiled_by_address[address] += amount
            compiled_wallet_by_address[address] += amount
    for row in share_rows:
        amount = int(row["staked_to_vault_atto"])
        stage = row["migration_stage"]
        treatment = row["issuance_treatment"]
        if treatment != route_treatment(row):
            raise ValueError("vault issuance treatment contradicts destination")
        if treatment == "redistributed":
            raise ValueError("redistribution cannot contain vault shares")
        treatment_staked[treatment] += amount
        if treatment == "issue":
            if not stage:
                raise ValueError("issued vault row has no migration stage")
            stage_staked[stage] += amount
            if row["destination_status"] == "ready":
                ready_stage_staked[stage] += amount
            elif row["destination_status"] == "hold":
                held_stage_staked[stage] += amount
            else:
                raise ValueError("issued vault row has terminal status")
            address = lib.any_to_hex(row["delegator_address"])
            compiled_by_address[address] += amount
            compiled_staked_by_address[address] += amount
    stage_policy_mismatches = [
        address
        for address, stage in migration_stages.items()
        if (
            compiled_by_address[address] != stage["allocation"]
            or compiled_wallet_by_address[address] != stage["wallet"]
            or compiled_staked_by_address[address] != stage["staked"]
        )
    ]
    if stage_policy_mismatches:
        raise ValueError(
            "compiled routing does not match migration-stage allocation for: "
            + ", ".join(sorted(stage_policy_mismatches))
        )
    pending_stage_review_claims = sum(
        claim["migration_stage"] == "manual_review"
        and compiled_by_address[claim["address"]] > 0
        for claim in claims.values()
    )
    all_stages = sorted(set(stage_wallet) | set(stage_staked))
    stage_totals = {
        stage: {
            "wallet_airdrop_atto": str(stage_wallet[stage]),
            "staked_to_vault_atto": str(stage_staked[stage]),
            "total_claim_atto": str(
                stage_wallet[stage] + stage_staked[stage]
            ),
            "ready_wallet_airdrop_atto": str(ready_stage_wallet[stage]),
            "ready_staked_to_vault_atto": str(ready_stage_staked[stage]),
            "ready_total_claim_atto": str(
                ready_stage_wallet[stage] + ready_stage_staked[stage]
            ),
            "held_wallet_airdrop_atto": str(held_stage_wallet[stage]),
            "held_staked_to_vault_atto": str(held_stage_staked[stage]),
            "held_total_claim_atto": str(
                held_stage_wallet[stage] + held_stage_staked[stage]
            ),
        }
        for stage in all_stages
    }
    all_treatments = sorted(set(treatment_wallet) | set(treatment_staked))
    issuance_treatment_totals = {
        treatment: {
            "wallet_airdrop_atto": str(treatment_wallet[treatment]),
            "staked_to_vault_atto": str(treatment_staked[treatment]),
            "total_claim_atto": str(
                treatment_wallet[treatment] + treatment_staked[treatment]
            ),
        }
        for treatment in all_treatments
    }
    if (
        treatment_wallet["not_issued"] != not_issued_wallet
        or treatment_staked["not_issued"] != not_issued_staked
        or treatment_wallet["redistributed"] != redistributed_wallet
        or treatment_staked["redistributed"] != redistributed_staked
    ):
        raise ValueError("issuance-treatment totals do not close")
    vault_stage_totals = {
        field: sum(int(row[field]) for row in vault_stage_rows)
        for field in VAULT_STAGE_FIELDS
        if field.endswith("_atto")
    }
    if (
        vault_stage_totals["base_vault_assets_atto"]
        - vault_stage_totals["not_issued_assets_atto"]
        != vault_stage_totals["post_policy_assets_atto"]
        or vault_stage_totals["not_issued_assets_atto"]
        != not_issued_staked
    ):
        raise ValueError("compiled vault-stage totals do not close")
    held_governor_assets = Counter()
    vault_stage_by_validator = {
        lib.any_to_hex(row["validator_address"]): row
        for row in vault_stage_rows
    }
    for row in governor_exceptions:
        if row["destination_status"] != "hold":
            continue
        stages = vault_stage_by_validator[
            lib.any_to_hex(row["validator_address"])
        ]
        held_governor_assets["initial"] += int(
            stages["initial_assets_atto"]
        )
        held_governor_assets["next_stage"] += int(
            stages["next_stage_assets_atto"]
        )
        held_governor_assets["deferred"] += int(
            stages["qualified_deferred_assets_atto"]
        ) + int(stages["uncompiled_deferred_assets_atto"])
        held_governor_assets["manual_review"] += int(
            stages["manual_review_assets_atto"]
        )
    stage_policy_gates = defaultdict(list)
    release_stages = {"initial"}
    for decision in pending_policy_decisions:
        scope = POLICY_DECISION_STAGE_SCOPE.get(
            decision,
            {"initial", "next_stage", "deferred", "manual_review"},
        )
        for stage in scope:
            stage_policy_gates[stage].append(decision)
    stage_readiness = {}
    for stage in ("initial", "next_stage", "deferred", "manual_review"):
        totals = stage_totals.get(
            stage,
            {
                "wallet_airdrop_atto": "0",
                "staked_to_vault_atto": "0",
                "total_claim_atto": "0",
                "ready_wallet_airdrop_atto": "0",
                "ready_staked_to_vault_atto": "0",
                "ready_total_claim_atto": "0",
                "held_wallet_airdrop_atto": "0",
                "held_staked_to_vault_atto": "0",
                "held_total_claim_atto": "0",
            },
        )
        held_delivery = int(totals["held_total_claim_atto"])
        governor_hold = held_governor_assets[stage]
        policy_gates = sorted(stage_policy_gates[stage])
        release_authorized = stage in release_stages
        blockers = []
        if held_delivery:
            blockers.append("unresolved_delivery")
        if governor_hold:
            blockers.append("unresolved_validator_governor")
        blockers.extend(f"policy:{decision}" for decision in policy_gates)
        if not release_authorized:
            blockers.append("stage_release_not_authorized")
        stage_readiness[stage] = {
            **totals,
            "held_validator_governor_assets_atto": str(governor_hold),
            "pending_policy_decisions": policy_gates,
            "release_authorized": release_authorized,
            "status": "ready" if not blockers else "hold",
            "blockers": blockers,
        }
    result = {
        "status": (
            "ready"
            if not unresolved_wallet
            and not unresolved_staked
            and not unresolved_governors
            and not inactive_routes
            and not pending_policy_decisions
            and not pending_stage_review_claims
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
        "route_inputs": [
            {"path": path, "sha256": file_sha256(path)}
            for path in args.routes
        ],
        "destination_files": args.destinations,
        "destination_inputs": [
            {"path": path, "sha256": file_sha256(path)}
            for path in args.destinations
        ],
        "validator_accounts": args.validator_accounts,
        "migration_stages": args.migration_stages,
        "migration_stages_sha256": file_sha256(args.migration_stages),
        "migration_stage_policy_rows": len(migration_stages),
        "pending_stage_review_claims": pending_stage_review_claims,
        "stage_totals": stage_totals,
        "stage_readiness": stage_readiness,
        "initial_stage_status": stage_readiness["initial"]["status"],
        "issuance_treatment_totals": issuance_treatment_totals,
        "vault_stage_rows": len(vault_stage_rows),
        "vault_stage_totals": {
            field: str(value)
            for field, value in sorted(vault_stage_totals.items())
        },
        "contract_review_policy_state": contract_policy_state,
        "active_routes": sum(len(rows) for rows in routes_by_key.values()),
        "inactive_routes": inactive_routes,
        "pending_policy_decisions": pending_policy_decisions,
        "routing_exception_rows": len(routing_exceptions),
        "validator_account_exceptions": len(validator_accounts),
        "governor_exception_rows": len(governor_exceptions),
        "source_wallet_airdrop_atto": str(source_wallet),
        "source_wone_airdrop_atto": str(source_wone),
        "source_staked_to_vault_atto": str(source_staked),
        "source_total_claim_atto": str(source_wallet + source_staked),
        "routed_wallet_airdrop_atto": str(routed_wallet),
        "routed_staked_to_vault_atto": str(routed_staked),
        "not_issued_wallet_airdrop_atto": str(not_issued_wallet),
        "not_issued_staked_to_vault_atto": str(not_issued_staked),
        "not_issued_total_claim_atto": str(
            not_issued_wallet + not_issued_staked
        ),
        "redistributed_wallet_airdrop_atto": str(redistributed_wallet),
        "redistributed_staked_to_vault_atto": str(redistributed_staked),
        "redistributed_total_claim_atto": str(
            redistributed_wallet + redistributed_staked
        ),
        "wone_reserve_source_atto": str(wone_reserve_source),
        "wone_redistributed_to_holders_atto": str(
            redistributed_wallet
        ),
        "wone_retained_not_issued_atto": str(
            wone_retained_not_issued
        ),
        "wone_recipient_not_issued_rows": len(
            wone_recipient_not_issued
        ),
        "issuable_wallet_airdrop_atto": str(
            source_wallet - not_issued_wallet - redistributed_wallet
        ),
        "issuable_staked_to_vault_atto": str(
            source_staked - not_issued_staked - redistributed_staked
        ),
        "issuable_total_claim_atto": str(
            source_wallet
            + source_staked
            - not_issued_wallet
            - not_issued_staked
            - redistributed_wallet
            - redistributed_staked
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
            "vault_stages": {
                "path": args.vault_stage_output,
                "sha256": file_sha256(args.vault_stage_output),
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
