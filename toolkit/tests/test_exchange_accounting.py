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


class ExchangeAccountingPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load(ACCOUNTING_SCRIPT, "build_exchange_accounting")

    @staticmethod
    def normalized(destination_status="configured"):
        return {
            "address_hex": "0x0000000000000000000000000000000000000001",
            "address_one": "one1test",
            "source_row": "2",
            "configured_destination": (
                "0x0000000000000000000000000000000000000002"
                if destination_status == "configured"
                else ""
            ),
            "configured_destination_status": destination_status,
            "submitted_balance_atto": "",
            "authorization_type": "none",
            "authorization_status": "not_provided",
        }

    @staticmethod
    def claim(native_total, wone_airdrop=0):
        return {
            "address": "0x0000000000000000000000000000000000000001",
            "liquid_shard0_atto": str(native_total),
            "liquid_shard1_atto": "0",
            "native_wallet_airdrop_atto": str(native_total),
            "wone_balance_atto": str(wone_airdrop),
            "wone_airdrop_atto": str(wone_airdrop),
            "wallet_airdrop_atto": str(native_total + wone_airdrop),
            "staked_to_vault_atto": "0",
            "native_total_claim_atto": str(native_total),
            "qualification_total_atto": str(
                native_total + wone_airdrop
            ),
            "total_claim_atto": str(native_total + wone_airdrop),
        }

    def test_manual_route_includes_below_threshold_wone(self):
        threshold = 1000 * 10**18
        native = 500 * 10**18
        wone = 100 * 10**18
        claim = self.claim(native, wone)
        exchange = {
            "config": {
                "id": "example",
                "delivery_policy": "manual_current_claim",
            }
        }
        row = self.module.build_audit_row(
            exchange,
            self.normalized(),
            claim,
            wone,
            None,
            None,
            None,
            threshold,
            self.module.parse_utc("2026-09-10T14:00:00Z"),
        )
        self.assertEqual(
            row["planned_wallet_airdrop_atto"],
            str(native + wone),
        )
        self.assertEqual(
            row["planned_total_entitlement_atto"],
            str(native + wone),
        )
        self.assertEqual(
            row["remaining_not_airdropped_atto"],
            "0",
        )
        self.assertEqual(
            row["planned_delivery_status"],
            "manual_destination_configured",
        )
        self.assertEqual(row["migration_stage"], "exchange_aggregate")

    def test_gate_requires_threshold_and_automatic_category(self):
        threshold = 1000 * 10**18
        claim = self.claim(threshold)
        exchange = {
            "config": {
                "id": "gate",
                "delivery_policy": "automatic_threshold",
            }
        }
        row = self.module.build_audit_row(
            exchange,
            self.normalized("not_required_same_address"),
            claim,
            0,
            {
                "last_activity_time_utc": "",
                "last_activity_block": "",
                "last_activity_shard": "",
                "last_activity_type": "",
            },
            "automatic",
            None,
            threshold,
            self.module.parse_utc("2026-09-10T14:00:00Z"),
        )
        self.assertEqual(
            row["planned_delivery_status"], "automatic_same_address"
        )
        self.assertEqual(
            row["planned_wallet_airdrop_atto"], str(threshold)
        )
        self.assertEqual(
            row["planned_total_entitlement_atto"], str(threshold)
        )

    def test_gate_stage_prevents_deferred_wallet_from_initial_delivery(self):
        threshold = 1000 * 10**18
        claim = self.claim(threshold)
        exchange = {
            "config": {
                "id": "gate",
                "delivery_policy": "automatic_threshold",
            }
        }
        row = self.module.build_audit_row(
            exchange,
            self.normalized("not_required_same_address"),
            claim,
            0,
            {
                "last_activity_time_utc": "2025-01-01T00:00:00Z",
                "last_activity_block": "1",
                "last_activity_shard": "0",
                "last_activity_type": "regular",
            },
            "automatic",
            {
                "stage": "deferred",
                "treatment": "issue",
                "allocation": threshold,
            },
            threshold,
            self.module.parse_utc("2026-09-10T14:00:00Z"),
            stages_complete=True,
        )
        self.assertEqual(row["migration_stage"], "deferred")
        self.assertEqual(
            row["planned_delivery_status"],
            "deferred_stage_not_initial",
        )
        self.assertEqual(row["planned_total_entitlement_atto"], "0")

    def test_manual_audit_separates_erc20_and_vault_principal(self):
        threshold = 1000 * 10**18
        wallet = 400 * 10**18
        staked = 600 * 10**18
        claim = self.claim(threshold)
        claim.update(
            {
                "liquid_shard0_atto": str(wallet),
                "native_wallet_airdrop_atto": str(wallet),
                "wallet_airdrop_atto": str(wallet),
                "staked_to_vault_atto": str(staked),
            }
        )
        exchange = {
            "config": {
                "id": "example",
                "delivery_policy": "manual_current_claim",
            }
        }
        row = self.module.build_audit_row(
            exchange,
            self.normalized(),
            claim,
            0,
            {
                "last_activity_time_utc": "",
                "last_activity_block": "",
                "last_activity_shard": "",
                "last_activity_type": "",
            },
            "excluded_address",
            None,
            threshold,
            self.module.parse_utc("2026-09-10T14:00:00Z"),
        )
        self.assertEqual(
            row["planned_wallet_airdrop_atto"], str(wallet)
        )
        self.assertEqual(
            row["planned_staked_to_vault_atto"], str(staked)
        )
        self.assertEqual(
            row["planned_total_entitlement_atto"], str(threshold)
        )

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
