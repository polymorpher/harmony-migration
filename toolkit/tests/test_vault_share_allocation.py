import csv
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
    / "build-vault-share-allocation.py"
)
VERIFY_SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "claims"
    / "verify-vault-delegations.py"
)


def address(index):
    return f"0x{index:040x}"


def secure_key(index):
    return "0x" + lib.keccak256(bytes.fromhex(address(index)[2:])).hex()


class VaultShareAllocationTest(unittest.TestCase):
    def test_splits_priority_and_deferred_vault_shares(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claims = root / "claims.csv"
            automatic = root / "automatic.csv"
            contracts = root / "contracts.csv"
            excluded = root / "excluded.csv"
            delegations = root / "delegations.csv"
            vaults = root / "vaults.csv"
            priority = root / "priority.csv"
            deferred = root / "deferred.csv"
            automatic_wallet = root / "automatic-wallet.csv"
            contract_wallet = root / "contract-wallet.csv"
            excluded_wallet = root / "excluded-wallet.csv"
            summary = root / "summary.json"
            delegation_verification = root / "delegation-verification.json"

            claim_fields = (
                "secure_key",
                "address",
                "wone_airdrop_atto",
                "wallet_airdrop_atto",
                "staked_to_vault_atto",
                "qualification_total_atto",
                "total_claim_atto",
            )

            def claim(index, wallet, vault, wone=0):
                return {
                    "secure_key": secure_key(index),
                    "address": address(index),
                    "wone_airdrop_atto": str(wone * 10**18),
                    "wallet_airdrop_atto": str(wallet * 10**18),
                    "staked_to_vault_atto": str(vault * 10**18),
                    "qualification_total_atto": str(
                        (wallet + vault) * 10**18
                    ),
                    "total_claim_atto": str(
                        (wallet + vault) * 10**18
                    ),
                }

            claims_by_index = {
                1: claim(1, 100, 1000, wone=10),
                2: claim(2, 200, 1000),
                3: claim(3, 300, 1000),
                4: claim(4, 100, 800),
            }
            claim_rows = tuple(
                sorted(
                    claims_by_index.values(),
                    key=lambda row: row["secure_key"],
                )
            )
            with claims.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=claim_fields, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(claim_rows)
            for path, row in (
                (automatic, claims_by_index[1]),
                (contracts, claims_by_index[2]),
                (excluded, claims_by_index[3]),
            ):
                with path.open("w", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=claim_fields, lineterminator="\n"
                    )
                    writer.writeheader()
                    writer.writerow(row)

            detail_fields = (
                "validator_address",
                "validator_secure_key",
                "delegator_address",
                "delegator_secure_key",
                "staked_to_vault_atto",
                "is_self_delegation",
            )
            detail_rows = []
            for validator, delegator, amount in (
                (10, 1, 600),
                (11, 1, 400),
                (10, 2, 1000),
                (11, 3, 1000),
                (10, 4, 800),
            ):
                detail_rows.append(
                    {
                        "validator_address": address(validator),
                        "validator_secure_key": secure_key(validator),
                        "delegator_address": address(delegator),
                        "delegator_secure_key": secure_key(delegator),
                        "staked_to_vault_atto": str(amount * 10**18),
                        "is_self_delegation": "false",
                    }
                )
            with delegations.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=detail_fields, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(detail_rows)

            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "--all-claims",
                    str(claims),
                    "--delegations",
                    str(delegations),
                    "--automatic-claims",
                    str(automatic),
                    "--contract-review-claims",
                    str(contracts),
                    "--excluded-claims",
                    str(excluded),
                    "--vault-deposits-output",
                    str(vaults),
                    "--priority-shares-output",
                    str(priority),
                    "--deferred-shares-output",
                    str(deferred),
                    "--automatic-wallet-output",
                    str(automatic_wallet),
                    "--contract-wallet-output",
                    str(contract_wallet),
                    "--excluded-wallet-output",
                    str(excluded_wallet),
                    "--summary",
                    str(summary),
                    "--minimum-one",
                    "1000",
                ),
                check=True,
                capture_output=True,
                text=True,
            )

            result = json.loads(summary.read_text())
            self.assertEqual(
                result["vault_assets_atto"], str(3800 * 10**18)
            )
            self.assertEqual(
                result["priority_staked_to_vault_atto"],
                str(3000 * 10**18),
            )
            self.assertEqual(
                result["deferred_staked_to_vault_atto"],
                str(800 * 10**18),
            )
            with priority.open(newline="") as handle:
                priority_rows = list(csv.DictReader(handle))
            automatic_rows = [
                row
                for row in priority_rows
                if row["allocation_category"] == "automatic"
            ]
            self.assertEqual(len(automatic_rows), 2)
            self.assertTrue(
                all(row["share_destination_address"] for row in automatic_rows)
            )
            self.assertTrue(
                all(
                    not row["share_destination_address"]
                    for row in priority_rows
                    if row["allocation_category"] != "automatic"
                )
            )
            with automatic_wallet.open(newline="") as handle:
                direct = list(csv.DictReader(handle))
            self.assertEqual(len(direct), 1)
            self.assertEqual(
                direct[0]["wallet_airdrop_atto"], str(100 * 10**18)
            )
            self.assertEqual(
                direct[0]["native_wallet_airdrop_atto"],
                str(90 * 10**18),
            )
            self.assertEqual(
                direct[0]["wone_airdrop_atto"],
                str(10 * 10**18),
            )
            self.assertEqual(
                direct[0]["destination_address"],
                claim_rows[0]["address"],
            )
            subprocess.run(
                (
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "--database",
                    str(delegations),
                    "--rpc",
                    str(delegations),
                    "--output",
                    str(delegation_verification),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(
                json.loads(delegation_verification.read_text())[
                    "files_identical"
                ]
            )


if __name__ == "__main__":
    unittest.main()
