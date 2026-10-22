#!/usr/bin/env python3
"""Tests for the claim tree, the list builder, the Safe approval batch, and the proof lookup.

    python3 tools/test_tools.py

The Solidity side of the same checks is `test/TreeFixture.t.sol`, which claims every row of a list
built here against the compiled contract.
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claim_tree as ct  # noqa: E402
import approve_claims as approve  # noqa: E402
import build_claims as build  # noqa: E402
import proof  # noqa: E402
import release_plan as release  # noqa: E402
import safe_batch  # noqa: E402
import send_calldata as send  # noqa: E402

common = ct.common
FUNDER = "0x" + "f1" * 20
ONE_SAFE = "0x" + "f2" * 20
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
ONE_TOKEN = "0xff74317e948695297acb79cb2de06111f5cc16b3"
SPENDER = "0x" + "c1" * 20


def account(i):
    return "0x" + common.keccak256(f"account-{i}".encode())[12:].hex()


def chains_constants():
    return ct.load_chains()


class TreeTest(unittest.TestCase):
    def test_create2_matches_eip1014_examples(self):
        zero = b"\x00" * 32
        self.assertEqual(
            ct.create2_address("0x" + "00" * 20, zero, b"\x00"), "0x4d1a2e2bb4f88f0250f26ffff098b0b30b26bf38"
        )
        self.assertEqual(
            ct.create2_address("0xdeadbeef00000000000000000000000000000000", zero, b"\x00"),
            "0xb928f69bb1d91cd65274e3c79d8986362984fda3",
        )

    def test_tree_is_the_airdrop_tree_and_every_proof_verifies(self):
        for n in range(1, 41):
            rows = [(i, account(i), ct.NATIVE_TOKEN if i % 3 == 0 else DAI, 10**18 + i) for i in range(n)]
            leaves = ct.leaves(rows)
            levels = ct.levels(leaves)
            self.assertEqual(levels, common.tree_levels(leaves))
            root = ct.root(levels)
            for i, row in enumerate(rows):
                self.assertTrue(ct.verify(ct.proof(levels, i), root, leaves[i]))
                altered = ct.leaf(row[0], row[1], row[2], row[3] + 1)
                self.assertFalse(ct.verify(ct.proof(levels, i), root, altered))

    def test_leaf_binds_the_index(self):
        self.assertNotEqual(ct.leaf(0, account(1), DAI, 5), ct.leaf(1, account(1), DAI, 5))

    def test_parallel_hashing_matches_serial(self):
        rows = [(i, account(i), DAI, i + 1) for i in range(300)]
        saved = ct.PARALLEL_MIN, ct.CHUNK
        try:
            ct.PARALLEL_MIN, ct.CHUNK = 8, 16
            parallel = ct.levels(ct.leaves(rows, workers=2), workers=2)
        finally:
            ct.PARALLEL_MIN, ct.CHUNK = saved
        self.assertEqual(parallel, ct.levels(ct.leaves(rows)))

    def test_tree_file_round_trip(self):
        levels = ct.levels(ct.leaves([(i, account(i), DAI, 1) for i in range(9)]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tree.txt")
            ct.write_tree(path, levels)
            self.assertEqual(ct.read_tree(path, [len(level) for level in levels]), levels)
            with self.assertRaises(ValueError):
                ct.read_tree(path, [1])

    def test_committed_fixture_is_current(self):
        fixture = json.loads((ct.PROJECT / "test" / "fixtures" / "claim-tree.json").read_text())
        rows = [(i, a, t, int(v)) for i, (a, t, v) in
                enumerate(zip(fixture["accounts"], fixture["row_tokens"], fixture["amounts"]))]
        levels = ct.levels(ct.leaves(rows))
        self.assertEqual(ct.hex0x(ct.root(levels)), fixture["root"])
        self.assertEqual([ct.hex0x(x) for x in levels[0]], fixture["leaves"])
        if not ct.ARTIFACT.exists():
            self.skipTest("run `forge build` to compare the fixture with the bytecode")
        code = ct.unhex(json.loads(ct.ARTIFACT.read_text())["bytecode"]["object"])
        init_code = code + ct.constructor_args(
            fixture["funder"], fixture["wrapped_native"], fixture["chain_id"], ct.unhex(fixture["root"]),
            fixture["claim_count"], ct.unhex(fixture["list_hash"]),
        )
        self.assertEqual(ct.hex0x(common.keccak256(init_code)), fixture["init_code_keccak256"],
                         "contract changed: rerun tools/build_fixture.py")


class BuildTest(unittest.TestCase):
    def config(self, one_custody=None):
        return {
            "chains": {"ethereum": {"chain_id": 1, "custody_multisig": FUNDER.upper().replace("0X", "0x")}},
            "one_claims": {"claim_chain": "ethereum", "claim_token": ONE_TOKEN, "custody_multisig": one_custody},
        }

    def test_one_contract_per_chain_by_default(self):
        rows = [{"token": ONE_TOKEN}, {"token": DAI}]
        groups = build.contract_groups(self.config(), "ethereum", rows)
        self.assertEqual([(g[0], g[1], len(g[3])) for g in groups], [("ethereum", FUNDER, 2)])
        same = build.contract_groups(self.config(one_custody=FUNDER), "ethereum", rows)
        self.assertEqual(len(same), 1)

    def test_separate_one_multisig_gets_its_own_contract(self):
        rows = [{"token": ONE_TOKEN}, {"token": DAI}, {"token": ONE_TOKEN}]
        groups = build.contract_groups(self.config(one_custody=ONE_SAFE), "ethereum", rows)
        self.assertEqual([(g[0], g[1], len(g[3])) for g in groups],
                         [("ethereum", FUNDER, 1), ("ethereum-one", ONE_SAFE, 2)])

    def write_claims(self, tmp, lines):
        path = os.path.join(tmp, "claims.csv")
        with open(path, "w", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["account", "token", "amount_raw", "pools"])
            writer.writerows(lines)
        return path

    def test_claim_list_validation(self):
        assets = {DAI: {}, ct.NATIVE_TOKEN: {}}
        with tempfile.TemporaryDirectory() as tmp:
            ok = self.write_claims(tmp, [[account(1), DAI, "5", "p"], [account(1), "native", "7", "q"]])
            rows = build.read_claims(ok, "ethereum", assets)
            self.assertEqual([r["token"] for r in rows], [DAI, ct.NATIVE_TOKEN])
            for bad in ([[account(1), DAI, "5", "p"], [account(1), DAI, "6", "p"]],
                        [[account(1), DAI, "0", "p"]],
                        [[account(1), USDT, "5", "p"]],
                        [["0x" + "00" * 20, DAI, "5", "p"]]):
                with self.assertRaises(ValueError):
                    build.read_claims(self.write_claims(tmp, bad), "ethereum", assets)

    def test_funding_sources(self):
        plan = {
            "chains": {"ethereum": {"transfers": [
                {"pool_id": "ethereum-dai", "escrow": "0x" + "e1" * 20, "amount_raw": "9"},
            ]}},
            "one_funding": {"from": "0x" + "e2" * 20, "pools": {"bsc-one": "3", "ethereum-one": "4"}},
        }
        sources = build.funding_sources(plan)
        self.assertEqual(sources["ethereum-dai"], [{"from": "0x" + "e1" * 20, "amount_raw": "9", "kind": "escrow"}])
        self.assertEqual([s["amount_raw"] for s in sources["bsc-one"] + sources["ethereum-one"]], ["3", "4"])

    def test_list_tree_and_proof_lookup(self):
        rows = [{"account": account(i % 5), "token": DAI if i % 2 else ct.NATIVE_TOKEN, "amount": 100 + i,
                 "pools": "p"} for i in range(10)]
        rows = list({(r["account"], r["token"]): r for r in rows}.values())
        assets = {DAI: {"symbol": "DAI", "decimals": 18, "pools": ["ethereum-dai"]},
                  ct.NATIVE_TOKEN: {"symbol": "ETH", "decimals": 18, "pools": ["ethereum-eth"]}}
        sources = {"ethereum-dai": [{"from": "0x" + "e1" * 20, "amount_raw": "1000", "kind": "escrow"}]}
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(output_dir=tmp, replace=False, workers=1)
            manifest = build.build_group(args, chains_constants(), "ethereum", "ethereum", FUNDER,
                                         "custody_multisig", rows, assets, None, sources)
            self.assertEqual(manifest["claim_count"], len(rows))
            self.assertEqual(manifest["deployment"]["status"], "no_bytecode")
            wrapped = [a for a in manifest["approvals"] if a["token"] == WETH]
            self.assertEqual(int(wrapped[0]["amount_raw"]),
                             sum(r["amount"] for r in rows if r["token"] == ct.NATIVE_TOKEN))
            dai = [a for a in manifest["approvals"] if a["token"] == DAI][0]
            self.assertEqual(dai["sources"], sources["ethereum-dai"])
            result = proof.lookup(os.path.join(tmp, "ethereum"), account(1))
            self.assertEqual(len(result["rows"]), len([r for r in rows if r["account"] == account(1)]))
            samples = json.loads(Path(tmp, "ethereum", "samples.json").read_text())
            self.assertEqual(sorted(r["token"] for r in samples["rows"]), sorted([DAI, ct.NATIVE_TOKEN]))


class ApproveTest(unittest.TestCase):
    def manifest(self):
        return {
            "label": "ethereum", "chain": "ethereum", "chain_id": 1, "funder": FUNDER, "wrapped_native": WETH,
            "root": "0x" + "ab" * 32, "claim_count": 3, "list_sha256": "cd" * 32,
            "assets": [
                {"token": ct.NATIVE_TOKEN, "symbol": "ETH", "decimals": 18, "total_raw": "5"},
                {"token": DAI, "symbol": "DAI", "decimals": 18, "total_raw": "7"},
                {"token": USDT, "symbol": "USDT", "decimals": 6, "total_raw": "11"},
            ],
            "approvals": [
                {"token": WETH, "symbol": "WETH", "decimals": 18, "amount_raw": "5", "amount": "5e-18"},
                {"token": DAI, "symbol": "DAI", "decimals": 18, "amount_raw": "7", "amount": "7e-18"},
                {"token": USDT, "symbol": "USDT", "decimals": 6, "amount_raw": "11", "amount": "0.000011"},
            ],
            "deployment": {
                "status": "ready", "address": SPENDER, "create2_deployer": ct.CREATE2_DEPLOYER,
                "salt": "0x" + "00" * 32, "constructor": {"funder": FUNDER},
            },
        }

    def build(self, tmp, nonce=7):
        manifest = self.manifest()
        Path(tmp, "manifest.json").write_text(json.dumps(manifest))
        args = SimpleNamespace(nonce=nonce, safe_version="1.3.0", no_wrap=False, replace=False)
        record = approve.build(args, {"custody_requirements": {}}, chains_constants(), manifest, tmp, None)
        return record, json.loads(Path(tmp, "safe-approve.json").read_text())

    def test_batch_wraps_resets_usdt_and_approves_exact_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, _ = self.build(tmp)
        calls = [(c["method"], c["to"], c["args"].get("value"), c["value"]) for c in record["approve"]["calls"]]
        self.assertEqual(calls, [
            ("deposit()", WETH, None, "5"),
            ("approve(address,uint256)", WETH, "5", "0"),
            ("approve(address,uint256)", DAI, "7", "0"),
            ("approve(address,uint256)", USDT, "0", "0"),
            ("approve(address,uint256)", USDT, "11", "0"),
        ])
        self.assertTrue(all(c["args"].get("spender", SPENDER) == SPENDER for c in record["approve"]["calls"]))
        self.assertEqual([c["args"]["value"] for c in record["revoke"]["calls"]], ["0", "0", "0"])

    def test_transaction_builder_file_and_safe_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, batch = self.build(tmp)
            revoke = json.loads(Path(tmp, "safe-revoke.json").read_text())
        for file in (batch, revoke):
            self.assertEqual(file["meta"]["checksum"], safe_batch.tx_builder_checksum(file))
            self.assertEqual(file["chainId"], "1")
        tx = record["approve"]["safe_transaction"]
        self.assertEqual(tx["operation"], safe_batch.DELEGATECALL)
        self.assertEqual(tx["to"].lower(), safe_batch.MULTISEND_CALL_ONLY["1.3.0"][0].lower())
        decoded = safe_batch.decode_multisend(ct.unhex(tx["data"]))
        self.assertEqual(
            [(op, to.lower(), value, common.hex0x(data)) for op, to, value, data in decoded],
            [(0, c["to"], int(c["value"]), c["data"]) for c in record["approve"]["calls"]],
        )
        self.assertEqual(tx["nonce"], 7)
        self.assertTrue(tx["safeTxHash"].startswith("0x") and len(tx["safeTxHash"]) == 66)

    def test_without_nonce_hashes_are_left_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, _ = self.build(tmp, nonce=None)
        self.assertIsNone(record["approve"]["safe_transaction"]["safeTxHash"])


class ReleaseTest(unittest.TestCase):
    # A real delivery the Ethereum USDC escrow accepted from Harmony (nonce 851): 200 USDC credited
    # to 0x47c7…84e1, which was both the payload's `to` and `from`.
    HISTORICAL_TO = "0x47c7901b7eb9db736da98b957ca3a7f8e6c984e1"
    HISTORICAL_PAYLOAD = (
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000080"
        "00000000000000000000000000000000000000000000000000000000000000c0"
        "000000000000000000000000000000000000000000000000000000000bebc200"
        "0000000000000000000000000000000000000000000000000000000000000014"
        "47c7901b7eb9db736da98b957ca3a7f8e6c984e1000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000014"
        "47c7901b7eb9db736da98b957ca3a7f8e6c984e1000000000000000000000000"
    )

    def test_release_payload_reproduces_a_real_delivery(self):
        self.assertEqual(
            release.release_payload(self.HISTORICAL_TO, 200_000_000, packet_type=True).hex(),
            self.HISTORICAL_PAYLOAD,
        )
        legacy = release.release_payload(self.HISTORICAL_TO, 10**17, packet_type=False)
        self.assertEqual(len(legacy), 128)
        self.assertEqual(int.from_bytes(legacy[0:32], "big"), 64)
        self.assertEqual(int.from_bytes(legacy[32:64], "big"), 10**17)
        self.assertEqual(int.from_bytes(legacy[64:96], "big"), 20)
        self.assertEqual(legacy[96:116], bytes.fromhex(self.HISTORICAL_TO[2:]))

    def test_owner_setup_and_one_reserve_calls(self):
        safe = "0x19565f4771843467aad632d6b56c75396785b06c"
        owners = [account(1), account(2), account(3), account(4)]
        calls = release.owner_setup_calls(safe, [owners[3]], owners, 2)
        self.assertEqual(
            [(c["args"]["owner"], c["args"]["_threshold"]) for c in calls],
            [(owners[2], "1"), (owners[1], "1"), (owners[0], "2")],
        )
        batch = approve.tx_builder(56, safe, calls, "owners", "owners")
        self.assertEqual(batch["meta"]["checksum"], safe_batch.tx_builder_checksum(batch))
        self.assertEqual(batch["transactions"][0]["contractMethod"]["name"], "addOwnerWithThreshold")

        one_call = release.one_reserve_call(ONE_TOKEN, safe, 500, "reserve")
        one_batch = approve.tx_builder(1, ONE_SAFE, [one_call], "one", "one")
        self.assertEqual(one_batch["meta"]["checksum"], safe_batch.tx_builder_checksum(one_batch))
        self.assertEqual(one_batch["transactions"][0]["contractMethod"]["name"], "transfer")
        self.assertEqual(one_batch["transactions"][0]["contractInputsValues"]["value"], "500")

    # A real ReceiveUln301 delivery on Ethereum (verify in 0xa3b8156e…, commitVerification in
    # 0x2b43f47b…): nonce 4151 from chain 106 to 0x31e4…d28f, payload `(bytes to, uint256 amount)`.
    ULN301_HEADER = (
        "01" "0000000000001037" "0000006a"
        "000000000000000000000000d402298a793948698b9a63311404fbbee944eafd"
        "00000065"
        "00000000000000000000000031e4efe290973ebe91b3a875a7994f650942d28f"
    )
    ULN301_GUID = "b864abb53d2cacc10172e4b3f64698ce056f179d8d33572d39aad28a96ca0366"
    ULN301_MESSAGE = (
        "0000000000000000000000000000000000000000000000000000000000000040"
        "0000000000000000000000000000000000000000000012bd70e6911282ced887"
        "0000000000000000000000000000000000000000000000000000000000000014"
        "199737907e02dda6970677eec41fedaddec3c539000000000000000000000000"
    )
    ULN301_PAYLOAD_HASH = "4ceb825dbd13867e92ffc74c93dc11f10763ddceb974e76ade40cdd994fd1e78"
    # `getAppUlnConfig` of an app that never set a config, as read on all five chains.
    UNSET_ULN_CONFIG = b"".join(x.to_bytes(32, "big") for x in (0x20, 0, 0, 0, 0, 0xC0, 0xE0, 0, 0))

    def test_packet_reproduces_a_real_uln301_delivery(self):
        header = release.packet_header(4151, 106, "0xd402298a793948698b9a63311404fbbee944eafd", 101,
                                       "0x31e4efe290973ebe91b3a875a7994f650942d28f")
        self.assertEqual(header.hex(), self.ULN301_HEADER)
        full, payload_hash = release.packet(header, bytes.fromhex(self.ULN301_MESSAGE))
        self.assertEqual(full.hex(), self.ULN301_HEADER + self.ULN301_GUID + self.ULN301_MESSAGE)
        self.assertEqual(payload_hash.hex(), self.ULN301_PAYLOAD_HASH)

    def test_uln_config_names_only_the_owner(self):
        dvn = account(7)
        words = [int.from_bytes(c, "big") for c in
                 (release.uln_config(dvn)[i:i + 32] for i in range(0, 320, 32))]
        self.assertEqual(len(release.uln_config(dvn)), 320)
        self.assertEqual(words, [0x20, 1, 1, 255, 0, 0xC0, 0x100, 1, int(dvn, 16), 0])

    def escrow(self, stored_version, trusted=b""):
        return {
            "pool_id": "ethereum-dai", "escrow": account(1), "owner": account(2), "lz_chain_id": 101,
            "locked_raw": "1000", "payload_format": "oft_v1_packet_type", "path_nonce": 1,
            "stored_receive_version": stored_version, "receive_version": 5, "receive_library": account(3),
            "previous_trusted_remote": common.hex0x(trusted),
            "previous_uln_config": common.hex0x(self.UNSET_ULN_CONFIG),
            "previous_executor": common.hex0x(bytes(32)),
        }

    def test_release_calls_restore_what_they_change(self):
        safe = "0x19565f4771843467aad632d6b56c75396785b06c"
        stale = release.packed(account(4), account(1))
        e = self.escrow(0, trusted=stale)
        calls = release.release_calls(e, safe)
        esc, lib_addr = e["escrow"], e["receive_library"]
        sel = {s: common.selector(s) for s in release.SEL}
        self.assertEqual([(c["to"], c["data"][:4]) for c in calls], [
            (esc, sel["setConfig(uint16,uint16,uint256,bytes)"]),
            (esc, sel["setConfig(uint16,uint16,uint256,bytes)"]),
            (esc, sel["setReceiveVersion(uint16)"]),
            (esc, sel["setTrustedRemote(uint16,bytes)"]),
            (lib_addr, sel["verify(bytes,bytes32,uint64)"]),
            (lib_addr, sel["commitVerification(bytes,uint256)"]),
            (esc, sel["setTrustedRemote(uint16,bytes)"]),
            (esc, sel["setReceiveVersion(uint16)"]),
            (esc, sel["setConfig(uint16,uint16,uint256,bytes)"]),
            (esc, sel["setConfig(uint16,uint16,uint256,bytes)"]),
        ])
        self.assertTrue(all(c["value"] == 0 for c in calls))
        enc = release.abi_encode

        def config(version, kind, value):
            return enc(["uint16", "uint16", "uint256", "bytes"], [version, 101, kind, value])

        self.assertEqual(calls[0]["data"][4:], config(5, 2, release.uln_config(e["owner"])))
        self.assertEqual(calls[1]["data"][4:], config(5, 1, bytes(12) + bytes.fromhex(e["owner"][2:])))
        self.assertEqual(calls[2]["data"][4:], enc(["uint16"], [5]))
        self.assertEqual(calls[3]["data"][4:], enc(["uint16", "bytes"], [101, release.packed(safe, esc)]))
        header, full, payload_hash = release.release_packet(e, safe)
        self.assertEqual(header, release.packet_header(1, 101, safe, 101, esc))
        self.assertEqual(full[81:113], common.keccak256(header[1:]))
        self.assertEqual(full[113:], release.release_payload(safe, 1000))
        self.assertEqual(calls[4]["data"][4:], enc(["bytes", "bytes32", "uint64"], [header, payload_hash, 1]))
        self.assertEqual(calls[5]["data"][4:], enc(["bytes", "uint256"], [full, release.DST_GAS]))
        self.assertEqual(calls[6]["data"][4:], enc(["uint16", "bytes"], [101, stale]))
        self.assertEqual(calls[7]["data"][4:], enc(["uint16"], [0]))
        self.assertEqual(calls[8]["data"][4:], config(5, 1, bytes(32)))
        self.assertEqual(calls[9]["data"][4:], config(5, 2, self.UNSET_ULN_CONFIG))

        pinned = release.release_calls(self.escrow(5), safe)
        self.assertEqual(len(pinned), 8)
        self.assertEqual(pinned[5]["data"][4:], enc(["uint16", "bytes"], [101, b""]))

    def test_receive_state_refuses_a_started_release(self):
        safe, esc, owner, endpoint, lib_addr = account(9), account(1), account(2), account(5), account(3)

        def word(x):
            return int(x, 16).to_bytes(32, "big") if isinstance(x, str) else int(x).to_bytes(32, "big")

        def answers(trusted=b"", uln=self.UNSET_ULN_CONFIG, executor=bytes(32)):
            sel = lambda s: common.hex0x(common.selector(s))  # noqa: E731
            return {
                (esc, sel("lzEndpoint()")): word(endpoint),
                (endpoint, sel("uaConfigLookup(address)")): word(0) + word(0) + word(0) + word(0),
                (endpoint, sel("getReceiveVersion(address)")): word(5),
                (endpoint, sel("libraryLookup(uint16)")): word(lib_addr),
                (lib_addr, sel("version()")): word(3) + word(0) + word(1),
                (endpoint, sel("getInboundNonce(uint16,bytes)")): word(0),
                (endpoint, sel("hasStoredPayload(uint16,bytes)")): word(0),
                (esc, sel("trustedRemoteLookup(uint16)")): release.abi_encode(["bytes"], [trusted]),
                (lib_addr, sel("getAppUlnConfig(address,uint32)")): uln,
                (lib_addr, sel("executors(address,uint32)")): executor,
            }

        class FakeRpc:
            def __init__(self, table):
                self.table = table

            def call_soft(self, method, params):
                return common.hex0x(self.table[(params[0]["to"], params[0]["data"][:10])]), None

        state = release.receive_state(FakeRpc(answers()), esc, owner, 101, safe)
        self.assertEqual((state["path_nonce"], state["receive_version"], state["stored_receive_version"]), (1, 5, 0))
        self.assertEqual(state["previous_trusted_remote"], "0x")
        for started in (answers(trusted=release.packed(safe, esc)),
                        answers(uln=release.uln_config(owner)),
                        answers(executor=bytes(12) + bytes.fromhex(owner[2:]))):
            with self.assertRaises(SystemExit):
                release.receive_state(FakeRpc(started), esc, owner, 101, safe)

    def test_sender_log_belongs_to_one_file_version(self):
        plan = {"chain": "arbitrum", "chain_id": 42161, "from": account(2), "transactions": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.json.sent.json")
            fresh = send.load_log(path, "aa", plan)
            self.assertEqual((fresh["sent"], fresh["from"]), ([], account(2)))
            send.save_log(path, fresh | {"sent": [{"index": 0, "tx_hash": "0x01", "nonce": 7}]})
            self.assertEqual(len(send.load_log(path, "aa", plan)["sent"]), 1)
            with self.assertRaises(SystemExit):
                send.load_log(path, "bb", plan)

    def test_handoff_readme_lists_hashes_gas_and_checks(self):
        safe = "0x19565f4771843467aad632d6b56c75396785b06c"
        native = self.escrow(0) | {"escrow": account(5), "kind": "native", "token": None, "symbol": "ETH",
                                   "decimals": 18, "locked_raw": str(10**17)}
        proxy = self.escrow(0) | {"kind": "proxy", "token": DAI, "symbol": "DAI", "decimals": 18}
        files = [{"chain": "ethereum", "file": "1-release/ethereum-0x0-eoa.json", "calls": 20,
                  "escrows": [native["escrow"], proxy["escrow"]]}]
        by_escrow = {("ethereum", e["escrow"]): e for e in (native, proxy)}
        text = release.handoff_readme(account(2), files, by_escrow, {"chains": {"ethereum": {"chain_id": 1}}},
                                      safe, {"ethereum-0x0-eoa.json": "ab" * 32}, {"ethereum": (10**9, 5 * 10**15)},
                                      "2026-10-22 00:00 UTC")
        self.assertIn("ab" * 32, text)
        self.assertIn("ETH 0.1", text)
        self.assertIn("| ethereum | 20 | 0.002 ETH | 0.005 ETH |", text)
        self.assertIn(f'cast call {native["escrow"]} "balanceOf(address)(uint256)" {native["escrow"]}', text)
        self.assertIn(f'cast call {DAI} "balanceOf(address)(uint256)" {proxy["escrow"]}', text)

    def test_decode_call_reads_back_each_release_call(self):
        safe = "0x19565f4771843467aad632d6b56c75396785b06c"
        e = self.escrow(0)
        esc, owner = e["escrow"], e["owner"]
        text = [release.decode_call(c["data"]) for c in release.release_calls(e, safe)]
        self.assertIn(f"required [`{owner}`], optional none, 1 confirmation", text[0])
        self.assertIn(f"executor for chain 101 = `{owner}`", text[1])
        self.assertIn("pin receive library version 5", text[2])
        self.assertIn(f"trust sender `{safe}` on chain 101", text[3])
        self.assertIn(f"attest message 1 from `{safe}` on chain 101 to `{esc}` on chain 101", text[4])
        self.assertIn(f"the escrow pays 1000 raw units to `{safe}`", text[5])
        self.assertIn("trust no sender on chain 101", text[6])
        self.assertIn("follow LayerZero's default receive library", text[7])
        self.assertIn("executor for chain 101 = unset", text[8])
        self.assertIn("verifier config for chain 101 = unset", text[9])
        legacy = release.release_calls(e | {"payload_format": "oft_v1_legacy"}, safe)[5]
        self.assertIn(f"pays 1000 raw units to `{safe}`", release.decode_call(legacy["data"]))

    def test_legacy_escrow_gets_the_two_tuple_payload(self):
        safe = "0x19565f4771843467aad632d6b56c75396785b06c"
        e = self.escrow(0) | {"payload_format": "oft_v1_legacy"}
        _, full, _ = release.release_packet(e, safe)
        self.assertEqual(full[113:], release.release_payload(safe, 1000, packet_type=False))


if __name__ == "__main__":
    unittest.main()
