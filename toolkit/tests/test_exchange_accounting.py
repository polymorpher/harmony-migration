import csv
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path


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
        digest = int.from_bytes(self.module.eip191_hash(message), "big")
        nonce = 11
        point = self.module.scalar_multiply(
            nonce, self.module.SECP256K1_G
        )
        r = point[0] % self.module.SECP256K1_N
        s = (
            pow(nonce, -1, self.module.SECP256K1_N)
            * (digest + r * private_key)
            % self.module.SECP256K1_N
        )
        recovery_id = point[1] & 1
        if s > self.module.SECP256K1_N // 2:
            s = self.module.SECP256K1_N - s
            recovery_id ^= 1
        signature = (
            "0x"
            + r.to_bytes(32, "big").hex()
            + s.to_bytes(32, "big").hex()
            + bytes([27 + recovery_id]).hex()
        )
        public_key = self.module.scalar_multiply(
            private_key, self.module.SECP256K1_G
        )
        encoded = public_key[0].to_bytes(
            32, "big"
        ) + public_key[1].to_bytes(32, "big")
        expected = (
            "0x" + self.module.lib.keccak256(encoded)[-20:].hex()
        )
        self.assertEqual(
            self.module.recover_eip191_address(
                message, signature, "test"
            ),
            expected,
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

    def test_manual_route_activates_native_claim_but_not_deferred_wone(self):
        threshold = 1000 * 10**18
        native = 500 * 10**18
        wone = 100 * 10**18
        claim = self.claim(native, 0)
        claim["wone_balance_atto"] = str(wone)
        claim["qualification_total_atto"] = str(native + wone)
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
            threshold,
            self.module.parse_utc("2026-09-10T14:00:00Z"),
        )
        self.assertEqual(row["planned_wallet_airdrop_atto"], str(native))
        self.assertEqual(
            row["planned_total_entitlement_atto"], str(native)
        )
        self.assertEqual(
            row["remaining_not_airdropped_atto"], str(wone)
        )
        self.assertEqual(
            row["planned_delivery_status"],
            "manual_destination_configured",
        )

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
