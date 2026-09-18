#!/usr/bin/env python3

"""Build validator-vault deposits and share-entitlement routing ledgers."""

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


ATTO_PER_ONE = 10**18
DETAIL_FIELDS = (
    "validator_address",
    "validator_secure_key",
    "delegator_address",
    "delegator_secure_key",
    "staked_to_vault_atto",
    "is_self_delegation",
)


def parse_one(value):
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    atto = amount * ATTO_PER_ONE
    if amount < 0 or atto != atto.to_integral_value():
        raise argparse.ArgumentTypeError("invalid exact ONE threshold")
    return int(atto)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-claims", required=True)
    parser.add_argument("--delegations", required=True)
    parser.add_argument("--automatic-claims", required=True)
    parser.add_argument("--contract-review-claims", required=True)
    parser.add_argument("--excluded-claims", required=True)
    parser.add_argument("--vault-deposits-output", required=True)
    parser.add_argument("--priority-shares-output", required=True)
    parser.add_argument("--deferred-shares-output", required=True)
    parser.add_argument("--automatic-wallet-output", required=True)
    parser.add_argument("--contract-wallet-output", required=True)
    parser.add_argument("--excluded-wallet-output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument(
        "--minimum-one",
        required=True,
        type=parse_one,
    )
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_allocation_fields(row, location):
    total_claim = int(row["total_claim_atto"])
    wallet = int(row["wallet_airdrop_atto"])
    staked = int(row["staked_to_vault_atto"])
    if (
        min(total_claim, wallet, staked) < 0
        or wallet + staked != total_claim
    ):
        raise ValueError(f"{location}: allocation component mismatch")
    return total_claim, wallet, staked


def load_all_claims(path):
    claims = {}
    previous = None
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "secure_key",
            "address",
            "total_claim_atto",
            "wallet_airdrop_atto",
            "staked_to_vault_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing fields {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(
                    f"{path}:{line}: secure keys are not increasing"
                )
            previous = key
            if key in claims:
                raise ValueError(f"{path}:{line}: duplicate secure key")
            total_claim, wallet, staked = require_allocation_fields(
                row, f"{path}:{line}"
            )
            wone = int(row.get("wone_airdrop_atto", "0") or 0)
            qualification_total = int(
                row.get("qualification_total_atto", total_claim)
            )
            if (
                wone < 0
                or wone > wallet
                or qualification_total < total_claim
            ):
                raise ValueError(f"{path}:{line}: invalid WONE overlay")
            claims[key] = {
                "address": row["address"].lower(),
                "total_claim": total_claim,
                "wallet": wallet,
                "wone": wone,
                "qualification_total": qualification_total,
                "staked": staked,
            }
    return claims


def load_category(path, category, claims, assignments):
    totals = {
        "rows": 0,
        "wallet": 0,
        "wone": 0,
        "staked": 0,
        "total_claim": 0,
    }
    with open(path, newline="") as source:
        for line, row in enumerate(csv.DictReader(source), start=2):
            key = row["secure_key"].lower()
            if key not in claims:
                raise ValueError(f"{path}:{line}: unknown secure key")
            if key in assignments:
                raise ValueError(f"{path}:{line}: category overlap")
            address = lib.require_address_secure_key(
                row["address"],
                key,
                f"{path}:{line}",
            )
            total_claim, wallet, staked = require_allocation_fields(
                row, f"{path}:{line}"
            )
            expected = claims[key]
            if (
                total_claim != expected["total_claim"]
                or wallet != expected["wallet"]
                or staked != expected["staked"]
                or address != expected["address"]
                or int(row.get("wone_airdrop_atto", "0") or 0)
                != expected["wone"]
            ):
                raise ValueError(f"{path}:{line}: claim mismatch")
            assignments[key] = category
            totals["rows"] += 1
            totals["wallet"] += wallet
            totals["wone"] += expected["wone"]
            totals["staked"] += staked
            totals["total_claim"] += total_claim
    return totals


def write_csv(path, fields, rows):
    if os.path.exists(path) or os.path.exists(path + ".partial"):
        raise FileExistsError(path)
    with open(path + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(path + ".partial", path)


def main():
    args = parse_args()
    outputs = (
        args.vault_deposits_output,
        args.priority_shares_output,
        args.deferred_shares_output,
        args.automatic_wallet_output,
        args.contract_wallet_output,
        args.excluded_wallet_output,
        args.summary,
    )
    for path in outputs:
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(path)

    claims = load_all_claims(args.all_claims)
    assignments = {}
    category_totals = {}
    for path, category in (
        (args.automatic_claims, "automatic"),
        (args.contract_review_claims, "contract_review"),
        (args.excluded_claims, "excluded"),
    ):
        category_totals[category] = load_category(
            path, category, claims, assignments
        )

    expected_priority = {
        key
        for key, claim in claims.items()
        if claim["qualification_total"] >= args.minimum_one
    }
    if set(assignments) != expected_priority:
        raise ValueError(
            "eligibility category union does not match total-claim threshold"
        )

    by_delegator = defaultdict(int)
    by_validator = {}
    priority_rows = []
    deferred_rows = []
    seen_pairs = set()
    total_detail = 0
    with open(args.delegations, newline="") as source:
        reader = csv.DictReader(source)
        missing = set(DETAIL_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{args.delegations}: missing fields {sorted(missing)}"
            )
        for line, row in enumerate(reader, start=2):
            validator_key = row["validator_secure_key"].lower()
            delegator_key = row["delegator_secure_key"].lower()
            lib.require_address_secure_key(
                row["validator_address"],
                validator_key,
                f"{args.delegations}:{line} validator",
            )
            lib.require_address_secure_key(
                row["delegator_address"],
                delegator_key,
                f"{args.delegations}:{line} delegator",
            )
            pair = (validator_key, delegator_key)
            if pair in seen_pairs:
                raise ValueError(
                    f"{args.delegations}:{line}: duplicate delegation"
                )
            seen_pairs.add(pair)
            amount = int(row["staked_to_vault_atto"])
            if amount <= 0:
                raise ValueError(
                    f"{args.delegations}:{line}: non-positive delegation"
                )
            claim = claims.get(delegator_key)
            if claim is None:
                raise ValueError(
                    f"{args.delegations}:{line}: unknown delegator"
                )
            if claim["address"] != row["delegator_address"].lower():
                raise ValueError(
                    f"{args.delegations}:{line}: delegator address mismatch"
                )
            by_delegator[delegator_key] += amount
            total_detail += amount

            vault = by_validator.setdefault(
                validator_key,
                {
                    "validator_address": row["validator_address"],
                    "validator_secure_key": validator_key,
                    "vault_assets_atto": 0,
                    "priority_staked_to_vault_atto": 0,
                    "deferred_staked_to_vault_atto": 0,
                    "delegation_rows": 0,
                },
            )
            if vault["validator_address"].lower() != row[
                "validator_address"
            ].lower():
                raise ValueError(
                    f"{args.delegations}:{line}: validator address mismatch"
                )
            vault["vault_assets_atto"] += amount
            vault["delegation_rows"] += 1

            base = {field: row[field] for field in DETAIL_FIELDS}
            category = assignments.get(delegator_key)
            if category is None:
                vault["deferred_staked_to_vault_atto"] += amount
                deferred_rows.append(base)
            else:
                vault["priority_staked_to_vault_atto"] += amount
                destination = (
                    row["delegator_address"]
                    if category == "automatic"
                    else ""
                )
                status = {
                    "automatic": "same-address",
                    "contract_review": "manual-contract-recovery",
                    "excluded": "policy-routed",
                }[category]
                base.update(
                    {
                        "allocation_category": category,
                        "share_destination_address": destination,
                        "destination_status": status,
                    }
                )
                priority_rows.append(base)

    for key, claim in claims.items():
        if by_delegator.get(key, 0) != claim["staked"]:
            raise ValueError(
                f"vault-share detail mismatch for secure key {key}"
            )

    vault_rows = sorted(
        by_validator.values(),
        key=lambda row: row["validator_secure_key"],
    )
    priority_rows.sort(
        key=lambda row: (
            row["validator_secure_key"],
            row["delegator_secure_key"],
        )
    )
    deferred_rows.sort(
        key=lambda row: (
            row["validator_secure_key"],
            row["delegator_secure_key"],
        )
    )
    priority_fields = DETAIL_FIELDS + (
        "allocation_category",
        "share_destination_address",
        "destination_status",
    )
    write_csv(
        args.vault_deposits_output,
        (
            "validator_address",
            "validator_secure_key",
            "vault_assets_atto",
            "priority_staked_to_vault_atto",
            "deferred_staked_to_vault_atto",
            "delegation_rows",
        ),
        vault_rows,
    )
    write_csv(args.priority_shares_output, priority_fields, priority_rows)
    write_csv(args.deferred_shares_output, DETAIL_FIELDS, deferred_rows)

    wallet_fields = (
        "secure_key",
        "source_address",
        "native_wallet_airdrop_atto",
        "wone_airdrop_atto",
        "wallet_airdrop_atto",
        "staked_to_vault_atto",
        "total_claim_atto",
        "allocation_category",
        "destination_address",
        "destination_status",
    )
    wallet_rows = {
        "automatic": [],
        "contract_review": [],
        "excluded": [],
    }
    wallet_status = {
        "automatic": "same-address",
        "contract_review": "manual-contract-recovery",
        "excluded": "policy-routed",
    }
    for key, category in sorted(assignments.items()):
        claim = claims[key]
        if claim["wallet"] == 0:
            continue
        wallet_rows[category].append(
            {
                "secure_key": key,
                "source_address": claim["address"],
                "native_wallet_airdrop_atto": str(
                    claim["wallet"] - claim["wone"]
                ),
                "wone_airdrop_atto": str(claim["wone"]),
                "wallet_airdrop_atto": str(claim["wallet"]),
                "staked_to_vault_atto": str(claim["staked"]),
                "total_claim_atto": str(claim["total_claim"]),
                "allocation_category": category,
                "destination_address": (
                    claim["address"] if category == "automatic" else ""
                ),
                "destination_status": wallet_status[category],
            }
        )
    write_csv(
        args.automatic_wallet_output,
        wallet_fields,
        wallet_rows["automatic"],
    )
    write_csv(
        args.contract_wallet_output,
        wallet_fields,
        wallet_rows["contract_review"],
    )
    write_csv(
        args.excluded_wallet_output,
        wallet_fields,
        wallet_rows["excluded"],
    )

    total_vault_principal = sum(
        claim["staked"] for claim in claims.values()
    )
    if total_detail != total_vault_principal:
        raise ValueError("global vault-share detail mismatch")
    for category, rows in wallet_rows.items():
        if (
            sum(int(row["wallet_airdrop_atto"]) for row in rows)
            != category_totals[category]["wallet"]
        ):
            raise ValueError(
                f"{category} direct wallet-airdrop total mismatch"
            )
    result = {
        "priority_label_semantics": (
            "legacy name for the inclusive snapshot-threshold base set; "
            "not the six-month initial migration stage"
        ),
        "minimum_atto": str(args.minimum_one),
        "all_claims_sha256": file_sha256(args.all_claims),
        "delegations_sha256": file_sha256(args.delegations),
        "vaults": len(vault_rows),
        "delegation_rows": len(seen_pairs),
        "vault_assets_atto": str(total_detail),
        "priority_staked_to_vault_atto": str(
            sum(
                int(row["staked_to_vault_atto"])
                for row in priority_rows
            )
        ),
        "deferred_staked_to_vault_atto": str(
            sum(
                int(row["staked_to_vault_atto"])
                for row in deferred_rows
            )
        ),
        "category_totals": {
            category: {
                key: str(value) if key != "rows" else value
                for key, value in totals.items()
            }
            for category, totals in category_totals.items()
        },
        "outputs": {
            "vault_deposits": {
                "path": args.vault_deposits_output,
                "sha256": file_sha256(args.vault_deposits_output),
            },
            "priority_shares": {
                "path": args.priority_shares_output,
                "sha256": file_sha256(args.priority_shares_output),
            },
            "deferred_shares": {
                "path": args.deferred_shares_output,
                "sha256": file_sha256(args.deferred_shares_output),
            },
            "automatic_wallet_airdrop": {
                "path": args.automatic_wallet_output,
                "sha256": file_sha256(args.automatic_wallet_output),
            },
            "contract_wallet_base": {
                "path": args.contract_wallet_output,
                "sha256": file_sha256(args.contract_wallet_output),
            },
            "excluded_wallet_routing": {
                "path": args.excluded_wallet_output,
                "sha256": file_sha256(args.excluded_wallet_output),
            },
        },
        "status": "passed",
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
