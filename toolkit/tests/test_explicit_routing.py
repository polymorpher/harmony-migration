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
    / "apply-routes.py"
)


def write_csv(path, fields, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


class ExplicitRoutingTest(unittest.TestCase):
    def test_partial_route_uses_wallet_then_pro_rata_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claim_fields = (
                "secure_key",
                "address",
                "wallet_airdrop_atto",
                "staked_to_vault_atto",
                "total_claim_atto",
                "code_hash_shard0",
                "code_hash_shard1",
            )

            def claim(index, wallet, staked, code_bearing=False):
                return {
                    "secure_key": f"0x{index:064x}",
                    "address": f"0x{index:040x}",
                    "wallet_airdrop_atto": str(wallet),
                    "staked_to_vault_atto": str(staked),
                    "total_claim_atto": str(wallet + staked),
                    "code_hash_shard0": (
                        "0x" + "1" * 64
                        if code_bearing
                        else "0xc5d2460186f7233c927e7db2dcc703c0e"
                        "500b653ca82273b7bfad8045d85a470"
                    ),
                    "code_hash_shard1": "",
                }

            automatic = root / "automatic.csv"
            contracts = root / "contracts.csv"
            excluded = root / "excluded.csv"
            all_claims = root / "all-claims.csv"
            claim_rows = (
                claim(1, 100, 100),
                claim(2, 50, 50),
                claim(3, 80, 0),
                claim(4, 10, 20),
                claim(5, 30, 20, code_bearing=True),
            )
            write_csv(all_claims, claim_fields, claim_rows)
            write_csv(
                automatic, claim_fields, [claim_rows[0], claim_rows[4]]
            )
            write_csv(contracts, claim_fields, [claim_rows[1]])
            write_csv(excluded, claim_fields, [claim_rows[2]])

            shares = root / "shares.csv"
            deferred_shares = root / "deferred-shares.csv"
            share_fields = (
                "validator_address",
                "validator_secure_key",
                "delegator_address",
                "delegator_secure_key",
                "staked_to_vault_atto",
            )
            write_csv(
                shares,
                share_fields,
                [
                    {
                        "validator_address": f"0x{10:040x}",
                        "validator_secure_key": f"0x{10:064x}",
                        "delegator_address": f"0x{1:040x}",
                        "delegator_secure_key": f"0x{1:064x}",
                        "staked_to_vault_atto": "60",
                    },
                    {
                        "validator_address": f"0x{11:040x}",
                        "validator_secure_key": f"0x{11:064x}",
                        "delegator_address": f"0x{1:040x}",
                        "delegator_secure_key": f"0x{1:064x}",
                        "staked_to_vault_atto": "40",
                    },
                    {
                        "validator_address": f"0x{10:040x}",
                        "validator_secure_key": f"0x{10:064x}",
                        "delegator_address": f"0x{2:040x}",
                        "delegator_secure_key": f"0x{2:064x}",
                        "staked_to_vault_atto": "50",
                    },
                    {
                        "validator_address": f"0x{11:040x}",
                        "validator_secure_key": f"0x{11:064x}",
                        "delegator_address": f"0x{5:040x}",
                        "delegator_secure_key": f"0x{5:064x}",
                        "staked_to_vault_atto": "20",
                    },
                ],
            )
            write_csv(
                deferred_shares,
                share_fields,
                [
                    {
                        "validator_address": f"0x{11:040x}",
                        "validator_secure_key": f"0x{11:064x}",
                        "delegator_address": f"0x{4:040x}",
                        "delegator_secure_key": f"0x{4:064x}",
                        "staked_to_vault_atto": "20",
                    }
                ],
            )
            base_vaults = root / "base-vaults.csv"
            write_csv(
                base_vaults,
                (
                    "validator_address",
                    "validator_secure_key",
                    "vault_assets_atto",
                ),
                [
                    {
                        "validator_address": f"0x{10:040x}",
                        "validator_secure_key": f"0x{10:064x}",
                        "vault_assets_atto": "110",
                    },
                    {
                        "validator_address": f"0x{11:040x}",
                        "validator_secure_key": f"0x{11:064x}",
                        "vault_assets_atto": "80",
                    },
                ],
            )
            destinations = root / "destinations.csv"
            write_csv(
                destinations,
                ("destination_id", "destination_address", "status", "notes"),
                [
                    {
                        "destination_id": "treasury",
                        "destination_address": "",
                        "status": "hold",
                        "notes": "",
                    },
                    {
                        "destination_id": "recovery",
                        "destination_address": f"0x{20:040x}",
                        "status": "ready",
                        "notes": "",
                    },
                ],
            )
            routes = root / "routes.csv"
            route_fields = (
                "route_id",
                "priority",
                "source_address",
                "destination_id",
                "destination_address",
                "amount_atto",
                "allocation_method",
                "reason",
                "evidence",
                "notes",
            )
            write_csv(
                routes,
                route_fields,
                [
                    {
                        "route_id": "partial-treasury",
                        "priority": "100",
                        "source_address": f"0x{1:040x}",
                        "destination_id": "treasury",
                        "destination_address": "",
                        "amount_atto": "150",
                        "allocation_method": "wallet_first_pro_rata_vault",
                        "reason": "test",
                        "evidence": "",
                        "notes": "",
                    },
                    {
                        "route_id": "contract-recovery",
                        "priority": "10",
                        "source_address": f"0x{2:040x}",
                        "destination_id": "recovery",
                        "destination_address": "",
                        "amount_atto": "ALL",
                        "allocation_method": "wallet_first_pro_rata_vault",
                        "reason": "test",
                        "evidence": "",
                        "notes": "",
                    },
                    {
                        "route_id": "excluded-treasury",
                        "priority": "100",
                        "source_address": f"0x{3:040x}",
                        "destination_id": "treasury",
                        "destination_address": "",
                        "amount_atto": "ALL",
                        "allocation_method": "wallet_first_pro_rata_vault",
                        "reason": "test",
                        "evidence": "",
                        "notes": "",
                    },
                    {
                        "route_id": "deferred-treasury",
                        "priority": "100",
                        "source_address": f"0x{4:040x}",
                        "destination_id": "treasury",
                        "destination_address": "",
                        "amount_atto": "ALL",
                        "allocation_method": "wallet_first_pro_rata_vault",
                        "reason": "test",
                        "evidence": "",
                        "notes": "",
                    },
                ],
            )
            governors = root / "governors.csv"
            write_csv(
                governors,
                (
                    "validator_address",
                    "destination_id",
                    "destination_address",
                    "status",
                    "reason",
                    "evidence",
                    "notes",
                ),
                [
                    {
                        "validator_address": f"0x{11:040x}",
                        "destination_id": "recovery",
                        "destination_address": "",
                        "status": "ready",
                        "reason": "test override",
                        "evidence": "",
                        "notes": "",
                    }
                ],
            )
            validator_accounts = root / "validator-accounts.csv"
            write_csv(
                validator_accounts,
                ("address",),
                [{"address": f"0x{5:040x}"}],
            )
            policy_decisions = root / "policy-decisions.csv"
            write_csv(
                policy_decisions,
                (
                    "decision_id",
                    "status",
                    "decision",
                    "evidence",
                    "notes",
                ),
                [
                    {
                        "decision_id": "test-policy",
                        "status": "resolved",
                        "decision": "fixture",
                        "evidence": "",
                        "notes": "",
                    }
                ],
            )
            exceptions = root / "routing-exceptions.csv"
            governor_exceptions = root / "governor-exceptions.csv"
            unresolved = root / "unresolved.csv"
            summary = root / "summary.json"
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--all-claims",
                    str(all_claims),
                    "--automatic-claims",
                    str(automatic),
                    "--contract-claims",
                    str(contracts),
                    "--excluded-claims",
                    str(excluded),
                    "--priority-shares",
                    str(shares),
                    "--deferred-shares",
                    str(deferred_shares),
                    "--base-vault-deposits",
                    str(base_vaults),
                    "--validator-accounts",
                    str(validator_accounts),
                    "--routes",
                    str(routes),
                    "--destinations",
                    str(destinations),
                    "--governors",
                    str(governors),
                    "--policy-decisions",
                    str(policy_decisions),
                    "--exceptions-output",
                    str(exceptions),
                    "--governor-exceptions-output",
                    str(governor_exceptions),
                    "--unresolved-output",
                    str(unresolved),
                    "--summary",
                    str(summary),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(summary.read_text())
            self.assertEqual(result["status"], "hold")
            self.assertEqual(result["source_wallet_airdrop_atto"], "270")
            self.assertEqual(result["source_staked_to_vault_atto"], "190")
            self.assertEqual(result["unresolved_wallet_airdrop_atto"], "190")
            self.assertEqual(result["unresolved_staked_to_vault_atto"], "70")
            with exceptions.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            treasury = [
                row
                for row in rows
                if row["route_id"] == "partial-treasury"
                and row["component"] == "vault_shares"
            ]
            self.assertEqual(
                sorted(int(row["amount_atto"]) for row in treasury),
                [20, 30],
            )
            self.assertFalse(
                any(
                    row["source_address"].lower()
                    == f"0x{1:040x}"
                    and row["route_id"].startswith("default-")
                    for row in rows
                )
            )
            validator_rows = [
                row
                for row in rows
                if row["source_address"].lower() == f"0x{5:040x}"
            ]
            self.assertEqual(
                {
                    (row["component"], int(row["amount_atto"]))
                    for row in validator_rows
                },
                {("wallet_airdrop", 30), ("vault_shares", 20)},
            )
            self.assertTrue(
                all(
                    row["source_category"] == "validator_account"
                    and row["source_code_bearing"] == "true"
                    and row["exception_type"]
                    == "validator_wrapper_same_address"
                    and row["destination_status"] == "ready"
                    for row in validator_rows
                )
            )
            self.assertEqual(result["validator_account_exceptions"], 1)
            with governor_exceptions.open(newline="") as handle:
                governor_rows = list(csv.DictReader(handle))
            self.assertEqual(len(governor_rows), 1)
            self.assertEqual(
                governor_rows[0]["exception_type"],
                "explicit_governor_route",
            )


if __name__ == "__main__":
    unittest.main()
