#!/usr/bin/env python3
"""`vault-batch deploy`: where the sender's key comes from, and deploying a run from a private key on Anvil.

    python3 tools/test_vault_deploy.py

The Anvil tests need anvil, forge and cast on PATH and are skipped without them.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import vault_batch  # noqa: E402

# Anvil's first two development accounts.
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
SENDER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OTHER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
PASSWORD = "correct horse battery"
ACCOUNT = "testdeployer"
SAFE = "0x" + "5a" * 20
TOKEN = "0x" + "70" * 20
VALIDATORS = ["0x" + c * 40 for c in "1234"]
DELEGATOR = "0x" + "de" * 20
HAVE_FOUNDRY = all(shutil.which(t) for t in ("anvil", "forge", "cast"))
TTY = SimpleNamespace(isatty=lambda: True)


def key_args(keystore_dir: Path, helper: Path | None, **overrides) -> SimpleNamespace:
    values = {"keystore_dir": str(keystore_dir), "account": ACCOUNT, "touchid_helper": helper and str(helper),
              "keychain_service": "foundry-keystore", "no_touchid": False, "env": keystore_dir / "no.env"}
    values.update(overrides)
    return SimpleNamespace(**values)


def fake_helper(path: Path, calls: Path, output: str = PASSWORD, code: int = 0) -> Path:
    """A stand-in for touchid-helper: records its arguments and prints the secret without a newline."""
    path.write_text(f"#!/bin/sh\necho \"$@\" >> '{calls}'\n"
                    + (f"printf '%s' '{output}'\n" if code == 0 else f"echo '{output}' >&2\n") + f"exit {code}\n")
    path.chmod(0o755)
    return path


def import_keystore(keystore_dir: Path) -> None:
    subprocess.run(["cast", "wallet", "import", ACCOUNT, "--keystore-dir", str(keystore_dir),
                    "--private-key", SENDER_KEY, "--unsafe-password", PASSWORD], check=True, capture_output=True)


@unittest.skipUnless(shutil.which("cast"), "cast is not installed")
class SenderKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.keystores = cls.dir / "keystores"
        cls.keystores.mkdir()
        import_keystore(cls.keystores)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.calls = self.dir / f"calls-{uuid.uuid4().hex}.txt"
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("TOUCHID_HELPER", "VAULT_SENDER_ACCOUNT"):
            os.environ.pop(name, None)

    def test_touchid_helper_unlocks_the_keystore_without_a_prompt(self):
        helper = fake_helper(self.dir / "helper-ok", self.calls)
        with mock.patch("getpass.getpass") as prompt:
            key, source = vault_batch.sender_key(key_args(self.keystores, helper))
        self.assertEqual(key, SENDER_KEY)
        self.assertIn("touchid-helper", source)
        prompt.assert_not_called()
        self.assertEqual(self.calls.read_text().split(), ["get", "-s", "foundry-keystore", "-a", ACCOUNT])

    def test_the_keystore_name_and_helper_path_come_from_the_env_file(self):
        helper = fake_helper(self.dir / "helper-env", self.calls)
        env = self.dir / "touchid.env"
        env.write_text(f"VAULT_SENDER_ACCOUNT={ACCOUNT}\nTOUCHID_HELPER={helper}\n")
        with mock.patch("getpass.getpass") as prompt:
            key, _ = vault_batch.sender_key(key_args(self.keystores, None, account=None, env=env))
        self.assertEqual(key, SENDER_KEY)
        prompt.assert_not_called()
        self.assertEqual(self.calls.read_text().split(), ["get", "-s", "foundry-keystore", "-a", ACCOUNT])

    def test_without_a_keystore_name_the_private_key_is_asked_for(self):
        helper = fake_helper(self.dir / "helper-unused", self.calls)
        with mock.patch("sys.stdin", TTY), mock.patch("getpass.getpass", return_value=SENDER_KEY) as prompt:
            key, source = vault_batch.sender_key(key_args(self.keystores, helper, account=None))
        self.assertEqual((key, source), (SENDER_KEY, "typed private key"))
        prompt.assert_called_once()
        self.assertFalse(self.calls.exists())

    def test_without_touchid_helper_set_the_password_is_asked_for_once(self):
        with mock.patch("sys.stdin", TTY), mock.patch("getpass.getpass", return_value=PASSWORD) as prompt:
            key, source = vault_batch.sender_key(key_args(self.keystores, None))
        self.assertEqual(key, SENDER_KEY)
        self.assertIn("typed", source)
        prompt.assert_called_once()

    def test_a_helper_path_that_is_not_there_stops(self):
        with mock.patch("getpass.getpass") as prompt, \
                self.assertRaisesRegex(common.InputError, "not an executable file"):
            vault_batch.sender_key(key_args(self.keystores, self.dir / "not-installed"))
        prompt.assert_not_called()

    def test_no_touchid_types_the_password_even_with_the_helper(self):
        helper = fake_helper(self.dir / "helper-unused", self.calls)
        with mock.patch("sys.stdin", TTY), mock.patch("getpass.getpass", return_value=PASSWORD):
            key, _ = vault_batch.sender_key(key_args(self.keystores, helper, no_touchid=True))
        self.assertEqual(key, SENDER_KEY)
        self.assertFalse(self.calls.exists())

    def test_a_refused_touchid_stops_instead_of_prompting(self):
        helper = fake_helper(self.dir / "helper-no", self.calls, "touchid-helper: not approved: Canceled by user.", 1)
        with mock.patch("getpass.getpass") as prompt, self.assertRaisesRegex(common.InputError, "not approved"):
            vault_batch.sender_key(key_args(self.keystores, helper))
        prompt.assert_not_called()

    def test_a_wrong_password_is_reported(self):
        helper = fake_helper(self.dir / "helper-wrong", self.calls, "not the password")
        with self.assertRaisesRegex(common.InputError, "Mac Mismatch"):
            vault_batch.sender_key(key_args(self.keystores, helper))

    def test_without_a_keystore_the_private_key_is_asked_for(self):
        with mock.patch("sys.stdin", TTY), mock.patch("getpass.getpass", return_value=SENDER_KEY[2:] + "\n"):
            key, source = vault_batch.sender_key(key_args(self.keystores, None, account="missing"))
        self.assertEqual((key, source), (SENDER_KEY, "typed private key"))

    def test_a_malformed_private_key_is_refused(self):
        with mock.patch("sys.stdin", TTY), mock.patch("getpass.getpass", return_value="0x1234"), \
                self.assertRaisesRegex(common.InputError, "32 bytes"):
            vault_batch.sender_key(key_args(self.keystores, None, account="missing"))

    def test_no_terminal_no_prompt(self):
        with mock.patch("sys.stdin", SimpleNamespace(isatty=lambda: False)), \
                self.assertRaisesRegex(common.InputError, "not a terminal"):
            vault_batch.sender_key(key_args(self.keystores, None, account="missing"))


class RedactUrlTests(unittest.TestCase):
    def test_api_keys_in_the_url_are_hidden(self):
        for url in ("https://eth-mainnet.g.alchemy.com/v2/SECRETKEY", "https://rpc.example/?apikey=SECRETKEY",
                    "https://user:SECRETKEY@rpc.example"):
            self.assertNotIn("SECRETKEY", common.redact_url(url))
        self.assertEqual(common.redact_url("https://eth-mainnet.g.alchemy.com/v2/SECRETKEY"),
                         "https://eth-mainnet.g.alchemy.com/<redacted>")

    def test_urls_without_secrets_are_shown(self):
        for url in ("https://ethereum-rpc.publicnode.com", "http://127.0.0.1:8545", "http://127.0.0.1:8545/"):
            self.assertEqual(common.redact_url(url), url)


class EnvFileTests(unittest.TestCase):
    def test_airdrop_env_selects_another_settings_file(self):
        folder = vault_batch.HERE
        with mock.patch.dict(os.environ):
            os.environ.pop("AIRDROP_ENV", None)
            self.assertEqual(common.default_env_path(), folder / ".env")
            os.environ["AIRDROP_ENV"] = ".env.sepolia"
            self.assertEqual(common.default_env_path(), folder / ".env.sepolia")
            os.environ["AIRDROP_ENV"] = "/tmp/rehearsal.env"
            self.assertEqual(common.default_env_path(), Path("/tmp/rehearsal.env"))


class EtherscanInputTests(unittest.TestCase):
    def test_license_and_notice_are_published_with_the_source(self):
        data = json.loads(vault_batch.etherscan_input())
        self.assertEqual(sorted(data["sources"]), ["LICENSE", "NOTICE.txt", "src/GovernorDelegator.sol"])
        for name in ("LICENSE", "NOTICE.txt"):
            text = (vault_batch.ARTIFACT_PATH.parent / name).read_text()
            self.assertEqual(data["sources"][name]["content"], "/*\n" + text + "*/\n")
        pinned = json.loads(vault_batch.COMPILER_INPUT_PATH.read_text())
        self.assertEqual(data["settings"], pinned["settings"])
        self.assertEqual(data["sources"]["src/GovernorDelegator.sol"], pinned["sources"]["src/GovernorDelegator.sol"])

    @unittest.skipUnless(os.environ.get("SOLC_0_8_30"), "set SOLC_0_8_30 to a solc 0.8.30 binary")
    def test_the_submitted_input_compiles_to_the_audited_code(self):
        result = subprocess.run([os.environ["SOLC_0_8_30"], "--standard-json"], input=vault_batch.etherscan_input(),
                                capture_output=True, text=True, check=True)
        out = json.loads(result.stdout)
        self.assertEqual([e for e in out.get("errors", []) if e["severity"] == "error"], [])
        evm = out["contracts"]["src/GovernorDelegator.sol"]["GovernorDelegator"]["evm"]
        artifact = vault_batch.load_artifact()
        self.assertEqual(bytes.fromhex(evm["bytecode"]["object"]), artifact.creation)
        self.assertEqual(bytes.fromhex(evm["deployedBytecode"]["object"]), artifact.runtime_template)

    def test_an_edited_notice_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("GovernorDelegator.json", "GovernorDelegator.compiler-input.json", "LICENSE", "NOTICE.txt"):
                shutil.copy(vault_batch.ARTIFACT_PATH.parent / name, folder / name)
            (folder / "NOTICE.txt").write_text((folder / "NOTICE.txt").read_text() + "edited\n")
            with mock.patch.object(vault_batch, "ARTIFACT_PATH", folder / "GovernorDelegator.json"), \
                    mock.patch.object(vault_batch, "COMPILER_INPUT_PATH", folder / "GovernorDelegator.compiler-input.json"), \
                    self.assertRaisesRegex(common.InputError, "NOTICE.txt differs"):
                vault_batch.etherscan_input()


def start_fake_etherscan():
    """A stand-in for Etherscan's verification API that records every request it gets.

    `exact` maps an address to the constructor arguments it was verified with; `similar` maps an
    address to the verified contract Etherscan shows instead (and refuses a submission for it).
    """
    state = {"posts": [], "gets": [], "exact": {}, "similar": {}, "pending": {}, "unindexed": set(),
             "polls": defaultdict(int)}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            state["gets"].append(params)
            if params.get("action") == "getsourcecode":
                address = params["address"].lower()
                other = state["similar"].get(address)
                args = state["exact"].get(address if other is None else other.lower())
                entry = {"SourceCode": "{...}" if args is not None else "", "ConstructorArguments": args or "",
                         "SimilarMatch": other or ""}
                return self.reply({"status": "1", "message": "OK", "result": [entry]})
            guid = params.get("guid", "")
            state["polls"][guid] += 1
            if state["polls"][guid] == 1:
                return self.reply({"status": "0", "message": "NOTOK", "result": "Pending in queue"})
            state["exact"][guid] = state["pending"].pop(guid, state["exact"].get(guid))
            self.reply({"status": "1", "message": "OK", "result": "Pass - Verified"})

        def do_POST(self):
            fields = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            fields["_query"] = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            state["posts"].append(fields)
            address = fields["contractaddress"].lower()
            if address in state["exact"] or address in state["similar"]:
                return self.reply({"status": "0", "message": "NOTOK", "result": "Contract source code already verified"})
            if address in state["unindexed"]:
                state["unindexed"].discard(address)
                return self.reply({"status": "0", "message": "NOTOK", "result": f"Unable to locate ContractCode at {address}"})
            state["pending"][address] = fields["constructorArguements"]
            self.reply({"status": "1", "message": "OK", "result": address})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}/v2/api"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAVE_FOUNDRY, "anvil, forge and cast are needed")
class DeployOnAnvilTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.keystores = cls.dir / "keystores"
        cls.keystores.mkdir()
        import_keystore(cls.keystores)
        cls.helper = fake_helper(cls.dir / "helper", cls.dir / "helper-calls.txt")
        cls.env = cls.dir / "empty.env"
        cls.env.write_text("")
        cls.environment = mock.patch.dict(os.environ)
        cls.environment.start()
        for name in ("RPC_URL", "CHAIN_ID", "RESERVE_ADDRESS", "TOKEN_ADDRESS", "TOUCHID_HELPER", "VAULT_SENDER_ACCOUNT",
                     "ETHERSCAN_API_KEY"):
            os.environ.pop(name, None)
        cls.no_wait = mock.patch.object(vault_batch, "ETHERSCAN_POLL_SECONDS", 0)
        cls.no_wait.start()
        cls.etherscan_server, cls.etherscan_state, url = start_fake_etherscan()
        cls.etherscan = ("--etherscan-url", url, "--etherscan-api-key", "TESTKEY")
        port = free_port()
        cls.rpc_url = f"http://127.0.0.1:{port}"
        cls.anvil = subprocess.Popen(["anvil", "--port", str(port), "--silent"], stdout=subprocess.DEVNULL)
        cls.rpc = common.Rpc(cls.rpc_url, timeout=5, retries=1)
        for _ in range(80):
            try:
                cls.rpc.chain_id()
                break
            except RuntimeError:
                time.sleep(0.25)
        else:
            raise RuntimeError("anvil did not start")
        cls.runs = []

    @classmethod
    def tearDownClass(cls):
        cls.etherscan_server.shutdown()
        cls.no_wait.stop()
        cls.environment.stop()
        cls.anvil.terminate()
        cls.anvil.wait()
        for run in cls.runs:
            shutil.rmtree(run, ignore_errors=True)
        cls.tmp.cleanup()

    def build(self, validators: list[str], phase: str = "deploy") -> Path:
        deposits = self.dir / f"deposits-{len(self.runs)}.csv"
        with deposits.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["validator_address", "delegator_address", "amount_one"])
            writer.writerows([v, DELEGATOR, "20000"] for v in validators)
        run = vault_batch.HERE / "runs" / f"test-sender-deploy-{uuid.uuid4().hex[:8]}"
        self.runs.append(run)
        with redirect_stdout(io.StringIO()):
            vault_batch.main(["build", "--input", str(deposits), "--out-dir", str(run), "--safe", SAFE, "--token", TOKEN,
                              "--chain-id", "31337", "--safe-version", "1.4.1", "--no-nonce", "--phase", phase,
                              "--env", str(self.env)])
        return run

    def setUp(self):
        self.ledger_dir = self.dir / f"ledger-{self._testMethodName}"
        self.etherscan_state["posts"].clear()
        self.etherscan_state["gets"].clear()

    def deploy_args(self, run: Path, extra: tuple) -> list[str]:
        return ["deploy", "--out-dir", str(run), "--rpc-url", self.rpc_url, "--keystore-dir", str(self.keystores),
                "--account", ACCOUNT, "--touchid-helper", str(self.helper), "--ledger-dir", str(self.ledger_dir),
                "--env", str(self.env), *extra]

    def deploy(self, run: Path, *extra: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            vault_batch.main(self.deploy_args(run, extra))
        return out.getvalue()

    def refused(self, run: Path, *extra: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out), self.assertRaises(SystemExit):
            vault_batch.main(self.deploy_args(run, extra))
        return out.getvalue()

    def vaults(self, run: Path) -> list[dict]:
        with (run / "vaults.csv").open(newline="") as handle:
            return list(csv.DictReader(handle))

    def code(self, address: str) -> str:
        return self.rpc.call("eth_getCode", [address, "latest"])

    def ledgers(self) -> list[Path]:
        return sorted(self.ledger_dir.glob("deployments-*.csv"))

    def test_deploy_records_every_vault_and_resumes(self):
        run = self.build(VALIDATORS[:3])
        vaults = self.vaults(run)

        self.deploy(run)
        self.assertTrue(all(self.code(v["vault_address"]) == "0x" for v in vaults), "a dry run sent something")
        self.assertEqual(self.ledgers(), [])
        self.assertEqual(list(run.glob("sender-deploy-*.json")), [])

        self.assertIn(f"not to --sender {OTHER}", self.refused(run, "--broadcast", "--sender", OTHER))

        # someone else deploys the second vault first; it is the same contract at the same address
        artifact = vault_batch.load_artifact()
        front = vaults[1]
        data = common.hex0x(vault_batch.deploy_data(artifact, TOKEN, front["validator_address"], front["governor_address"]))
        self.rpc.call("eth_sendTransaction", [{"from": OTHER, "to": vault_batch.FACTORY, "data": data, "gas": hex(900_000)}])

        text = self.deploy(run, "--broadcast", "--sender", SENDER, *self.etherscan)
        self.assertIn("OK: every vault", text)
        self.assertIn("etherscan        : 3/3", text)
        self.assertEqual(text.count(" exact match  "), 3)
        posts = {p["contractaddress"]: p for p in self.etherscan_state["posts"]}
        self.assertEqual(sorted(posts), sorted(v["vault_address"] for v in vaults))
        for v in vaults:
            p = posts[v["vault_address"]]
            self.assertEqual((p["contractname"], p["compilerversion"], p["codeformat"], p["apikey"], p["_query"]),
                             ("src/GovernorDelegator.sol:GovernorDelegator", "v0.8.30+commit.73712a01",
                              "solidity-standard-json-input", "TESTKEY", {"chainid": "31337"}))
            self.assertEqual(p["constructorArguements"], (vault_batch.aw(TOKEN) + vault_batch.aw(v["governor_address"])).hex())
            self.assertEqual(json.loads(p["sourceCode"]), json.loads(vault_batch.etherscan_input()))
        (ledger,) = self.ledgers()
        with ledger.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([r["vault_address"] for r in rows], [v["vault_address"] for v in vaults])
        self.assertEqual([r["status"] for r in rows], ["deployed", "existing", "deployed"])
        self.assertEqual(rows[1]["tx_hash"], "")
        for r in (rows[0], rows[2]):
            receipt = self.rpc.call("eth_getTransactionReceipt", [r["tx_hash"]])
            self.assertEqual((receipt["status"], receipt["from"].lower(), int(receipt["blockNumber"], 16)),
                             ("0x1", SENDER.lower(), int(r["block_number"])))
            self.assertEqual(r["sender"], SENDER)
            self.assertEqual(r["run"], run.name)
        expected = common.hex0x(common.keccak256(artifact.runtime(TOKEN)))
        for v in vaults:
            self.assertEqual(common.hex0x(common.keccak256(common.unhex(self.code(v["vault_address"])))), expected)
        plan = json.loads(next(run.glob("sender-deploy-*.json")).read_text())
        self.assertEqual(plan["vaults"], [vaults[0]["vault_address"], vaults[2]["vault_address"]])
        # each transaction carries the planned limit (the node's estimate plus 20%), not forge's own estimate
        sent_gas = {r["vault_address"]: int(self.rpc.call("eth_getTransactionByHash", [r["tx_hash"]])["gas"], 16)
                    for r in (rows[0], rows[2])}
        self.assertEqual(sent_gas, dict(zip(plan["vaults"], plan["gas"])))
        for r in (rows[0], rows[2]):
            used = int(self.rpc.call("eth_getTransactionReceipt", [r["tx_hash"]])["gasUsed"], 16)
            self.assertLess(used, sent_gas[r["vault_address"]])

        self.assertIn("nothing to deploy", self.deploy(run, "--broadcast"))
        self.assertEqual(len(self.ledgers()), 1)
        self.assertEqual(self.verify_on_etherscan(run).count(" exact match  "), 3)

        # a later run that shares those vaults lists them as existing, with the transactions recorded earlier
        later = self.build(VALIDATORS)
        self.assertIn("etherscan        : not verified", self.deploy(later, "--broadcast"))
        newest = self.ledgers()[-1]
        with newest.open(newline="") as handle:
            rows2 = {r["vault_address"]: r for r in csv.DictReader(handle)}
        self.assertEqual(len(rows2), 4)
        self.assertEqual(rows2[vaults[0]["vault_address"]]["status"], "existing")
        self.assertEqual(rows2[vaults[0]["vault_address"]]["tx_hash"], rows[0]["tx_hash"])
        self.assertEqual(rows2[vaults[1]["vault_address"]]["tx_hash"], "")
        new = [r for r in rows2.values() if r["status"] == "deployed"]
        self.assertEqual([r["validator_address"].lower() for r in new], [VALIDATORS[3]])

    def verify_on_etherscan(self, run: Path) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            vault_batch.main(["etherscan", "--out-dir", str(run), "--rpc-url", self.rpc_url, "--env", str(self.env),
                              *self.etherscan])
        return out.getvalue()

    def test_etherscan_waits_until_a_new_vault_is_indexed(self):
        run = self.build(["0x" + "5" * 40, "0x" + "6" * 40])
        self.deploy(run, "--broadcast")
        first, second = (v["vault_address"] for v in self.vaults(run))
        self.etherscan_state["unindexed"].add(first.lower())
        text = self.verify_on_etherscan(run)
        self.assertIn("OK: every deployed vault", text)
        self.assertEqual(text.count(" exact match  "), 2)
        sent = [p["contractaddress"] for p in self.etherscan_state["posts"]]
        self.assertEqual((sent.count(first), sent.count(second)), (2, 1))

    def test_a_similar_match_is_reported_as_such(self):
        run = self.build(["0x" + "7" * 40, "0x" + "9" * 40])
        self.deploy(run, "--broadcast")
        first, second = (v["vault_address"] for v in self.vaults(run))
        self.etherscan_state["exact"][first.lower()] = "00" * 64
        self.etherscan_state["similar"][second.lower()] = first
        text = self.verify_on_etherscan(run)
        self.assertIn(f"{second} similar match of {first}", text)
        self.assertIn(f"{first} similar match of another contract", text)
        self.assertIn("note: 2 vault(s) are similar matches", text)
        self.assertIn("2 as a similar match", text)

    def test_a_run_without_deployments_is_refused(self):
        self.assertIn("deploys no vaults", self.refused(self.build(VALIDATORS[:1], phase="fund")))

    def test_a_run_outside_runs_is_refused(self):
        run = self.build(VALIDATORS[:1])
        moved = self.dir / run.name
        shutil.copytree(run, moved)
        self.assertIn("must be inside", self.refused(moved))


if __name__ == "__main__":
    unittest.main()
