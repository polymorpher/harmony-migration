#!/usr/bin/env python3
"""safe-batch crosscheck: a queued payment against its sheet, the Safe's history and the exchange wallets.

    python3 tools/test_crosscheck.py
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import safe_batch  # noqa: E402

ONE = 10**18
SAFE = common.to_checksum("0x" + "5a" * 20)
TOKEN = common.to_checksum("0x" + "70" * 20)
ALICE, BOB, CAROL, DAVE = ("0x" + c * 20 for c in ("aa", "bb", "cc", "dd"))
HOT = "0x" + "ee" * 20
EXCHANGE_COLUMNS = ["exchange_id", "address_hex", "address_one", "configured_destination",
                    "configured_staking_destination"]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)
    return path


def record(nonce: int, transfers: list[tuple[str, int]], executed: bool) -> dict:
    """A Safe Transaction Service record of a payment, with its real Safe transaction hash."""
    rows = [common.Row(common.to_checksum(a), amount, "") for a, amount in transfers]
    tx = safe_batch.transfers_tx(TOKEN, safe_batch.MULTISEND_CALL_ONLY["1.4.1"][0], rows, nonce)
    return {"safe": SAFE, "to": tx.to, "value": "0", "data": common.hex0x(tx.data), "operation": tx.operation,
            "nonce": nonce, "safeTxGas": 0, "baseGas": 0, "gasPrice": "0", "gasToken": safe_batch.ZERO_ADDRESS,
            "refundReceiver": safe_batch.ZERO_ADDRESS, "isExecuted": executed, "isSuccessful": True if executed else None,
            "executionDate": "2026-10-01T00:00:00Z" if executed else None, "transactionHash": None,
            "confirmations": [], "confirmationsRequired": 2,
            "safeTxHash": safe_batch.tx_record(1, SAFE, tx)["safeTxHash"]}


class AmountTests(unittest.TestCase):
    def test_rounded_amounts_carry_half_a_unit_of_their_last_decimal(self):
        self.assertEqual(safe_batch.sheet_amount("17788985.19", "x"), (1778898519 * 10**16, 5 * 10**15))
        self.assertEqual(safe_batch.sheet_amount("1,234.5", "x"), (12345 * 10**17, 5 * 10**16))
        self.assertEqual(safe_batch.sheet_amount("60000", "x"), (60000 * ONE, ONE // 2))
        self.assertEqual(safe_batch.sheet_amount("2.2958E-14", "x"), (22958, 0))

    def test_full_precision_is_exact(self):
        self.assertEqual(safe_batch.sheet_amount("6051737833.637404544333610648", "x"),
                         (6051737833637404544333610648, 0))

    def test_bad_amounts_are_refused(self):
        for text in ("", "abc", "-1", "1.0000000000000000001"):
            with self.assertRaises(common.InputError):
                safe_batch.sheet_amount(text, "x")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def inventory_zip(self, alter: bool = False, drop: bool = False) -> Path:
        files = {
            "kucoin.csv": [{"exchange_id": "kucoin", "address_hex": common.to_checksum(DAVE), "configured_destination": HOT}],
            "htx.csv": [{"exchange_id": "htx", "address_hex": ALICE.upper().replace("0X", "0x")}],
        }
        contents = {}
        for name, rows in files.items():
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=EXCHANGE_COLUMNS, restval="", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            contents[name] = buffer.getvalue().encode()
        summary = {"exchanges": {name[:-4]: {"output": f"exchanges/wallets-standardized/{name}", "normalized_rows": 1,
                                              "output_sha256": hashlib.sha256(data).hexdigest()}
                                 for name, data in contents.items()}}
        if alter:
            contents["htx.csv"] += b"htx,0x" + b"12" * 20 + b",,,\n"
        path = self.dir / "wallets-standardized.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("wallets-standardized/summary.json", json.dumps(summary))
            for name, data in contents.items():
                if not (drop and name == "htx.csv"):
                    archive.writestr(f"wallets-standardized/{name}", data)
                archive.writestr(f"__MACOSX/wallets-standardized/._{name}", b"\x00\x05\x16\x07 not a csv")
        return path


class InputTests(Fixture):
    def test_zip_is_read_and_checked_against_its_summary(self):
        inventory = safe_batch.read_inventory(self.inventory_zip())
        self.assertEqual(inventory.problems, [])
        self.assertTrue(inventory.has_summary)
        self.assertEqual(inventory.wallets, {DAVE: "kucoin", ALICE: "htx"})
        self.assertEqual(inventory.known_as(HOT), "kucoin delivery address")

    def test_an_altered_or_missing_file_is_a_problem(self):
        self.assertIn("SHA-256 differs", " ".join(safe_batch.read_inventory(self.inventory_zip(alter=True)).problems))
        self.assertIn("missing", " ".join(safe_batch.read_inventory(self.inventory_zip(drop=True)).problems))

    def test_hold_lists_and_csv_lists(self):
        hold = self.dir / "hold.txt"
        hold.write_text(f"# held\n{common.to_checksum(ALICE)}\n# another\n{common.to_one1(BOB)}\n")
        self.assertEqual(safe_batch.read_address_list(hold), {ALICE, BOB})
        listed = write_csv(self.dir / "list.csv", ["address", "amount_atto"], [{"address": CAROL, "amount_atto": "1"}])
        self.assertEqual(safe_batch.read_address_list(listed), {CAROL})


class CrosscheckTests(Fixture):
    def run_check(self, sheet: list[dict], records: list[dict], next_nonce: int, safe_tx_hash=None, exclude=None):
        sheet_path = write_csv(self.dir / "sheet.csv", ["address", "migration_balance"], sheet)
        args = SimpleNamespace(env=self.dir / "none.env", safe=SAFE, token=TOKEN, sheet=sheet_path, amount_column=None,
                               exchange_wallets=self.inventory_zip(), exclude=exclude, service_url=None, chain_id=1,
                               api_key=None, safe_tx_hash=safe_tx_hash, out_dir=self.dir / "out")
        info = {"nonce": str(next_nonce), "version": "1.4.1", "threshold": 2, "owners": [ALICE, BOB, CAROL]}
        stdout = io.StringIO()
        code = 0
        with mock.patch.object(safe_batch, "service_json", lambda url, key: info), \
                mock.patch.object(safe_batch, "service_pages", lambda url, key: records), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            try:
                safe_batch.cmd_crosscheck(args)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue()

    def test_a_payment_equal_to_its_rounded_sheet_with_nobody_paid_before_passes(self):
        records = [record(0, [(CAROL, 5 * ONE)], True), record(1, [(BOB, 1234567891234567891)], False)]
        code, report = self.run_check([{"address": BOB, "migration_balance": "1.234567891"}], records, 1)
        self.assertEqual(code, 0, report)
        self.assertIn("RESULT: PASS", report)
        out = self.dir / "out"
        self.assertIn(",1.234567891,ok", (out / "payment-vs-sheet.csv").read_text())
        self.assertIn(CAROL, (out / "completed-safe-transfers.csv").read_text())
        self.assertEqual((out / "report.txt").read_text().strip(), report.strip())

    def test_a_wallet_the_safe_paid_before_fails(self):
        records = [record(0, [(CAROL, 5 * ONE)], True), record(1, [(BOB, 2 * ONE), (CAROL, 7 * ONE)], False)]
        code, report = self.run_check([{"address": BOB, "migration_balance": "2"},
                                       {"address": CAROL, "migration_balance": "7"}], records, 1)
        self.assertEqual(code, 1)
        self.assertIn("paid 5 ONE at nonce 0", report)
        self.assertIn("RESULT: FAIL in check 2.", report)

    def test_differences_and_exchange_wallets_fail(self):
        records = [record(1, [(BOB, 2 * ONE), (DAVE, ONE), (HOT, ONE)], False)]
        sheet = [{"address": BOB, "migration_balance": "2.01"}, {"address": DAVE, "migration_balance": "1"},
                 {"address": HOT, "migration_balance": "1"}, {"address": CAROL, "migration_balance": "3"}]
        code, report = self.run_check(sheet, records, 1)
        self.assertEqual(code, 1)
        self.assertIn("the sheet says 2.01", report)
        self.assertIn("in the sheet, not paid", report)
        self.assertIn("kucoin exchange wallet", report)
        self.assertIn("kucoin delivery address", report)
        self.assertIn("RESULT: FAIL in check 1, 4.", report)

    def test_another_queued_payment_to_the_same_wallet_fails(self):
        mine, other = record(1, [(BOB, 2 * ONE)], False), record(2, [(BOB, 2 * ONE)], False)
        sheet = [{"address": BOB, "migration_balance": "2"}]
        code, report = self.run_check(sheet, [mine, other], 1)
        self.assertEqual(code, 1)
        self.assertIn("is paid twice (nonce 1 and nonce 2)", report)
        code, report = self.run_check(sheet, [mine, other], 1, safe_tx_hash=[mine["safeTxHash"]])
        self.assertEqual(code, 1)
        self.assertIn("also queued at nonce 2", report)
        self.assertIn("RESULT: FAIL in check 3.", report)

    def test_exclude_lists(self):
        hold = self.dir / "hold.txt"
        hold.write_text(f"# held\n{BOB}\n")
        code, report = self.run_check([{"address": BOB, "migration_balance": "2"}], [record(1, [(BOB, 2 * ONE)], False)],
                                      1, exclude=[hold])
        self.assertEqual(code, 1)
        self.assertIn("RESULT: FAIL in check 5.", report)

    def test_competing_proposals_for_the_sheet_need_a_hash(self):
        records = [record(1, [(BOB, 2 * ONE)], False), record(1, [(BOB, 2 * ONE), (CAROL, ONE)], False)]
        code, _ = self.run_check([{"address": BOB, "migration_balance": "2"}], records, 1)
        self.assertEqual(code, 1)
        self.assertFalse((self.dir / "out").exists())

    def test_a_proposal_whose_data_does_not_match_its_hash_fails(self):
        tampered = record(1, [(BOB, 2 * ONE)], False)
        tampered["safeTxHash"] = record(1, [(BOB, 3 * ONE)], False)["safeTxHash"]
        code, report = self.run_check([{"address": BOB, "migration_balance": "2"}], [tampered], 1)
        self.assertEqual(code, 1)
        self.assertIn("its content hashes to", report)


if __name__ == "__main__":
    unittest.main()
