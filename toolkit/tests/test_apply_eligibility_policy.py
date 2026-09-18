import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CONTRACT_REVIEW = (
    Path(__file__).parents[1] / "scripts" / "contract-review"
)
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "claims"
    / "apply-eligibility-policy.py"
)
VERIFY_SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "claims"
    / "verify-eligibility-policy.py"
)
EMPTY_CODE_HASH = (
    "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
)


class ApplyEligibilityPolicyTest(unittest.TestCase):
    def test_blank_code_metadata_is_unresolved(self):
        spec = importlib.util.spec_from_file_location(
            "apply_eligibility_policy", SCRIPT
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIsNone(
            module.code_bearing(
                {"code_hash_shard0": "", "code_hash_shard1": ""}
            )
        )
        self.assertIsNone(
            module.code_bearing(
                {
                    "code_hash_shard0": "",
                    "code_hash_shard1": EMPTY_CODE_HASH,
                }
            )
        )

    def test_splits_automatic_validator_contract_and_excluded_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "claims.csv"
            automatic = root / "automatic.csv"
            contracts = root / "contracts.csv"
            excluded = root / "excluded.csv"
            exclusions = root / "exclusions.csv"
            validators = root / "validators.csv"
            summary = root / "summary.json"
            verification = root / "verification.json"
            fields = (
                "secure_key",
                "address",
                "liquid_shard0_atto",
                "liquid_shard1_atto",
                "liquid_total_atto",
                "active_staked_or_delegated_atto",
                "pending_undelegation_atto",
                "unclaimed_staking_reward_atto",
                "pending_cross_shard_atto",
                "wallet_airdrop_atto",
                "staked_to_vault_atto",
                "total_claim_atto",
                "code_hash_shard0",
                "code_hash_shard1",
            )

            def row(
                address,
                value,
                code=EMPTY_CODE_HASH,
                wallet=None,
            ):
                wallet = value if wallet is None else wallet
                vault = value - wallet
                return {
                    "secure_key": (
                        "0x"
                        + lib.keccak256(bytes.fromhex(address[2:])).hex()
                    ),
                    "address": address,
                    "liquid_shard0_atto": str(wallet),
                    "liquid_shard1_atto": "0",
                    "liquid_total_atto": str(wallet),
                    "active_staked_or_delegated_atto": str(vault),
                    "pending_undelegation_atto": "0",
                    "unclaimed_staking_reward_atto": "0",
                    "pending_cross_shard_atto": "0",
                    "wallet_airdrop_atto": str(wallet),
                    "staked_to_vault_atto": str(vault),
                    "total_claim_atto": str(value),
                    "code_hash_shard0": code,
                    "code_hash_shard1": "",
                }

            validator = "0x0000000000000000000000000000000000000003"
            blocked = "0x000000000000000000000000000000000000dead"
            with source.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=fields, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(
                    sorted(
                        (
                            row(
                                "0x0000000000000000000000000000000000000001",
                                999 * 10**18,
                            ),
                            row(
                                "0x0000000000000000000000000000000000000002",
                                1000 * 10**18,
                                wallet=100 * 10**18,
                            ),
                            row(
                                validator,
                                1001 * 10**18,
                                "0x01",
                            ),
                            row(
                                "0x0000000000000000000000000000000000000004",
                                1002 * 10**18,
                                "0x02",
                            ),
                            row(blocked, 1003 * 10**18, "0x03"),
                        ),
                        key=lambda item: item["secure_key"],
                    )
                )
            with validators.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=("address",), lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(
                    ({"address": validator}, {"address": blocked})
                )
            with exclusions.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("address", "reason"),
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(
                    {"address": blocked, "reason": "exchange reroute"}
                )
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(source),
                    "--automatic-output",
                    str(automatic),
                    "--contract-review-output",
                    str(contracts),
                    "--excluded-address-output",
                    str(excluded),
                    "--automatic-code-addresses",
                    str(validators),
                    "--summary",
                    str(summary),
                    "--minimum-one",
                    "1000",
                    "--comparison",
                    "ge",
                    "--exclude-addresses-file",
                    str(exclusions),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            metadata = json.loads(summary.read_text())
            self.assertEqual(metadata["categories"]["automatic"]["rows"], 2)
            self.assertEqual(metadata["categories"]["contract_review"]["rows"], 1)
            self.assertEqual(metadata["categories"]["excluded_address"]["rows"], 1)
            self.assertEqual(metadata["exact_threshold_rows"], 1)
            self.assertEqual(
                metadata["automatic_code_addresses_found"], [validator]
            )
            self.assertEqual(
                metadata[
                    "automatic_code_addresses_suppressed_by_exclusion"
                ],
                [blocked],
            )
            self.assertEqual(
                metadata["categories"]["automatic"]["components_atto"][
                    "wallet_airdrop"
                ],
                str(1101 * 10**18),
            )
            self.assertEqual(
                metadata["categories"]["automatic"]["components_atto"][
                    "staked_to_vault"
                ],
                str(900 * 10**18),
            )
            subprocess.run(
                (
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "--input",
                    str(source),
                    "--automatic",
                    str(automatic),
                    "--contract-review",
                    str(contracts),
                    "--excluded-address",
                    str(excluded),
                    "--automatic-code-addresses",
                    str(validators),
                    "--exclude-addresses-file",
                    str(exclusions),
                    "--policy-summary",
                    str(summary),
                    "--output",
                    str(verification),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(verification.read_text())["status"], "passed"
            )


if __name__ == "__main__":
    unittest.main()
