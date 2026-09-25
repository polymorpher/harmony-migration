import csv
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPT = (
    ROOT
    / "toolkit"
    / "scripts"
    / "claims"
    / "verify-wone-allocation.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_wone_allocation",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = load_module()
SCALE = VERIFY.ATTO_PER_ONE


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_csv(path, fields, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def secure_key(address):
    normalized = address.lower()
    return "0x" + VERIFY.lib.keccak256(
        bytes.fromhex(normalized[2:])
    ).hex()


def checksum(address):
    return VERIFY.lib.to_checksum(address.lower())


def native_row(
    address,
    *,
    liquid_shard0,
    liquid_shard1=0,
    staked=0,
    nonce_shard0="1",
    nonce_shard1="",
    code_hash_shard0=VERIFY.EMPTY_CODE_HASH,
    code_hash_shard1="",
):
    address = checksum(address)
    values = {
        "liquid_shard0": liquid_shard0,
        "liquid_shard1": liquid_shard1,
        "liquid_total": liquid_shard0 + liquid_shard1,
        "active_staked_or_delegated": staked,
        "pending_undelegation": 0,
        "unclaimed_staking_reward": 0,
        "pending_cross_shard": 0,
    }
    values["wallet_airdrop"] = (
        values["liquid_total"]
        + values["pending_undelegation"]
        + values["unclaimed_staking_reward"]
        + values["pending_cross_shard"]
    )
    values["staked_to_vault"] = staked
    values["total_claim"] = values["wallet_airdrop"] + staked
    price = "0.00074801"
    row = {
        "secure_key": secure_key(address),
        "address": address,
        "address_or_secure_key": address,
        "address_resolved": "true",
        "claims_shard0_block": "93623067",
        "claims_shard1_block": "95882100",
        "valuation_price_reference_shard0_block": "93448483",
        "valuation_price_usd_per_one": price,
        "nonce_shard0": nonce_shard0,
        "nonce_shard1": nonce_shard1,
        "code_hash_shard0": code_hash_shard0,
        "code_hash_shard1": code_hash_shard1,
    }
    for component, amount in values.items():
        row[f"{component}_atto"] = str(amount)
        row[f"{component}_one"] = VERIFY.fixed(amount)
    row["wallet_airdrop_usd"] = VERIFY.usd_value(
        values["wallet_airdrop"],
        price,
        "fixture",
    )
    row["total_usd"] = VERIFY.usd_value(
        values["total_claim"],
        price,
        "fixture",
    )
    return {field: row[field] for field in VERIFY.NATIVE_FIELDS}


def overlay_row(native, wone_balance):
    row = dict(native)
    native_wallet = int(native["wallet_airdrop_atto"])
    staked = int(native["staked_to_vault_atto"])
    native_total = int(native["total_claim_atto"])
    qualification = native_total + wone_balance
    wone_airdrop = (
        wone_balance if qualification >= VERIFY.MINIMUM_ATTO else 0
    )
    wallet = native_wallet + wone_airdrop
    total = native_total + wone_airdrop
    amounts = {
        "native_wallet_airdrop": native_wallet,
        "wone_balance": wone_balance,
        "wone_airdrop": wone_airdrop,
        "wallet_airdrop": wallet,
        "staked_to_vault": staked,
        "qualification_total": qualification,
        "native_total_claim": native_total,
        "total_claim": total,
    }
    for component, amount in amounts.items():
        row[f"{component}_atto"] = str(amount)
        row[f"{component}_one"] = VERIFY.fixed(amount)
    price = row["valuation_price_usd_per_one"]
    row["wallet_airdrop_usd"] = VERIFY.usd_value(
        wallet,
        price,
        "fixture",
    )
    row["total_usd"] = VERIFY.usd_value(total, price, "fixture")
    return {field: row[field] for field in VERIFY.MIGRATION_FIELDS}


def route_row(
    route_id,
    priority,
    source_address,
    destination_id,
    amount_atto,
    allocation_method,
    reason,
):
    return {
        "route_id": route_id,
        "priority": str(priority),
        "source_address": source_address.lower(),
        "destination_id": destination_id,
        "destination_address": "",
        "amount_atto": str(amount_atto),
        "allocation_method": allocation_method,
        "reason": reason,
        "evidence": "fixture",
        "notes": "fixture",
    }


def exception_row(
    source_address,
    amount,
    route_id,
    priority,
    destination_id,
    destination_status,
    reason,
):
    source_address = checksum(source_address)
    return {
        "component": "wallet_airdrop",
        "source_secure_key": secure_key(source_address),
        "source_address": source_address,
        "source_category": "contract_review",
        "source_code_bearing": "true",
        "migration_stage": (
            "" if destination_status in {"redistributed", "not_issuing"}
            else "next_stage"
        ),
        "issuance_treatment": (
            "redistributed"
            if destination_status == "redistributed"
            else "not_issued"
            if destination_status == "not_issuing"
            else "issue"
        ),
        "validator_secure_key": "",
        "validator_address": "",
        "amount_atto": str(amount),
        "exception_type": "explicit_route",
        "route_id": route_id,
        "route_priority": str(priority),
        "destination_id": destination_id,
        "destination_address": "",
        "destination_status": destination_status,
        "reason": reason,
        "evidence": "fixture",
    }


class AllocationFixture:
    def __init__(self, root):
        self.root = root
        self.paths = {
            name: root / filename
            for name, filename in {
                "wone_holders": "wone-holders.csv",
                "wone_summary": "wone-summary.json",
                "wone_only_metadata": "wone-only-metadata.csv",
                "native_claims": "native-claims.csv",
                "migration_claims": "migration-claims.csv",
                "migration_summary": "migration-summary.json",
                "threshold_claims": "threshold-claims.csv",
                "threshold_summary": "threshold-summary.json",
                "bridge_base": "bridge-reserves.base.csv",
                "bridge_routes": "bridge-reserves.csv",
                "bridge_summary": "bridge-reserves-summary.json",
                "routing_exceptions": "routing-exceptions.csv",
                "routing_governor_exceptions": (
                    "validator-governor-exceptions.csv"
                ),
                "routing_unresolved": "unresolved-routing.csv",
                "routing_summary": "routing-summary.json",
            }.items()
        }
        self.addresses = {
            "wone": VERIFY.WONE_ADDRESS,
            "layerzero_bsc": VERIFY.LAYERZERO_ADDRESSES[0],
            "layerzero_eth": VERIFY.LAYERZERO_ADDRESSES[1],
            "user": "0x" + "11" * 20,
            "baseline": "0x" + "22" * 20,
            "wone_only": "0x" + "33" * 20,
            "below": "0x" + "44" * 20,
            "dead": "0x000000000000000000000000000000000000dead",
        }
        self.build()

    def build(self):
        holders = {
            self.addresses["wone"]: 2 * SCALE,
            self.addresses["user"]: 400 * SCALE,
            self.addresses["wone_only"]: 1000 * SCALE,
            self.addresses["below"]: 5 * SCALE,
            self.addresses["dead"]: 3 * SCALE,
        }
        holder_rows = [
            {
                "address": checksum(address),
                "wone_balance_atto": str(amount),
                "wone_balance": VERIFY.fixed(amount),
            }
            for address, amount in sorted(holders.items())
        ]
        write_csv(
            self.paths["wone_holders"],
            VERIFY.HOLDER_FIELDS,
            holder_rows,
        )
        reserve = sum(holders.values())
        self.reserve = reserve
        write_json(
            self.paths["wone_summary"],
            {
                "schema_version": 1,
                "status": "passed",
                "contract_address": checksum(VERIFY.WONE_ADDRESS),
                "cutoff_block": 93623067,
                "holder_count": len(holder_rows),
                "total_holder_balance_atto": str(reserve),
                "contract_total_supply_atto": str(reserve),
                "contract_native_reserve_atto": str(reserve),
                "reserve_minus_supply_atto": "0",
                "output_path": str(self.paths["wone_holders"]),
                "output_sha256": sha256(self.paths["wone_holders"]),
            },
        )

        metadata_row = {
            "address": checksum(self.addresses["wone_only"]),
            "wone_balance_atto": str(1000 * SCALE),
            "combined_total_atto": str(1000 * SCALE),
            "claim_row_present": "false",
            "nonce_shard0": "1",
            "code_hash_shard0": VERIFY.EMPTY_CODE_HASH,
            "code_status": "code_less",
        }
        write_csv(
            self.paths["wone_only_metadata"],
            VERIFY.METADATA_FIELDS,
            (metadata_row,),
        )

        native_rows = [
            native_row(
                self.addresses["wone"],
                liquid_shard0=reserve,
                liquid_shard1=7 * SCALE,
                nonce_shard1="0",
                code_hash_shard0=(
                    "0x" + "aa" * 32
                ),
                code_hash_shard1=VERIFY.EMPTY_CODE_HASH,
            ),
            native_row(
                self.addresses["layerzero_bsc"],
                liquid_shard0=11 * SCALE,
                code_hash_shard0="0x" + "bb" * 32,
            ),
            native_row(
                self.addresses["layerzero_eth"],
                liquid_shard0=12 * SCALE,
                code_hash_shard0="0x" + "cc" * 32,
            ),
            native_row(
                self.addresses["user"],
                liquid_shard0=500 * SCALE,
                staked=100 * SCALE,
            ),
            native_row(
                self.addresses["baseline"],
                liquid_shard0=1000 * SCALE,
            ),
        ]
        native_rows.sort(key=lambda row: row["secure_key"])
        write_csv(
            self.paths["native_claims"],
            VERIFY.NATIVE_FIELDS,
            native_rows,
        )

        effective_wone = {
            self.addresses["user"]: 400 * SCALE,
        }
        migration_rows = [
            overlay_row(
                row,
                effective_wone.get(row["address"].lower(), 0),
            )
            for row in native_rows
        ]
        wone_only_native = native_row(
            self.addresses["wone_only"],
            liquid_shard0=0,
            nonce_shard0=metadata_row["nonce_shard0"],
            code_hash_shard0=metadata_row["code_hash_shard0"],
        )
        migration_rows.append(
            overlay_row(wone_only_native, 1000 * SCALE)
        )
        migration_rows.sort(key=lambda row: row["secure_key"])
        self.migration_rows = migration_rows
        write_csv(
            self.paths["migration_claims"],
            VERIFY.MIGRATION_FIELDS,
            migration_rows,
        )

        threshold_rows = [
            row
            for row in migration_rows
            if int(row["qualification_total_atto"])
            >= VERIFY.MINIMUM_ATTO
        ]
        write_csv(
            self.paths["threshold_claims"],
            VERIFY.MIGRATION_FIELDS,
            threshold_rows,
        )

        exclusions = sorted(
            {
                VERIFY.WONE_ADDRESS,
                *VERIFY.LAYERZERO_ADDRESSES,
                self.addresses["dead"],
            }
        )
        excluded_details = [
            {
                "address": address,
                "wone_balance_atto": str(holders[address]),
            }
            for address in sorted(holders)
            if address in exclusions
        ]
        native_wallet = sum(
            int(row["wallet_airdrop_atto"]) for row in native_rows
        )
        native_total = sum(
            int(row["total_claim_atto"]) for row in native_rows
        )
        wallet = sum(
            int(row["wallet_airdrop_atto"]) for row in migration_rows
        )
        staked = sum(
            int(row["staked_to_vault_atto"]) for row in migration_rows
        )
        total = sum(
            int(row["total_claim_atto"]) for row in migration_rows
        )
        redistributed = sum(
            int(row["wone_airdrop_atto"]) for row in migration_rows
        )
        retained = reserve - redistributed
        self.redistributed = redistributed
        self.retained = retained
        baseline = sum(
            int(row["total_claim_atto"]) >= VERIFY.MINIMUM_ATTO
            for row in native_rows
        )
        newly_existing = sum(
            int(row["native_total_claim_atto"]) < VERIFY.MINIMUM_ATTO
            and int(row["qualification_total_atto"])
            >= VERIFY.MINIMUM_ATTO
            for row in migration_rows
            if row["secure_key"] in {
                native["secure_key"] for native in native_rows
            }
        )
        qualified = len(threshold_rows)
        write_json(
            self.paths["migration_summary"],
            {
                "schema_version": 1,
                "status": "passed",
                "minimum_atto": str(VERIFY.MINIMUM_ATTO),
                "native_claims": str(self.paths["native_claims"]),
                "native_claims_sha256": sha256(
                    self.paths["native_claims"]
                ),
                "wone_holders": str(self.paths["wone_holders"]),
                "wone_holders_sha256": sha256(
                    self.paths["wone_holders"]
                ),
                "wone_summary": str(self.paths["wone_summary"]),
                "wone_summary_sha256": sha256(
                    self.paths["wone_summary"]
                ),
                "new_holder_metadata": str(
                    self.paths["wone_only_metadata"]
                ),
                "new_holder_metadata_sha256": sha256(
                    self.paths["wone_only_metadata"]
                ),
                "output": str(self.paths["migration_claims"]),
                "output_sha256": sha256(
                    self.paths["migration_claims"]
                ),
                "native_rows": len(native_rows),
                "output_rows": len(migration_rows),
                "new_wone_only_rows": 1,
                "newly_qualified_wone_only_rows": 1,
                "baseline_qualified_rows": baseline,
                "newly_qualified_existing_native_rows": newly_existing,
                "newly_qualified_rows": newly_existing + 1,
                "qualified_rows": qualified,
                "priority_wone_holder_rows": sum(
                    int(row["wone_airdrop_atto"]) > 0
                    for row in migration_rows
                ),
                "wone_holder_rows": len(holder_rows),
                "excluded_addresses_requested": exclusions,
                "excluded_wone_holders": excluded_details,
                "excluded_wone_holder_rows": len(excluded_details),
                "excluded_wone_atto": str(
                    sum(int(row["wone_balance_atto"]) for row in excluded_details)
                ),
                "wone_reserve_atto": str(reserve),
                "wone_redistributed_to_priority_atto": str(redistributed),
                "wone_retained_not_issued_atto": str(retained),
                "native_wallet_airdrop_atto": str(native_wallet),
                "native_total_claim_atto": str(native_total),
                "wallet_airdrop_atto": str(wallet),
                "staked_to_vault_atto": str(staked),
                "expanded_total_claim_atto": str(total),
                "component_totals_atto": {
                    "native_wallet_airdrop": str(native_wallet),
                    "wone_airdrop": str(redistributed),
                    "wallet_airdrop": str(wallet),
                    "staked_to_vault": str(staked),
                    "native_total_claim": str(native_total),
                    "total_claim": str(total),
                },
                "conservation": {
                    "wone_reserve_equals_redistributed_plus_retained": True,
                    "expanded_total_equals_native_plus_priority_wone": True,
                },
            },
        )

        threshold_wallet = sum(
            int(row["wallet_airdrop_atto"]) for row in threshold_rows
        )
        threshold_staked = sum(
            int(row["staked_to_vault_atto"]) for row in threshold_rows
        )
        threshold_total = sum(
            int(row["total_claim_atto"]) for row in threshold_rows
        )
        threshold_wone = sum(
            int(row["wone_airdrop_atto"]) for row in threshold_rows
        )
        write_json(
            self.paths["threshold_summary"],
            {
                "comparison": "ge",
                "threshold_field": "qualification_total_atto",
                "minimum_atto": str(VERIFY.MINIMUM_ATTO),
                "input": str(self.paths["migration_claims"]),
                "input_sha256": sha256(
                    self.paths["migration_claims"]
                ),
                "output": str(self.paths["threshold_claims"]),
                "output_sha256": sha256(
                    self.paths["threshold_claims"]
                ),
                "input_rows": len(migration_rows),
                "output_rows": len(threshold_rows),
                "exact_threshold_rows": sum(
                    int(row["qualification_total_atto"])
                    == VERIFY.MINIMUM_ATTO
                    for row in migration_rows
                ),
                "wallet_airdrop_atto": str(threshold_wallet),
                "staked_to_vault_atto": str(threshold_staked),
                "total_claim_atto": str(threshold_total),
                "wone_airdrop_atto": str(threshold_wone),
            },
        )

        base_routes = [
            route_row(
                "layerzero-nativeoft-bsc-custody",
                400,
                self.addresses["layerzero_bsc"],
                "layerzero-nativeoft-custody",
                "SHARD0_LIQUID",
                "wallet_first_pro_rata_vault",
                "layerzero_nativeoft_reconciliation_hold",
            ),
            route_row(
                "layerzero-nativeoft-ethereum-custody",
                400,
                self.addresses["layerzero_eth"],
                "layerzero-nativeoft-custody",
                "SHARD0_LIQUID",
                "wallet_first_pro_rata_vault",
                "layerzero_nativeoft_reconciliation_hold",
            ),
        ]
        write_csv(
            self.paths["bridge_base"],
            VERIFY.ROUTE_FIELDS,
            base_routes,
        )
        bridge_routes = base_routes + [
            route_row(
                VERIFY.WONE_REDISTRIBUTION_ROUTE,
                400,
                VERIFY.WONE_ADDRESS,
                "wone-holder-redistribution",
                redistributed,
                "wallet_only",
                "wone_holder_delivery_redistribution",
            ),
            route_row(
                VERIFY.WONE_RETAINED_ROUTE,
                401,
                VERIFY.WONE_ADDRESS,
                "not-issuing",
                retained,
                "wallet_only",
                "wone_reserve_remainder_retained_not_issued",
            ),
        ]
        bridge_routes.sort(
            key=lambda row: (int(row["priority"]), row["route_id"])
        )
        self.bridge_routes = bridge_routes
        write_csv(
            self.paths["bridge_routes"],
            VERIFY.ROUTE_FIELDS,
            bridge_routes,
        )
        write_json(
            self.paths["bridge_summary"],
            {
                "schema_version": 1,
                "status": "passed",
                "input": str(self.paths["bridge_base"]),
                "input_sha256": sha256(self.paths["bridge_base"]),
                "output": str(self.paths["bridge_routes"]),
                "output_sha256": sha256(self.paths["bridge_routes"]),
                "route_rows": len(bridge_routes),
                "wone_summary": str(self.paths["migration_summary"]),
                "wone_summary_sha256": sha256(
                    self.paths["migration_summary"]
                ),
                "wone_reserve_atto": str(reserve),
                "wone_redistributed_to_holders_atto": str(redistributed),
                "wone_retained_not_issued_atto": str(retained),
            },
        )

        exception_rows = [
            exception_row(
                VERIFY.WONE_ADDRESS,
                redistributed,
                VERIFY.WONE_REDISTRIBUTION_ROUTE,
                400,
                "wone-holder-redistribution",
                "redistributed",
                "wone_holder_delivery_redistribution",
            ),
            exception_row(
                VERIFY.WONE_ADDRESS,
                retained,
                VERIFY.WONE_RETAINED_ROUTE,
                401,
                "not-issuing",
                "not_issuing",
                "wone_reserve_remainder_retained_not_issued",
            ),
            exception_row(
                VERIFY.WONE_ADDRESS,
                7 * SCALE,
                "contract-policy-other-" + VERIFY.WONE_ADDRESS[2:],
                500,
                "not-issuing",
                "not_issuing",
                "reviewed_contract_allocation_not_issued",
            ),
            exception_row(
                self.addresses["layerzero_bsc"],
                11 * SCALE,
                "layerzero-nativeoft-bsc-custody",
                400,
                "layerzero-nativeoft-custody",
                "hold",
                "layerzero_nativeoft_reconciliation_hold",
            ),
            exception_row(
                self.addresses["layerzero_eth"],
                12 * SCALE,
                "layerzero-nativeoft-ethereum-custody",
                400,
                "layerzero-nativeoft-custody",
                "hold",
                "layerzero_nativeoft_reconciliation_hold",
            ),
        ]
        exception_rows.sort(
            key=lambda row: (
                row["source_secure_key"],
                int(row["route_priority"]),
                row["route_id"],
            )
        )
        write_csv(
            self.paths["routing_exceptions"],
            VERIFY.ROUTING_EXCEPTION_FIELDS,
            exception_rows,
        )
        self.paths["routing_governor_exceptions"].write_text(
            "validator_address,destination_address\n",
            encoding="utf-8",
        )
        self.paths["routing_unresolved"].write_text(
            "component,source_address,amount_atto\n",
            encoding="utf-8",
        )

        exception_wallet = sum(
            int(row["amount_atto"]) for row in exception_rows
        )
        implicit_wallet = wallet - exception_wallet
        reviewed_contract_non_issuance = 7 * SCALE
        issuable_wallet = (
            wallet
            - retained
            - redistributed
            - reviewed_contract_non_issuance
        )
        issuable_staked = staked
        write_json(
            self.paths["routing_summary"],
            {
                "status": "hold",
                "claims": len(migration_rows),
                "priority_claims": len(threshold_rows),
                "explicitly_routed_deferred_claims": (
                    len(migration_rows) - len(threshold_rows)
                ),
                "route_files": [str(self.paths["bridge_routes"])],
                "routing_exception_rows": len(exception_rows),
                "source_wallet_airdrop_atto": str(wallet),
                "source_wone_airdrop_atto": str(redistributed),
                "source_staked_to_vault_atto": str(staked),
                "source_total_claim_atto": str(total),
                "routed_wallet_airdrop_atto": str(wallet),
                "routed_staked_to_vault_atto": str(staked),
                "exception_wallet_airdrop_atto": str(exception_wallet),
                "exception_staked_to_vault_atto": "0",
                "implicit_wallet_airdrop_atto": str(implicit_wallet),
                "implicit_staked_to_vault_atto": str(staked),
                "not_issued_wallet_airdrop_atto": str(
                    retained + reviewed_contract_non_issuance
                ),
                "not_issued_staked_to_vault_atto": "0",
                "not_issued_total_claim_atto": str(
                    retained + reviewed_contract_non_issuance
                ),
                "redistributed_wallet_airdrop_atto": str(redistributed),
                "redistributed_staked_to_vault_atto": "0",
                "redistributed_total_claim_atto": str(redistributed),
                "exchange_manual_wallet_airdrop_atto": "0",
                "exchange_manual_staked_to_vault_atto": "0",
                "exchange_manual_total_claim_atto": "0",
                "issuable_wallet_airdrop_atto": str(issuable_wallet),
                "issuable_staked_to_vault_atto": str(issuable_staked),
                "issuable_total_claim_atto": str(
                    issuable_wallet + issuable_staked
                ),
                "unresolved_wallet_airdrop_atto": str(30 * SCALE),
                "unresolved_staked_to_vault_atto": "0",
                "wone_reserve_source_atto": str(reserve),
                "wone_redistributed_to_holders_atto": str(redistributed),
                "wone_retained_not_issued_atto": str(retained),
                "wone_recipient_not_issued_rows": 0,
                "outputs": {
                    "routing_exceptions": {
                        "path": str(self.paths["routing_exceptions"]),
                        "sha256": sha256(
                            self.paths["routing_exceptions"]
                        ),
                    },
                    "governor_exceptions": {
                        "path": str(
                            self.paths["routing_governor_exceptions"]
                        ),
                        "sha256": sha256(
                            self.paths["routing_governor_exceptions"]
                        ),
                    },
                    "unresolved": {
                        "path": str(self.paths["routing_unresolved"]),
                        "sha256": sha256(
                            self.paths["routing_unresolved"]
                        ),
                    },
                },
            },
        )

    def command(self, *extra):
        arguments = [
            sys.executable,
            str(SCRIPT),
            "--wone-holders",
            str(self.paths["wone_holders"]),
            "--wone-summary",
            str(self.paths["wone_summary"]),
            "--wone-only-metadata",
            str(self.paths["wone_only_metadata"]),
            "--native-claims",
            str(self.paths["native_claims"]),
            "--migration-claims",
            str(self.paths["migration_claims"]),
            "--migration-summary",
            str(self.paths["migration_summary"]),
            "--threshold-claims",
            str(self.paths["threshold_claims"]),
            "--threshold-summary",
            str(self.paths["threshold_summary"]),
            "--bridge-routes",
            str(self.paths["bridge_routes"]),
            "--bridge-summary",
            str(self.paths["bridge_summary"]),
            "--routing-exceptions",
            str(self.paths["routing_exceptions"]),
            "--routing-summary",
            str(self.paths["routing_summary"]),
            "--ledger-cache",
            str(self.paths["wone_holders"].parent / "ledger-pass.json"),
        ]
        arguments.extend(extra)
        return arguments


class VerifyWoneAllocationTest(unittest.TestCase):
    def run_verifier(self, fixture, *extra):
        return subprocess.run(
            fixture.command(*extra),
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_success_and_optional_output_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = AllocationFixture(Path(directory))
            result = self.run_verifier(fixture)
            self.assertEqual(result.returncode, 0, result.stderr)
            verification = json.loads(result.stdout)
            self.assertEqual(verification["status"], "passed")
            self.assertTrue(
                verification["checks"]["claim_overlay_exact_merge"]
            )
            self.assertEqual(
                verification["shard1"][
                    "wone_non_backing_residual_atto"
                ],
                str(7 * SCALE),
            )

            output = Path(directory) / "verification.json"
            written = self.run_verifier(
                fixture,
                "--output",
                str(output),
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["status"],
                "passed",
            )
            checked = self.run_verifier(
                fixture,
                "--check-output",
                str(output),
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            refused = self.run_verifier(
                fixture,
                "--output",
                str(output),
            )
            self.assertNotEqual(refused.returncode, 0)
            replaced = self.run_verifier(
                fixture,
                "--output",
                str(output),
                "--replace",
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)

    def test_ledger_pass_is_reused_only_while_inputs_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = AllocationFixture(Path(directory))
            first = self.run_verifier(fixture)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertNotIn("reusing", first.stderr)
            second = self.run_verifier(fixture)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("reusing", second.stderr)
            self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
            full = self.run_verifier(fixture, "--full")
            self.assertEqual(full.returncode, 0, full.stderr)
            self.assertNotIn("reusing", full.stderr)

            write_csv(
                fixture.paths["wone_only_metadata"],
                VERIFY.METADATA_FIELDS,
                (),
            )
            changed = self.run_verifier(fixture)
            self.assertNotEqual(changed.returncode, 0)
            self.assertNotIn("reusing", changed.stderr)

    def test_rejects_truncated_wone_only_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = AllocationFixture(Path(directory))
            write_csv(
                fixture.paths["wone_only_metadata"],
                VERIFY.METADATA_FIELDS,
                (),
            )
            summary = json.loads(
                fixture.paths["migration_summary"].read_text(
                    encoding="utf-8"
                )
            )
            summary["new_holder_metadata_sha256"] = sha256(
                fixture.paths["wone_only_metadata"]
            )
            write_json(fixture.paths["migration_summary"], summary)

            result = self.run_verifier(fixture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "WONE-only metadata does not equal the complete delivery set",
                result.stderr,
            )

    def test_rejects_reserve_route_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = AllocationFixture(Path(directory))
            rows = [dict(row) for row in fixture.bridge_routes]
            for row in rows:
                if row["route_id"] == VERIFY.WONE_REDISTRIBUTION_ROUTE:
                    row["amount_atto"] = str(fixture.redistributed - 1)
            write_csv(
                fixture.paths["bridge_routes"],
                VERIFY.ROUTE_FIELDS,
                rows,
            )
            summary = json.loads(
                fixture.paths["bridge_summary"].read_text(encoding="utf-8")
            )
            summary["output_sha256"] = sha256(
                fixture.paths["bridge_routes"]
            )
            write_json(fixture.paths["bridge_summary"], summary)

            result = self.run_verifier(fixture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "bridge WONE redistribution route.amount_atto",
                result.stderr,
            )

    def test_loads_hash_pinned_aggregate_exchange_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            addresses = root / "exchange.csv"
            summary_path = root / "normalization.json"
            address = "0x" + "55" * 20
            write_csv(
                addresses,
                ("exchange_id", "address_hex"),
                ({"exchange_id": "example", "address_hex": address},),
            )
            normalization = {
                "schema_version": 2,
                "exchanges": {
                    "example": {
                        "delivery_policy": "manual_from_reserve",
                        "normalized_rows": 1,
                        "output": str(addresses),
                        "output_sha256": sha256(addresses),
                    },
                    "gate": {
                        "delivery_policy": "automatic_threshold",
                        "normalized_rows": 0,
                        "output": str(root / "unused.csv"),
                        "output_sha256": "0" * 64,
                    },
                },
            }
            write_json(summary_path, normalization)
            migration = {
                "aggregate_delivery_summary": str(summary_path),
                "aggregate_delivery_summary_sha256": sha256(summary_path),
                "aggregate_delivery_sources": [
                    {
                        "exchange_id": "example",
                        "path": str(addresses),
                        "sha256": sha256(addresses),
                        "addresses": 1,
                    }
                ],
                "aggregate_delivery_addresses_requested": 1,
                "aggregate_delivery_addresses_active": 1,
                "aggregate_delivery_addresses_suppressed": [],
            }
            paths = {
                "aggregate_delivery_summary": summary_path.resolve(),
                "aggregate_delivery_source_0": addresses.resolve(),
            }
            hashes = {
                name: sha256(path) for name, path in paths.items()
            }
            loaded, stats = VERIFY.load_aggregate_delivery_addresses(
                migration,
                set(),
                paths,
                hashes,
            )
            self.assertEqual(loaded, {address})
            self.assertEqual(stats["active"], 1)
            self.assertEqual(stats["sources"], ["example"])


if __name__ == "__main__":
    unittest.main()
