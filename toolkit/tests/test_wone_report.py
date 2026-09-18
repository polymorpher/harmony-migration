import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "claims"
    / "build-wone-report.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("build_wone_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WoneReportTest(unittest.TestCase):
    def test_policy_address_sets_distinguish_newcomers_from_reclassification(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def summary(prefix, categories):
                result = {"categories": {}}
                for category, addresses in categories.items():
                    path = root / f"{prefix}-{category}.csv"
                    with path.open("w", newline="") as output:
                        writer = csv.DictWriter(
                            output,
                            fieldnames=("address",),
                            lineterminator="\n",
                        )
                        writer.writeheader()
                        writer.writerows(
                            {"address": address} for address in addresses
                        )
                    result["categories"][category] = {
                        "output": str(path),
                        "rows": len(addresses),
                    }
                return result

            prior = summary(
                "prior",
                {
                    "automatic": ["0x" + "01" * 20, "0x" + "02" * 20],
                    "contract_review": [],
                    "excluded_address": [],
                },
            )
            current = summary(
                "current",
                {
                    "automatic": ["0x" + "01" * 20, "0x" + "03" * 20],
                    "contract_review": ["0x" + "04" * 20],
                    "excluded_address": ["0x" + "02" * 20],
                },
            )
            prior_categories, prior_union = module.load_policy_addresses(
                prior, "prior"
            )
            current_categories, current_union = module.load_policy_addresses(
                current, "current"
            )
            newcomers = current_union - prior_union
            self.assertEqual(
                newcomers,
                {"0x" + "03" * 20, "0x" + "04" * 20},
            )
            self.assertEqual(
                len(newcomers & current_categories["excluded_address"]), 0
            )
            self.assertIn(
                "0x" + "02" * 20,
                prior_categories["automatic"]
                & current_categories["excluded_address"],
            )


if __name__ == "__main__":
    unittest.main()
