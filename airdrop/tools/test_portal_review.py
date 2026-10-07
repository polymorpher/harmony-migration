#!/usr/bin/env python3
"""Payment lists from the claim portal must be approved and still to send; other lists are unaffected.

    python3 tools/test_portal_review.py
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import vault_batch  # noqa: E402

ONE = 10**18
ALICE = "0x" + "aa" * 20
BOB = "0x" + "bb" * 20
VAL1 = "0x" + "11" * 20

# The columns these tools look at, in the claim portal's export order.
PORTAL_WALLETS = ["id", "address", "signer", "wallet_allocation_atto", "wallet_allocation_one", "decision",
                  "wallet_destination_status", "wallet_status", "vault_status", "signature", "message"]
PORTAL_VAULTS = ["address", "validator_address", "staked_atto", "held_one", "expected_shares_atto",
                 "expected_shares_one", "governor_status", "destination_status", "decision", "sent_status",
                 "blocked_reason", "sent_run"]


def write(path: Path, columns: list[str], rows: list[dict]) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)
    return path


def wallet_row(address=ALICE, amount=1000, decision="approved", status="pending"):
    return {"id": "1", "address": address, "signer": address, "wallet_allocation_atto": str(amount * ONE),
            "decision": decision, "wallet_destination_status": "ready", "wallet_status": status,
            "message": "line one\nline two"}


def vault_row(address=ALICE, amount=300, decision="approved", status="pending", destination="ready"):
    return {"address": address, "validator_address": VAL1, "staked_atto": str(amount * ONE),
            "expected_shares_atto": str(amount * ONE), "destination_status": destination, "decision": decision,
            "sent_status": status}


DEPOSIT_ARGS = SimpleNamespace(validator_column=None, delegator_column=None, amount_column=None, amount_unit=None)


class WalletListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, path, **kwargs):
        return common.read_rows(path, "address", kwargs.pop("amount_column", "wallet_allocation_atto"),
                                review_portal_exports=True, **kwargs)

    def test_plain_lists_are_read_as_before(self):
        path = write(self.dir / "list.csv", ["address", "amount_one"], [{"address": ALICE, "amount_one": "5"}])
        rows, info = common.read_rows(path, review_portal_exports=True)
        self.assertEqual([(r.address.lower(), r.amount) for r in rows], [(ALICE, 5 * ONE)])
        self.assertNotIn("portal_review_columns", info["sources"][0])

    def test_filtered_export_is_accepted_and_recorded(self):
        path = write(self.dir / "export.csv", PORTAL_WALLETS, [wallet_row(), wallet_row(BOB, 2000)])
        rows, info = self.read(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(info["sources"][0]["portal_review_columns"], ["decision", "wallet_status"])

    def test_unapproved_or_rejected_rows_are_refused(self):
        for decision in ("none", "rejected", ""):
            path = write(self.dir / "export.csv", PORTAL_WALLETS, [wallet_row(), wallet_row(BOB, decision=decision)])
            with self.assertRaises(common.InputError) as caught:
                self.read(path)
            self.assertIn("only approved wallets may be paid", str(caught.exception))
            self.assertIn("--decision approved --wallet pending", str(caught.exception))
            self.assertIn("export.csv:3", str(caught.exception))

    def test_sent_and_blocked_wallet_parts_are_refused(self):
        for status in ("sent", "blocked", "none"):
            path = write(self.dir / "export.csv", PORTAL_WALLETS, [wallet_row(status=status)])
            with self.assertRaises(common.InputError) as caught:
                self.read(path)
            self.assertIn(f"wallet_status is '{status}', not 'pending'", str(caught.exception))

    def test_vault_shares_export_is_not_a_payment_list(self):
        path = write(self.dir / "vaults.csv", PORTAL_VAULTS, [vault_row()])
        with self.assertRaises(common.InputError) as caught:
            self.read(path, amount_column="expected_shares_atto")
        self.assertIn("vault-shares export", str(caught.exception))

    def test_readers_that_do_not_pay_are_unchanged(self):
        path = write(self.dir / "export.csv", PORTAL_WALLETS, [wallet_row(decision="none", status="sent")])
        rows, _ = common.read_rows(path, "address", "wallet_allocation_atto")
        self.assertEqual(len(rows), 1)


class DepositListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pipeline_list_is_read_as_before(self):
        path = write(self.dir / "initial.csv", ["validator_address", "beneficiary_address", "destination_status",
                                                 "amount_atto"],
                     [{"validator_address": VAL1, "beneficiary_address": ALICE, "destination_status": "ready",
                       "amount_atto": str(ONE)}])
        deposits, info = vault_batch.read_deposits([path], DEPOSIT_ARGS, review_portal_exports=True)
        self.assertEqual(len(deposits), 1)
        self.assertNotIn("portal_review_columns", info["sources"][0])

    def test_filtered_export_is_accepted(self):
        path = write(self.dir / "vaults.csv", PORTAL_VAULTS, [vault_row(), vault_row(BOB, 0, "none", "none", "none")])
        deposits, info = vault_batch.read_deposits([path], DEPOSIT_ARGS, review_portal_exports=True)
        self.assertEqual([(d.delegator.lower(), d.amount) for d in deposits], [(ALICE, 300 * ONE)])
        self.assertEqual(info["sources"][0]["zero_rows_skipped"], 1)
        self.assertEqual(info["sources"][0]["portal_review_columns"], ["decision", "sent_status"])

    def test_unapproved_sent_and_blocked_positions_are_refused(self):
        cases = [
            (vault_row(decision="none"), "only approved wallets may be paid"),
            (vault_row(status="sent"), "sent_status is 'sent', not 'pending'"),
            (vault_row(status="blocked"), "sent_status is 'blocked', not 'pending'"),
            (vault_row(status="blocked", destination="hold"), "destination_status is 'hold', not ready"),
        ]
        for row, message in cases:
            path = write(self.dir / "vaults.csv", PORTAL_VAULTS, [row])
            with self.assertRaises(common.InputError) as caught:
                vault_batch.read_deposits([path], DEPOSIT_ARGS, review_portal_exports=True)
            self.assertIn(message, str(caught.exception))

    def test_compare_lists_are_not_reviewed(self):
        path = write(self.dir / "vaults.csv", PORTAL_VAULTS, [vault_row(decision="none", status="sent")])
        deposits, _ = vault_batch.read_deposits([path], DEPOSIT_ARGS)
        self.assertEqual(len(deposits), 1)

    def test_zero_rows_are_skipped_before_any_check(self):
        path = write(self.dir / "initial.csv", ["validator_address", "beneficiary_address", "destination_status",
                                                 "amount_atto"],
                     [{"validator_address": VAL1, "beneficiary_address": ALICE, "destination_status": "hold",
                       "amount_atto": "0"},
                      {"validator_address": VAL1, "beneficiary_address": BOB, "destination_status": "ready",
                       "amount_atto": str(ONE)}])
        deposits, info = vault_batch.read_deposits([path], DEPOSIT_ARGS, review_portal_exports=True)
        self.assertEqual((len(deposits), info["sources"][0]["zero_rows_skipped"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
