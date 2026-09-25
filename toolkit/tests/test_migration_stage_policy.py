import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "claims"
    / "build-migration-stage-policy.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "build_migration_stage_policy", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrationStagePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_contract_policy_preserves_identity_stage_distinction(self):
        bridge = next(iter(self.module.LAYERZERO_ADDRESSES))
        self.assertEqual(
            self.module.contract_policy(
                "0x" + "01" * 20,
                {"primary_category": "multisig-wallet"},
            ),
            ("multisig", "next_stage"),
        )
        self.assertEqual(
            self.module.contract_policy(
                "0x" + "02" * 20,
                {"primary_category": "onewallet"},
            ),
            ("onewallet", "next_stage"),
        )
        self.assertEqual(
            self.module.contract_policy(
                bridge,
                {"primary_category": "known-app"},
            ),
            ("layerzero_bridge_collateral", "next_stage"),
        )
        self.assertEqual(
            self.module.contract_policy(
                "0x" + "03" * 20,
                {"primary_category": "smartvault-wallet"},
            ),
            ("smartvault", ""),
        )
        self.assertIsNone(
            self.module.contract_policy(
                "0x" + "04" * 20,
                {"primary_category": "validator-account"},
            )
        )

    def test_six_calendar_month_window_preserves_cutoff_time(self):
        cutoff = datetime(2026, 9, 10, 14, tzinfo=timezone.utc)
        self.assertEqual(
            self.module.subtract_calendar_months(cutoff, 6),
            datetime(2026, 3, 10, 14, tzinfo=timezone.utc),
        )

    def test_incident_deductions_apply_before_threshold(self):
        one = 10**18
        since = datetime(2026, 3, 10, 14, tzinfo=timezone.utc)
        active = datetime(2026, 8, 20, tzinfo=timezone.utc)
        stage = self.module.wallet_stage
        # 1,000,000 ONE credited by an exploit, 5 ONE legitimate: not eligible
        self.assertEqual(
            stage(5 * one, 5 * one, active, since, 6),
            ("deferred", "below 1,000 ONE after incident deductions", False),
        )
        # a legitimate remainder at the threshold still qualifies
        self.assertEqual(stage(1000 * one, 1000 * one, active, since, 6)[:2], ("initial", "wallet activity within 6 months"))
        self.assertEqual(stage(0, 0, active, since, 6)[0], "")
        self.assertEqual(
            stage(2000 * one, 2000 * one, None, since, 6),
            ("deferred", "no indexed wallet activity", True),
        )

    def test_final_component_split_applies_deduction_wallet_first(self):
        self.assertEqual(
            self.module.split_final_components(100, 900, 950),
            (50, 900),
        )
        self.assertEqual(
            self.module.split_final_components(100, 900, 800),
            (0, 800),
        )
        self.assertEqual(
            self.module.split_final_components(100, 900, 0),
            (0, 0),
        )


if __name__ == "__main__":
    unittest.main()
