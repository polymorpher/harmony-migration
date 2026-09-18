import csv
import hashlib
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
    / "materialize-initial-stage.py"
)


def address(index):
    return f"0x{index:040x}"


def key(index):
    return f"0x{index:064x}"


def write_csv(path, fields, rows):
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MaterializeInitialStageTest(unittest.TestCase):
    def test_materializes_only_initial_issue_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stages = root / "stages.csv"
            stage_fields = (
                "secure_key",
                "address",
                "account_classification",
                "routing_category",
                "migration_stage",
                "issuance_treatment",
                "migration_wallet_allocation_atto",
                "migration_staked_to_vault_atto",
                "migration_allocation_atto",
            )
            write_csv(
                stages,
                stage_fields,
                [
                    {
                        "secure_key": key(1),
                        "address": address(1),
                        "account_classification": "wallet",
                        "routing_category": "automatic_policy",
                        "migration_stage": "initial",
                        "issuance_treatment": "issue",
                        "migration_wallet_allocation_atto": "100",
                        "migration_staked_to_vault_atto": "50",
                        "migration_allocation_atto": "150",
                    },
                    {
                        "secure_key": key(2),
                        "address": address(2),
                        "account_classification": "wallet",
                        "routing_category": "exchange_or_manual",
                        "migration_stage": "initial",
                        "issuance_treatment": "issue",
                        "migration_wallet_allocation_atto": "80",
                        "migration_staked_to_vault_atto": "0",
                        "migration_allocation_atto": "80",
                    },
                    {
                        "secure_key": key(3),
                        "address": address(3),
                        "account_classification": "genuine_contract",
                        "routing_category": "contract_policy",
                        "migration_stage": "next_stage",
                        "issuance_treatment": "issue",
                        "migration_wallet_allocation_atto": "1000",
                        "migration_staked_to_vault_atto": "0",
                        "migration_allocation_atto": "1000",
                    },
                ],
            )

            base_shares = root / "base-shares.csv"
            write_csv(
                base_shares,
                (
                    "validator_secure_key",
                    "validator_address",
                    "delegator_secure_key",
                    "delegator_address",
                    "staked_to_vault_atto",
                ),
                [
                    {
                        "validator_secure_key": key(10),
                        "validator_address": address(10),
                        "delegator_secure_key": key(1),
                        "delegator_address": address(1),
                        "staked_to_vault_atto": "60",
                    }
                ],
            )

            exceptions = root / "exceptions.csv"
            exception_fields = (
                "component",
                "source_secure_key",
                "source_address",
                "validator_secure_key",
                "validator_address",
                "amount_atto",
                "migration_stage",
                "issuance_treatment",
                "destination_id",
                "destination_address",
                "destination_status",
                "reason",
                "evidence",
            )
            write_csv(
                exceptions,
                exception_fields,
                [
                    {
                        "component": "vault_shares",
                        "source_secure_key": key(1),
                        "source_address": address(1),
                        "validator_secure_key": key(10),
                        "validator_address": address(10),
                        "amount_atto": "10",
                        "migration_stage": "initial",
                        "issuance_treatment": "not_issued",
                        "destination_id": "not-issuing",
                        "destination_address": "",
                        "destination_status": "not_issuing",
                        "reason": "fixture deduction",
                        "evidence": "fixture",
                    },
                    {
                        "component": "wallet_airdrop",
                        "source_secure_key": key(2),
                        "source_address": address(2),
                        "validator_secure_key": "",
                        "validator_address": "",
                        "amount_atto": "80",
                        "migration_stage": "initial",
                        "issuance_treatment": "issue",
                        "destination_id": "exchange",
                        "destination_address": "",
                        "destination_status": "hold",
                        "reason": "fixture manual hold",
                        "evidence": "fixture",
                    },
                ],
            )

            vault_stages = root / "vault-stages.csv"
            write_csv(
                vault_stages,
                (
                    "validator_secure_key",
                    "validator_address",
                    "initial_assets_atto",
                ),
                [
                    {
                        "validator_secure_key": key(10),
                        "validator_address": address(10),
                        "initial_assets_atto": "50",
                    }
                ],
            )
            governors = root / "governors.csv"
            write_csv(
                governors,
                (
                    "validator_address",
                    "destination_address",
                    "destination_status",
                    "reason",
                    "evidence",
                ),
                [],
            )
            routing_summary = root / "routing-summary.json"
            routing_summary.write_text(
                json.dumps(
                    {
                        "status": "hold",
                        "migration_stages_sha256": sha256(stages),
                        "outputs": {
                            "routing_exceptions": {
                                "path": str(exceptions),
                                "sha256": sha256(exceptions),
                            },
                            "governor_exceptions": {
                                "path": str(governors),
                                "sha256": sha256(governors),
                            },
                            "vault_stages": {
                                "path": str(vault_stages),
                                "sha256": sha256(vault_stages),
                            },
                        },
                        "stage_readiness": {
                            "initial": {
                                "wallet_airdrop_atto": "180",
                                "staked_to_vault_atto": "50",
                                "held_wallet_airdrop_atto": "80",
                                "held_staked_to_vault_atto": "0",
                                "held_validator_governor_assets_atto": "0",
                                "pending_policy_decisions": [],
                                "status": "hold",
                            }
                        },
                    }
                )
            )

            wallets = root / "wallets.csv"
            shares = root / "shares.csv"
            vaults = root / "vaults.csv"
            unresolved = root / "unresolved.csv"
            summary = root / "summary.json"
            report = root / "report.md"
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--stage-policy",
                    str(stages),
                    "--base-priority-shares",
                    str(base_shares),
                    "--routing-exceptions",
                    str(exceptions),
                    "--routing-summary",
                    str(routing_summary),
                    "--vault-stages",
                    str(vault_stages),
                    "--governor-exceptions",
                    str(governors),
                    "--wallet-output",
                    str(wallets),
                    "--shares-output",
                    str(shares),
                    "--vault-output",
                    str(vaults),
                    "--unresolved-output",
                    str(unresolved),
                    "--summary",
                    str(summary),
                    "--report",
                    str(report),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(summary.read_text())
            self.assertEqual(result["status"], "hold")
            self.assertEqual(result["source_addresses"], 2)
            self.assertEqual(result["wallet_allocation_atto"], "180")
            self.assertEqual(result["vault_share_allocation_atto"], "50")
            with wallets.open(newline="") as source:
                wallet_rows = list(csv.DictReader(source))
            self.assertEqual(
                {row["source_address"] for row in wallet_rows},
                {address(1), address(2)},
            )
            with shares.open(newline="") as source:
                share_rows = list(csv.DictReader(source))
            self.assertEqual(len(share_rows), 1)
            self.assertEqual(share_rows[0]["amount_atto"], "50")
            with unresolved.open(newline="") as source:
                unresolved_rows = list(csv.DictReader(source))
            self.assertEqual(len(unresolved_rows), 1)
            self.assertEqual(
                unresolved_rows[0]["item_type"], "wallet_delivery"
            )
            self.assertIn(
                "Initial-stage materialization", report.read_text()
            )


if __name__ == "__main__":
    unittest.main()
