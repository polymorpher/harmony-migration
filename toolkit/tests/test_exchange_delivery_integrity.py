import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
NATIVE_SCRIPT = (
    ROOT / "toolkit" / "scripts" / "exchanges" / "build-exchange-native-policy.py"
)
VERIFY_SCRIPT = (
    ROOT / "toolkit" / "scripts" / "exchanges" / "verify-exchange-routing.py"
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_csv(path, rows):
    with Path(path).open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


SOURCE = "0x" + "1" * 40
WALLET = "0x" + "2" * 40
STAKING = "0x" + "3" * 40


def audit_row(**overrides):
    row = {
        "exchange_id": "binance",
        "address_hex": SOURCE,
        "address_one": "one1source",
        "claim_status": "present",
        "liquid_shard0_atto": "600",
        "liquid_shard1_atto": "0",
        "pending_cross_shard_atto": "0",
        "pending_undelegation_atto": "0",
        "unclaimed_staking_reward_atto": "0",
        "native_wallet_airdrop_atto": "600",
        "wone_airdrop_atto": "0",
        "wallet_airdrop_atto": "600",
        "staked_to_vault_atto": "400",
        "native_total_claim_atto": "1000",
        "total_claim_atto": "1000",
        "wallet_component_atto": "600",
        "staking_component_atto": "400",
        "migration_stage": "exchange_manual",
        "issuance_treatment": "manual_from_reserve",
        "delivery_policy": "manual_from_reserve",
        "destination_mode": "aggregate_split",
        "delivery_tier": "aggregate_split",
        "planned_wallet_destination": WALLET,
        "planned_staking_destination": STAKING,
        "planned_delivery_status": "exchange_manual",
        "planned_total_entitlement_atto": "1000",
    }
    row.update(overrides)
    return row


class NativePolicyPinningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load(NATIVE_SCRIPT, "build_exchange_native_policy")

    def fixture(self, root):
        policy = root / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "delivery_source": "year_2050_supply_reserve",
                    "exchanges": [
                        {
                            "id": "binance",
                            "delivery_policy": "manual_from_reserve",
                            "destination_mode": "aggregate_split",
                        }
                    ],
                }
            )
        )
        normalization = root / "normalization.json"
        normalization.write_text("{}")
        audits = root / "audits"
        audits.mkdir()
        write_csv(audits / "binance.csv", [audit_row()])
        summary = root / "summary.json"
        summary.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "status": "passed",
                    "routes_emitted": True,
                    "delivery_policy": "manual_from_reserve",
                    "delivery_source": "year_2050_supply_reserve",
                    "policy_sha256": sha256(policy),
                    "normalization_summary_sha256": sha256(normalization),
                    "audit_outputs": {
                        "binance": {
                            "path": str(audits / "binance.csv"),
                            "sha256": sha256(audits / "binance.csv"),
                        }
                    },
                    "exchanges": {
                        "binance": {
                            "destination_mode": "aggregate_split",
                            "memo_status": "ready_for_manual_reserve_delivery",
                            "destination_status": "configured",
                            "destination": WALLET,
                            "staking_destination": STAKING,
                            "totals": {
                                "planned_total_entitlement_atto": "1000",
                                "planned_staked_to_vault_atto": "400",
                            },
                        }
                    },
                }
            )
        )
        return policy, normalization, audits, summary

    def test_pinned_summary_accepts_untampered_audits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, normalization, audits, summary = self.fixture(root)
            loaded = self.module.load_exchange_summary(
                str(summary), str(policy), audits, str(normalization)
            )
            rows = self.module.load_audit(audits / "binance.csv", "binance")
            deliveries, _wallets = self.module.build_deliveries("binance", rows)
            config = {"id": "binance", "destination_mode": "aggregate_split"}
            self.module.require_summary_agreement(
                "binance", config, loaded, rows, deliveries
            )
            self.assertEqual(
                {(row["destination_address"], row["total_delivery_atto"]) for row in deliveries},
                {(WALLET, "600"), (STAKING, "400")},
            )

    def test_tampered_audit_amount_is_rejected_by_hash_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, normalization, audits, summary = self.fixture(root)
            write_csv(
                audits / "binance.csv",
                [
                    audit_row(
                        liquid_shard0_atto="700",
                        native_wallet_airdrop_atto="700",
                        wallet_airdrop_atto="700",
                        native_total_claim_atto="1100",
                        total_claim_atto="1100",
                        wallet_component_atto="700",
                        planned_total_entitlement_atto="1100",
                    )
                ],
            )
            with self.assertRaisesRegex(ValueError, "audit CSV hash"):
                self.module.load_exchange_summary(
                    str(summary), str(policy), audits, str(normalization)
                )

    def test_tampered_destination_is_rejected_against_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, normalization, audits, summary = self.fixture(root)
            loaded = self.module.load_exchange_summary(
                str(summary), str(policy), audits, str(normalization)
            )
            rows = [audit_row(planned_staking_destination="0x" + "9" * 40)]
            deliveries, _wallets = self.module.build_deliveries("binance", rows)
            config = {"id": "binance", "destination_mode": "aggregate_split"}
            with self.assertRaisesRegex(ValueError, "split destinations differ"):
                self.module.require_summary_agreement(
                    "binance", config, loaded, rows, deliveries
                )
            summary_value = json.loads(summary.read_text())
            summary_value["exchanges"]["binance"]["memo_status"] = "stage_policy_pending"
            with self.assertRaisesRegex(ValueError, "not ready"):
                self.module.require_summary_agreement(
                    "binance",
                    config,
                    summary_value,
                    self.module.load_audit(audits / "binance.csv", "binance"),
                    deliveries,
                )

    def test_reported_total_is_parsed_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "total.txt"
            path.write_text("12345678901.123456789012345678\n")
            self.assertEqual(
                self.module.load_reported_total(path), 12345678901123456789012345678
            )
            path.write_text("1.0000000000000000001\n")
            with self.assertRaises(ValueError):
                self.module.load_reported_total(path)


class RoutingVerificationEqualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load(VERIFY_SCRIPT, "verify_exchange_routing")

    def routes(self):
        planned = {
            "amount": 1000,
            "wallet_component": 600,
            "staking_component": 400,
            "stage": "exchange_manual",
            "treatment": "manual_from_reserve",
            "tier": "aggregate_split",
            "status": "exchange_manual",
            "wallet_destination": WALLET,
            "staking_destination": STAKING,
        }
        return {
            f"exchange-binance-{SOURCE[2:]}-wallet": {
                "exchange_id": "binance",
                "address": SOURCE,
                "suffix": "-wallet",
                "planned": planned,
                "status": "exchange_manual",
                "destination_id": "exchange-binance",
                "expected_destination": WALLET,
            },
            f"exchange-binance-{SOURCE[2:]}-staking": {
                "exchange_id": "binance",
                "address": SOURCE,
                "suffix": "-staking",
                "planned": planned,
                "status": "exchange_manual",
                "destination_id": "exchange-binance-staking",
                "expected_destination": STAKING,
            },
        }

    def compiled(self, path, status="exchange_manual", staking_destination=STAKING):
        write_csv(
            path,
            [
                {
                    "route_id": f"exchange-binance-{SOURCE[2:]}-wallet",
                    "amount_atto": "600",
                    "destination_status": status,
                    "destination_address": WALLET,
                    "migration_stage": "exchange_manual",
                    "issuance_treatment": "manual_from_reserve",
                },
                {
                    "route_id": f"exchange-binance-{SOURCE[2:]}-staking",
                    "amount_atto": "400",
                    "destination_status": status,
                    "destination_address": staking_destination,
                    "migration_stage": "exchange_manual",
                    "issuance_treatment": "manual_from_reserve",
                },
            ],
        )

    def test_audits_must_match_accounting_summary_hashes(self):
        policy = {"exchanges": [{"id": "binance"}]}
        with tempfile.TemporaryDirectory() as directory:
            audits = Path(directory)
            write_csv(audits / "binance.csv", [audit_row()])
            summary = {
                "status": "passed",
                "audit_outputs": {
                    "binance": {
                        "path": "artifacts/x/audits/binance.csv",
                        "sha256": sha256(audits / "binance.csv"),
                    }
                },
            }
            self.module.require_pinned_audits(summary, policy, audits)
            write_csv(audits / "binance.csv", [audit_row(planned_total_entitlement_atto="0")])
            with self.assertRaisesRegex(ValueError, "audit CSV hash"):
                self.module.require_pinned_audits(summary, policy, audits)
            summary["audit_outputs"]["okx"] = summary["audit_outputs"]["binance"]
            with self.assertRaisesRegex(ValueError, "audit set differs"):
                self.module.require_pinned_audits(summary, policy, audits)

    def test_exact_destination_and_status_equality(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exceptions.csv"
            self.compiled(path)
            amounts, _statuses, _stages = self.module.load_compiled(path, self.routes())
            self.assertEqual(sorted(amounts.values()), [400, 600])
            self.compiled(path, status="hold")
            with self.assertRaisesRegex(ValueError, "does not equal the planned status"):
                self.module.load_compiled(path, self.routes())
            self.compiled(path, staking_destination=WALLET)
            with self.assertRaisesRegex(ValueError, "do not equal the planned destination"):
                self.module.load_compiled(path, self.routes())


if __name__ == "__main__":
    unittest.main()
