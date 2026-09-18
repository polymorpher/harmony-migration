import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CONTRACT_REVIEW = (
    Path(__file__).parents[1] / "scripts" / "contract-review"
)
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "routing"
    / "apply-routes.py"
)
INITIALIZER = SCRIPT.with_name("init-local-routing.py")

POLICY_DECISION_FIELDS = (
    "decision_id", "status", "decision", "evidence", "notes"
)


def address(index):
    return f"0x{index:040x}"


def secure_key(index):
    return "0x" + lib.keccak256(bytes.fromhex(address(index)[2:])).hex()


def resolved_policy_decisions():
    return [
        {
            "decision_id": decision_id,
            "status": "resolved",
            "decision": "fixture decision",
        }
        for decision_id in (
            "contract-recovery-custody",
            "layerzero-nativeoft-reconciliation",
            "rollback-exploit-proceeds",
            "wone-holder-redistribution",
        )
    ]


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
                "liquid_shard0_atto",
                "wone_airdrop_atto",
                "wallet_airdrop_atto",
                "staked_to_vault_atto",
                "total_claim_atto",
                "code_hash_shard0",
                "code_hash_shard1",
            )

            def claim(
                index,
                wallet,
                staked,
                code_bearing=False,
                wone=0,
            ):
                return {
                    "secure_key": secure_key(index),
                    "address": address(index),
                    "liquid_shard0_atto": str(wallet),
                    "wone_airdrop_atto": str(wone),
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
                claim(6, 20, 0, wone=20),
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
                        "validator_address": address(10),
                        "validator_secure_key": secure_key(10),
                        "delegator_address": address(1),
                        "delegator_secure_key": secure_key(1),
                        "staked_to_vault_atto": "60",
                    },
                    {
                        "validator_address": address(11),
                        "validator_secure_key": secure_key(11),
                        "delegator_address": address(1),
                        "delegator_secure_key": secure_key(1),
                        "staked_to_vault_atto": "40",
                    },
                    {
                        "validator_address": address(10),
                        "validator_secure_key": secure_key(10),
                        "delegator_address": address(2),
                        "delegator_secure_key": secure_key(2),
                        "staked_to_vault_atto": "50",
                    },
                    {
                        "validator_address": address(11),
                        "validator_secure_key": secure_key(11),
                        "delegator_address": address(5),
                        "delegator_secure_key": secure_key(5),
                        "staked_to_vault_atto": "20",
                    },
                ],
            )
            write_csv(
                deferred_shares,
                share_fields,
                [
                    {
                        "validator_address": address(11),
                        "validator_secure_key": secure_key(11),
                        "delegator_address": address(4),
                        "delegator_secure_key": secure_key(4),
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
                        "validator_address": address(10),
                        "validator_secure_key": secure_key(10),
                        "vault_assets_atto": "110",
                    },
                    {
                        "validator_address": address(11),
                        "validator_secure_key": secure_key(11),
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
                    {
                        "destination_id": "contract-recovery-custody",
                        "destination_address": f"0x{20:040x}",
                        "status": "ready",
                        "notes": "",
                    },
                    {
                        "destination_id": "not-issuing",
                        "destination_address": "",
                        "status": "not_issuing",
                        "notes": "",
                    },
                    {
                        "destination_id": "wone-holder-redistribution",
                        "destination_address": "",
                        "status": "redistributed",
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
                "policy_state_block",
                "policy_state_block_hash",
                "policy_state_root",
            )
            write_csv(
                routes,
                route_fields,
                [
                    {
                        "route_id": "wone-redistribution",
                        "priority": "50",
                        "source_address": f"0x{6:040x}",
                        "destination_id": "wone-holder-redistribution",
                        "destination_address": "",
                        "amount_atto": "20",
                        "allocation_method": "wallet_only",
                        "reason": "test",
                        "evidence": "",
                        "notes": "",
                    },
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
                        "destination_id": "contract-recovery-custody",
                        "destination_address": "",
                        "amount_atto": "ALL",
                        "allocation_method": "wallet_first_pro_rata_vault",
                        "reason": (
                            "non_multisig_contract_recovery_custody"
                        ),
                        "evidence": "",
                        "notes": "",
                        "policy_state_block": "10",
                        "policy_state_block_hash": "0xabc",
                        "policy_state_root": "0xdef",
                    },
                    {
                        "route_id": "excluded-not-issuing",
                        "priority": "100",
                        "source_address": f"0x{3:040x}",
                        "destination_id": "not-issuing",
                        "destination_address": "",
                        "amount_atto": "SHARD0_LIQUID",
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
                (
                    "address",
                    "policy_state_block",
                    "policy_state_block_hash",
                    "policy_state_root",
                ),
                [
                    {
                        "address": f"0x{5:040x}",
                        "policy_state_block": "10",
                        "policy_state_block_hash": "0xabc",
                        "policy_state_root": "0xdef",
                    }
                ],
            )
            policy_decisions = root / "policy-decisions.csv"
            write_csv(
                policy_decisions,
                POLICY_DECISION_FIELDS,
                resolved_policy_decisions(),
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
            self.assertEqual(result["source_wallet_airdrop_atto"], "290")
            self.assertEqual(result["source_wone_airdrop_atto"], "20")
            self.assertEqual(result["source_staked_to_vault_atto"], "190")
            self.assertEqual(result["unresolved_wallet_airdrop_atto"], "110")
            self.assertEqual(result["unresolved_staked_to_vault_atto"], "70")
            self.assertEqual(result["not_issued_wallet_airdrop_atto"], "80")
            self.assertEqual(result["not_issued_staked_to_vault_atto"], "0")
            self.assertEqual(result["not_issued_total_claim_atto"], "80")
            self.assertEqual(result["redistributed_total_claim_atto"], "20")
            self.assertEqual(result["issuable_total_claim_atto"], "380")
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
            redistributed = [
                row
                for row in rows
                if row["route_id"] == "wone-redistribution"
            ]
            self.assertEqual(len(redistributed), 1)
            self.assertEqual(
                redistributed[0]["destination_status"],
                "redistributed",
            )
            non_issued = [
                row
                for row in rows
                if row["route_id"] == "excluded-not-issuing"
            ]
            self.assertEqual(len(non_issued), 1)
            self.assertEqual(
                non_issued[0]["destination_status"], "not_issuing"
            )
            with unresolved.open(newline="") as handle:
                unresolved_rows = list(csv.DictReader(handle))
            self.assertFalse(
                any(
                    row["route_id"] == "wone-redistribution"
                    for row in unresolved_rows
                )
            )
            self.assertFalse(
                any(
                    row["route_id"] == "excluded-not-issuing"
                    for row in unresolved_rows
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


class PolicyDecisionGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("apply_routes", SCRIPT)
        cls.routing = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.routing)

    def load_decisions(self, rows, fields=POLICY_DECISION_FIELDS):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy-decisions.csv"
            write_csv(path, fields, rows)
            return self.routing.load_policy_decisions(path)

    def test_header_only_file_rejects_missing_decisions(self):
        with self.assertRaisesRegex(
            ValueError, "missing required policy decisions"
        ):
            self.load_decisions([])

    def test_each_required_decision_must_be_present(self):
        rows = resolved_policy_decisions()
        for missing in rows:
            with self.subTest(decision_id=missing["decision_id"]):
                with self.assertRaisesRegex(ValueError, missing["decision_id"]):
                    self.load_decisions([row for row in rows if row != missing])

    def test_unrelated_resolved_decision_cannot_replace_required_decisions(self):
        with self.assertRaisesRegex(
            ValueError, "missing required policy decisions"
        ):
            self.load_decisions(
                [{
                    "decision_id": "unrelated",
                    "status": "resolved",
                    "decision": "fixture",
                }]
            )

    def test_complete_resolved_decisions_have_no_pending_gates(self):
        self.assertEqual(self.load_decisions(resolved_policy_decisions()), [])

    def test_required_and_additional_pending_decisions_remain_gates(self):
        rows = resolved_policy_decisions()
        rows[0].update(status="pending", decision="")
        rows.append({"decision_id": "additional-review", "status": "pending"})
        self.assertEqual(
            self.load_decisions(rows),
            [rows[0]["decision_id"], "additional-review"],
        )

    def test_invalid_schema_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing fields"):
            self.load_decisions([], fields=("decision_id",))

    def test_public_example_retains_required_pending_decisions(self):
        example = (
            SCRIPT.parents[3] / "routing" / "policy-decisions.example.csv"
        )
        self.assertEqual(
            self.routing.load_policy_decisions(example),
            [
                "rollback-exploit-proceeds",
                "layerzero-nativeoft-reconciliation",
            ],
        )

    def test_initializer_creates_split_reserve_gates_and_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "routing"
            subprocess.run(
                (
                    sys.executable,
                    str(INITIALIZER),
                    "--directory",
                    str(root),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                self.routing.load_policy_decisions(
                    root / "policy-decisions.csv"
                ),
                [
                    "rollback-exploit-proceeds",
                    "layerzero-nativeoft-reconciliation",
                ],
            )
            with (root / "bridge-reserves.base.csv").open(
                newline=""
            ) as source:
                reserve_route_ids = {
                    row["route_id"] for row in csv.DictReader(source)
                }
            self.assertEqual(
                reserve_route_ids,
                {
                    "layerzero-nativeoft-bsc-custody",
                    "layerzero-nativeoft-ethereum-custody",
                },
            )
            with (root / "destinations.csv").open(newline="") as source:
                destinations = {
                    row["destination_id"]: row
                    for row in csv.DictReader(source)
                }
            self.assertEqual(
                destinations["wone-holder-redistribution"]["status"],
                "redistributed",
            )
            self.assertIn("layerzero-nativeoft-custody", destinations)
            self.assertIn("contract-recovery-custody", destinations)
            self.assertIn("not-issuing", destinations)


if __name__ == "__main__":
    unittest.main()
