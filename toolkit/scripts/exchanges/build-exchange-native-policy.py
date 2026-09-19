#!/usr/bin/env python3

"""Build exchange-local native ONE totals and Binance.US vault adjustments."""

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


EXCHANGE_ORDER = ("binance", "binance-us", "mexc", "okx", "kucoin")
NATIVE_DELIVERY_FIELDS = (
    "exchange_id",
    "destination_address",
    "inventory_wallets",
    "positive_native_claims",
    "native_wallet_atto",
    "delegated_released_atto",
    "direct_delivery_atto",
)
BINANCE_WALLET_FIELDS = (
    "source_address",
    "address_one",
    "liquid_shard0_atto",
    "liquid_shard1_atto",
    "pending_undelegation_atto",
    "unclaimed_staking_reward_atto",
    "pending_cross_shard_atto",
    "native_wallet_atto",
    "delegated_released_atto",
    "direct_delivery_atto",
    "destination_address",
)
DELEGATION_FIELDS = (
    "validator_address",
    "validator_secure_key",
    "delegator_address",
    "delegator_secure_key",
    "staked_released_atto",
    "destination_address",
)
VAULT_FIELDS = (
    "validator_address",
    "validator_secure_key",
    "base_vault_assets_atto",
    "binance_us_withdrawal_atto",
    "adjusted_vault_assets_atto",
    "delegation_rows_removed",
)
GATE_ROUTE_FIELDS = (
    "source_address",
    "destination_address",
    "destination_one",
    "native_wallet_atto",
    "staked_to_vault_atto",
    "native_total_claim_atto",
    "reason",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audits-dir", required=True)
    parser.add_argument("--native-claims", required=True)
    parser.add_argument("--delegations", required=True)
    parser.add_argument("--vaults", required=True)
    parser.add_argument("--gate-addition", required=True)
    parser.add_argument("--gate-destination", required=True)
    parser.add_argument("--output-dir", required=True)
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


def load_gate_route(addition_path, destination_path):
    source_value = Path(addition_path).read_text(encoding="utf-8").strip()
    destination_value = Path(destination_path).read_text(
        encoding="utf-8"
    ).strip()
    source_address = lib.any_to_hex(source_value)
    destination_address = lib.any_to_hex(destination_value)
    if source_address is None or destination_address is None:
        raise ValueError("Gate source or destination is invalid")
    return {
        "source_address": lib.to_checksum(source_address),
        "destination_address": lib.to_checksum(destination_address),
        "destination_one": lib.hex_to_bech32(destination_address),
        "reason": "gate_designated_native_delivery",
    }


def one(value, commas=False):
    whole, fraction = divmod(int(value), 10**18)
    return f"{whole:,}.{fraction:018d}" if commas else f"{whole}.{fraction:018d}"


def atomic_csv(path, fields, rows, replace):
    path = Path(path)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def atomic_json(path, value, replace):
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


def atomic_text(path, value, replace):
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


def load_audit(path, exchange_id):
    rows = []
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "exchange_id",
            "address_hex",
            "address_one",
            "claim_status",
            "native_wallet_airdrop_atto",
            "staked_to_vault_atto",
            "native_total_claim_atto",
            "configured_destination",
            "planned_total_entitlement_atto",
        }
        if not required <= set(reader.fieldnames or ()):
            raise ValueError(f"{path}: missing native exchange fields")
        for line, row in enumerate(reader, start=2):
            if row["exchange_id"] != exchange_id:
                raise ValueError(f"{path}:{line}: exchange id mismatch")
            wallet = int(row["native_wallet_airdrop_atto"])
            staked = int(row["staked_to_vault_atto"])
            total = int(row["native_total_claim_atto"])
            if min(wallet, staked, total) < 0 or wallet + staked != total:
                raise ValueError(f"{path}:{line}: native arithmetic mismatch")
            rows.append(row)
    return rows


def audit_summary(rows):
    destinations = {
        row["configured_destination"]
        for row in rows
        if row["configured_destination"]
    }
    if len(destinations) > 1:
        raise ValueError("exchange audit has mixed destinations")
    wallet = sum(int(row["native_wallet_airdrop_atto"]) for row in rows)
    staked = sum(int(row["staked_to_vault_atto"]) for row in rows)
    total = sum(int(row["native_total_claim_atto"]) for row in rows)
    if wallet + staked != total:
        raise ValueError("exchange native totals do not close")
    return {
        "inventory_wallets": len(rows),
        "claim_rows": sum(row["claim_status"] == "present" for row in rows),
        "positive_native_claims": sum(
            int(row["native_total_claim_atto"]) > 0 for row in rows
        ),
        "native_wallet_atto": wallet,
        "staked_to_vault_atto": staked,
        "native_total_claim_atto": total,
        "destination_address": next(iter(destinations), ""),
    }


def load_native_components(path, address_to_exchange):
    component_fields = (
        "liquid_shard0_atto",
        "liquid_shard1_atto",
        "liquid_total_atto",
        "active_staked_or_delegated_atto",
        "pending_undelegation_atto",
        "unclaimed_staking_reward_atto",
        "pending_cross_shard_atto",
        "wallet_airdrop_atto",
        "staked_to_vault_atto",
        "total_claim_atto",
    )
    records = {}
    totals = defaultdict(lambda: defaultdict(int))
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {"address", *component_fields}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError("native claim ledger is missing component fields")
        for line, row in enumerate(reader, start=2):
            address = row["address"].lower()
            exchange_id = address_to_exchange.get(address)
            if exchange_id is None:
                continue
            if address in records:
                raise ValueError(
                    f"{path}:{line}: duplicate native claim address"
                )
            values = {
                field: int(row[field]) for field in component_fields
            }
            if min(values.values()) < 0:
                raise ValueError(f"{path}:{line}: negative native component")
            if (
                values["liquid_shard0_atto"]
                + values["liquid_shard1_atto"]
                != values["liquid_total_atto"]
                or values["liquid_total_atto"]
                + values["pending_undelegation_atto"]
                + values["unclaimed_staking_reward_atto"]
                + values["pending_cross_shard_atto"]
                != values["wallet_airdrop_atto"]
                or values["active_staked_or_delegated_atto"]
                != values["staked_to_vault_atto"]
                or values["wallet_airdrop_atto"]
                + values["staked_to_vault_atto"]
                != values["total_claim_atto"]
            ):
                raise ValueError(
                    f"{path}:{line}: native component arithmetic mismatch"
                )
            records[address] = values
            for field, value in values.items():
                totals[exchange_id][field] += value
    return records, totals


def render_report(result):
    rows = [
        (
            item["display_name"],
            f"{item['inventory_wallets']:,}",
            f"{item['positive_native_claims']:,}",
            one(item["native_wallet_atto"], True),
            one(item["delegated_released_atto"], True),
            one(item["direct_delivery_atto"], True),
            item["destination_address"] or "pending",
        )
        for exchange_id, item in result["exchanges"].items()
        if exchange_id != "binance"
    ]
    table = [
        "| Exchange | Inventory wallets | Positive native claims | "
        "Native wallet ONE | Delegated ONE released | Direct aggregate ONE | "
        "Destination |",
        "|---|---|---|---|---|---|---|",
        *("| " + " | ".join(row) + " |" for row in rows),
    ]
    gate = result["gate"]
    return f"""# Exchange native ONE delivery and Gate reconciliation

All amounts below are independently summed from row-level cutoff native-ONE
accounting. Exchange-submitted totals are not trusted. WONE is excluded because
it was not part of the exchange submissions.

## Non-Gate aggregate native delivery

{chr(10).join(table)}

Binance.US requested that its delegated principal be removed from validator
vaults and added to direct aggregate wallet delivery. Exactly
**{one(result['binance_us']['delegated_released_atto'], True)} ONE** from
{result['binance_us']['delegation_rows']:,} delegation rows across
{result['binance_us']['validators']:,} validators is therefore included in its
direct total and subtracted from those validators' vault assets.

| Binance.US native component | ONE |
|---|---:|
| Shard-0 liquid | {one(result['binance_us']['components']['liquid_shard0_atto'], True)} |
| Shard-1 liquid | {one(result['binance_us']['components']['liquid_shard1_atto'], True)} |
| Pending undelegation | {one(result['binance_us']['components']['pending_undelegation_atto'], True)} |
| Unclaimed staking reward | {one(result['binance_us']['components']['unclaimed_staking_reward_atto'], True)} |
| Supported pending cross-shard receipts | {one(result['binance_us']['components']['pending_cross_shard_atto'], True)} |
| Native wallet subtotal | {one(result['binance_us']['components']['wallet_airdrop_atto'], True)} |
| Active delegated principal released | {one(result['binance_us']['components']['staked_to_vault_atto'], True)} |
| Direct aggregate delivery | {one(result['exchanges']['binance-us']['direct_delivery_atto'], True)} |

Detailed source-wallet, delegation, and adjusted-validator rows are retained in:

- `binance-us-wallet-deliveries.csv`;
- `binance-us-delegation-withdrawals.csv`;
- `binance-us-validator-vault-adjustments.csv`.

## Gate — separate reconciliation

Gate has not requested aggregate delivery. This section is retained so Harmony
and Gate can verify the complete native position and discuss treatment of the
amount outside the current ordinary delivery.

| Gate measure | Wallets | Native ONE |
|---|---:|---:|
| Submitted inventory plus supplemental source | {gate['inventory_wallets']:,} | — |
| Positive cutoff native claims | {gate['positive_native_claims']:,} | {one(gate['native_total_claim_atto'], True)} |
| Current ordinary delivery | {gate['ordinary_delivery_wallets']:,} | {one(gate['ordinary_native_delivery_atto'], True)} |
| Designated supplemental source route | 1 | {one(gate['supplemental_native_delivery_atto'], True)} |
| Total currently identified delivery | {gate['current_delivery_wallets']:,} | {one(gate['current_native_delivery_atto'], True)} |
| Native amount outside current delivery | {gate['remaining_wallets']:,} | {one(gate['remaining_native_atto'], True)} |
| Candidate total if Gate elects aggregate delivery | {gate['positive_native_claims']:,} | {one(gate['native_total_claim_atto'], True)} |

Supplemental source `{gate['supplemental_route']['source_address']}` routes to
`{gate['supplemental_route']['destination_address']}`
(`{gate['supplemental_route']['destination_one']}`). The destination itself
already has **{one(gate['destination_existing_native_claim_atto'], True)} ONE**
at cutoff; that pre-existing destination balance is shown for review but is not
added to the source-route total.

Gate's WONE census and any unrelated global policy are intentionally outside
this exchange memo.
"""


def main():
    args = parse_args()
    audits_dir = Path(args.audits_dir)
    output_dir = Path(args.output_dir)
    gate_route = load_gate_route(
        args.gate_addition,
        args.gate_destination,
    )
    audits = {}
    summaries = {}
    address_to_exchange = {}
    for exchange_id in (*EXCHANGE_ORDER, "gate"):
        path = audits_dir / f"{exchange_id}.csv"
        audits[exchange_id] = load_audit(path, exchange_id)
        summaries[exchange_id] = audit_summary(audits[exchange_id])
        for row in audits[exchange_id]:
            address = row["address_hex"].lower()
            if address in address_to_exchange:
                raise ValueError(
                    f"cross-exchange audit overlap: {address}"
                )
            address_to_exchange[address] = exchange_id
    for address, label in (
        (gate_route["source_address"].lower(), "gate_supplemental"),
        (gate_route["destination_address"].lower(), "gate_destination"),
    ):
        if address in address_to_exchange:
            raise ValueError(
                f"Gate special address already appears in inventory: {address}"
            )
        address_to_exchange[address] = label
    native_records, native_totals = load_native_components(
        args.native_claims,
        address_to_exchange,
    )
    gate_source = native_records.get(gate_route["source_address"].lower())
    gate_destination = native_records.get(
        gate_route["destination_address"].lower()
    )
    if gate_source is None or gate_destination is None:
        raise ValueError("Gate source or destination has no native claim row")
    for exchange_id, rows in audits.items():
        expected_addresses = {
            row["address_hex"].lower()
            for row in rows
            if row["claim_status"] == "present"
        }
        actual_addresses = {
            address
            for address in native_records
            if address_to_exchange[address] == exchange_id
        }
        if actual_addresses != expected_addresses:
            raise ValueError(
                f"{exchange_id}: native claim address set mismatch"
            )
        components = native_totals[exchange_id]
        if (
            components["wallet_airdrop_atto"]
            != summaries[exchange_id]["native_wallet_atto"]
            or components["staked_to_vault_atto"]
            != summaries[exchange_id]["staked_to_vault_atto"]
            or components["total_claim_atto"]
            != summaries[exchange_id]["native_total_claim_atto"]
        ):
            raise ValueError(
                f"{exchange_id}: native component totals mismatch"
            )

    binance_rows = audits["binance-us"]
    binance_addresses = {
        row["address_hex"].lower(): row for row in binance_rows
    }
    delegation_rows = []
    delegated_by_wallet = defaultdict(int)
    delegated_by_validator = defaultdict(int)
    delegation_count_by_validator = defaultdict(int)
    with open(args.delegations, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "validator_address",
            "validator_secure_key",
            "delegator_address",
            "delegator_secure_key",
            "staked_to_vault_atto",
        }
        if not required <= set(reader.fieldnames or ()):
            raise ValueError("delegation ledger is missing required fields")
        for row in reader:
            delegator = row["delegator_address"].lower()
            if delegator not in binance_addresses:
                continue
            amount = int(row["staked_to_vault_atto"])
            validator = row["validator_address"].lower()
            delegated_by_wallet[delegator] += amount
            delegated_by_validator[validator] += amount
            delegation_count_by_validator[validator] += 1
            delegation_rows.append(
                {
                    "validator_address": row["validator_address"],
                    "validator_secure_key": row["validator_secure_key"],
                    "delegator_address": row["delegator_address"],
                    "delegator_secure_key": row["delegator_secure_key"],
                    "staked_released_atto": str(amount),
                    "destination_address": summaries["binance-us"][
                        "destination_address"
                    ],
                }
            )
    expected_staked = summaries["binance-us"]["staked_to_vault_atto"]
    if sum(delegated_by_wallet.values()) != expected_staked:
        raise ValueError("Binance.US delegation ledger does not close")
    for address, row in binance_addresses.items():
        if delegated_by_wallet[address] != int(row["staked_to_vault_atto"]):
            raise ValueError(
                f"Binance.US wallet delegation mismatch: {address}"
            )

    wallet_rows = []
    for address, row in sorted(binance_addresses.items()):
        components = native_records.get(
            address,
            {
                "liquid_shard0_atto": 0,
                "liquid_shard1_atto": 0,
                "pending_undelegation_atto": 0,
                "unclaimed_staking_reward_atto": 0,
                "pending_cross_shard_atto": 0,
                "wallet_airdrop_atto": 0,
            },
        )
        wallet = components["wallet_airdrop_atto"]
        released = delegated_by_wallet[address]
        direct = wallet + released
        if direct == 0:
            continue
        wallet_rows.append(
            {
                "source_address": row["address_hex"],
                "address_one": row["address_one"],
                "liquid_shard0_atto": str(
                    components["liquid_shard0_atto"]
                ),
                "liquid_shard1_atto": str(
                    components["liquid_shard1_atto"]
                ),
                "pending_undelegation_atto": str(
                    components["pending_undelegation_atto"]
                ),
                "unclaimed_staking_reward_atto": str(
                    components["unclaimed_staking_reward_atto"]
                ),
                "pending_cross_shard_atto": str(
                    components["pending_cross_shard_atto"]
                ),
                "native_wallet_atto": str(wallet),
                "delegated_released_atto": str(released),
                "direct_delivery_atto": str(direct),
                "destination_address": summaries["binance-us"][
                    "destination_address"
                ],
            }
        )

    vault_rows = []
    seen_validators = set()
    with open(args.vaults, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "validator_address",
            "validator_secure_key",
            "vault_assets_atto",
        }
        if not required <= set(reader.fieldnames or ()):
            raise ValueError("vault ledger is missing required fields")
        for row in reader:
            validator = row["validator_address"].lower()
            withdrawal = delegated_by_validator.get(validator, 0)
            if not withdrawal:
                continue
            base = int(row["vault_assets_atto"])
            adjusted = base - withdrawal
            if adjusted < 0:
                raise ValueError(
                    f"Binance.US withdrawal exceeds vault: {validator}"
                )
            seen_validators.add(validator)
            vault_rows.append(
                {
                    "validator_address": row["validator_address"],
                    "validator_secure_key": row["validator_secure_key"],
                    "base_vault_assets_atto": str(base),
                    "binance_us_withdrawal_atto": str(withdrawal),
                    "adjusted_vault_assets_atto": str(adjusted),
                    "delegation_rows_removed": str(
                        delegation_count_by_validator[validator]
                    ),
                }
            )
    if seen_validators != set(delegated_by_validator):
        raise ValueError("Binance.US delegation references an unknown vault")

    native_delivery_rows = []
    exchange_result = {}
    for exchange_id in EXCHANGE_ORDER:
        summary = summaries[exchange_id]
        if (
            exchange_id != "binance-us"
            and summary["staked_to_vault_atto"] != 0
        ):
            raise ValueError(
                f"{exchange_id}: delegated treatment requires a decision"
            )
        released = expected_staked if exchange_id == "binance-us" else 0
        direct = (
            summary["native_total_claim_atto"]
            if exchange_id == "binance-us"
            else summary["native_wallet_atto"]
        )
        exchange_result[exchange_id] = {
            "display_name": {
                "binance": "Binance",
                "binance-us": "Binance.US",
                "mexc": "MEXC",
                "okx": "OKX",
                "kucoin": "KuCoin",
            }[exchange_id],
            **summary,
            "components": dict(native_totals[exchange_id]),
            "delegated_released_atto": released,
            "direct_delivery_atto": direct,
        }
        if direct:
            native_delivery_rows.append(
                {
                    "exchange_id": exchange_id,
                    "destination_address": summary["destination_address"],
                    "inventory_wallets": str(summary["inventory_wallets"]),
                    "positive_native_claims": str(
                        summary["positive_native_claims"]
                    ),
                    "native_wallet_atto": str(summary["native_wallet_atto"]),
                    "delegated_released_atto": str(released),
                    "direct_delivery_atto": str(direct),
                }
            )

    gate_rows = audits["gate"]
    current_gate_rows = [
        row
        for row in gate_rows
        if int(row["planned_total_entitlement_atto"]) > 0
    ]
    gate_inventory_total = summaries["gate"]["native_total_claim_atto"]
    gate_ordinary_current = sum(
        int(row["native_total_claim_atto"]) for row in current_gate_rows
    )
    gate_source_total = gate_source["total_claim_atto"]
    gate_total = gate_inventory_total + gate_source_total
    gate_current = gate_ordinary_current + gate_source_total
    gate_remaining = gate_total - gate_current
    gate_result = {
        **summaries["gate"],
        "inventory_components": dict(native_totals["gate"]),
        "supplemental_components": dict(gate_source),
        "destination_components": dict(gate_destination),
        "inventory_wallets": summaries["gate"]["inventory_wallets"] + 1,
        "positive_native_claims": (
            summaries["gate"]["positive_native_claims"] + 1
        ),
        "native_total_claim_atto": gate_total,
        "ordinary_delivery_wallets": len(current_gate_rows),
        "ordinary_native_delivery_atto": gate_ordinary_current,
        "supplemental_delivery_wallets": 1,
        "supplemental_native_delivery_atto": gate_source_total,
        "current_delivery_wallets": len(current_gate_rows) + 1,
        "current_native_delivery_atto": gate_current,
        "remaining_wallets": (
            summaries["gate"]["inventory_wallets"] - len(current_gate_rows)
        ),
        "remaining_native_atto": gate_remaining,
        "supplemental_route": {
            **gate_route,
            "source_native_claim_atto": str(gate_source_total),
        },
        "destination_existing_native_claim_atto": str(
            gate_destination["total_claim_atto"]
        ),
    }
    if min(gate_current, gate_remaining) < 0:
        raise ValueError("Gate native reconciliation is negative")

    outputs = {
        "native_deliveries": output_dir / "exchange-native-deliveries.csv",
        "binance_wallets": output_dir / "binance-us-wallet-deliveries.csv",
        "binance_delegations": (
            output_dir / "binance-us-delegation-withdrawals.csv"
        ),
        "binance_vaults": (
            output_dir / "binance-us-validator-vault-adjustments.csv"
        ),
        "gate_route": output_dir / "gate-supplemental-native-route.csv",
    }
    atomic_csv(
        outputs["native_deliveries"],
        NATIVE_DELIVERY_FIELDS,
        native_delivery_rows,
        args.replace,
    )
    atomic_csv(
        outputs["binance_wallets"],
        BINANCE_WALLET_FIELDS,
        wallet_rows,
        args.replace,
    )
    atomic_csv(
        outputs["binance_delegations"],
        DELEGATION_FIELDS,
        delegation_rows,
        args.replace,
    )
    atomic_csv(
        outputs["binance_vaults"],
        VAULT_FIELDS,
        vault_rows,
        args.replace,
    )
    atomic_csv(
        outputs["gate_route"],
        GATE_ROUTE_FIELDS,
        (
            {
                "source_address": gate_route["source_address"],
                "destination_address": gate_route[
                    "destination_address"
                ],
                "destination_one": gate_route["destination_one"],
                "native_wallet_atto": str(
                    gate_source["wallet_airdrop_atto"]
                ),
                "staked_to_vault_atto": str(
                    gate_source["staked_to_vault_atto"]
                ),
                "native_total_claim_atto": str(gate_source_total),
                "reason": gate_route["reason"],
            },
        ),
        args.replace,
    )
    result = {
        "schema_version": 1,
        "status": "passed",
        "policy": (
            "exchange-local native ONE accounting; WONE excluded; "
            "Binance.US delegated principal converted to direct delivery"
        ),
        "exchanges": exchange_result,
        "gate": gate_result,
        "binance_us": {
            "delegators": len(
                {
                    row["delegator_address"].lower()
                    for row in delegation_rows
                }
            ),
            "validators": len(delegated_by_validator),
            "delegation_rows": len(delegation_rows),
            "delegated_released_atto": str(expected_staked),
            "components": dict(native_totals["binance-us"]),
        },
        "inputs": {
            "audits": {
                exchange_id: {
                    "path": str(audits_dir / f"{exchange_id}.csv"),
                    "sha256": file_sha256(
                        audits_dir / f"{exchange_id}.csv"
                    ),
                }
                for exchange_id in (*EXCHANGE_ORDER, "gate")
            },
            "delegations": {
                "path": args.delegations,
                "sha256": file_sha256(args.delegations),
            },
            "gate_addition": {
                "path": args.gate_addition,
                "sha256": file_sha256(args.gate_addition),
            },
            "gate_destination": {
                "path": args.gate_destination,
                "sha256": file_sha256(args.gate_destination),
            },
            "native_claims": {
                "path": args.native_claims,
                "sha256": file_sha256(args.native_claims),
            },
            "vaults": {
                "path": args.vaults,
                "sha256": file_sha256(args.vaults),
            },
        },
        "outputs": {
            label: {
                "path": str(path),
                "sha256": file_sha256(path),
            }
            for label, path in outputs.items()
        },
    }
    atomic_json(args.summary, result, args.replace)
    atomic_text(args.report, render_report(result), args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
