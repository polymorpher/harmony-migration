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
    / "build-contract-treasury-routes.py"
)


class ContractTreasuryRoutesTest(unittest.TestCase):
    def test_excludes_validators_and_multisigs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "contracts.csv"
            fields = (
                "address",
                "primary_category",
                "subcategory",
                "total_claim_one",
            )
            rows = [
                (1, "validator-account", "validator", "1000"),
                (2, "multisig-wallet", "2-of-3 Safe", "2000"),
                (3, "onewallet", "1wallet v16", "3000"),
                (4, "known-app", "bridge", "4000"),
            ]
            with source.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output, fieldnames=fields, lineterminator="\n"
                )
                writer.writeheader()
                for index, category, subcategory, amount in rows:
                    writer.writerow(
                        {
                            "address": f"0x{index:040x}",
                            "primary_category": category,
                            "subcategory": subcategory,
                            "total_claim_one": amount,
                        }
                    )
            routes = root / "routes.csv"
            summary = root / "summary.json"
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--contracts",
                    str(source),
                    "--output",
                    str(routes),
                    "--summary",
                    str(summary),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            with routes.open(newline="") as handle:
                output_rows = list(csv.DictReader(handle))
            self.assertEqual(len(output_rows), 2)
            self.assertEqual(
                {row["source_address"] for row in output_rows},
                {f"0x{3:040x}", f"0x{4:040x}"},
            )
            self.assertTrue(
                all(row["destination_id"] == "treasury" for row in output_rows)
            )
            self.assertTrue(
                all(row["amount_atto"] == "ALL" for row in output_rows)
            )
            result = json.loads(summary.read_text())
            self.assertEqual(result["validator_rows_skipped"], 1)
            self.assertEqual(result["multisig_rows_skipped"], 1)
            self.assertEqual(result["non_multisig_contract_routes"], 2)


if __name__ == "__main__":
    unittest.main()
