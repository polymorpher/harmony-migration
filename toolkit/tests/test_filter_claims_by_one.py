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
    / "filter-claims-by-one.py"
)


class FilterClaimsByOneTest(unittest.TestCase):
    def run_filter(self, comparison):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.csv"
            output = root / "output.csv"
            summary = root / "summary.json"
            with source.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "secure_key",
                        "wallet_airdrop_atto",
                        "staked_to_vault_atto",
                        "total_claim_atto",
                    ),
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(
                    (
                        {
                            "secure_key": "0x01",
                            "wallet_airdrop_atto": str(99 * 10**18),
                            "staked_to_vault_atto": str(900 * 10**18),
                            "total_claim_atto": str(999 * 10**18),
                        },
                        {
                            "secure_key": "0x02",
                            "wallet_airdrop_atto": str(100 * 10**18),
                            "staked_to_vault_atto": str(900 * 10**18),
                            "total_claim_atto": str(1000 * 10**18),
                        },
                        {
                            "secure_key": "0x03",
                            "wallet_airdrop_atto": str(101 * 10**18),
                            "staked_to_vault_atto": str(900 * 10**18),
                            "total_claim_atto": str(1001 * 10**18),
                        },
                    )
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
                    "--minimum-one",
                    "1000",
                    "--comparison",
                    comparison,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            with output.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            metadata = json.loads(summary.read_text())
            return rows, metadata

    def test_greater_than_or_equal_includes_exact_threshold(self):
        rows, metadata = self.run_filter("ge")
        self.assertEqual([row["secure_key"] for row in rows], ["0x02", "0x03"])
        self.assertEqual(metadata["exact_threshold_rows"], 1)
        self.assertEqual(
            metadata["wallet_airdrop_atto"], str(201 * 10**18)
        )
        self.assertEqual(
            metadata["staked_to_vault_atto"], str(1800 * 10**18)
        )

    def test_strict_greater_than_excludes_exact_threshold(self):
        rows, metadata = self.run_filter("gt")
        self.assertEqual([row["secure_key"] for row in rows], ["0x03"])
        self.assertEqual(metadata["exact_threshold_rows"], 1)

    def test_wone_qualification_total_controls_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.csv"
            output = root / "output.csv"
            summary = root / "summary.json"
            scale = 10**18
            fields = (
                "secure_key",
                "wallet_airdrop_atto",
                "staked_to_vault_atto",
                "native_total_claim_atto",
                "wone_balance_atto",
                "wone_airdrop_atto",
                "qualification_total_atto",
                "total_claim_atto",
            )
            with source.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fields,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "secure_key": "0x01",
                        "wallet_airdrop_atto": str(900 * scale),
                        "staked_to_vault_atto": str(100 * scale),
                        "native_total_claim_atto": str(600 * scale),
                        "wone_balance_atto": str(400 * scale),
                        "wone_airdrop_atto": str(400 * scale),
                        "qualification_total_atto": str(1000 * scale),
                        "total_claim_atto": str(1000 * scale),
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
                    "--minimum-one",
                    "1000",
                    "--comparison",
                    "ge",
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            with output.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            metadata = json.loads(summary.read_text())
            self.assertEqual(len(rows), 1)
            self.assertEqual(metadata["wone_airdrop_atto"], str(400 * scale))
            self.assertEqual(
                metadata["threshold_field"],
                "qualification_total_atto",
            )


if __name__ == "__main__":
    unittest.main()
