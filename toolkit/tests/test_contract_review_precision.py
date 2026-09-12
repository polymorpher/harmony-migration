import sys
import unittest
from pathlib import Path


CONTRACT_REVIEW = (
    Path(__file__).parents[1] / "scripts" / "contract-review"
)
sys.path.insert(0, str(CONTRACT_REVIEW))

import contract_review_lib as lib  # noqa: E402


class ContractReviewPrecisionTest(unittest.TestCase):
    def test_one_string_round_trip_is_exact(self):
        one = "123456789.123456789123456789"
        atto = 123456789123456789123456789
        self.assertEqual(lib.one_str_to_atto(one), atto)
        self.assertEqual(lib.atto_to_one_str(atto), one)

    def test_one_string_rejects_sub_atto_precision(self):
        with self.assertRaisesRegex(ValueError, "exceeds 18 decimals"):
            lib.one_str_to_atto("1.0000000000000000001")


if __name__ == "__main__":
    unittest.main()
