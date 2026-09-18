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
    / "apply-wone-qualification.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "apply_wone_qualification",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WoneQualificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def row(self, wallet, staked):
        total = wallet + staked
        return {
            "wallet_airdrop_atto": str(wallet),
            "staked_to_vault_atto": str(staked),
            "total_claim_atto": str(total),
            "valuation_price_usd_per_one": "0.00074801",
        }

    def test_wone_crosses_inclusive_threshold(self):
        scale = self.module.ATTO_PER_ONE
        result = self.module.apply_overlay(
            self.row(500 * scale, 100 * scale),
            400 * scale,
            1000 * scale,
        )
        self.assertEqual(result["qualification_total_atto"], str(1000 * scale))
        self.assertEqual(result["wone_airdrop_atto"], str(400 * scale))
        self.assertEqual(result["wallet_airdrop_atto"], str(900 * scale))
        self.assertEqual(result["total_claim_atto"], str(1000 * scale))
        self.assertEqual(
            result["native_total_claim_atto"],
            str(600 * scale),
        )

    def test_below_threshold_wone_is_not_in_current_airdrop(self):
        scale = self.module.ATTO_PER_ONE
        result = self.module.apply_overlay(
            self.row(500 * scale, 100 * scale),
            399 * scale,
            1000 * scale,
        )
        self.assertEqual(result["qualification_total_atto"], str(999 * scale))
        self.assertEqual(result["wone_balance_atto"], str(399 * scale))
        self.assertEqual(result["wone_airdrop_atto"], "0")
        self.assertEqual(result["total_claim_atto"], str(600 * scale))

    def test_native_qualifier_receives_all_wone(self):
        scale = self.module.ATTO_PER_ONE
        result = self.module.apply_overlay(
            self.row(950 * scale, 50 * scale),
            7 * scale,
            1000 * scale,
        )
        self.assertEqual(result["wone_airdrop_atto"], str(7 * scale))
        self.assertEqual(result["total_claim_atto"], str(1007 * scale))

    def test_output_fields_preserve_old_fields_and_add_overlay(self):
        fields = [
            "secure_key",
            "wallet_airdrop_atto",
            "total_claim_atto",
            "wallet_airdrop_one",
            "total_claim_one",
        ]
        result = self.module.output_fields(fields)
        self.assertEqual(len(result), len(fields) + len(self.module.EXTRA_FIELDS))
        self.assertEqual(len(result), len(set(result)))
        self.assertLess(
            result.index("wone_airdrop_atto"),
            result.index("wallet_airdrop_atto"),
        )
        self.assertLess(
            result.index("qualification_total_atto"),
            result.index("total_claim_atto"),
        )

    def test_wone_only_metadata_must_cover_complete_threshold_set(self):
        threshold = 1000 * self.module.ATTO_PER_ONE
        holders = {
            "0x" + "01" * 20: threshold,
            "0x" + "02" * 20: threshold + 1,
            "0x" + "03" * 20: threshold - 1,
        }
        native = {"0x" + "01" * 20}
        expected_metadata = {"0x" + "02" * 20}
        self.module.require_complete_new_holder_metadata(
            holders,
            native,
            expected_metadata,
            threshold,
        )
        with self.assertRaisesRegex(
            ValueError,
            "does not equal the complete threshold set",
        ):
            self.module.require_complete_new_holder_metadata(
                holders,
                native,
                set(),
                threshold,
            )

    def test_main_rejects_truncated_new_holder_metadata(self):
        scale = self.module.ATTO_PER_ONE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native_path = root / "native.csv"
            holders_path = root / "holders.csv"
            holder_summary_path = root / "holders-summary.json"
            metadata_path = root / "metadata.csv"
            output_path = root / "output.csv"
            output_summary_path = root / "output-summary.json"

            base_components = (
                "liquid_shard0",
                "liquid_shard1",
                "liquid_total",
                "active_staked_or_delegated",
                "pending_undelegation",
                "unclaimed_staking_reward",
                "pending_cross_shard",
                "wallet_airdrop",
                "staked_to_vault",
                "total_claim",
            )
            fields = (
                "secure_key",
                "address",
                "address_or_secure_key",
                "address_resolved",
                "claims_shard0_block",
                "claims_shard1_block",
                "valuation_price_reference_shard0_block",
                "valuation_price_usd_per_one",
                *(name + "_atto" for name in base_components),
                *(name + "_one" for name in base_components),
                "wallet_airdrop_usd",
                "total_usd",
                "nonce_shard0",
                "nonce_shard1",
                "code_hash_shard0",
                "code_hash_shard1",
            )
            reserve = 2001 * scale
            wone = self.module.WONE_ADDRESS
            secure_key = (
                "0x"
                + self.module.lib.keccak256(
                    bytes.fromhex(wone[2:])
                ).hex()
            )
            native = {field: "" for field in fields}
            native.update(
                {
                    "secure_key": secure_key,
                    "address": wone,
                    "address_or_secure_key": wone,
                    "address_resolved": "true",
                    "claims_shard0_block": "93623067",
                    "claims_shard1_block": "95882100",
                    "valuation_price_reference_shard0_block": "93448483",
                    "valuation_price_usd_per_one": "0.00074801",
                    "liquid_shard0_atto": str(reserve),
                    "liquid_shard1_atto": "0",
                    "liquid_total_atto": str(reserve),
                    "active_staked_or_delegated_atto": "0",
                    "pending_undelegation_atto": "0",
                    "unclaimed_staking_reward_atto": "0",
                    "pending_cross_shard_atto": "0",
                    "wallet_airdrop_atto": str(reserve),
                    "staked_to_vault_atto": "0",
                    "total_claim_atto": str(reserve),
                }
            )
            for name in base_components:
                native[name + "_one"] = self.module.fixed(
                    int(native[name + "_atto"])
                )
            native["wallet_airdrop_usd"] = "0.0"
            native["total_usd"] = "0.0"
            with native_path.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fields,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(native)

            holder_rows = [
                ("0x" + "01" * 20, 1000 * scale),
                ("0x" + "02" * 20, 1001 * scale),
            ]
            with holders_path.open("w", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(
                    ("address", "wone_balance_atto", "wone_balance")
                )
                for address, amount in holder_rows:
                    writer.writerow(
                        (address, str(amount), self.module.fixed(amount))
                    )
            holder_summary_path.write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "cutoff_block": 93623067,
                        "contract_address": wone,
                        "reserve_minus_supply_atto": "0",
                        "contract_total_supply_atto": str(reserve),
                        "contract_native_reserve_atto": str(reserve),
                        "holder_count": 2,
                        "total_holder_balance_atto": str(reserve),
                        "output_sha256": hashlib.sha256(
                            holders_path.read_bytes()
                        ).hexdigest(),
                    }
                )
            )
            with metadata_path.open("w", newline="") as handle:
                fields = (
                    "address",
                    "wone_balance_atto",
                    "combined_total_atto",
                    "claim_row_present",
                    "nonce_shard0",
                    "code_hash_shard0",
                    "code_status",
                )
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fields,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "address": holder_rows[0][0],
                        "wone_balance_atto": str(holder_rows[0][1]),
                        "combined_total_atto": str(holder_rows[0][1]),
                        "claim_row_present": "false",
                        "nonce_shard0": "0",
                        "code_hash_shard0": "0x" + "00" * 32,
                        "code_status": "code_less",
                    }
                )

            result = subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--native-claims",
                    str(native_path),
                    "--wone-holders",
                    str(holders_path),
                    "--wone-summary",
                    str(holder_summary_path),
                    "--new-holder-metadata",
                    str(metadata_path),
                    "--minimum-one",
                    "1000",
                    "--output",
                    str(output_path),
                    "--summary",
                    str(output_summary_path),
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("complete threshold set", result.stderr)
            self.assertFalse(output_path.exists())
            self.assertFalse(Path(str(output_path) + ".partial").exists())


if __name__ == "__main__":
    unittest.main()
