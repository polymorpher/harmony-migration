import csv
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
    / "routing"
    / "build-wone-routes.py"
)
FIELDS = (
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


class WoneRoutesTest(unittest.TestCase):
    def test_replaces_legacy_route_with_exact_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "routes.csv"
            wone = root / "wone.json"
            output = root / "output.csv"
            summary = root / "summary.json"
            with source.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=FIELDS,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(
                    (
                        {
                            "route_id": "wone-reserve-custody",
                            "priority": "400",
                            "source_address": (
                                "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a"
                            ),
                            "destination_id": "old",
                        },
                        {
                            "route_id": "layerzero",
                            "priority": "500",
                            "source_address": f"0x{2:040x}",
                            "destination_id": "hold",
                        },
                    )
                )
            wone.write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "wone_reserve_atto": "100",
                        "wone_redistributed_to_priority_atto": "91",
                        "wone_retained_not_issued_atto": "9",
                    }
                )
            )
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--wone-summary",
                    str(wone),
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--summary",
                    str(summary),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            with output.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            by_id = {row["route_id"]: row for row in rows}
            self.assertNotIn("wone-reserve-custody", by_id)
            self.assertEqual(
                by_id["wone-priority-holder-redistribution"]["amount_atto"],
                "91",
            )
            self.assertEqual(
                by_id["wone-priority-holder-redistribution"][
                    "destination_id"
                ],
                "wone-holder-redistribution",
            )
            self.assertEqual(
                by_id["wone-reserve-remainder-not-issued"]["amount_atto"],
                "9",
            )
            self.assertEqual(
                by_id["wone-reserve-remainder-not-issued"][
                    "destination_id"
                ],
                "not-issuing",
            )

    def test_rejects_in_place_route_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "routes.csv"
            wone = root / "wone.json"
            summary = root / "summary.json"
            with source.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=FIELDS,
                    lineterminator="\n",
                )
                writer.writeheader()
            original = source.read_bytes()
            wone.write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "wone_reserve_atto": "100",
                        "wone_redistributed_to_priority_atto": "91",
                        "wone_retained_not_issued_atto": "9",
                    }
                )
            )
            result = subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--wone-summary",
                    str(wone),
                    "--input",
                    str(source),
                    "--output",
                    str(source),
                    "--summary",
                    str(summary),
                    "--replace",
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--input and --output must differ", result.stderr)
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
