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

    def write(self, path, fields, rows):
        with path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def run_builder(self, root, *inventories):
        command = [sys.executable, str(SCRIPT)]
        for inventory in inventories:
            command += ["--inventory", str(inventory)]
        command += ["--output", str(root / "routes.csv"), "--summary", str(root / "summary.json")]
        return subprocess.run(command, capture_output=True, text=True)

    def test_revert_leak_stacks_only_on_retained_caps(self):
        retained_fields = ("address_hex", "incident", "retained_cap_atto", "migration_treatment")
        leak_fields = ("address_hex", "incident", "unbacked_credit_atto", "retained_cap_atto", "migration_treatment")
        shared = f"0x{5:040x}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retained = root / "retained.csv"
            leak = root / "leak.csv"
            existing = root / "existing.csv"
            self.write(retained, retained_fields, [
                {"address_hex": shared, "incident": "may-2025", "retained_cap_atto": "3",
                 "migration_treatment": "not_issued"},
            ])
            self.write(leak, leak_fields, [
                {"address_hex": shared, "incident": "may-2025", "unbacked_credit_atto": "900",
                 "retained_cap_atto": "4", "migration_treatment": "not_issued"},
                {"address_hex": f"0x{6:040x}", "incident": "june-july-2026", "unbacked_credit_atto": "50",
                 "retained_cap_atto": "50", "migration_treatment": "not_issued"},
            ])
            result = self.run_builder(root, retained, leak)
            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "routes.csv").open(newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(
                sorted((row["source_address"], row["reason"], row["amount_atto"]) for row in rows),
                sorted([
                    (shared, "not_issuing_historical_incident_retained_cap", "3"),
                    (shared, "not_issuing_revert_leak_credit_recipient", "4"),
                    (f"0x{6:040x}", "not_issuing_revert_leak_credit_recipient", "50"),
                ]),
            )
            summary = json.loads((root / "summary.json").read_text())
            self.assertEqual(summary["categories"]["revert_leak_credit_recipient"]["not_issued_atto"], "54")

            self.write(existing, ("address_hex", "category", "not_issued_atto"), [
                {"address_hex": f"0x{6:040x}", "category": "burn_or_inaccessible", "not_issued_atto": "1"},
            ])
            (root / "routes.csv").unlink()
            (root / "summary.json").unlink()
            result = self.run_builder(root, existing, leak)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate inventory address", result.stderr)

            result = self.run_builder(root, leak, leak)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate inventory address", result.stderr)


if __name__ == "__main__":
    unittest.main()
