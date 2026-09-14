import importlib.util
import sys
import unittest
from pathlib import Path


CONTRACT_REVIEW = (
    Path(__file__).parents[1] / "scripts" / "contract-review"
)
sys.path.insert(0, str(CONTRACT_REVIEW))

import contract_review_lib as lib  # noqa: E402


CLASSIFIER = CONTRACT_REVIEW / "classify-contracts.py"
FETCHER = CONTRACT_REVIEW / "fetch-contract-facts.py"


class ContractReviewPrecisionTest(unittest.TestCase):
    @staticmethod
    def load_module(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_one_string_round_trip_is_exact(self):
        one = "123456789.123456789123456789"
        atto = 123456789123456789123456789
        self.assertEqual(lib.one_str_to_atto(one), atto)
        self.assertEqual(lib.atto_to_one_str(atto), one)

    def test_one_string_rejects_sub_atto_precision(self):
        with self.assertRaisesRegex(ValueError, "exceeds 18 decimals"):
            lib.one_str_to_atto("1.0000000000000000001")

    def test_address_secure_key_match_is_required(self):
        address = "0x0000000000000000000000000000000000000001"
        key = "0x" + lib.keccak256(bytes.fromhex(address[2:])).hex()
        self.assertEqual(
            lib.require_address_secure_key(address, key),
            address,
        )
        with self.assertRaisesRegex(
            ValueError, "address does not match secure key"
        ):
            lib.require_address_secure_key(address, "0x" + "00" * 32)

    def test_validator_wrapper_address_requires_canonical_nested_rlp(self):
        raw_address = bytes.fromhex("11" * 20)
        address_item = bytes([0x94]) + raw_address
        validator_payload = address_item + bytes([0x80])
        validator = bytes([0xC0 + len(validator_payload)]) + validator_payload
        wrapper_payload = validator + bytes([0xC0])
        wrapper = bytes([0xC0 + len(wrapper_payload)]) + wrapper_payload
        self.assertEqual(
            lib.rlp_validator_wrapper_address("0x" + wrapper.hex()),
            "0x" + raw_address.hex(),
        )
        malformed = bytes([wrapper[0] + 1]) + wrapper[1:]
        self.assertIsNone(
            lib.rlp_validator_wrapper_address("0x" + malformed.hex())
        )

    def test_validator_requires_rpc_and_rlp_evidence(self):
        classifier = self.load_module("classify_contracts", CLASSIFIER)
        self.assertTrue(
            classifier.is_verified_validator(
                {
                    "validator": {
                        "is_validator": True,
                        "rlp_wrapper_matches": True,
                    }
                }
            )
        )
        self.assertFalse(
            classifier.is_verified_validator(
                {
                    "validator": {
                        "is_validator": True,
                        "rlp_wrapper_matches": False,
                    }
                }
            )
        )

    def test_classification_requires_one_pinned_policy_state(self):
        classifier = self.load_module("classify_contracts", CLASSIFIER)
        identity = {
            "block": 10,
            "block_hash": "0xabc",
            "state_root": "0xdef",
        }
        facts = {
            "0x0000000000000000000000000000000000000001": {
                "basic": {
                    "basic_evidence_version": 2,
                    "policy_state_block": identity["block"],
                    "policy_state_block_hash": identity["block_hash"],
                    "policy_state_root": identity["state_root"],
                },
                "validator": {
                    "validator_evidence_version": 2,
                    "is_validator": False,
                    "policy_state_block": identity["block"],
                    "policy_state_block_hash": identity["block_hash"],
                    "policy_state_root": identity["state_root"],
                },
                "probes_block": identity["block"],
                "probe_evidence_version": 2,
                "probes_block_hash": identity["block_hash"],
                "probes_state_root": identity["state_root"],
                "creation": {"block": 1},
                "funding": {
                    "funding_evidence_version": 2,
                    "block": 2,
                    "policy_state_block": identity["block"],
                    "policy_state_block_hash": identity["block_hash"],
                    "policy_state_root": identity["state_root"],
                },
            }
        }
        self.assertEqual(
            classifier.policy_state_identity(
                facts,
                {"_policy_state": dict(identity, schema_version=2)},
                {"_policy_state": dict(identity, schema_version=2)},
            ),
            identity,
        )
        mismatched = dict(identity, block=11)
        with self.assertRaisesRegex(
            ValueError,
            "more than one cutoff block, hash, or state root",
        ):
            classifier.policy_state_identity(
                facts,
                {
                    "_policy_state": dict(
                        mismatched,
                        schema_version=2,
                    )
                },
                {"_policy_state": dict(identity, schema_version=2)},
            )

    def test_abi_probes_use_explicit_policy_block(self):
        fetcher = self.load_module("fetch_contract_facts", FETCHER)

        class Client:
            def __init__(self):
                self.calls = []

            def batch(self, calls):
                self.calls = calls
                return [("0x01", None) for _call in calls]

        client = Client()
        address = "0x0000000000000000000000000000000000000001"
        facts = {address: {"validator": {"is_validator": False}}}
        block = {"hash": "0xabc", "stateRoot": "0xdef"}
        fetcher.stage_probes(client, facts, [address], "0xa", block)
        self.assertTrue(
            all(call[1][-1] == "0xa" for call in client.calls)
        )
        self.assertEqual(facts[address]["probes_block"], 10)

    def test_validator_rpc_uses_explicit_policy_block(self):
        fetcher = self.load_module("fetch_contract_facts", FETCHER)

        class Client:
            def __init__(self):
                self.calls = []

            def batch(self, calls):
                self.calls.extend(calls)
                return [
                    (None, {"message": "not a validator"})
                    for _call in calls
                ]

        client = Client()
        address = "0x0000000000000000000000000000000000000001"
        facts = {address: {"basic": {"code_cutoff": "0x"}}}
        block = {"hash": "0xabc", "stateRoot": "0xdef"}
        fetcher.stage_validator(client, facts, [address], 10, block)
        self.assertEqual(
            client.calls[0],
            (
                "hmyv2_getValidatorInformationByBlockNumber",
                [address, 10],
            ),
        )
        self.assertEqual(
            facts[address]["validator"]["policy_state_block_hash"],
            "0xabc",
        )

    def test_post_cutoff_funding_transaction_is_ignored(self):
        fetcher = self.load_module("fetch_contract_facts", FETCHER)
        tx, info = fetcher.cutoff_direct_value_transfer(
            {
                "first_direct_value_received_tx": {"block": 11},
                "first_direct_value_received_info": {"reliable": True},
            },
            10,
        )
        self.assertIsNone(tx)
        self.assertTrue(info["post_cutoff_ignored"])


if __name__ == "__main__":
    unittest.main()
