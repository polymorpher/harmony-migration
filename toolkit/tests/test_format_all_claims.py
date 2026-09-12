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
    / "claims"
    / "format-all-claims.py"
)


class FormatAllClaimsTest(unittest.TestCase):
    def test_formats_and_sums_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ledger.csv"
            output = root / "claims.csv"
            summary = root / "summary.json"
            fields = (
                "secure_key",
                "address",
                "liquid_shard0_atto",
                "liquid_shard1_atto",
                "liquid_total_atto",
                "active_delegation_atto",
                "pending_undelegation_atto",
                "unclaimed_reward_atto",
                "pending_cross_shard_atto",
                "total_claim_atto",
                "nonce_shard0",
                "nonce_shard1",
                "code_hash_shard0",
                "code_hash_shard1",
            )
            with source.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=fields, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "secure_key": "0x01",
                        "address": "0x0000000000000000000000000000000000000001",
                        "liquid_shard0_atto": "1000000000000000000",
                        "liquid_shard1_atto": "2000000000000000000",
                        "liquid_total_atto": "3000000000000000000",
                        "active_delegation_atto": "4000000000000000000",
                        "pending_undelegation_atto": "5000000000000000000",
                        "unclaimed_reward_atto": "6000000000000000000",
                        "pending_cross_shard_atto": "7000000000000000000",
                        "total_claim_atto": "25000000000000000000",
                        "nonce_shard0": "1",
                        "nonce_shard1": "",
                        "code_hash_shard0": "0x02",
                        "code_hash_shard1": "",
                    }
                )
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--summary",
                    str(summary),
                    "--shard0-block",
                    "10",
                    "--shard1-block",
                    "20",
                    "--price-reference-shard0-block",
                    "9",
                    "--price-usd-per-one",
                    "0.5",
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            with output.open(newline="") as handle:
                row = next(csv.DictReader(handle))
            metadata = json.loads(summary.read_text())
            self.assertEqual(
                row["total_claim_one"],
                "25.000000000000000000",
            )
            self.assertEqual(
                row["wallet_airdrop_one"],
                "21.000000000000000000",
            )
            self.assertEqual(
                row["staked_to_vault_one"],
                "4.000000000000000000",
            )
            self.assertEqual(
                row["total_usd"],
                "12.5000000000000000000",
            )
            self.assertEqual(
                row["wallet_airdrop_usd"],
                "10.5000000000000000000",
            )
            self.assertEqual(row["claims_shard0_block"], "10")
            self.assertEqual(metadata["unresolved_addresses"], 0)


if __name__ == "__main__":
    unittest.main()
