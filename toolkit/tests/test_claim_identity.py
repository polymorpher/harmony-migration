import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
CONTRACT_REVIEW = SCRIPTS / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402

LEDGER = SCRIPTS / "claims" / "actual-supply-ledger.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaimIdentityTest(unittest.TestCase):
    def test_ledger_input_rejects_address_key_mismatch(self):
        ledger = load_module("actual_supply_ledger", LEDGER)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            with path.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=("secure_key", "address"),
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "secure_key": "0x" + "00" * 32,
                        "address": (
                            "0x0000000000000000000000000000000000000001"
                        ),
                    }
                )
            with self.assertRaisesRegex(
                ValueError, "address does not match secure key"
            ):
                list(ledger.rows(path, "fixture"))

    def test_identity_helper_accepts_verified_preimage(self):
        address = "0x0000000000000000000000000000000000000001"
        key = "0x" + lib.keccak256(bytes.fromhex(address[2:])).hex()
        self.assertEqual(
            lib.require_address_secure_key(address, key),
            address,
        )


if __name__ == "__main__":
    unittest.main()
