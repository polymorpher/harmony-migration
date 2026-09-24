import csv
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).parents[2]
NORMALIZE_SCRIPT = (
    ROOT
    / "toolkit"
    / "scripts"
    / "exchanges"
    / "normalize-exchange-wallets.py"
)
ACCOUNTING_SCRIPT = (
    ROOT
    / "toolkit"
    / "scripts"
    / "exchanges"
    / "build-exchange-accounting.py"
)
ROUTING_SCRIPT = (
    ROOT / "toolkit" / "scripts" / "routing" / "apply-routes.py"
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExchangeNormalizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load(NORMALIZE_SCRIPT, "normalize_exchange_wallets")

    @staticmethod
    def signature(module, private_key, message, nonce):
        digest = int.from_bytes(module.eip191_hash(message), "big")
        point = module.scalar_multiply(nonce, module.SECP256K1_G)
        r = point[0] % module.SECP256K1_N
        s = (
            pow(nonce, -1, module.SECP256K1_N)
            * (digest + r * private_key)
            % module.SECP256K1_N
        )
        recovery_id = point[1] & 1
        if s > module.SECP256K1_N // 2:
            s = module.SECP256K1_N - s
            recovery_id ^= 1
        return (
            "0x"
            + r.to_bytes(32, "big").hex()
            + s.to_bytes(32, "big").hex()
            + bytes([27 + recovery_id]).hex()
        )

    @staticmethod
    def private_key_address(module, private_key):
        public_key = module.scalar_multiply(
            private_key,
            module.SECP256K1_G,
        )
        encoded = public_key[0].to_bytes(
            32,
            "big",
        ) + public_key[1].to_bytes(32, "big")
        return "0x" + module.lib.keccak256(encoded)[-20:].hex()

    @staticmethod
    def write_xlsx(path, sheets):
        workbook_sheets = []
        relationships = []
        with zipfile.ZipFile(path, "w") as archive:
            for index, (name, rows) in enumerate(sheets, start=1):
                relationship_id = f"rId{index}"
                workbook_sheets.append(
                    f'<sheet name="{escape(name)}" sheetId="{index}" '
                    f'r:id="{relationship_id}"/>'
                )
                relationships.append(
                    f'<Relationship Id="{relationship_id}" '
                    f'Target="worksheets/sheet{index}.xml" '
                    'Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/worksheet"/>'
                )
                xml_rows = []
                for row_number, values in enumerate(rows, start=1):
                    cells = []
                    for column, value in enumerate(values, start=1):
                        reference = f"{chr(64 + column)}{row_number}"
                        if isinstance(value, tuple):
                            cached, formula = value
                            cells.append(
                                f'<c r="{reference}" t="str"><f>'
                                f"{escape(formula)}</f><v>"
                                f"{escape(str(cached))}</v></c>"
                            )
                            continue
                        cells.append(
                            f'<c r="{reference}" t="inlineStr"><is><t>'
                            f"{escape(str(value))}</t></is></c>"
                        )
                    xml_rows.append(
                        f'<row r="{row_number}">{"".join(cells)}</row>'
                    )
                archive.writestr(
                    f"xl/worksheets/sheet{index}.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main"><sheetData>'
                    + "".join(xml_rows)
                    + "</sheetData></worksheet>",
                )
            archive.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships"><sheets>'
                + "".join(workbook_sheets)
                + "</sheets></workbook>",
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">'
                + "".join(relationships)
                + "</Relationships>",
            )

    @staticmethod
    def write_docx(path, paragraphs):
        body = "".join(
            "<w:p><w:r><w:t>"
            + escape(paragraph)
            + "</w:t></w:r></w:p>"
            for paragraph in paragraphs
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body>'
                + body
                + "</w:body></w:document>",
            )

    def test_reads_rows_beyond_incorrect_xlsx_dimension(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.xlsx"
            workbook = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
            relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
</Relationships>"""
            sheet = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <dimension ref="A1"/>
 <sheetData>
  <row r="1"><c r="A1" t="inlineStr"><is><t>address</t></is></c></row>
  <row r="2"><c r="A2" t="inlineStr"><is><t>one1first</t></is></c></row>
  <row r="3"><c r="A3" t="inlineStr"><is><t>one1second</t></is></c></row>
 </sheetData>
</worksheet>"""
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("xl/workbook.xml", workbook)
                archive.writestr(
                    "xl/_rels/workbook.xml.rels", relationships
                )
                archive.writestr("xl/worksheets/sheet1.xml", sheet)
            header, rows, physical, blank = self.module.xlsx_rows(
                path, "Sheet1"
            )
            self.assertEqual(header, ["address"])
            self.assertEqual(
                rows,
                [
                    (2, {"address": "one1first"}),
                    (3, {"address": "one1second"}),
                ],
            )
            self.assertEqual(physical, 2)
            self.assertEqual(blank, 0)

    def test_recovers_low_s_eip191_signature(self):
        private_key = 7
        message = "authorize deterministic exchange test"
        signature = self.signature(self.module, private_key, message, 11)
        expected = self.private_key_address(self.module, private_key)
        self.assertEqual(
            self.module.recover_eip191_address(
                message, signature, "test"
            ),
            expected,
        )

    def test_parses_kucoin_multishard_balances_and_docx_signatures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workbook = root / "kucoin.xlsx"
            authorization = root / "kucoin-signatures.docx"
            destination = "0x000000000000000000000000000000000000002a"
            signer = self.private_key_address(self.module, 7)
            signer_one = self.module.lib.hex_to_bech32(signer)
            zero = self.private_key_address(self.module, 8)
            zero_one = self.module.lib.hex_to_bech32(zero)
            message = (
                "Kucoin owns this address, and transfers all its eligible "
                "balance for Harmony ERC-20 airdrop to "
                f"[{destination}]"
            )
            signature = self.signature(self.module, 7, message, 11)
            self.write_xlsx(
                workbook,
                (
                    (
                        "shard0",
                        (
                            ("address", "confirmBalance", "sign", "balanceWei"),
                            (
                                signer_one,
                                "1000",
                                "SIGN",
                                str(1000 * 10**18),
                            ),
                            (zero_one, "0", "", "0"),
                        ),
                    ),
                    (
                        "shard1",
                        (
                            ("address", "balance", "sign", "balanceWei"),
                            (signer_one, "2", "", str(2 * 10**18)),
                        ),
                    ),
                    (
                        "summary",
                        (
                            ("shard0", "1000", str(1000 * 10**18)),
                            ("shard1", "2", str(2 * 10**18)),
                            ("total", "1002", str(1002 * 10**18)),
                            ("shard0 地址数", "2", ""),
                            ("shard1 非 0 地址数", "1", ""),
                            ("shard0 快照高度", "10", ""),
                            (
                                "shard0 快照时间 (UTC)",
                                "2026-09-10 14:00:00",
                                "",
                            ),
                            ("shard1 快照高度", "11", ""),
                            (
                                "shard1 快照时间 (UTC)",
                                "2026-09-10 13:59:59",
                                "",
                            ),
                        ),
                    ),
                    (
                        "balance >= 1000",
                        (
                            (
                                "shard",
                                "balance",
                                "balanceWei",
                                "address",
                                "EIP-191 signature",
                            ),
                            (
                                "shard0",
                                "1000",
                                str(1000 * 10**18),
                                signer_one,
                                signature,
                            ),
                        ),
                    ),
                ),
            )
            self.write_docx(
                authorization,
                (
                    "ONE SIGN MESSAGE EIP-191 signature",
                    message,
                    f"{signer_one}        {signature}",
                ),
            )
            config = {
                "id": "kucoin",
                "authorization_file": authorization.name,
                "snapshot_blocks": {"shard0": 10, "shard1": 11},
                "snapshot_times_utc": {
                    "shard0": "2026-09-10T14:00:00Z",
                    "shard1": "2026-09-10T13:59:59Z",
                },
            }
            rows, physical, blank, details = self.module.parse_kucoin(
                config,
                workbook,
                self.module.file_sha256(workbook),
                destination,
                "configured",
            )
            by_address = {
                row["address_hex"].lower(): row for row in rows
            }
            self.assertEqual(len(rows), 2)
            self.assertEqual(physical, 3)
            self.assertEqual(blank, 0)
            self.assertEqual(details["cross_sheet_merged_rows"], 1)
            self.assertEqual(details["authorization_designated_rows"], 1)
            self.assertEqual(
                by_address[signer]["submitted_balance_atto"],
                str(1002 * 10**18),
            )
            self.assertEqual(
                by_address[signer]["authorization_status"],
                "verified",
            )
            self.assertEqual(
                by_address[zero]["authorization_status"],
                "not_provided",
            )

    def digitalx_workbook(self, path, destination_cell, wallet_total_cell):
        warm = "one17rwxqdssmqqlq500kchjgsjp0ympfn66hrfd95"
        deposit = "one1krklpz37t8wurqpdj7lp9hhswe4qdemuf6wh0m"
        explorer = "https://explorer.harmony.one/address/"
        transaction = "0x" + "ab" * 32
        self.write_xlsx(
            path,
            (
                (
                    "summary",
                    (
                        ("Category", "amount"),
                        (
                            "Sum of current wallet balance",
                            (wallet_total_cell, "sum('current wallet balance'!C2:C3)"),
                        ),
                        (
                            "deposit amounts invalidated by the network rollback",
                            ("249349.2348", "sum('rolled-back transactions'!B2:B2)"),
                        ),
                        ("rolled-back withdrawals", "0.0"),
                        ("Total Sum", ("1004350.909", "B2+B3")),
                        (
                            "EVM address to receive the above total in ERC-20 ONE",
                            destination_cell,
                        ),
                    ),
                ),
                (
                    "current wallet balance",
                    (
                        ("wallet type", "address", "balance", "explorer"),
                        (
                            "warm/sender wallet",
                            warm,
                            "755001.6742711",
                            (explorer + warm, '"' + explorer + '"&B2'),
                        ),
                        (
                            "user deposit address",
                            deposit,
                            "1.2958E-14",
                            (explorer + deposit, '"' + explorer + '"&B3'),
                        ),
                    ),
                ),
                (
                    "rolled-back transactions",
                    (
                        ("txid", "amount", "explorer"),
                        (
                            transaction,
                            "249349.2348",
                            (
                                "https://explorer.harmony.one/tx/" + transaction,
                                '"https://explorer.harmony.one/tx/"&A2',
                            ),
                        ),
                    ),
                ),
            ),
        )
        return warm, deposit, transaction

    def test_parses_digitalx_summary_workbook(self):
        destination = "0xf0bc8FdDB1F358cEf470D63F96aE65B1D7914953"
        config = {
            "id": "digitalx",
            "worksheet": "current wallet balance",
            "summary_worksheet": "summary",
            "rollback_worksheet": "rolled-back transactions",
        }
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "digitalx.xlsx"
            warm, deposit, transaction = self.digitalx_workbook(
                workbook, destination, "755001.674"
            )
            rows, physical, blank, details = self.module.parse_digitalx(
                config,
                workbook,
                self.module.file_sha256(workbook),
                destination,
                "configured",
            )
            by_one = {row["address_one"]: row for row in rows}
            self.assertEqual((len(rows), physical, blank), (2, 2, 0))
            self.assertEqual(
                by_one[warm]["submitted_balance_atto"],
                str(755001_674271100000000000),
            )
            self.assertEqual(by_one[deposit]["submitted_balance_atto"], "12958")
            self.assertEqual(by_one[warm]["authorization_status"], "not_provided")
            self.assertEqual(details["declared_destination"], destination)
            self.assertEqual(details["rolled_back_deposit_rows"], 1)
            self.assertEqual(
                details["rolled_back_deposits"][0]["transaction_hash"],
                transaction,
            )
            self.assertEqual(
                details["rolled_back_deposit_total_atto"],
                str(249349_234800000000000000),
            )
            self.assertEqual(
                details["source_summary_checks"]["wallet_balance"][
                    "rounding_delta_atto"
                ],
                str(271100000012958),
            )
            self.assertEqual(
                details["wallet_type_rows"],
                {"warm/sender wallet": 1, "user deposit address": 1},
            )

    def test_digitalx_rejects_destination_and_total_mismatches(self):
        destination = "0xf0bc8FdDB1F358cEf470D63F96aE65B1D7914953"
        config = {
            "id": "digitalx",
            "worksheet": "current wallet balance",
            "summary_worksheet": "summary",
            "rollback_worksheet": "rolled-back transactions",
        }
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "digitalx.xlsx"
            self.digitalx_workbook(
                workbook,
                "0x000000000000000000000000000000000000dead",
                "755001.674",
            )
            with self.assertRaisesRegex(ValueError, "declared destination"):
                self.module.parse_digitalx(
                    config,
                    workbook,
                    self.module.file_sha256(workbook),
                    destination,
                    "configured",
                )
            self.digitalx_workbook(workbook, destination, "755001.675")
            with self.assertRaisesRegex(ValueError, "displayed total"):
                self.module.parse_digitalx(
                    config,
                    workbook,
                    self.module.file_sha256(workbook),
                    destination,
                    "configured",
                )

    WALLET = "0x28c6c06298d514db089934071355e5743bf21d60"
    STAKING = "0xf977814e90da44bfa03b6295a0616a897441acec"

    def destination_status(self, mode, text, filename="dest.txt"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / filename
            path.write_text(text, encoding="utf-8")
            config = {
                "id": "example",
                "destination_mode": mode,
                "destination_file": filename,
                "destination_required": True,
            }
            return self.module.destination_status(config, Path(directory))

    def test_split_destinations_are_role_based_not_positional(self):
        reversed_order = (
            "role,address,notes\n"
            f'staking,{self.STAKING},"delegated principal and unclaimed rewards"\n'
            f'wallet,{self.WALLET},"liquid balances and WONE"\n'
        )
        wallet, staking, status, _path, notes = self.destination_status(
            "aggregate_split", reversed_order
        )
        self.assertEqual(status, "configured")
        self.assertEqual(wallet.lower(), self.WALLET)
        self.assertEqual(staking.lower(), self.STAKING)
        self.assertEqual(
            notes,
            {
                "staking": "delegated principal and unclaimed rewards",
                "wallet": "liquid balances and WONE",
            },
        )

    def test_destination_rows_require_roles_and_english_notes(self):
        cases = (
            (f"{self.WALLET}\n{self.STAKING}\n", "role,address,notes"),
            (f"wallet,{self.WALLET},\nstaking,{self.STAKING},x\n", "English"),
            (f"wallet,{self.WALLET},note\nwallet,{self.STAKING},note\n", "duplicate"),
            (f"wallet,{self.WALLET},note\n", "exactly the roles"),
            (f"wallet,{self.WALLET},note\nstaking,{self.WALLET},note\n", "must differ"),
            (f"treasury,{self.WALLET},note\n", "unknown destination role"),
        )
        for text, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                self.destination_status("aggregate_split", text)
        with self.assertRaisesRegex(ValueError, "exactly the roles"):
            self.destination_status(
                "aggregate", f"wallet,{self.WALLET},liquid balances\n"
            )
        wallet, staking, status, _path, notes = self.destination_status(
            "aggregate", f"# comment\naggregate,{self.WALLET},all components\n"
        )
        self.assertEqual((wallet.lower(), staking, status), (self.WALLET, "", "configured"))
        self.assertEqual(notes, {"aggregate": "all components"})

    def test_bybit_csv_requires_matching_pair_and_total_row(self):
        header = "Site,Chain,Coin,ONE Address,0x Address,amount,,,\n"
        one_a = self.module.lib.hex_to_bech32(self.WALLET)
        one_b = self.module.lib.hex_to_bech32(self.STAKING)
        rows = (
            f"Bybit,ONE,ONE,{one_a},{self.WALLET},\"1,000.5\",,,\n"
            f"Bybit,ONE,ONE,{one_b},{self.STAKING},\"237,988,937.9034\",,,\n"
        )
        config = {"id": "bybit"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bybit.csv"
            path.write_text(
                "\ufeff" + header + rows + "总计,\"237,989,938.4034\",,,,,,,\n",
                encoding="utf-8",
            )
            parsed, physical, blank, details = self.module.parse_bybit(
                config, path, self.module.file_sha256(path), "", "same_address"
            )
            self.assertEqual(len(parsed), 2)
            self.assertEqual(
                sum(int(row["submitted_balance_atto"]) for row in parsed),
                237_989_938_403_400_000_000_000_000,
            )
            self.assertEqual(
                details["source_total_row_atto"],
                str(237_989_938_403_400_000_000_000_000),
            )
            path.write_text(
                "\ufeff" + header + rows + "总计,\"237,989,938.4035\",,,,,,,\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "total"):
                self.module.parse_bybit(
                    config, path, self.module.file_sha256(path), "", "same_address"
                )
            swapped = rows.replace(f"{one_a},{self.WALLET}", f"{one_a},{self.STAKING}", 1)
            path.write_text(
                "\ufeff" + header + swapped + "总计,\"237,989,938.4034\",,,,,,,\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                self.module.parse_bybit(
                    config, path, self.module.file_sha256(path), "", "same_address"
                )

    def test_one_to_atto_conversion_is_exact_for_large_values(self):
        self.assertEqual(
            self.module.decimal_one_to_atto("12345678901.123456789012345678", "t"),
            12345678901123456789012345678,
        )
        self.assertEqual(
            self.module.decimal_one_to_atto("9.876543210987654321E+12", "t"),
            9876543210987654321000000000000,
        )
        for bad in ("1.0000000000000000001", "-1", "abc", "1e-19"):
            with self.assertRaises(ValueError):
                self.module.decimal_one_to_atto(bad, "t")
        check = self.module.displayed_total_matches(
            12345678901123456789012345678, "12345678901.1235", "t"
        )
        self.assertEqual(check["rounding_delta_atto"], str(-43210987654322))

    def test_formulas_outside_allowed_columns_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "wallets.xlsx"
            self.write_xlsx(
                workbook,
                (
                    (
                        "Sheet1",
                        (
                            ("address", "balance"),
                            ("one1first", ("1", "SUM(1)")),
                        ),
                    ),
                ),
            )
            with self.assertRaisesRegex(ValueError, "formulas are not allowed"):
                self.module.xlsx_rows(workbook, "Sheet1")
            header, rows, _physical, _blank = self.module.xlsx_rows(
                workbook, "Sheet1", formula_columns=("balance",)
            )
            self.assertEqual(header, ["address", "balance"])
            self.assertEqual(rows, [(2, {"address": "one1first", "balance": "1"})])


class ExchangeAccountingPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load(ACCOUNTING_SCRIPT, "build_exchange_accounting")

    CUTOFF = "2026-09-10T14:00:00Z"
    WALLET_DESTINATION = "0x0000000000000000000000000000000000000002"
    STAKING_DESTINATION = "0x0000000000000000000000000000000000000003"

    @classmethod
    def normalized(cls, destination_status="configured", staking=False):
        configured = destination_status == "configured"
        return {
            "address_hex": "0x0000000000000000000000000000000000000001",
            "address_one": "one1test",
            "source_row": "2",
            "configured_destination": cls.WALLET_DESTINATION if configured else "",
            "configured_staking_destination": (
                cls.STAKING_DESTINATION if configured and staking else ""
            ),
            "configured_destination_status": destination_status,
            "submitted_balance_atto": "",
            "authorization_type": "none",
            "authorization_status": "not_provided",
        }

    @staticmethod
    def claim(native_total, wone_airdrop=0, staked=0, unclaimed=0):
        wallet_native = native_total - staked
        liquid = wallet_native - unclaimed
        return {
            "address": "0x0000000000000000000000000000000000000001",
            "liquid_shard0_atto": str(liquid),
            "liquid_shard1_atto": "0",
            "pending_cross_shard_atto": "0",
            "pending_undelegation_atto": "0",
            "unclaimed_staking_reward_atto": str(unclaimed),
            "native_wallet_airdrop_atto": str(wallet_native),
            "wone_balance_atto": str(wone_airdrop),
            "wone_airdrop_atto": str(wone_airdrop),
            "wallet_airdrop_atto": str(wallet_native + wone_airdrop),
            "staked_to_vault_atto": str(staked),
            "native_total_claim_atto": str(native_total),
            "qualification_total_atto": str(native_total + wone_airdrop),
            "total_claim_atto": str(native_total + wone_airdrop),
        }

    @staticmethod
    def exchange(exchange_id, mode):
        return {
            "config": {
                "id": exchange_id,
                "delivery_policy": "manual_from_reserve",
                "destination_mode": mode,
            }
        }

    NO_ACTIVITY = {
        "last_activity_time_utc": "",
        "last_activity_block": "",
        "last_activity_shard": "",
        "last_activity_type": "",
    }

    def build(self, exchange, normalized, claim, wone=0, **kwargs):
        defaults = {
            "activity": self.NO_ACTIVITY,
            "category": None,
            "stage_record": None,
            "threshold": 1000 * 10**18,
        }
        defaults.update(kwargs)
        return self.module.build_audit_row(
            exchange,
            normalized,
            claim,
            wone,
            defaults["activity"],
            defaults["category"],
            defaults["stage_record"],
            defaults["threshold"],
            self.module.parse_utc(self.CUTOFF),
            categories_complete=defaults.get("categories_complete", True),
            stages_complete=defaults.get("stages_complete", False),
        )

    def test_manual_route_includes_below_threshold_wone(self):
        native = 500 * 10**18
        wone = 100 * 10**18
        row = self.build(
            self.exchange("example", "aggregate"),
            self.normalized(),
            self.claim(native, wone),
            wone,
        )
        self.assertEqual(row["planned_wallet_airdrop_atto"], str(native + wone))
        self.assertEqual(row["planned_total_entitlement_atto"], str(native + wone))
        self.assertEqual(row["remaining_not_airdropped_atto"], "0")
        self.assertEqual(row["planned_delivery_status"], "exchange_manual")
        self.assertEqual(row["migration_stage"], "exchange_manual")
        self.assertEqual(row["issuance_treatment"], "manual_from_reserve")
        self.assertEqual(row["delivery_tier"], "aggregate")
        self.assertEqual(row["planned_wallet_destination"], self.WALLET_DESTINATION)
        self.assertEqual(row["planned_staking_destination"], self.WALLET_DESTINATION)

    def test_same_address_mode_delivers_to_the_source_wallet(self):
        threshold = 1000 * 10**18
        row = self.build(
            self.exchange("bybit", "same_address"),
            self.normalized("same_address"),
            self.claim(threshold),
            category="excluded_address",
            stage_record={"stage": "deferred", "treatment": "issue", "allocation": threshold},
            stages_complete=True,
        )
        self.assertEqual(row["migration_stage"], "exchange_manual")
        self.assertEqual(row["delivery_tier"], "same_address")
        self.assertEqual(row["planned_delivery_status"], "exchange_manual")
        self.assertEqual(row["planned_wallet_destination"], row["address_hex"])
        self.assertEqual(row["planned_total_entitlement_atto"], str(threshold))
        routes = self.module.exchange_routes("bybit", row, 300)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["destination_address"], row["address_hex"])
        self.assertEqual(routes[0]["destination_id"], "")
        self.assertEqual(routes[0]["amount_atto"], "ALL")
        self.assertEqual(routes[0]["status"], "exchange_manual")
        self.assertEqual(routes[0]["reason"], "exchange_manual_reserve_delivery")

    def test_gate_tier_follows_the_initial_stage(self):
        threshold = 1000 * 10**18
        gate = self.exchange("gate", "tiered")
        initial = self.build(
            gate,
            self.normalized(),
            self.claim(threshold),
            category="excluded_address",
            stage_record={"stage": "initial", "treatment": "issue", "allocation": threshold},
            stages_complete=True,
        )
        self.assertEqual(initial["delivery_tier"], "same_address_initial")
        self.assertEqual(initial["planned_wallet_destination"], initial["address_hex"])
        deferred = self.build(
            gate,
            self.normalized(),
            self.claim(threshold),
            category="excluded_address",
            stage_record={"stage": "deferred", "treatment": "issue", "allocation": threshold},
            stages_complete=True,
        )
        self.assertEqual(deferred["delivery_tier"], "aggregated_non_initial")
        self.assertEqual(deferred["planned_wallet_destination"], self.WALLET_DESTINATION)
        self.assertEqual(deferred["planned_total_entitlement_atto"], str(threshold))
        self.assertEqual(deferred["migration_stage"], "exchange_manual")
        pending = self.build(
            gate,
            self.normalized(),
            self.claim(threshold),
            category="excluded_address",
        )
        self.assertEqual(pending["delivery_tier"], "tier_pending")
        self.assertEqual(pending["planned_delivery_status"], "tier_pending")

    def test_manual_audit_separates_erc20_and_vault_principal(self):
        threshold = 1000 * 10**18
        wallet = 400 * 10**18
        staked = 600 * 10**18
        row = self.build(
            self.exchange("example", "aggregate"),
            self.normalized(),
            self.claim(threshold, staked=staked),
            category="excluded_address",
        )
        self.assertEqual(row["planned_wallet_airdrop_atto"], str(wallet))
        self.assertEqual(row["planned_staked_to_vault_atto"], str(staked))
        self.assertEqual(row["planned_total_entitlement_atto"], str(threshold))
        self.assertEqual(row["wallet_component_atto"], str(wallet))
        self.assertEqual(row["staking_component_atto"], str(staked))

    def test_split_mode_emits_wallet_and_staking_routes(self):
        threshold = 1000 * 10**18
        staked = 600 * 10**18
        unclaimed = 50 * 10**18
        wone = 25 * 10**18
        row = self.build(
            self.exchange("binance", "aggregate_split"),
            self.normalized(staking=True),
            self.claim(threshold, wone, staked=staked, unclaimed=unclaimed),
            wone,
            category="excluded_address",
            stage_record={"stage": "initial", "treatment": "issue", "allocation": threshold + wone},
            stages_complete=True,
        )
        liquid = threshold - staked - unclaimed
        self.assertEqual(row["wallet_component_atto"], str(liquid + wone))
        self.assertEqual(row["staking_component_atto"], str(staked + unclaimed))
        self.assertEqual(row["planned_wallet_destination"], self.WALLET_DESTINATION)
        self.assertEqual(row["planned_staking_destination"], self.STAKING_DESTINATION)
        routes = self.module.exchange_routes("binance", row, 300)
        self.assertEqual([route["route_id"][-8:] for route in routes], ["1-wallet", "-staking"])
        wallet_route, staking_route = routes
        self.assertEqual(wallet_route["allocation_method"], "wallet_only")
        self.assertEqual(wallet_route["amount_atto"], str(liquid + wone))
        self.assertEqual(wallet_route["destination_id"], "exchange-binance")
        self.assertEqual(wallet_route["priority"], "300")
        self.assertEqual(staking_route["allocation_method"], "wallet_first_pro_rata_vault")
        self.assertEqual(staking_route["amount_atto"], "ALL")
        self.assertEqual(staking_route["destination_id"], "exchange-binance-staking")
        self.assertEqual(staking_route["priority"], "301")

    def test_missing_destination_holds_the_manual_delivery(self):
        threshold = 1000 * 10**18
        row = self.build(
            self.exchange("example", "aggregate"),
            self.normalized("missing_file"),
            self.claim(threshold),
            category="excluded_address",
        )
        self.assertEqual(row["planned_delivery_status"], "manual_destination_hold")
        self.assertEqual(row["planned_total_entitlement_atto"], str(threshold))
        self.assertEqual(row["migration_stage"], "exchange_manual")

    def test_submitted_reconciliation_can_use_both_liquid_shards(self):
        normalized = self.normalized()
        normalized["submitted_balance_atto"] = str(3 * 10**18)
        claim = self.claim(3 * 10**18)
        claim["liquid_shard0_atto"] = str(1 * 10**18)
        claim["liquid_shard1_atto"] = str(2 * 10**18)
        submitted, status, delta = self.module.submitted_reconciliation(
            normalized,
            claim,
            "liquid_total",
        )
        self.assertEqual(submitted, str(3 * 10**18))
        self.assertEqual(status, "exact_liquid_total_match")
        self.assertEqual(delta, "0")


class ExchangeDestinationMergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load(ROUTING_SCRIPT, "apply_routes_for_exchange_test")

    def test_loads_multiple_destination_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index in (1, 2):
                path = Path(directory) / f"destinations-{index}.csv"
                with path.open("w", newline="") as output:
                    writer = csv.DictWriter(
                        output,
                        fieldnames=(
                            "destination_id",
                            "destination_address",
                            "status",
                            "notes",
                        ),
                        lineterminator="\n",
                    )
                    writer.writeheader()
                    writer.writerow(
                        {
                            "destination_id": f"exchange-{index}",
                            "destination_address": (
                                f"0x{index:040x}"
                            ),
                            "status": "ready",
                        }
                    )
                paths.append(path)
            destinations = self.module.load_destinations(paths)
            self.assertEqual(set(destinations), {"exchange-1", "exchange-2"})


if __name__ == "__main__":
    unittest.main()
