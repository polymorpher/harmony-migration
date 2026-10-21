import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
ARCHIVE = ROOT / "toolkit" / "scripts" / "archive"
sys.path.insert(0, str(ARCHIVE))
import archive_lib as lib  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ARCHIVE / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCAN = load_script("scan-blocks.py")
TRACE = load_script("trace-internal.py")

ALICE = "0x" + "01" * 20
BOB = "0x" + "02" * 20
POOL = "0x" + "03" * 20
CHILD = "0x" + "04" * 20
ONE = lib.ONE


def xshard_input(value, to, shard):
    return ("0x28480800" + format(value, "064x") + to[2:].rjust(64, "0") + format(shard, "064x"))


def frame(position, path, kind="call", call_type="call", sender=POOL, to=BOB, value=0, data="0x",
          error=None, block_hash="0xb1"):
    action = {"from": sender, "gas": "0x0"}
    if kind == "call":
        action.update(callType=call_type, to=to, input=data)
        if call_type not in ("delegatecall", "staticcall"):
            action["value"] = hex(value)
    elif kind == "create":
        action.update(init=data, value=hex(value))
    elif kind == "suicide":
        action = {"address": sender, "refundAddress": to, "balance": hex(value)}
    out = {"blockHash": block_hash, "transactionHash": "0xe" + str(position), "transactionPosition": position,
           "traceAddress": list(path), "type": kind, "action": action}
    if kind == "create":
        out["result"] = {"address": to}
    if error:
        out["error"] = error
    return out


def tx(index, to, value=0, data="0x", shard=0, to_shard=0, tx_hash=None):
    return {"transactionIndex": index, "hash": tx_hash or f"0xh{index}", "ethHash": f"0xeth{index}",
            "from": ALICE, "to": to, "value": value, "input": data, "shardID": shard, "toShardID": to_shard}


class ClassificationTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(lib.classify_tx(tx(0, BOB, to_shard=1), BOB), "xshard_native")
        self.assertEqual(lib.classify_tx(tx(0, lib.XSHARD_PRECOMPILE, data="0x28"), lib.XSHARD_PRECOMPILE),
                         "xshard_precompile")
        self.assertEqual(lib.classify_tx(tx(0, ""), ""), "create")
        self.assertEqual(lib.classify_tx(tx(0, BOB), BOB), "transfer")
        self.assertEqual(lib.classify_tx(tx(0, POOL, data="0xa9059cbb"), POOL), "call")

    def test_decode_xshard_input(self):
        decoded = lib.decode_xshard_input(xshard_input(5 * ONE, BOB, 1))
        self.assertEqual(decoded, {"value": 5 * ONE, "to": BOB, "to_shard": 1})
        self.assertIsNone(lib.decode_xshard_input("0x28480800"))

    def test_address_accepts_bech32(self):
        self.assertEqual(lib.address(lib.crl.hex_to_bech32(ALICE)), ALICE)
        self.assertEqual(lib.address(ALICE.upper().replace("0X", "0x")), ALICE)
        self.assertEqual(lib.address(""), "")
        with self.assertRaises(ValueError):
            lib.address("one1invalid")

    def test_staking_amount(self):
        self.assertEqual(lib.staking_amount({"type": "Delegate", "msg": {"amount": 7 * ONE}}, None), 7 * ONE)
        receipt = {"logs": [{"topics": [lib.COLLECT_REWARDS_TOPIC], "data": hex(3 * ONE)}]}
        self.assertEqual(lib.staking_amount({"type": "CollectRewards", "msg": {}}, receipt), 3 * ONE)
        self.assertIsNone(lib.staking_amount({"type": "EditValidator", "msg": {}}, {"logs": []}))


class SignedValueTest(unittest.TestCase):
    # A mainnet transaction from the abandoned August 2026 branch. An RPC gateway that
    # parsed numbers as floating point reported its value as 5000000522000000000000000000.
    TX = {"hash": "0x007d94bb03b47b2089b65f368f4af58bf484a5cf3ae6bf97dfd4ab11247bdb4b",
          "ethHash": "0x0b7143734d9d971f2e12d79bf4f5648b8b79d458642dfefab3404ab0023ef309",
          "nonce": 21663, "gasPrice": 100000000000, "gas": 1200000, "shardID": 0, "toShardID": 0,
          "to": "one1qcd8ul330t8y2z2q6kknxukmpd7zqh0yyyzpm8", "input": "0xd0e30db0", "v": "0xc6ac98a4",
          "r": "0x61ba4fea94e0ebc03c5e72df5dc2e70e396dcbf47071b7c32283fb3b96ef25e2",
          "s": "0x66b423b9c4146e9612c542e00d17d4f7e208f26d353ef3340afc303a1410e146"}

    def test_only_the_signed_value_reproduces_the_hash(self):
        self.assertTrue(lib.signed_value_matches(self.TX, 5000000522000000533215051776))
        self.assertFalse(lib.signed_value_matches(self.TX, 5000000522000000000000000000))

    def test_rlp_encoding(self):
        self.assertEqual(lib.rlp_encode(0), b"\x80")
        self.assertEqual(lib.rlp_encode(b"\x05"), b"\x05")
        self.assertEqual(lib.rlp_encode([]), b"\xc0")
        self.assertEqual(lib.rlp_encode(b"a" * 60)[:2], b"\xb8\x3c")


def signed_tx(index, block, to, value, nonce, to_shard=0, data="0x"):
    """A plain transaction whose hash is computed from its fields."""
    tx = {"blockNumber": block["number"], "blockHash": block["hash"], "transactionIndex": index,
          "from": lib.crl.hex_to_bech32(ALICE), "to": lib.crl.hex_to_bech32(to), "value": value, "input": data,
          "nonce": nonce, "gasPrice": 10 ** 11, "gas": 21000, "shardID": 0, "toShardID": to_shard,
          "v": "0x25", "r": "0x" + "11" * 32, "s": "0x" + "22" * 32, "ethHash": ""}
    fields = [nonce, 10 ** 11, 21000, 0, to_shard, bytes.fromhex(to[2:]), value, b"", 0x25,
              int("11" * 32, 16), int("22" * 32, 16)]
    tx["hash"] = "0x" + lib.crl.keccak256(lib.rlp_encode(fields)).hex()
    return tx


class ImportExportTest(unittest.TestCase):
    FORK = "0x" + "aa" * 32

    def write_export(self, root, tamper=None, wrong_value=False):
        directory = root / "export"
        directory.mkdir()
        hashes = [self.FORK] + ["0x" + format(n, "064x") for n in (101, 102, 103)]
        blocks = [{"number": 100 + i, "hash": hashes[i], "parentHash": hashes[i - 1], "timestamp": 1786500000 + i,
                   "epoch": 3003, "transactions": [], "stakingTransactions": []} for i in (1, 2, 3)]
        early = signed_tx(0, blocks[0], BOB, 7 * ONE, 1)
        transfer = signed_tx(0, blocks[1], BOB, 3 * ONE, 2)
        native = signed_tx(1, blocks[1], BOB, 5 * ONE, 3, to_shard=1)
        if wrong_value:
            transfer["value"] += 1
        rewards = {"type": "CollectRewards", "hash": "0x" + "ee" * 32, "from": lib.crl.hex_to_bech32(ALICE),
                   "transactionIndex": 0, "gasPrice": 10 ** 11, "msg": {"delegatorAddress": ALICE},
                   "blockHash": blocks[2]["hash"], "blockNumber": 103}
        blocks[0]["transactions"] = [early]
        blocks[1]["transactions"] = [transfer, native]
        blocks[2]["stakingTransactions"] = [rewards]
        (directory / "blocks_101_103.json").write_text(json.dumps({"jsonrpc": "2.0", "id": 1, "result": blocks}))
        receipts = [{"transactionHash": tx["hash"], "blockHash": tx["blockHash"], "status": 1, "gasUsed": 21000,
                     "logs": []} for tx in (early, transfer, native)]
        receipts.append({"transactionHash": rewards["hash"], "blockHash": blocks[2]["hash"], "status": 1,
                         "gasUsed": 30000, "logs": [{"topics": [lib.COLLECT_REWARDS_TOPIC], "data": hex(4 * ONE)}]})
        (directory / "receipts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in receipts))
        files = [{"path": p.name, "bytes": p.stat().st_size, "sha256": lib.file_sha256(p)}
                 for p in sorted(directory.iterdir())]
        (directory / "source_manifest.json").write_text(json.dumps({
            "blockRange": {"start": 101, "end": 103, "firstHash": hashes[1], "lastHash": hashes[3]},
            "counts": {"regularTransactions": 3, "stakingTransactions": 1, "receipts": 4},
            "files": files, "source": "Harmony shard-0 JSON-RPC"}))
        if tamper:
            with open(directory / tamper, "a") as handle:
                handle.write("\n")
        return directory, hashes

    def run_import(self, root, directory, *extra):
        command = [sys.executable, str(ARCHIVE / "import-rpc-export.py"), "--export", str(directory),
                   "--shard", "0", "--branch", "rolled_back", "--fork", f"100:{self.FORK}",
                   "--out", str(root / "run"), *extra]
        return subprocess.run(command, capture_output=True, text=True)

    def test_imports_a_verified_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, hashes = self.write_export(root)
            result = self.run_import(root, directory, "--start", "102")
            self.assertEqual(result.returncode, 0, result.stderr)
            run = root / "run"
            txs = lib.read_csv(run / "transactions.csv")
            self.assertEqual([(r["block_number"], r["kind"], r["value_atto"]) for r in txs],
                             [("102", "transfer", str(3 * ONE)), ("102", "xshard_native", str(5 * ONE))])
            staking = lib.read_csv(run / "staking.csv")
            self.assertEqual([(r["type"], r["amount_atto"]) for r in staking], [("CollectRewards", str(4 * ONE))])
            receipts = lib.read_csv(run / "receipts" / "outgoing-receipts.csv")
            self.assertEqual([(r["mechanism"], r["to_shard"]) for r in receipts], [("native", "1")])
            stage = json.loads((run / "stage.json").read_text())
            self.assertEqual(stage["identity"]["stage"], "import-rpc-export")
            self.assertEqual({a["block"]: a["hash"] for a in stage["identity"]["anchors"]},
                             {100: self.FORK, 101: hashes[1]})
            self.assertEqual(stage["boundaries"]["start"]["parent_hash"], hashes[1])
            self.assertEqual(stage["counters"]["tx_ge_min_signature_verified"], 2)
            self.assertTrue(stage["internal_unavailable"])

    def test_rejects_tampered_files_and_unsigned_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, _ = self.write_export(root, tamper="receipts.jsonl")
            self.assertIn("SHA-256", self.run_import(root, directory).stderr)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, _ = self.write_export(root, wrong_value=True)
            self.assertIn("does not reproduce its hash", self.run_import(root, directory).stderr)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, _ = self.write_export(root)
            result = subprocess.run([sys.executable, str(ARCHIVE / "import-rpc-export.py"), "--export",
                                     str(directory), "--shard", "0", "--branch", "rolled_back",
                                     "--fork", "100:0x" + "bb" * 32, "--out", str(root / "run")],
                                    capture_output=True, text=True)
            self.assertIn("does not follow", result.stderr)


class InternalTransferTest(unittest.TestCase):
    def test_value_frames_and_reverts(self):
        frames = [
            frame(0, [], value=10 * ONE, to=POOL, sender=ALICE),
            frame(0, [0], value=2 * ONE),
            frame(0, [1], call_type="delegatecall", to=CHILD),
            frame(0, [2], value=4 * ONE, error="Reverted"),
            frame(0, [2, 0], value=1 * ONE),
            frame(0, [3], kind="suicide", sender=POOL, to=ALICE, value=6 * ONE),
            frame(0, [4], kind="create", to=CHILD, value=ONE),
            frame(0, [5], value=0),
        ]
        rows, counters = lib.internal_transfers(frames)
        self.assertEqual(counters, {"frames": 8, "internal_value_gt0": 5})
        by_path = {row["trace_address"]: row for row in rows}
        self.assertEqual(set(by_path), {"0", "2", "2.0", "3", "4"})
        self.assertEqual(by_path["0"]["effective"], 1)
        self.assertEqual(by_path["2"]["effective"], 0)
        self.assertEqual(by_path["2.0"]["effective"], 0)
        self.assertEqual((by_path["3"]["type"], by_path["3"]["to"], by_path["3"]["value_atto"]),
                         ("selfdestruct", ALICE, 6 * ONE))
        self.assertEqual((by_path["4"]["type"], by_path["4"]["to"]), ("create", CHILD))

    def test_flatten_block_trace_shapes(self):
        a, b, c = frame(0, []), frame(1, []), frame(2, [])
        self.assertEqual(lib.flatten_block_trace([{"result": [a]}, [b], c]), [a, b, c])
        with self.assertRaises(lib.RpcError):
            lib.flatten_block_trace([{"error": "execution timeout"}])

    def test_root_revert_makes_children_ineffective(self):
        rows, _ = lib.internal_transfers([frame(1, [], error="Reverted"), frame(1, [0], value=ONE)])
        self.assertEqual(rows[0]["effective"], 0)


class OutgoingReceiptTest(unittest.TestCase):
    def block(self, txs, epoch=2000):
        return {"number": 100, "hash": "0xb1", "timestamp": 1700000000, "epoch": epoch, "transactions": txs}

    def test_native_transaction(self):
        rows, fallback = lib.outgoing_receipts(self.block([tx(0, BOB, 9 * ONE, to_shard=1)]), [], 0)
        self.assertFalse(fallback)
        self.assertEqual([(r["mechanism"], r["to_shard"], r["value_atto"]) for r in rows],
                         [("native", 1, 9 * ONE)])

    def test_constructor_receipt_survives_revert_before_fix(self):
        data = xshard_input(3 * ONE, ALICE, 1)
        frames = [
            frame(0, [], to=POOL, sender=ALICE, value=3 * ONE),
            frame(0, [0], kind="create", sender=POOL, to=CHILD, value=3 * ONE, error="Reverted"),
            frame(0, [0, 0], to=lib.XSHARD_PRECOMPILE, sender=CHILD, value=3 * ONE, data=data),
        ]
        rows, fallback = lib.outgoing_receipts(self.block([tx(0, POOL, 3 * ONE)]), frames, 0)
        self.assertFalse(fallback)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["mechanism"], rows[0]["depth"], rows[0]["ancestor_reverted"],
                          rows[0]["to"], rows[0]["tx_hash"], rows[0]["eth_hash"]),
                         ("precompile", 2, 1, ALICE, "0xh0", "0xeth0"))

        after_fix, fallback = lib.outgoing_receipts(self.block([tx(0, POOL, 3 * ONE)], epoch=2964), frames, 0)
        self.assertTrue(fallback)
        self.assertEqual(after_fix[0]["mechanism"], "precompile_replay_incompatible")

    def test_replay_incompatible_delegatecall(self):
        data = xshard_input(25 * ONE, ALICE, 1)
        frames = [
            frame(0, [], to=POOL, sender=ALICE, error="Reverted"),
            frame(0, [0], call_type="delegatecall", sender=POOL, to=lib.XSHARD_PRECOMPILE, data=data,
                  error="Reverted"),
        ]
        rows, fallback = lib.outgoing_receipts(self.block([tx(0, POOL)]), frames, 0)
        self.assertTrue(fallback)
        self.assertEqual((rows[0]["mechanism"], rows[0]["value_atto"]),
                         ("precompile_replay_incompatible_delegatecall", 25 * ONE))

    def test_without_trace_reads_direct_precompile_calls(self):
        data = xshard_input(2 * ONE, BOB, 1)
        rows, _ = lib.outgoing_receipts(self.block([tx(0, lib.XSHARD_PRECOMPILE, 2 * ONE, data)]), None, 0)
        self.assertEqual([(r["mechanism"], r["to"]) for r in rows], [("precompile", BOB)])


class FakeRpc:
    def __init__(self, blocks, receipts, code=()):
        self.blocks = {b["hash"]: b for b in blocks}
        self.receipts = receipts
        self.code = set(code)

    def batch(self, calls, size=500):
        out = []
        for method, params in calls:
            if method == "hmyv2_getBlockByHash":
                out.append(self.blocks.get(params[0]))
            elif method == "hmyv2_getBlockReceipts":
                out.append(self.receipts[params[0]])
            elif method == "eth_getCode":
                out.append("0x60" if params[0] in self.code else "0x")
            else:
                raise AssertionError(method)
        return out


class ListedScanTest(unittest.TestCase):
    def setUp(self):
        def block(number, block_hash, parent, txs, staking=()):
            return {"number": number, "hash": block_hash, "parentHash": parent, "timestamp": 1786500000 + number,
                    "epoch": 3002, "transactions": txs, "stakingTransactions": list(staking)}

        transfer = {**tx(0, BOB, 3 * ONE, tx_hash="0xt1"), "gasPrice": 10 ** 11}
        call = {**tx(1, POOL, 0, data="0xa9059cbb", tx_hash="0xt2"), "gasPrice": 10 ** 11}
        native = {**tx(0, BOB, 2 * ONE, to_shard=1, tx_hash="0xt3"), "gasPrice": 10 ** 11}
        rewards = {"type": "CollectRewards", "hash": "0xs1", "from": ALICE, "transactionIndex": 0,
                   "gasPrice": 10 ** 11, "msg": {"delegatorAddress": ALICE}}
        self.rows = [{"block_number": "11", "block_hash": "0xb11", "parent_hash": "0xb10"},
                     {"block_number": "12", "block_hash": "0xb12", "parent_hash": "0xb11"}]
        self.rpc = FakeRpc(
            [block(11, "0xb11", "0xb10", [transfer, call]), block(12, "0xb12", "0xb11", [native], [rewards])],
            {"0xb11": [{"transactionHash": "0xt1", "status": 1, "gasUsed": 21000},
                       {"transactionHash": "0xt2", "status": 0, "gasUsed": 50000}],
             "0xb12": [{"transactionHash": "0xt3", "status": 1, "gasUsed": 21000},
                       {"transactionHash": "0xs1", "status": 1, "gasUsed": 30000,
                        "logs": [{"topics": [lib.COLLECT_REWARDS_TOPIC], "data": hex(4 * ONE)}]}]})

    def test_reads_listed_blocks_by_hash(self):
        SCAN.RPC, SCAN.MIN_VALUE = self.rpc, ONE
        result = SCAN.scan(self.rows)
        self.assertEqual([(r["tx_hash"], r["kind"], r["status"], r["gas_used"]) for r in result["tx"]],
                         [("0xt1", "transfer", 1, 21000), ("0xt3", "xshard_native", 1, 21000)])
        self.assertEqual([(r["block_number"], r["block_hash"]) for r in result["trace"]], [(11, "0xb11")])
        self.assertEqual([(r["block_number"], r["incoming"], r["outgoing"]) for r in result["xshard"]],
                         [(12, 0, 1)])
        self.assertEqual([(r["type"], r["amount_atto"]) for r in result["staking"]], [("CollectRewards", 4 * ONE)])
        self.assertEqual(result["segment"], {"start": 11, "end": 12, "first_parent_hash": "0xb10",
                                             "first_hash": "0xb11", "last_hash": "0xb12"})

    def test_rejects_a_block_that_differs_from_the_list(self):
        SCAN.RPC, SCAN.MIN_VALUE = self.rpc, ONE
        with self.assertRaises(lib.RpcError):
            SCAN.scan([{**self.rows[0], "parent_hash": "0xother"}, self.rows[1]])


class StageHelperTest(unittest.TestCase):
    def test_continuity(self):
        good = [{"start": 1, "end": 2, "first_parent_hash": "0xa", "last_hash": "0xb"},
                {"start": 3, "end": 4, "first_parent_hash": "0xb", "last_hash": "0xc"}]
        self.assertEqual([s["start"] for s in SCAN.verify_continuity(list(reversed(good)))], [1, 3])
        with self.assertRaises(ValueError):
            SCAN.verify_continuity([good[0], {**good[1], "first_parent_hash": "0xz"}])
        with self.assertRaises(ValueError):
            SCAN.verify_continuity([good[0], {**good[1], "start": 4}])

    def test_interleave_and_partial_report(self):
        cand = list(range(10))
        self.assertEqual(TRACE.ordered(cand, 3), [0, 3, 6, 9, 1, 4, 7, 2, 5, 8])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "block-stats.csv"
            rows = [{"block_number": n, "frames": 2, "internal_value_gt0": 1, "internal_ge_min": n % 2,
                     "internal_ge_min_effective": 0} for n in (0, 3, 6, 9, 1)]
            lib.write_csv(path, lib.BLOCK_STATS_FIELDS, rows)
            report = TRACE.report_partial(cand, path, 3)
        self.assertEqual(report["complete_passes"], 1)
        self.assertEqual(report["frames"]["estimate"], 20)

    def test_open_stage_requires_matching_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            state = lib.open_stage(stage, {"a": 1}, False)
            with self.assertRaises(FileExistsError):
                lib.open_stage(stage, {"a": 1}, False)
            with self.assertRaises(ValueError):
                lib.open_stage(stage, {"a": 2}, True)
            lib.finish_stage(stage, state, {})
            with self.assertRaises(ValueError):
                lib.open_stage(stage, {"a": 1}, True)


def write(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_run(root, name, shard, branch, start, end, anchors=(), txs=(), internal=(), receipts=(),
             staking=(), minimum=ONE, parent_hash="0xp", end_hash="0xe", internal_unavailable=None):
    run = root / name
    stage = "import-rpc-export" if internal_unavailable else "scan-blocks"
    identity = {"stage": stage, "shard": shard, "branch": branch, "start": start, "end": end,
                "min_value_atto": str(minimum), "step": 1000, "anchors": list(anchors)}
    bounds = {"start": {"block": start, "hash": "0xs", "parent_hash": parent_hash, "timestamp": 1,
                        "time_utc": "x"}, "end": {"block": end, "hash": end_hash, "timestamp": 2, "time_utc": "y"}}
    run.mkdir(parents=True)
    state = {"identity": identity, "complete": True, "rpc_host": "http://node", "boundaries": bounds,
             "chain_continuity": "verified", "counters": {}, "files": {}}
    if internal_unavailable:
        state["internal_unavailable"] = internal_unavailable
    (run / "stage.json").write_text(json.dumps(state))
    write(run / "transactions.csv", lib.TX_FIELDS, txs)
    write(run / "staking.csv", lib.STAKING_FIELDS, staking)
    if not internal_unavailable:
        (run / "internal").mkdir()
        (run / "internal" / "stage.json").write_text(json.dumps(
            {"identity": {"stage": "trace-internal", "scan": identity}, "complete": True, "counters": {}}))
        write(run / "internal" / "internal.csv", lib.INTERNAL_FIELDS, internal)
    write(run / "receipts" / "outgoing-receipts.csv", lib.RECEIPT_FIELDS, receipts)
    (run / "receipts" / "outgoing-receipts-summary.json").write_text(json.dumps(
        {"scan": identity, "unexplained_blocks": [], "counters": {"receipts": len(receipts)}}))
    return run


def tx_row(tx_hash, block, value, kind="transfer", status=1):
    return {"kind": kind, "block_number": block, "block_hash": f"0xb{block}", "timestamp": 1735689600 + block,
            "tx_index": 0, "tx_hash": tx_hash, "eth_hash": "0xeth", "from": ALICE, "to": BOB,
            "value_atto": value, "shard_id": 0, "to_shard_id": 0, "status": status, "gas_used": 21000,
            "gas_price": 10 ** 11, "input_selector": "", "input_bytes": 0}


class BuildArchiveTest(unittest.TestCase):
    def build(self, runs, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "archive"
            command = [sys.executable, str(ARCHIVE / "build-archive.py"), "--out", str(out)]
            for run in runs:
                command += ["--run", str(run)]
            result = subprocess.run(command + list(extra), capture_output=True, text=True)
            tables = {}
            if result.returncode == 0:
                for name in ("transactions.csv", "internal_transfers.csv", "cross_shard.csv", "staking.csv"):
                    with open(out / name, newline="") as handle:
                        tables[name] = list(csv.DictReader(handle))
                tables["manifest"] = json.loads((out / "manifest.json").read_text())
                summary = subprocess.run(
                    [sys.executable, str(ARCHIVE / "summarize-archive.py"), "--archive", str(out),
                     "--output-json", str(out / "summary.json"), "--output-md", str(out / "SUMMARY.md")],
                    capture_output=True, text=True)
                self.assertEqual(summary.returncode, 0, summary.stderr)
                tables["summary"] = json.loads((out / "summary.json").read_text())
            return result, tables

    def test_merges_runs_and_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = {"from_shard": 0, "to_shard": 1, "mechanism": "native", "block_number": 12,
                       "block_hash": "0xb12", "timestamp": 1735689612, "tx_index": 0, "tx_hash": "0xx",
                       "depth": 0, "ancestor_reverted": 0, "from": ALICE, "to": BOB, "value_atto": 5 * ONE}
            dust = {**receipt, "tx_hash": "0xdust", "value_atto": ONE // 2}
            internal = {"block_number": 11, "block_hash": "0xb11", "timestamp": 1735689611, "tx_index": 0,
                        "tx_hash": "0xc", "eth_hash": "0xe", "trace_address": "0", "type": "call",
                        "from": POOL, "to": BOB, "value_atto": 2 * ONE, "effective": 1}
            canonical = make_run(root, "s0", 0, "canonical", 10, 20,
                                 txs=[tx_row("0xa", 11, 3 * ONE), tx_row("0xa", 11, 3 * ONE),
                                      tx_row("0xx", 12, 5 * ONE, kind="xshard_native")],
                                 internal=[internal, internal], receipts=[receipt, dust],
                                 staking=[{"type": "Delegate", "block_number": 13, "block_hash": "0xb13",
                                           "timestamp": 1735689613, "tx_index": 0, "tx_hash": "0xs",
                                           "from": ALICE, "validator": POOL, "amount_atto": 100 * ONE,
                                           "status": 1}])
            rolled = make_run(root, "s0rb", 0, "rolled_back", 16, 30, anchors=[{"block": 15, "hash": "0xh"}],
                              txs=[tx_row("0xa", 17, 3 * ONE)])
            mapped = root / "dest.csv"
            write(mapped, lib.DESTINATION_FIELDS, [{**receipt, "dest_status": "credited",
                                                    "dest_block_number": 40, "dest_block_hash": "0xd",
                                                    "dest_timestamp": 1735689640}])
            result, tables = self.build([canonical, rolled], ["--destinations", str(mapped),
                                                              "--note", "shard 1 empty"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(r["branch"], r["tx_hash"]) for r in tables["transactions.csv"]],
                         [("canonical", "0xa"), ("canonical", "0xx"), ("rolled_back", "0xa")])
        self.assertEqual(tables["transactions.csv"][0]["fee_atto"], str(21000 * 10 ** 11))
        self.assertEqual(len(tables["internal_transfers.csv"]), 1)
        self.assertEqual([(r["tx_hash"], r["dest_status"], r["dest_block_number"])
                          for r in tables["cross_shard.csv"]], [("0xx", "credited", "40")])
        self.assertEqual(tables["staking.csv"][0]["delegator"], ALICE)
        manifest = tables["manifest"]
        self.assertEqual(manifest["notes"], ["shard 1 empty"])
        self.assertEqual(manifest["files"]["transactions.csv"]["by_group"],
                         {"shard0/canonical": 2, "shard0/rolled_back": 1})
        self.assertEqual(tables["summary"]["distinct_addresses"], 3)

    def test_rejects_unanchored_rollback_and_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = make_run(root, "a", 0, "canonical", 10, 20)
            unanchored = make_run(root, "b", 0, "rolled_back", 16, 30)
            result, _ = self.build([canonical, unanchored])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lacks --expect", result.stderr)
            overlap = make_run(root, "c", 0, "canonical", 20, 25)
            result, _ = self.build([canonical, overlap])
            self.assertIn("overlapping runs", result.stderr)

    def test_links_adjacent_rolled_back_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_run(root, "a", 0, "rolled_back", 16, 30, anchors=[{"block": 15, "hash": "0xf"}],
                             end_hash="0xe30")
            linked = make_run(root, "b", 0, "rolled_back", 31, 40, anchors=[{"block": 30, "hash": "0xe30"}],
                              parent_hash="0xe30", internal_unavailable="the export has no traces")
            result, tables = self.build([first, linked])
            self.assertEqual(result.returncode, 0, result.stderr)
            imported = [r for r in tables["manifest"]["runs"] if r["start"] == 31][0]
            self.assertEqual(imported["internal"], "unavailable: the export has no traces")
            self.assertEqual(imported["source"], "import-rpc-export")
            broken = make_run(root, "c", 0, "rolled_back", 31, 40, anchors=[{"block": 30, "hash": "0xe30"}],
                              parent_hash="0xother", internal_unavailable="the export has no traces")
            result, _ = self.build([first, broken])
            self.assertIn("do not link", result.stderr)

    def test_rejects_mixed_minimums(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, _ = self.build([make_run(root, "a", 0, "canonical", 1, 2),
                                    make_run(root, "b", 1, "canonical", 1, 2, minimum=0)])
        self.assertIn("different minimum", result.stderr)


if __name__ == "__main__":
    unittest.main()
