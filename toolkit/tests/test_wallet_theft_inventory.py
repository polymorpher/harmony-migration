import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
CONTRACT_REVIEW = ROOT / "toolkit" / "scripts" / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


SCRIPT = (
    ROOT
    / "toolkit"
    / "scripts"
    / "routing"
    / "merge-wallet-theft-inventory.py"
)


def address(index):
    return f"0x{index:040x}"


def secure_key(index):
    return "0x" + lib.keccak256(bytes.fromhex(address(index)[2:])).hex()


def one_address(index):
    return lib.hex_to_bech32(address(index))


def write_csv(path, fields, rows):
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


class WalletTheftInventoryTest(unittest.TestCase):
    def test_additions_route_existing_claims_but_victims_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical_perpetrators = root / "historical-perpetrators.csv"
            component_names = (
                "liquid_shard0",
                "liquid_shard1",
                "active_staked_or_delegated",
                "pending_undelegation",
                "unclaimed_staking_reward",
                "pending_cross_shard",
                "total_claim",
            )
            perp_fields = (
                "input_order",
                "address_bech32",
                "address_hex",
                "cutoff_row_present",
                "secure_key",
                *(f"{name}_atto" for name in component_names),
            )
            write_csv(
                historical_perpetrators,
                perp_fields,
                [
                    {
                        "input_order": "1",
                        "address_bech32": one_address(1),
                        "address_hex": address(1),
                        "cutoff_row_present": "true",
                        "secure_key": secure_key(1),
                        **{
                            f"{name}_atto": (
                                "10"
                                if name in {"liquid_shard0", "total_claim"}
                                else "0"
                            )
                            for name in component_names
                        },
                    }
                ],
            )

            historical_inventory = root / "historical-inventory.csv"
            inventory_fields = (
                "address_bech32",
                "address_hex",
                "category",
                "cutoff_claim_atto",
                "cutoff_claim_one",
                "treasury_reclaim_atto",
                "remaining_original_address_allocation_atto",
                "remaining_original_address_allocation_one",
            )
            write_csv(
                historical_inventory,
                inventory_fields,
                [
                    {
                        "address_bech32": one_address(1),
                        "address_hex": address(1),
                        "category": (
                            "reported_wallet_theft_perpetrator"
                        ),
                        "cutoff_claim_atto": "10",
                        "cutoff_claim_one": "0.000000000000000010",
                        "treasury_reclaim_atto": "10",
                        "remaining_original_address_allocation_atto": "0",
                        "remaining_original_address_allocation_one": (
                            "0.000000000000000000"
                        ),
                    }
                ],
            )

            additions = root / "additions.csv"
            addition_fields = (
                "case_id",
                "role",
                "address_bech32",
                "address_hex",
                "cutoff_claim_atto",
                "cutoff_claim_one",
                "evidence_basis",
                "source_transaction_hash",
                "source_transaction_eth_hash",
            )
            write_csv(
                additions,
                addition_fields,
                [
                    {
                        "case_id": "CASE-X",
                        "role": "report_linked_theft_recipient",
                        "address_bech32": one_address(2),
                        "address_hex": address(2),
                        "cutoff_claim_atto": "20",
                        "cutoff_claim_one": "0.000000000000000020",
                        "evidence_basis": "fixture",
                        "source_transaction_hash": "0x" + "1" * 64,
                        "source_transaction_eth_hash": "0x" + "2" * 64,
                    }
                ],
            )

            victims = root / "victims.csv"
            write_csv(
                victims,
                (
                    "case_id",
                    "victim",
                    "address_bech32",
                    "total_native_one",
                    "migration_treatment",
                ),
                [
                    {
                        "case_id": "CASE-Y",
                        "victim": "fixture victim",
                        "address_bech32": one_address(3),
                        "total_native_one": "0.000000000000000030",
                        "migration_treatment": "separate review",
                    }
                ],
            )

            all_claims = root / "all-claims.csv"
            claim_fields = (
                "secure_key",
                "address",
                *(f"{name}_atto" for name in component_names),
            )
            write_csv(
                all_claims,
                claim_fields,
                [
                    {
                        "secure_key": secure_key(index),
                        "address": address(index),
                        **{
                            f"{name}_atto": (
                                str(index * 10)
                                if name in {"liquid_shard0", "total_claim"}
                                else "0"
                            )
                            for name in component_names
                        },
                    }
                    for index in (2, 3)
                ],
            )

            outputs = {
                "perpetrator_output": root / "perpetrators.csv",
                "perpetrator_summary": root / "perpetrators.json",
                "victim_summary": root / "victims.json",
                "non_issuance_output": root / "non-issuance.csv",
                "non_issuance_summary": root / "non-issuance.json",
            }
            command = [
                sys.executable,
                str(SCRIPT),
                "--historical-inventory",
                str(historical_inventory),
                "--historical-perpetrators",
                str(historical_perpetrators),
                "--additions",
                str(additions),
                "--victims",
                str(victims),
                "--all-claims",
                str(all_claims),
            ]
            for name, path in outputs.items():
                command.extend(("--" + name.replace("_", "-"), str(path)))
            subprocess.run(command, check=True, capture_output=True, text=True)

            result = json.loads(
                outputs["non_issuance_summary"].read_text()
            )
            self.assertEqual(
                result["totals_atto"]["not_issued"], "30"
            )
            self.assertEqual(
                result["victim_total_claim_atto_not_routed"], "30"
            )
            with outputs["non_issuance_output"].open(newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(
                {row["address_hex"] for row in rows},
                {address(1), address(2)},
            )


if __name__ == "__main__":
    unittest.main()
