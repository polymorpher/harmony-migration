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
    / "build-non-issuance-routes.py"
)


class NonIssuanceRoutesTest(unittest.TestCase):
    def test_preserves_exact_previously_routed_amounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.csv"
            fields = (
                "address_hex",
                "category",
                "not_issued_atto",
                "cutoff_claim_atto",
                "remaining_original_address_allocation_atto",
            )
            with inventory.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output, fieldnames=fields, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(
                    (
                        {
                            "address_hex": f"0x{1:040x}",
                            "category": (
                                "blacklisted_extra_mint_recipient"
                            ),
                            "not_issued_atto": "40",
                            "cutoff_claim_atto": "100",
                            "remaining_original_address_allocation_atto": "60",
                        },
                        {
                            "address_hex": f"0x{2:040x}",
                            "category": "burn_or_inaccessible",
                            "not_issued_atto": "75",
                            "cutoff_claim_atto": "75",
                            "remaining_original_address_allocation_atto": "0",
                        },
                        {
                            "address_hex": f"0x{3:040x}",
                            "category": (
                                "reported_wallet_theft_perpetrator"
                            ),
                            "not_issued_atto": "0",
                            "cutoff_claim_atto": "0",
                            "remaining_original_address_allocation_atto": "0",
                        },
                        {
                            "address_hex": f"0x{4:040x}",
                            "category": "report_linked_theft_recipient",
                            "not_issued_atto": "5",
                            "cutoff_claim_atto": "5",
                            "remaining_original_address_allocation_atto": "0",
                        },
                    )
                )
            routes = root / "not-issuing.csv"
            summary = root / "summary.json"
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--inventory",
                    str(inventory),
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
                rows = list(csv.DictReader(source))
            self.assertEqual(
                [row["amount_atto"] for row in rows], ["40", "75", "5"]
            )
            self.assertTrue(
                all(
                    row["destination_id"] == "not-issuing"
                    and not row["destination_address"]
                    for row in rows
                )
            )
            result = json.loads(summary.read_text())
            self.assertEqual(result["not_issued_atto"], "120")
            self.assertEqual(result["routes"], 3)
            self.assertEqual(result["inventory_rows"], 4)

    def test_merges_historical_retained_caps_without_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.csv"
            historical = root / "historical.csv"
            with existing.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=("address_hex", "category", "not_issued_atto"),
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "address_hex": f"0x{1:040x}",
                        "category": "burn_or_inaccessible",
                        "not_issued_atto": "7",
                    }
                )
            with historical.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=(
                        "address_hex",
                        "incident",
                        "retained_cap_atto",
                        "migration_treatment",
                    ),
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "address_hex": f"0x{2:040x}",
                        "incident": "april-2026",
                        "retained_cap_atto": "11",
                        "migration_treatment": "not_issued",
                    }
                )
            routes = root / "routes.csv"
            summary = root / "summary.json"
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--inventory",
                    str(existing),
                    "--inventory",
                    str(historical),
                    "--output",
                    str(routes),
                    "--summary",
                    str(summary),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(summary.read_text())
            self.assertEqual(result["not_issued_atto"], "18")
            self.assertEqual(
                result["categories"]["historical_incident_retained_cap"][
                    "not_issued_atto"
                ],
                "11",
            )


if __name__ == "__main__":
    unittest.main()
