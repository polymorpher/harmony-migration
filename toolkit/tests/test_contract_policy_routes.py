import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "routing"
    / "build-contract-policy-routes.py"
)


class ContractPolicyRoutesTest(unittest.TestCase):
    def test_partitions_next_stage_and_not_issued_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts.csv"
            contract_fields = (
                "address",
                "primary_category",
                "subcategory",
                "policy_state_block",
                "policy_state_block_hash",
                "policy_state_root",
            )
            bridge = "0x5b18a4e73f9a4fe337a072516b317863ad3046aa"
            bridge_eth = (
                "0x905582f21fb9855c809d5b8933272a292dfbb138"
            )
            rows = [
                (1, "validator-account", "validator"),
                (2, "multisig-wallet", "2-of-3 Safe"),
                (3, "onewallet", "1wallet v16"),
                (bridge, "known-app", "LayerZero"),
                (bridge_eth, "known-app", "LayerZero"),
                (4, "smartvault-wallet", "SmartVault"),
                (5, "known-app", "pool"),
            ]
            with contracts.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output, fieldnames=contract_fields, lineterminator="\n"
                )
                writer.writeheader()
                for value, category, subcategory in rows:
                    address = (
                        value if isinstance(value, str) else f"0x{value:040x}"
                    )
                    writer.writerow(
                        {
                            "address": address,
                            "primary_category": category,
                            "subcategory": subcategory,
                            "policy_state_block": "10",
                            "policy_state_block_hash": "0xabc",
                            "policy_state_root": "0xdef",
                        }
                    )

            stages = root / "stages.csv"
            stage_fields = (
                "address",
                "account_classification",
                "policy_group",
                "migration_stage",
                "issuance_treatment",
                "migration_wallet_allocation_atto",
                "migration_staked_to_vault_atto",
                "migration_allocation_atto",
                "reviewed_contract_non_issuance_atto",
                "reviewed_contract_wallet_non_issuance_atto",
                "reviewed_contract_staked_non_issuance_atto",
            )
            stage_rows = [
                (2, "multisig", "next_stage", "issue", "2000", "0"),
                (3, "onewallet", "next_stage", "issue", "3000", "0"),
                (
                    bridge,
                    "layerzero_bridge_collateral",
                    "next_stage",
                    "issue",
                    "4000",
                    "0",
                ),
                (
                    bridge_eth,
                    "layerzero_bridge_collateral",
                    "next_stage",
                    "issue",
                    "4500",
                    "0",
                ),
                (4, "smartvault", "", "not_issued", "0", "5000"),
                (
                    5,
                    "other_reviewed_contract",
                    "",
                    "not_issued",
                    "0",
                    "6000",
                ),
            ]
            with stages.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output, fieldnames=stage_fields, lineterminator="\n"
                )
                writer.writeheader()
                for (
                    value,
                    group,
                    stage,
                    treatment,
                    allocation,
                    not_issued,
                ) in stage_rows:
                    address = (
                        value if isinstance(value, str) else f"0x{value:040x}"
                    )
                    writer.writerow(
                        {
                            "address": address,
                            "account_classification": "genuine_contract",
                            "policy_group": group,
                            "migration_stage": stage,
                            "issuance_treatment": treatment,
                            "migration_wallet_allocation_atto": allocation,
                            "migration_staked_to_vault_atto": "0",
                            "migration_allocation_atto": allocation,
                            "reviewed_contract_non_issuance_atto": not_issued,
                            "reviewed_contract_wallet_non_issuance_atto": (
                                not_issued
                            ),
                            "reviewed_contract_staked_non_issuance_atto": "0",
                        }
                    )

            routes = root / "routes.csv"
            summary = root / "summary.json"
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--contracts",
                    str(contracts),
                    "--stage-policy",
                    str(stages),
                    "--output",
                    str(routes),
                    "--summary",
                    str(summary),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            with routes.open(newline="") as source:
                output_rows = list(csv.DictReader(source))
            self.assertEqual(len(output_rows), 4)
            by_source = {
                row["source_address"]: row for row in output_rows
            }
            self.assertEqual(
                by_source[f"0x{2:040x}"]["destination_id"],
                "",
            )
            self.assertEqual(
                by_source[f"0x{3:040x}"]["destination_id"],
                "onewallet-recovery-multisig",
            )
            for index in (4, 5):
                self.assertEqual(
                    by_source[f"0x{index:040x}"]["destination_id"],
                    "not-issuing",
                )
                self.assertEqual(
                    by_source[f"0x{index:040x}"]["reason"],
                    "reviewed_contract_allocation_not_issued",
                )
            self.assertNotIn(bridge, by_source)
            self.assertTrue(
                all(row["amount_atto"] == "ALL" for row in output_rows)
            )

            result = json.loads(summary.read_text())
            self.assertEqual(result["validator_rows_skipped"], 1)
            self.assertEqual(result["reviewed_contracts"], 6)
            self.assertEqual(result["dedicated_layerzero_routes"], 2)
            self.assertEqual(
                result["groups"]["smartvault"]["not_issued_atto"], "5000"
            )
            self.assertEqual(
                result["policy_state"],
                {
                    "block": 10,
                    "block_hash": "0xabc",
                    "state_root": "0xdef",
                },
            )


if __name__ == "__main__":
    unittest.main()
