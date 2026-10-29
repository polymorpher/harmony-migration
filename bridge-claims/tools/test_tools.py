#!/usr/bin/env python3
"""Tests for the claim tree, the list builder, the Safe approval batch, and the proof lookup.

    python3 tools/test_tools.py

The Solidity side of the same checks is `test/TreeFixture.t.sol`, which claims every row of a list
built here against the compiled contract.
"""

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claim_tree as ct  # noqa: E402
import approve_claims as approve  # noqa: E402
import bridge_lib as lib  # noqa: E402
import build_claims as build  # noqa: E402
import claims_run  # noqa: E402
import package  # noqa: E402
import proof  # noqa: E402
import quarantine  # noqa: E402
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


def account(i):
    return "0x" + common.keccak256(f"account-{i}".encode())[12:].hex()


def chains_constants():
    return ct.load_chains()


def write_run(run, contracts, inputs=None):
    """A claim run as build_claims.py lays it out: for each label a manifest, claims.csv, and
    tree.txt, and summary.json committing each manifest. Its published index is written beside the
    run as SHA256SUMS; returns that path."""
    entries = []
    for n, (label, fields) in enumerate(contracts.items()):
        out = Path(run, label)
        out.mkdir(parents=True, exist_ok=True)
        Path(out, "claims.csv").write_text(f"index,account\n0,{account(n)}\n")
        Path(out, "tree.txt").write_text(f"0x{n:064x}\n")
        manifest = {"label": label, "chain": label, "chain_id": 1, "funder": FUNDER, "root": f"0x{n + 1:064x}",
                    "claim_count": 1, "list_file": "claims.csv", "list_sha256": lib.file_sha256(Path(out, "claims.csv")),
                    "tree_file": "tree.txt", "tree_sha256": lib.file_sha256(Path(out, "tree.txt")), "approvals": [],
                    "deployment": {"status": "ready", "address": account(200 + n)}} | fields
        Path(out, "manifest.json").write_text(json.dumps(manifest))
        entries.append(claims_run.committed_fields(manifest))
    Path(run, "summary.json").write_text(json.dumps({"inputs": inputs or {}, "contracts": entries}))
    files = ["summary.json"] + [f"{label}/{name}" for label in contracts
                                for name in ("claims.csv", "manifest.json", "tree.txt")]
    index = Path(run).parent / "SHA256SUMS"
    index.write_text("".join(f"{lib.file_sha256(Path(run, f))}  {f}\n" for f in files))
    return str(index)


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
            result["address"] = account(9)
            self.assertIn(" --chain 1 ", proof.cast_command(result))

    def test_amounts_are_plain_decimals(self):
        cases = {
            (172715615, 18): "0.000000000172715615",
            (100, 18): "0.0000000000000001",
            (10 ** 18, 18): "1",
            (39336660, 6): "39.33666",
            (62177050468244920346866563, 18): "62177050.468244920346866563",
            (10 ** 40 + 1, 18): "10000000000000000000000.000000000000000001",
            (0, 6): "0",
        }
        for (raw, decimals), text in cases.items():
            self.assertEqual(build.format_amount(raw, decimals), text)


class BindTest(unittest.TestCase):
    """`claims_run.bind`: every summary entry bound one to one to its manifest and files."""

    def run_dir(self, tmp):
        run = os.path.join(tmp, "run")
        index = write_run(run, {"ethereum": {}, "bsc": {"chain_id": 56},
                                "base": {"deployment": {"status": "no_funder", "address": None}}})
        return run, index

    def test_a_coherent_run_binds(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, index = self.run_dir(tmp)
            bound = claims_run.bind(run, index=index)
            self.assertEqual([c.entry["label"] for c in bound.contracts], ["ethereum", "bsc"])
            self.assertEqual(bound.index["entries"], 10)
            self.assertEqual([c.entry["label"] for c in claims_run.bind(run, ["bsc"]).contracts], ["bsc"])

    def test_every_disagreement_stops_before_any_chain_read(self):
        def manifest(run, **changes):
            path = Path(run, "bsc", "manifest.json")
            data = json.loads(path.read_text())
            for key, value in changes.items():
                if key in ("address", "status"):
                    data["deployment"][key] = value
                else:
                    data[key] = value
            path.write_text(json.dumps(data))

        def summary(run, change):
            data = json.loads(Path(run, "summary.json").read_text())
            change(data)
            Path(run, "summary.json").write_text(json.dumps(data))

        cases = {
            "root": lambda run: manifest(run, root="0x" + "99" * 32),
            "chain id": lambda run: manifest(run, chain_id=97),
            "row count": lambda run: manifest(run, claim_count=2),
            "funder": lambda run: manifest(run, funder=account(9)),
            "label": lambda run: manifest(run, label="other"),
            "address": lambda run: manifest(run, address=account(8)),
            "status": lambda run: manifest(run, status="no_funder"),
            "tampered list": lambda run: Path(run, "bsc", "claims.csv").write_text("index,account\n0,x\n"),
            "tampered tree": lambda run: Path(run, "bsc", "tree.txt").write_text("0x00\n"),
            "list outside its directory": lambda run: manifest(run, list_file="../ethereum/claims.csv"),
            "two entries for a label": lambda run: summary(run, lambda s: s["contracts"].append(s["contracts"][1])),
            "label that is a path": lambda run: summary(run, lambda s: s["contracts"][1].update(label="../bsc")),
            "manifest outside the summary": lambda run: (Path(run, "extra").mkdir(),
                                                         Path(run, "extra", "manifest.json").write_text("{}")),
            "missing manifest": lambda run: Path(run, "bsc", "manifest.json").unlink(),
        }
        for name, change in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                run, _ = self.run_dir(tmp)
                change(run)
                with self.assertRaises(SystemExit):
                    claims_run.bind(run)

    def test_labels_must_be_ready_summary_entries_named_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, _ = self.run_dir(tmp)
            for labels in (["base"], ["missing"], ["bsc", "bsc"]):
                with self.subTest(labels), self.assertRaises(SystemExit):
                    claims_run.bind(run, labels)

    def test_the_run_must_match_its_published_index(self):
        def changed_digest(index):
            Path(index).write_text("".join(("f" * 64 + l[64:] if l.endswith("  bsc/manifest.json") else l) + "\n"
                                           for l in Path(index).read_text().splitlines()))

        def summary_not_listed(index):
            Path(index).write_text("".join(l + "\n" for l in Path(index).read_text().splitlines()
                                           if not l.endswith("  summary.json")))

        def listed_file_missing_here(index):
            Path(index).write_text(Path(index).read_text() + "0" * 64 + "  bsc/samples.json\n")

        def not_an_index(index):
            Path(index).write_text("not a checksum list\n")

        for change in (changed_digest, summary_not_listed, listed_file_missing_here, not_an_index):
            with self.subTest(change.__name__), tempfile.TemporaryDirectory() as tmp:
                run, index = self.run_dir(tmp)
                change(index)
                with self.assertRaises(SystemExit):
                    claims_run.bind(run, index=index)

    def test_roster_is_the_committed_one(self):
        committed = json.loads(claims_run.ROSTER_FILE.read_text())
        requirements = claims_run.custody_requirements({})
        self.assertEqual((requirements["safe"], requirements["threshold"]), (committed["safe"], 2))
        self.assertEqual(requirements["owners"], sorted(committed["owners"]))
        self.assertEqual(claims_run.custody_requirements({"custody_requirements": {"owners": []}})["owners"],
                         requirements["owners"])
        for configured in ({"owners": committed["owners"][:3] + [account(1)]}, {"threshold": 1}, {"owner_count": 5}):
            with self.subTest(configured), self.assertRaises(SystemExit):
                claims_run.custody_requirements({"custody_requirements": configured})


def words(*values):
    return "0x" + "".join(format(int(v), "064x") for v in values)


# A stand-in for the compiled contract: creation code, and runtime code with six zeroed immutables.
ARTIFACT = {
    "creation_code": bytes.fromhex("6080604052") + b"\x01" * 40,
    "runtime_template": b"".join(b"\xbb" + bytes(32) for _ in range(6)),
    "immutables": [[(1 + 33 * i, 32)] for i in range(6)],
}
ROSTER = [account(100 + i) for i in range(4)]
REQUIREMENTS = {"safe": FUNDER, "owner_count": 4, "threshold": 2, "owners": ROSTER}
ETH = "ethereum"


def label_dir(run, label=ETH):
    path = os.path.join(run, label)
    os.makedirs(path, exist_ok=True)
    return path
SINGLETON = next(iter(approve.SAFE_SINGLETONS))
MULTISEND = safe_batch.MULTISEND_CALL_ONLY["1.3.0"][0].lower()


def selector_hex(signature):
    return "0x" + common.selector(signature).hex()


class FakeChain(approve.Chain):
    """Answers the approval preflight like a correctly set up chain; tests change one thing at a time."""

    def __init__(self, manifest, bound):
        super().__init__(None)
        c = manifest["deployment"]["constructor"]
        self.spender = bound["address"]
        self.hashes = {
            FUNDER: approve.SAFE_PROXY_CODEHASH,
            SINGLETON: approve.SAFE_SINGLETONS[SINGLETON],
            MULTISEND: safe_batch.MULTISEND_CALL_ONLY_CODE_HASH["1.3.0"],
            ct.CREATE2_DEPLOYER: ct.CREATE2_DEPLOYER_CODEHASH,
        }
        self.slots = {"0x0": words(int(SINGLETON, 16)), approve.GUARD_SLOT: words(0),
                      approve.FALLBACK_HANDLER_SLOT: words(0xf48f2b2d2a534e402487b3ee7c18c33aec0fe5e4)}
        self.owners, self.threshold, self.nonce, self.modules = list(ROSTER), 2, 7, []
        self.balances = {WETH: 0, DAI: 7, USDT: 11}
        self.allowances = {WETH: 0, DAI: 0, USDT: 0}
        self.decimals = {WETH: 18, DAI: 18, USDT: 6}
        self.symbols = {WETH: "WETH", DAI: "DAI", USDT: "USDT"}
        self.native = 5
        self.final_hash = "0x" + "ab" * 32
        self.getters = {
            "funder()": words(int(c["funder"], 16)), "wrappedNative()": words(int(c["wrappedNative"], 16)),
            "chainId()": words(c["chainId"]), "root()": c["root"], "claimCount()": words(c["claimCount"]),
            "listHash()": c["listHash"],
        }

    def pin(self):
        return {"number": 100, "hash": "0x" + "ab" * 32}

    def block_hash(self, number):
        return self.final_hash

    def chain_id(self):
        return 1

    def code_hash(self, address):
        return self.hashes.get(lib.address(address), approve.EMPTY_CODEHASH)

    def storage(self, address, slot):
        return self.slots.get(slot, words(0)) if lib.address(address) == FUNDER else words(0)

    def native_balance(self, address):
        return self.native

    def call(self, to, data):
        to, sel = lib.address(to), data[:10].lower()
        if to == FUNDER:
            return {
                selector_hex("getOwners()"): words(32, len(self.owners), *[int(o, 16) for o in self.owners]),
                selector_hex("getThreshold()"): words(self.threshold),
                selector_hex("VERSION()"): words(32, 5) + b"1.3.0".hex().ljust(64, "0"),
                selector_hex("nonce()"): words(self.nonce),
                selector_hex("getModulesPaginated(address,uint256)"):
                    words(64, 1, len(self.modules), *[int(m, 16) for m in self.modules]),
            }.get(sel)
        if to in self.balances:
            if sel == selector_hex("balanceOf(address)"):
                return words(self.balances[to])
            if sel == selector_hex("allowance(address,address)"):
                return words(self.allowances[to])
            if sel == selector_hex("decimals()"):
                return words(self.decimals[to])
            if sel == selector_hex("symbol()"):
                text = self.symbols[to].encode()
                return words(32, len(text)) + text.hex().ljust(64, "0")
        if to == self.spender and self.code_hash(to) != approve.EMPTY_CODEHASH:
            return next((v for k, v in self.getters.items() if selector_hex(k) == sel), None)
        return None


class ApproveTest(unittest.TestCase):
    """Each test's temporary directory is a run; its contract is `ethereum/`."""

    def manifest(self, out):
        """A manifest bound to ARTIFACT the way build_claims.py writes it, with its claims.csv."""
        Path(out, "claims.csv").write_text("index,account,token,amount_raw\n0,a,b,1\n")
        list_sha = lib.file_sha256(os.path.join(out, "claims.csv"))
        root = "0x" + "ab" * 32
        args_blob = ct.constructor_args(FUNDER, WETH, 1, ct.unhex(root), 3, bytes.fromhex(list_sha))
        init_code = ARTIFACT["creation_code"] + args_blob
        return {
            "label": "ethereum", "chain": "ethereum", "chain_id": 1, "funder": FUNDER, "wrapped_native": WETH,
            "root": root, "claim_count": 3, "list_file": "claims.csv", "list_sha256": list_sha,
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
                "status": "ready", "create2_deployer": ct.CREATE2_DEPLOYER, "salt": "0x" + "00" * 32,
                "address": ct.create2_address(ct.CREATE2_DEPLOYER, bytes(32), init_code),
                "constructor": {"funder": FUNDER, "wrappedNative": WETH, "chainId": 1, "root": root,
                                "claimCount": 3, "listHash": "0x" + list_sha},
                "constructor_args": common.hex0x(args_blob),
                "bytecode_keccak256": common.hex0x(common.keccak256(ARTIFACT["creation_code"])),
                "init_code_keccak256": common.hex0x(common.keccak256(init_code)),
            },
        }

    def build(self, tmp, nonce=7, draft=True, chain=None, manifest=None, chains=None, artifact=ARTIFACT,
              requirements=REQUIREMENTS, replace=True):
        out = label_dir(tmp)
        manifest = manifest or self.manifest(out)
        Path(out, "manifest.json").write_text(json.dumps(manifest))
        args = SimpleNamespace(nonce=nonce, no_wrap=False, replace=replace, draft=draft, claims_dir=tmp)
        return approve.refresh(args, {"custody_requirements": requirements}, chains or chains_constants(),
                               [(manifest, out, chain)], artifact)[0]

    def fake_chain(self, tmp, label=ETH):
        out = label_dir(tmp, label)
        manifest = self.manifest(out)
        return manifest, FakeChain(manifest, approve.bound_deployment(manifest, chains_constants(), ARTIFACT, out))

    def test_batch_wraps_resets_usdt_and_approves_exact_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.build(tmp)
        calls = [(c["method"], c["to"], c["args"].get("value"), c["value"]) for c in record["approve"]["calls"]]
        self.assertEqual(calls, [
            ("deposit()", WETH, None, "5"),
            ("approve(address,uint256)", WETH, "5", "0"),
            ("approve(address,uint256)", DAI, "7", "0"),
            ("approve(address,uint256)", USDT, "0", "0"),
            ("approve(address,uint256)", USDT, "11", "0"),
        ])
        spender = record["spender"]
        self.assertTrue(all(c["args"].get("spender", spender) == spender for c in record["approve"]["calls"]))
        self.assertEqual([c["args"]["value"] for c in record["revoke"]["calls"]], ["0", "0", "0"])

    def test_transaction_builder_file_and_safe_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.build(tmp)
            batch = json.loads(Path(tmp, ETH, "approval-draft", "safe-approve.draft.json").read_text())
            revoke = json.loads(Path(tmp, ETH, "approval-draft", "safe-revoke.draft.json").read_text())
        for file in (batch, revoke):
            self.assertEqual(file["meta"]["checksum"], safe_batch.tx_builder_checksum(file))
            self.assertEqual(file["chainId"], "1")
        tx = record["approve"]["safe_transaction"]
        self.assertEqual(tx["operation"], safe_batch.DELEGATECALL)
        self.assertEqual(tx["to"].lower(), MULTISEND)
        decoded = safe_batch.decode_multisend(ct.unhex(tx["data"]))
        self.assertEqual(
            [(op, to.lower(), value, common.hex0x(data)) for op, to, value, data in decoded],
            [(0, c["to"], int(c["value"]), c["data"]) for c in record["approve"]["calls"]],
        )
        self.assertEqual(tx["nonce"], 7)
        self.assertTrue(tx["safeTxHash"].startswith("0x") and len(tx["safeTxHash"]) == 66)

    def test_without_nonce_hashes_are_left_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.build(tmp, nonce=None)
        self.assertIsNone(record["approve"]["safe_transaction"]["safeTxHash"])

    def test_spender_is_recomputed_from_the_bytecode(self):
        def eoa_spender(m, chains, artifact):
            m["deployment"]["address"] = account(50)

        def other_factory(m, chains, artifact):
            m["deployment"]["create2_deployer"] = account(51)

        def other_factory_in_chains_file(m, chains, artifact):
            chains["create2_deployer"] = account(51)

        def other_creation_code(m, chains, artifact):
            artifact["creation_code"] += b"\x00"

        def other_list(m, chains, artifact):
            m["list_sha256"] = "00" * 32

        def other_salt(m, chains, artifact):
            m["deployment"]["salt"] = "0x" + "01" * 32

        for change in (eoa_spender, other_factory, other_factory_in_chains_file, other_creation_code, other_list,
                       other_salt):
            with self.subTest(change.__name__), tempfile.TemporaryDirectory() as tmp:
                manifest, chains, artifact = self.manifest(label_dir(tmp)), chains_constants(), dict(ARTIFACT)
                change(manifest, chains, artifact)
                with self.assertRaises(SystemExit):
                    self.build(tmp, manifest=manifest, chains=chains, artifact=artifact)
                self.assertEqual(sorted(os.listdir(Path(tmp, ETH))), ["claims.csv", "manifest.json"])
                self.assertEqual(package.read_record(tmp)["packages"], {})

    def test_signable_files_when_every_check_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, chain = self.fake_chain(tmp)
            record = self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
            sheet = Path(tmp, ETH, "approval", "APPROVALS.md").read_text()
            names = sorted(os.listdir(Path(tmp, ETH, "approval")))
            top = sorted(os.listdir(Path(tmp, ETH)))
            _, _, results = package.verify(tmp)
        self.assertEqual([c for c in record["preflight"]["checks"] if not c["ok"]], [])
        self.assertEqual(record["approve"]["safe_transaction"]["nonce"], 7)
        self.assertFalse(record["draft"])
        self.assertEqual(names, ["APPROVALS.md", "GENERATION", "PACKAGE.sha256", "approvals.json",
                                 "safe-approve.json", "safe-revoke.json"])
        self.assertEqual(top, ["approval", "claims.csv", "manifest.json"])
        self.assertEqual([(rel, ok) for rel, ok, _ in results], [("ethereum/approval", True)])
        self.assertNotIn("DRAFT", sheet)
        self.assertIn("block 100", sheet)
        self.assertIn(record["spender_binding"]["runtime_keccak256"], sheet)

    def test_failed_check_or_missing_chain_blocks_signable_files(self):
        def getter_compatible_contract(c):
            c.hashes[c.spender] = "0x" + "66" * 32

        def no_roster(c):
            return {"owner_count": 4, "threshold": 2}

        def other_owner(c):
            c.owners[0] = account(99)

        def module(c):
            c.modules = [account(98)]

        def guard(c):
            c.slots[approve.GUARD_SLOT] = words(int(account(97), 16))

        def other_singleton(c):
            c.slots["0x0"] = words(int(account(96), 16))

        def other_multisend(c):
            c.hashes[MULTISEND] = "0x" + "77" * 32

        def other_factory_code(c):
            c.hashes[ct.CREATE2_DEPLOYER] = "0x" + "88" * 32

        def unfunded(c):
            c.balances[DAI] = 6

        def reorg(c):
            c.final_hash = "0x" + "cd" * 32

        def other_custody_safe(c):
            return REQUIREMENTS | {"safe": account(95)}

        cases = [(getter_compatible_contract, None), (no_roster, None), (other_owner, None), (module, None),
                 (guard, None), (other_singleton, None), (other_multisend, None), (other_factory_code, None),
                 (unfunded, None), (reorg, None), (other_custody_safe, None), (lambda c: None, 3)]
        for change, nonce in cases:
            name = getattr(change, "__name__", "nonce")
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                manifest, chain = self.fake_chain(tmp)
                requirements = change(chain) or REQUIREMENTS
                with self.assertRaises(SystemExit):
                    self.build(tmp, nonce=nonce, draft=False, chain=chain, manifest=manifest, requirements=requirements)
                self.assertEqual(sorted(os.listdir(Path(tmp, ETH))), ["claims.csv", "manifest.json"])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            self.build(tmp, draft=False, chain=None)

        with tempfile.TemporaryDirectory() as tmp:
            manifest, chain = self.fake_chain(tmp)
            chain.hashes[chain.spender] = approve.bound_deployment(manifest, chains_constants(), ARTIFACT,
                                                                   label_dir(tmp))["runtime_keccak256"]
            record = self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
        self.assertTrue(record["spender_deployed"])
        self.assertTrue(all(c["ok"] for c in record["preflight"]["checks"]))

    def test_draft_files_are_named_and_marked_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, chain = self.fake_chain(tmp)
            chain.balances[DAI] = 0
            record = self.build(tmp, nonce=None, draft=True, chain=chain, manifest=manifest)
            top = sorted(os.listdir(Path(tmp, ETH)))
            names = sorted(os.listdir(Path(tmp, ETH, "approval-draft")))
            sheet = Path(tmp, ETH, "approval-draft", "APPROVALS.draft.md").read_text()
            batch = json.loads(Path(tmp, ETH, "approval-draft", "safe-approve.draft.json").read_text())
        self.assertEqual(top, ["approval-draft", "claims.csv", "manifest.json"])
        self.assertEqual(names, ["APPROVALS.draft.md", "GENERATION", "PACKAGE.sha256", "approvals.draft.json",
                                 "safe-approve.draft.json", "safe-revoke.draft.json"])
        self.assertTrue(record["draft"])
        self.assertTrue(sheet.startswith("# DRAFT, do not sign"))
        self.assertIn("**FAILED**", sheet)
        self.assertTrue(batch["meta"]["name"].startswith("DRAFT, do not sign"))

    def test_nonzero_allowance_is_reset_first_for_any_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, chain = self.fake_chain(tmp)
            chain.allowances[DAI] = 3
            record = self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
        calls = [(c["to"], c["args"].get("value")) for c in record["approve"]["calls"] if c["method"] != "deposit()"]
        self.assertEqual(calls, [(WETH, "5"), (DAI, "0"), (DAI, "7"), (USDT, "11")])

    def test_token_decimals_are_checked_and_labels_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, chain = self.fake_chain(tmp)
            chain.symbols[USDT] = "USD₮0"
            self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
            sheet = Path(tmp, ETH, "approval", "APPROVALS.md").read_text()
            self.assertIn("USDT (its `symbol()`: `USD₮0`)", sheet)
            chain.decimals[USDT] = 18
            with self.assertRaises(SystemExit) as stopped:
                self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
            self.assertIn("USDT decimals", str(stopped.exception))

    def test_failed_refresh_moves_the_earlier_package_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, ETH)
            manifest, chain = self.fake_chain(tmp)
            self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
            stale = Path(out, "approval", "safe-approve.json").read_bytes()
            chain.balances[DAI] = 6
            with self.assertRaises(SystemExit):
                self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
            self.assertEqual(sorted(os.listdir(out)), ["claims.csv", "manifest.json", "superseded"])
            self.assertEqual(package.read_record(tmp)["packages"], {})
            [moved] = Path(out, "superseded").iterdir()
            self.assertEqual(Path(moved, "safe-approve.json").read_bytes(), stale)
            self.assertIn("DAI funded", Path(moved, "INVALIDATED.md").read_text())

            chain.balances[DAI] = 7
            self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
            self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
            self.assertEqual(len(list(Path(out, "superseded").iterdir())), 2)
            self.assertTrue(Path(out, "approval", "safe-approve.json").exists())
            self.assertFalse([p for p in os.listdir(out) if "staging" in p])
            with self.assertRaises(SystemExit):
                self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest, replace=False)
            self.assertTrue(Path(out, "approval", "safe-approve.json").exists())
            self.assertEqual([ok for _, ok, _ in package.verify(tmp)[2]], [True])

    def test_one_failing_contract_publishes_no_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = []
            for label in ("a", "b"):
                manifest, chain = self.fake_chain(tmp, label)
                Path(tmp, label, "manifest.json").write_text(json.dumps(manifest))
                jobs.append((manifest, os.path.join(tmp, label), chain))
            jobs[1][2].balances[DAI] = 6
            args = SimpleNamespace(nonce=None, no_wrap=False, replace=True, draft=False, claims_dir=tmp)
            with self.assertRaises(SystemExit):
                approve.refresh(args, {"custody_requirements": REQUIREMENTS}, chains_constants(), jobs, ARTIFACT)
            for label in ("a", "b"):
                self.assertEqual(sorted(os.listdir(Path(tmp, label))), ["claims.csv", "manifest.json"])
            jobs[1][2].balances[DAI] = 7
            approve.refresh(args, {"custody_requirements": REQUIREMENTS}, chains_constants(), jobs, ARTIFACT)
            for label in ("a", "b"):
                self.assertTrue(Path(tmp, label, "approval", "safe-approve.json").exists())
            self.assertEqual(sorted(package.read_record(tmp)["packages"]), ["a/approval", "b/approval"])

    def test_signable_files_from_before_packages_stop_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, chain = self.fake_chain(tmp)
            Path(tmp, ETH, "safe-approve.json").write_text("{}")
            with self.assertRaises(SystemExit) as stopped:
                self.build(tmp, nonce=None, draft=False, chain=chain, manifest=manifest)
            self.assertIn("quarantine", str(stopped.exception))
            self.assertFalse(Path(tmp, ETH, "approval").exists())
            self.build(tmp, nonce=None, draft=True, chain=chain, manifest=manifest)
            self.assertTrue(Path(tmp, ETH, "approval-draft").exists())


def publish(run, rels, text="old"):
    with package.staged(run, rels, replace=True) as staging:
        for rel in rels:
            Path(staging.dirs[rel], "x.json").write_text(f"{text} {rel}")


def current(run):
    return {rel: ok for rel, ok, _ in package.verify(run)[2]}


# Run in a child process: publish a/pkg and b/pkg over earlier ones and die right after the first
# staged package is renamed into place, as a killed process would.
CRASH_BETWEEN_RENAMES = """
import os, sys
sys.path.insert(0, sys.argv[1])
import package
real = os.replace
def replace(src, dst):
    real(src, dst)
    if "-staging-" in os.path.basename(src):
        os._exit(3)
os.replace = replace
with package.staged(sys.argv[2], ["a/pkg", "b/pkg"], replace=True) as staging:
    for target, directory in staging.dirs.items():
        open(os.path.join(directory, "x.json"), "w").write("new")
"""


class PackageTest(unittest.TestCase):
    def legacy_run(self, run):
        files = ["eth/manifest.json", "eth/claims.csv", "eth/safe-approve.json", "eth/APPROVALS.md",
                 "eth/safe-approve.draft.json", "eth/approval/safe-approve.json", "bsc/manifest.json",
                 "bsc/approvals.json", "release/0-one-reserve/ethereum-0xf868743b-safe.json",
                 "release/handoff/safe-signers/README.md", "release-built-x/1-release/a-eoa.json",
                 "release-built-x/1-release/a-eoa.json.sent.json", "summary.json"]
        for f in files:
            Path(run, f).parent.mkdir(parents=True, exist_ok=True)
            Path(run, f).write_text(f)
        return files

    def test_quarantine_moves_every_signable_file_and_records_it(self):
        with tempfile.TemporaryDirectory() as run:
            files = self.legacy_run(run)
            _, planned = quarantine.quarantine(run, "test", dry_run=True)
            self.assertTrue(Path(run, "eth", "safe-approve.json").exists())
            dest, moved = quarantine.quarantine(run, "rejected", include=["eth", "bsc/approvals.json"])
            self.assertEqual(moved, ["bsc/approvals.json", "eth", "release", "release-built-x"])
            self.assertEqual(sorted(planned), ["bsc/approvals.json", "eth/APPROVALS.md", "eth/approval",
                                               "eth/safe-approve.draft.json", "eth/safe-approve.json", "release",
                                               "release-built-x"])
            left = sorted(str(p.relative_to(run)) for p in Path(run).rglob("*") if p.is_file()
                          and quarantine.QUARANTINE not in p.parts)
            self.assertEqual(left, [package.LOCK, package.RECORD, "bsc/manifest.json", "summary.json"])
            record = Path(dest, "QUARANTINE.md").read_text()
            for f in files:
                if f not in left:
                    self.assertIn(f"| `{f}` | `{package.file_sha256(Path(dest, f))}` |", record)
            self.assertEqual(Path(dest, "eth", "approval", "safe-approve.json").read_text(),
                             "eth/approval/safe-approve.json")
            self.assertEqual(json.loads(Path(dest, quarantine.JOURNAL).read_text())["status"], "done")
            self.assertIsNone(package.read_record(run)["pending"])
            for bad in ("..", quarantine.QUARANTINE, "missing", package.RECORD):
                with self.assertRaises(SystemExit):
                    quarantine.quarantine(run, "x", include=[bad])

    def test_quarantine_unlists_the_packages_it_moves(self):
        with tempfile.TemporaryDirectory() as run:
            Path(run, "eth").mkdir()
            Path(run, "eth", "manifest.json").write_text("{}")
            publish(run, ["eth/approval", "release"])
            self.assertEqual(current(run), {"eth/approval": True, "release": True})
            quarantine.quarantine(run, "rejected")
            self.assertEqual(package.read_record(run)["packages"], {})
            self.assertEqual(current(run), {})

    def test_failed_quarantine_move_puts_everything_back(self):
        real = os.replace

        def replace(src, dst):
            if src.endswith(os.path.join("eth", "safe-approve.json")) and os.sep + quarantine.QUARANTINE + os.sep in dst:
                raise PermissionError("denied")
            return real(src, dst)

        with tempfile.TemporaryDirectory() as run:
            files = self.legacy_run(run)
            with mock.patch("os.replace", side_effect=replace), self.assertRaises(SystemExit) as stopped:
                quarantine.quarantine(run, "rejected")
            self.assertIn("everything was put back", str(stopped.exception))
            for f in files:
                self.assertEqual(Path(run, f).read_text(), f)
            [dest] = Path(run, quarantine.QUARANTINE).iterdir()
            self.assertEqual(json.loads(Path(dest, quarantine.JOURNAL).read_text())["status"], "rolled_back")
            self.assertFalse(Path(dest, "QUARANTINE.md").exists())
            self.assertIsNone(package.read_record(run)["pending"])
            quarantine.quarantine(run, "rejected")
            self.assertFalse(Path(run, "eth", "safe-approve.json").exists())

    def test_interrupted_quarantine_blocks_the_run_until_recovered(self):
        real = os.replace

        def replace(src, dst):
            if src.endswith(os.path.join("eth", "safe-approve.json")) and os.sep + quarantine.QUARANTINE + os.sep in dst:
                raise KeyboardInterrupt
            return real(src, dst)

        with tempfile.TemporaryDirectory() as run:
            files = self.legacy_run(run)
            publish(run, ["bsc/approval", "other/pkg"])
            with mock.patch("os.replace", side_effect=replace), self.assertRaises(KeyboardInterrupt):
                quarantine.quarantine(run, "rejected")
            self.assertEqual(package.read_record(run)["pending"]["operation"], "quarantine")
            self.assertEqual(current(run), {"other/pkg": False})
            for blocked in (lambda: publish(run, ["eth/approval"]), lambda: quarantine.quarantine(run, "again")):
                with self.assertRaises(SystemExit) as stopped:
                    blocked()
                self.assertIn("--recover", str(stopped.exception))
            dest, moved = quarantine.recover(run)
            self.assertIn("eth/safe-approve.json", moved)
            for f in files:
                if f.startswith("release") or "approv" in f.lower() or f.endswith("draft.json"):
                    self.assertFalse(Path(run, f).exists(), f)
                    self.assertEqual(Path(dest, f).read_text(), f)
            self.assertIn("eth/safe-approve.json", Path(dest, "QUARANTINE.md").read_text())
            self.assertIsNone(package.read_record(run)["pending"])
            self.assertEqual(current(run), {"other/pkg": True})

    def test_staged_packages_publish_whole_or_not_at_all(self):
        with tempfile.TemporaryDirectory() as run:
            a, b = "a/pkg", "b/pkg"
            with package.staged(run, [a, b], replace=False) as staging:
                for target in (a, b):
                    Path(staging.dirs[target], "x.json").write_text(target)
            x = package.file_sha256(Path(run, a, "x.json"))
            generation = Path(run, a, "GENERATION").read_text()
            self.assertEqual(Path(run, a, "PACKAGE.sha256").read_text(),
                             f"{package.file_sha256(Path(run, a, 'GENERATION'))}  GENERATION\n{x}  x.json\n")
            record = package.read_record(run)
            self.assertEqual(record["packages"][a]["generation"], generation.strip())
            self.assertEqual(record["packages"][a]["package_sha256"], package.file_sha256(Path(run, a, "PACKAGE.sha256")))
            self.assertEqual(current(run), {a: True, b: True})
            with self.assertRaises(SystemExit):
                with package.staged(run, [a], replace=False):
                    pass
            with self.assertRaises(RuntimeError):
                with package.staged(run, [a, b], replace=True) as staging:
                    Path(staging.dirs[a], "x.json").write_text("new")
                    raise RuntimeError("check failed")
            for target in (a, b):
                self.assertFalse(Path(run, target).exists())
                [moved] = Path(run, target).parent.joinpath("superseded").iterdir()
                self.assertEqual(Path(moved, "x.json").read_text(), target)
                self.assertIn("check failed", Path(moved, "INVALIDATED.md").read_text())
                self.assertEqual(sorted(os.listdir(Path(run, target).parent)), ["superseded"])
            self.assertEqual(package.read_record(run)["packages"], {})
            self.assertIsNone(package.read_record(run)["pending"])

    def test_one_lock_covers_every_refresh_and_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.join(tmp, "run")
            os.mkdir(run)
            os.symlink(run, os.path.join(tmp, "same-run"))
            with package.staged(run, ["a/approval"], replace=False) as staging:
                for other_run, targets in ((run, ["a/approval", "b/approval"]), (run, ["b/approval"]),
                                           (run, ["release"]), (os.path.join(tmp, "same-run"), ["b/approval"])):
                    with self.assertRaises(SystemExit) as stopped:
                        with package.staged(other_run, targets, replace=True):
                            pass
                    self.assertIn("another refresh or quarantine", str(stopped.exception))
                with self.assertRaises(SystemExit):
                    quarantine.quarantine(run, "x")
                self.assertEqual(package.read_record(run)["pending"]["targets"], ["a/approval"])
                Path(staging.dirs["a/approval"], "x.json").write_text("a")
            self.assertEqual(current(run), {"a/approval": True})

    def test_symlinks_from_the_run_down_are_refused(self):
        with tempfile.TemporaryDirectory() as run, tempfile.TemporaryDirectory() as elsewhere:
            os.symlink(elsewhere, os.path.join(run, "a"))
            os.mkdir(os.path.join(run, "b"))
            os.symlink(elsewhere, os.path.join(run, "b", "approval"))
            for target in ("a/approval", "b/approval", "../outside"):
                with self.subTest(target), self.assertRaises(SystemExit):
                    with package.staged(run, [target], replace=True):
                        pass
            self.assertEqual(os.listdir(elsewhere), [])
            with self.assertRaises(ValueError):
                with package.staged(run, ["c/approval"], replace=True) as staging:
                    os.symlink("/etc/hosts", os.path.join(staging.dirs["c/approval"], "hosts"))
            self.assertFalse(Path(run, "c", "approval").exists())
            self.assertEqual(package.read_record(run)["packages"], {})

    def test_crash_between_renames_leaves_nothing_current(self):
        with tempfile.TemporaryDirectory() as run:
            publish(run, ["a/pkg", "b/pkg", "c/pkg"])
            child = subprocess.run([sys.executable, "-c", CRASH_BETWEEN_RENAMES, str(Path(__file__).parent), run],
                                   capture_output=True, text=True)
            self.assertEqual(child.returncode, 3, child.stderr)
            self.assertEqual(Path(run, "a", "pkg", "x.json").read_text(), "new")
            record = package.read_record(run)
            self.assertEqual(record["pending"]["targets"], ["a/pkg", "b/pkg"])
            self.assertEqual(sorted(record["packages"]), ["c/pkg"])
            self.assertEqual(current(run), {"c/pkg": False, "a/pkg": False})
            publish(run, ["a/pkg", "b/pkg"], text="again")
            self.assertEqual(current(run), {"a/pkg": True, "b/pkg": True, "c/pkg": True})

    def test_a_package_that_cannot_be_moved_is_not_current(self):
        with tempfile.TemporaryDirectory() as run:
            publish(run, ["release", "a/approval"])
            with mock.patch.object(package, "set_aside", side_effect=PermissionError("denied")):
                with self.assertRaises(RuntimeError):
                    with package.staged(run, ["release"], replace=True):
                        raise RuntimeError("check failed")
            self.assertTrue(Path(run, "release", "x.json").exists())
            _, record, results = package.verify(run)
            self.assertIsNone(record["pending"])
            self.assertEqual({rel: ok for rel, ok, _ in results}, {"a/approval": True, "release": False})
            self.assertIn("not in PACKAGES.json", dict((r, d) for r, _, d in results)["release"][0])

    def test_a_refresh_that_cannot_write_the_record_changes_nothing(self):
        with tempfile.TemporaryDirectory() as run:
            publish(run, ["a/pkg"])
            before = package.read_record(run)
            with mock.patch.object(package, "write_record", side_effect=OSError("read-only")):
                with self.assertRaises(OSError):
                    with package.staged(run, ["a/pkg"], replace=True):
                        self.fail("nothing may be built")
            self.assertEqual(package.read_record(run), before)
            self.assertEqual(current(run), {"a/pkg": True})

    def test_verify_checks_every_file_and_names_unlisted_packages(self):
        with tempfile.TemporaryDirectory() as run:
            publish(run, ["a/approval", "release"])
            Path(run, "a", "approval", "x.json").write_text("changed")
            Path(run, "release", "extra.json").write_text("{}")
            Path(run, "b", "approval").mkdir(parents=True)
            Path(run, "b", "approval", "PACKAGE.sha256").write_text("")
            _, _, results = package.verify(run)
            details = {rel: (ok, d) for rel, ok, d in results}
            self.assertEqual({rel: ok for rel, (ok, _) in details.items()},
                             {"a/approval": False, "release": False, "b/approval": False})
            self.assertIn("x.json differs from PACKAGE.sha256", details["a/approval"][1])
            self.assertIn("extra.json is not in PACKAGE.sha256", details["release"][1])
            self.assertIn("not in PACKAGES.json", details["b/approval"][1][0])
            publish(run, ["a/approval"])
            self.assertFalse([r for r, _, _ in package.verify(run)[2] if "superseded" in r])


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
        need = release.units(20 * release.EOA_TX_GAS * 10**9, 18, 8)
        self.assertIn(f"| ethereum | 20 | {need} ETH | 0.005 ETH |", text)
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

    CUSTODY = "0x19565f4771843467aad632d6b56c75396785b06c"

    def claim_fixture(self, tmp, bsc_funder=None, one_approval=115):
        """Allocations and a claim run built from them, as release_plan.claim_run expects; the run's
        published index is `<tmp>/SHA256SUMS`."""
        alloc, claims = Path(tmp, "allocations"), Path(tmp, "run")
        alloc.mkdir()
        Path(alloc, "pools.csv").write_text("pool_id,collateral_raw,claimable_raw,held_raw,below_minimum_raw,"
                                            "retained_raw\nbsc-one,100,70,20,0,10\nethereum-one,50,45,4,0,1\n")
        Path(alloc, "custody-plan.json").write_text(json.dumps({
            "chains": {"ethereum": {"transfers": []}, "bsc": {"transfers": []}},
            "one_funding": {"chain": "ethereum", "from": "0x" + "e2" * 20, "token": ONE_TOKEN, "amount_raw": "150",
                            "pools": {"bsc-one": "100", "ethereum-one": "50"}},
        }))
        Path(alloc, "summary.json").write_text(json.dumps({"outputs": {
            name: lib.file_sha256(Path(alloc, name)) for name in ("pools.csv", "custody-plan.json")}}))
        write_run(claims, {
            "ethereum": {"chain": "ethereum", "funder": self.CUSTODY,
                         "approvals": [{"token": ONE_TOKEN, "amount_raw": str(one_approval)}]},
            "bsc": {"chain": "bsc", "chain_id": 56, "funder": bsc_funder or self.CUSTODY, "approvals": []},
        }, inputs={name: lib.file_sha256(Path(alloc, name)) for name in ("summary.json", "custody-plan.json")})
        return str(alloc), str(claims)

    def test_release_is_bound_to_the_ready_claim_manifests(self):
        config = {"one_claims": {"claim_token": ONE_TOKEN}}

        def run(tmp, **fixture):
            alloc, claims = self.claim_fixture(tmp, **fixture)
            plan = json.loads(Path(alloc, "custody-plan.json").read_text())
            return release.claim_run(claims, alloc, config, plan, self.CUSTODY, os.path.join(tmp, "SHA256SUMS"))

        with tempfile.TemporaryDirectory() as tmp:
            bound = run(tmp)
        self.assertEqual(sorted(bound["manifests_sha256"]), ["bsc", "ethereum"])
        self.assertEqual(bound["published_index"]["entries"], 7)
        for fixture in ({"bsc_funder": account(5)}, {"one_approval": 114}):
            with self.subTest(fixture), tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
                run(tmp, **fixture)

        def other_allocations(alloc, claims):
            Path(alloc, "summary.json").write_text(Path(alloc, "summary.json").read_text() + " ")

        def changed_pools(alloc, claims):
            Path(alloc, "pools.csv").write_text(Path(alloc, "pools.csv").read_text() + "\n")

        def contract_not_ready(alloc, claims):
            summary = json.loads(Path(claims, "summary.json").read_text())
            summary["contracts"][1]["status"] = "no_funder"
            Path(claims, "summary.json").write_text(json.dumps(summary))
            manifest = json.loads(Path(claims, "bsc", "manifest.json").read_text())
            manifest["deployment"]["status"] = "no_funder"
            Path(claims, "bsc", "manifest.json").write_text(json.dumps(manifest))

        def chain_outside_the_plan(alloc, claims):
            plan = json.loads(Path(alloc, "custody-plan.json").read_text())
            del plan["chains"]["bsc"]
            return plan

        for change in (other_allocations, changed_pools, contract_not_ready, chain_outside_the_plan):
            with self.subTest(change.__name__), tempfile.TemporaryDirectory() as tmp:
                alloc, claims = self.claim_fixture(tmp)
                plan = change(alloc, claims) or json.loads(Path(alloc, "custody-plan.json").read_text())
                with self.assertRaises(SystemExit):
                    release.claim_run(claims, alloc, config, plan, self.CUSTODY)

    def test_release_rejects_a_manifest_the_summary_does_not_commit(self):
        """The second-pass review's case: summary unchanged, while the manifest's root, chain id, row
        count, list hash, and address change together with its claims.csv."""
        config = {"one_claims": {"claim_token": ONE_TOKEN}}
        with tempfile.TemporaryDirectory() as tmp:
            alloc, claims = self.claim_fixture(tmp)
            path = Path(claims, "bsc", "manifest.json")
            manifest = json.loads(path.read_text())
            Path(claims, "bsc", "claims.csv").write_text("index,account\n0,someone else\n")
            manifest.update(root="0x" + "99" * 32, chain_id=97, claim_count=2,
                            list_sha256=lib.file_sha256(Path(claims, "bsc", "claims.csv")))
            manifest["deployment"]["address"] = account(77)
            path.write_text(json.dumps(manifest))
            plan = json.loads(Path(alloc, "custody-plan.json").read_text())
            with self.assertRaises(SystemExit) as stopped:
                release.claim_run(claims, alloc, config, plan, self.CUSTODY)
            for field in ("root", "chain_id", "claim_count", "list_sha256", "address"):
                self.assertIn(f"bsc: summary {field}", str(stopped.exception))

    def test_reads_come_from_one_block_checked_again(self):
        class Upstream:
            def __init__(self):
                self.hash, self.seen = "0x" + "aa" * 32, []

            def call(self, method, params):
                self.seen.append((method, params))
                if method == "eth_getBlockByNumber":
                    return {"number": "0x64", "hash": self.hash}
                return "0x"

            def call_soft(self, method, params):
                self.seen.append((method, params))
                return "0x", None

        upstream = Upstream()
        reads = release.Reads({}, connect=lambda config, chain: upstream)
        rpc = reads("ethereum")
        release.read_safe_info(rpc, self.CUSTODY)
        approve.Chain(rpc).code(self.CUSTODY)
        rpc.call_soft("eth_call", [{"to": self.CUSTODY, "data": "0x"}, "latest"])
        self.assertEqual(reads.blocks(), {"ethereum": {"number": 100, "hash": "0x" + "aa" * 32}})
        self.assertFalse([p for _, p in upstream.seen if "latest" in p][1:])
        self.assertTrue(all("0x64" in p for m, p in upstream.seen[1:] if m != "eth_getBlockByNumber"))
        reads.recheck()
        upstream.hash = "0x" + "bb" * 32
        with self.assertRaises(SystemExit):
            reads.recheck()

    def run_planner(self, alloc, claims, *extra):
        argv = ["release_plan.py", "--routes", str(ct.REPO / "bridge" / "routes.json"),
                "--config", str(ct.REPO / "bridge" / "config.example.json"),
                "--allocations-dir", alloc, "--claims-dir", claims, *extra]
        saved = sys.argv
        try:
            sys.argv = argv
            release.main()
        finally:
            sys.argv = saved

    def test_failed_release_build_moves_the_stale_package_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            alloc, claims = self.claim_fixture(tmp)
            roster = Path(tmp, "roster.json")
            roster.write_text(claims_run.ROSTER_FILE.read_text().replace(self.CUSTODY, account(5)))
            planner = ("--published-index", os.path.join(tmp, "SHA256SUMS"), "--roster", str(roster))
            stale = Path(claims, "release", "0-one-reserve", "ethereum-0xf868743b-safe.json")
            stale.parent.mkdir(parents=True)
            stale.write_text('{"rejected": true}')
            with self.assertRaises(SystemExit):
                self.run_planner(alloc, claims, *planner)
            self.assertTrue(stale.exists())
            with self.assertRaises(SystemExit) as stopped:
                self.run_planner(alloc, claims, *planner, "--replace")
            self.assertIn("custody roster's Safe", str(stopped.exception))
            self.assertFalse(Path(claims, "release").exists())
            [moved] = Path(claims, "superseded").iterdir()
            self.assertEqual(Path(moved, "0-one-reserve", "ethereum-0xf868743b-safe.json").read_text(),
                             '{"rejected": true}')
            self.assertIn("custody roster's Safe", Path(moved, "INVALIDATED.md").read_text())
            self.assertEqual(sorted(os.listdir(claims)), [package.LOCK, package.RECORD, "bsc", "ethereum",
                                                          "summary.json", "superseded"])
            for outside in (os.path.join(tmp, "release"), os.path.join(claims, "plan")):
                with self.subTest(outside), self.assertRaises(SystemExit) as stopped:
                    self.run_planner(alloc, claims, *planner, "--output-dir", outside)
                self.assertIn("release/ or release-<name>/", str(stopped.exception))

    def test_one_split_matches_the_funded_collateral(self):
        header = "pool_id,collateral_raw,claimable_raw,held_raw,below_minimum_raw,retained_raw\n"
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pools.csv").write_text(header + "bsc-one,100,70,20,0,10\nethereum-one,50,45,4,0,1\n")
            split = release.one_split(tmp, {"pools": {"bsc-one": "100", "ethereum-one": "50"}})
            self.assertEqual(sum(p["claimable_raw"] for p in split.values()), 115)
            self.assertEqual(split["bsc-one"]["held_raw"] + split["bsc-one"]["retained_raw"], 30)
            for funded in ({"bsc-one": "101"}, {"missing-one": "5"}):
                with self.assertRaises(SystemExit):
                    release.one_split(tmp, {"pools": funded})
            Path(tmp, "pools.csv").write_text(header + "bsc-one,100,70,20,0,9\n")
            with self.assertRaises(SystemExit):
                release.one_split(tmp, {"pools": {"bsc-one": "100"}})

    def test_recipient_is_the_configured_custody_multisig(self):
        safe = "0x19565f4771843467aad632d6b56c75396785b06c"
        plan = {"chains": {"ethereum": {}, "bsc": {}}}

        def config(bsc=safe, one=None):
            return {"chains": {"ethereum": {"custody_multisig": safe}, "bsc": {"custody_multisig": bsc}},
                    "one_claims": {"claim_chain": "ethereum", "custody_multisig": one}}

        self.assertEqual(release.custody_safe(config(), plan), safe)
        self.assertEqual(release.custody_safe(config(), plan, safe.upper().replace("0X", "0x")), safe)
        for bad, requested in ((config(bsc=account(1)), None), (config(bsc=None), None),
                               (config(one=account(2)), None), (config(), account(3))):
            with self.assertRaises(SystemExit):
                release.custody_safe(bad, plan, requested)

    def test_custody_safe_must_be_official_and_owned_by_the_roster(self):
        roster = [account(i) for i in range(4)]
        identity = {"code_hash": approve.SAFE_PROXY_CODEHASH, "singleton": SINGLETON,
                    "singleton_code_hash": approve.SAFE_SINGLETONS[SINGLETON], "version": "1.3.0", "modules": [],
                    "guard": lib.ZERO_ADDRESS, "fallback_handler": lib.ZERO_ADDRESS}

        def problems(owners, threshold, **changes):
            info = {"is_safe": True, "owners": owners, "threshold": threshold}
            found = release.custody_problems("bsc", info, identity | changes, roster, 2)
            return info["status"], found

        self.assertEqual(problems(list(reversed(roster)), 2), ("ready", []))
        self.assertEqual(problems(roster[:1], 1), ("needs_owner_setup", []))
        self.assertEqual(problems(roster[:3] + [account(9)], 2)[0], "unexpected_owners")
        self.assertEqual(problems([account(9)], 1)[0], "unexpected_owners")
        self.assertEqual(len(problems(roster, 2, modules=[account(8)])[1]), 1)
        self.assertEqual(len(problems(roster, 2, code_hash="0x" + "00" * 32)[1]), 1)
        self.assertEqual(release.custody_problems("bsc", {"is_safe": False}, None, roster, 2),
                         ["bsc: the custody multisig is not a Safe"])

    def test_legacy_escrow_gets_the_two_tuple_payload(self):
        safe = "0x19565f4771843467aad632d6b56c75396785b06c"
        e = self.escrow(0) | {"payload_format": "oft_v1_legacy"}
        _, full, _ = release.release_packet(e, safe)
        self.assertEqual(full[113:], release.release_payload(safe, 1000, packet_type=False))


if __name__ == "__main__":
    unittest.main()
