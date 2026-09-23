#!/usr/bin/env python3
"""Command-line entry point for the committed-batch airdrop.

    ./airdrop.py help

Every command reads ./.env (copy .env.example). List work (build, verify, status, reconcile,
approve calldata) runs in-process from tools/. Anything that touches a wallet or sends a
transaction is delegated to `forge script`, and the exact forge command is printed before it runs
so it can be copied and audited. Extra Foundry flags can be appended after `--`, for example
`./airdrop.py execute 0 10 -- --priority-gas-price 1000000000` (values in wei).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))

import approve_calldata  # noqa: E402
import build  # noqa: E402
import common  # noqa: E402
import reconcile  # noqa: E402
import status  # noqa: E402
import verify  # noqa: E402

FORGE_SCRIPTS = {
    "deploy": ("script/Deploy.s.sol", "run(string)"),
    "execute": ("script/Execute.s.sol", "run(string,uint256,uint256)"),
    "simulate": ("script/Simulate.s.sol", "run(string)"),
    "verify-root": ("script/VerifyRoot.s.sol", "run(string)"),
    "rehearsal-token": ("script/DeployRehearsalToken.s.sol", "run(address,uint256)"),
}
GAS_ESTIMATE_MULTIPLIER = "110"  # percent; keeps a 400-recipient batch well under the 16,777,216 cap


class CliError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# environment and process helpers
# ---------------------------------------------------------------------------------------------


def load_environment() -> dict:
    env = common.load_env(HERE / ".env")
    for key, value in env.items():
        os.environ[key] = value  # forge scripts read the role addresses with vm.env*
    return env


def need(env: dict, *names: str) -> None:
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise CliError(f"{', '.join(missing)} not set (put it in .env)")


def need_tool(*names: str) -> None:
    for name in names:
        if shutil.which(name) is None:
            raise CliError(f"{name} is not installed or not on PATH")


def wallet_args(env: dict) -> list[str]:
    if env.get("EXECUTOR_PRIVATE_KEY"):
        return ["--private-key", env["EXECUTOR_PRIVATE_KEY"]]
    if env.get("FORGE_WALLET_ARGS"):
        return shlex.split(env["FORGE_WALLET_ARGS"])
    raise CliError("set EXECUTOR_PRIVATE_KEY or FORGE_WALLET_ARGS in .env")


def redact(cmd: list[str]) -> str:
    shown = list(cmd)
    for i, token in enumerate(shown):
        if token == "--private-key" and i + 1 < len(shown):
            shown[i + 1] = "<redacted>"
    return " ".join(shlex.quote(t) for t in shown)


def run(cmd: list[str], *, capture: bool = False, quiet: bool = False, env: dict | None = None) -> str:
    if not quiet:
        print(f"$ {redact(cmd)}", file=sys.stderr)
    sys.stdout.flush()  # keep our output ordered with the child's when piped
    sys.stderr.flush()
    merged = dict(os.environ)
    if env:
        merged.update(env)
    result = subprocess.run(cmd, cwd=HERE, env=merged, text=True, capture_output=capture)
    if result.returncode != 0:
        if capture:
            sys.stderr.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
        raise CliError(f"command failed with exit code {result.returncode}: {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}")
    return result.stdout if capture else ""


def forge_script(name: str, args: list[str], *, rpc: str | None = None, broadcast: bool = False, slow: bool = False,
                 sender: str | None = None, wallet: list[str] | None = None, extra: list[str] | None = None,
                 capture: bool = False, quiet: bool = False, env: dict | None = None) -> str:
    path, sig = FORGE_SCRIPTS[name]
    cmd = ["forge", "script", path, "--sig", sig, *args]
    if rpc:
        cmd += ["--rpc-url", rpc]
    if sender:
        cmd += ["--sender", sender]
    if broadcast:
        cmd += ["--broadcast"]
    if slow:
        cmd += ["--slow"]
    if wallet:
        cmd += wallet
    cmd += extra or []
    return run(cmd, capture=capture, quiet=quiet, env=env)


def logs_section(output: str) -> str:
    """The `== Logs ==` block of a forge script run, for compact display."""
    match = re.search(r"== Logs ==\n(.*?)(?:\n\n|\Z)", output, re.S)
    return match.group(1).strip() if match else output.strip()


def check_chain(env: dict) -> None:
    want = env.get("CHAIN_ID")
    if want and env.get("RPC_URL"):
        got = common.Rpc(env["RPC_URL"]).chain_id()
        if got != int(want):
            raise CliError(f"RPC reports chain id {got}, but .env says CHAIN_ID={want}")


def split_extra(argv: list[str]) -> tuple[list[str], list[str]]:
    """Everything after the first `--` is handed to forge untouched."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------


def cmd_build(env: dict, ns: argparse.Namespace) -> None:
    build.main(ns.args)


def cmd_verify(env: dict, ns: argparse.Namespace) -> None:
    need_tool("forge")
    run_dir = ns.run_dir or env.get("RUN_DIR")
    if not run_dir:
        raise CliError("pass --run-dir or set RUN_DIR in .env")
    on_chain = not ns.offline and bool(env.get("AIRDROP_ADDRESS")) and bool(env.get("RPC_URL"))
    if on_chain:
        check_chain(env)

    print("== Python verifier ==")
    py_args = ["--run-dir", run_dir]
    if not on_chain:
        py_args.append("--offline")
    verify.main(py_args)

    print("\n== Solidity verifier ==")
    if on_chain:
        forge_script("verify-root", [run_dir], rpc=env["RPC_URL"])
    else:
        forge_script("verify-root", [run_dir], env={"AIRDROP_ADDRESS": ""})


def cmd_deploy(env: dict, ns: argparse.Namespace) -> None:
    need_tool("forge")
    need(env, "RUN_DIR", "RPC_URL", "TOKEN_ADDRESS", "RESERVE_ADDRESS", "ADMIN_ADDRESS", "EXECUTOR_ADDRESS")
    forge_script("deploy", [env["RUN_DIR"]], rpc=env["RPC_URL"], broadcast=True, wallet=wallet_args(env),
                 extra=ns.extra)


def cmd_verify_contract(env: dict, ns: argparse.Namespace) -> None:
    need_tool("forge", "cast")
    need(env, "RUN_DIR", "AIRDROP_ADDRESS", "CHAIN_ID", "ETHERSCAN_API_KEY",
         "TOKEN_ADDRESS", "RESERVE_ADDRESS", "ADMIN_ADDRESS", "EXECUTOR_ADDRESS")
    manifest = common.read_manifest(Path(env["RUN_DIR"]))
    encoded = run([
        "cast", "abi-encode", "constructor(address,address,address,address,bytes32,uint256,uint256,bytes32)",
        env["TOKEN_ADDRESS"], env["RESERVE_ADDRESS"], env["ADMIN_ADDRESS"], env["EXECUTOR_ADDRESS"],
        manifest["root"], str(manifest["batch_count"]), str(manifest["total_amount"]), manifest["list_sha256"],
    ], capture=True, quiet=True).strip()
    run([
        "forge", "verify-contract", env["AIRDROP_ADDRESS"], "src/CommittedBatchAirdrop.sol:CommittedBatchAirdrop",
        "--chain", env["CHAIN_ID"], "--etherscan-api-key", env["ETHERSCAN_API_KEY"],
        "--constructor-args", encoded, "--watch", *ns.extra,
    ])


def cmd_approve_calldata(env: dict, ns: argparse.Namespace) -> None:
    approve_calldata.main(ns.args)


def cmd_simulate(env: dict, ns: argparse.Namespace) -> None:
    need_tool("forge")
    need(env, "RUN_DIR", "RPC_URL", "TOKEN_ADDRESS", "RESERVE_ADDRESS", "ADMIN_ADDRESS", "EXECUTOR_ADDRESS")
    forge_script("simulate", [env["RUN_DIR"]], rpc=env["RPC_URL"], extra=ns.extra)


def cmd_execute(env: dict, ns: argparse.Namespace) -> None:
    need_tool("forge")
    need(env, "RUN_DIR", "RPC_URL", "AIRDROP_ADDRESS", "EXECUTOR_ADDRESS")
    extra = ["--gas-estimate-multiplier", GAS_ESTIMATE_MULTIPLIER, *ns.extra]
    forge_script(
        "execute", [env["RUN_DIR"], str(ns.frm), str(ns.to)], rpc=env["RPC_URL"], sender=env["EXECUTOR_ADDRESS"],
        broadcast=not ns.dry_run, slow=not ns.dry_run, wallet=None if ns.dry_run else wallet_args(env), extra=extra,
    )


def cmd_status(env: dict, ns: argparse.Namespace) -> None:
    check_chain(env)
    status.main(ns.args)


def cmd_reconcile(env: dict, ns: argparse.Namespace) -> None:
    check_chain(env)
    reconcile.main(ns.args)


def cmd_deploy_rehearsal_token(env: dict, ns: argparse.Namespace) -> None:
    need_tool("forge")
    need(env, "RPC_URL")
    forge_script("rehearsal-token", [ns.holder, ns.supply], rpc=env["RPC_URL"], broadcast=True,
                 wallet=wallet_args(env), extra=ns.extra)


def cmd_test(env: dict, ns: argparse.Namespace) -> None:
    need_tool("forge")
    run(["forge", "test", *ns.args])


# ---------------------------------------------------------------------------------------------
# local demo: the whole flow against an Anvil node, using Anvil's well-known development keys
# ---------------------------------------------------------------------------------------------

ANVIL_RESERVE = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
ANVIL_RESERVE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_EXECUTOR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
ANVIL_EXECUTOR_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


def cmd_local_demo(env: dict, ns: argparse.Namespace) -> None:
    need_tool("anvil", "forge", "cast")
    port = ns.port
    rpc = f"http://127.0.0.1:{port}"
    run_dir = "runs/local-demo"
    demo_env = {
        "RPC_URL": rpc, "CHAIN_ID": "31337", "ETHERSCAN_API_KEY": "",
        "RESERVE_ADDRESS": ANVIL_RESERVE, "ADMIN_ADDRESS": ANVIL_RESERVE, "EXECUTOR_ADDRESS": ANVIL_EXECUTOR,
        "EXECUTOR_PRIVATE_KEY": ANVIL_EXECUTOR_KEY, "FORGE_WALLET_ARGS": "", "RUN_DIR": run_dir, "AIRDROP_ADDRESS": "",
    }
    os.environ.update(demo_env)

    def step(title: str) -> None:
        print(f"\n== {title} ==")

    anvil = subprocess.Popen(["anvil", "--port", str(port), "--silent"], cwd=HERE)
    try:
        client = common.Rpc(rpc, timeout=2, retries=1)
        for _ in range(40):
            try:
                client.chain_id()
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise CliError("anvil did not start")

        step("1. build the run directory from the example list")
        build.main(["--input", "examples/recipients-example.csv", "--run-dir", run_dir, "--label", "local demo",
                    "--batch-size", "3", "--force"])
        manifest = common.read_manifest(HERE / run_dir)

        step("2. deploy a rehearsal token whose supply belongs to the reserve")
        out = forge_script("rehearsal-token", [ANVIL_RESERVE, str(1_000_000 * 10**18)], rpc=rpc, broadcast=True,
                           wallet=["--private-key", ANVIL_RESERVE_KEY], capture=True, quiet=True)
        match = re.search(r"rehearsal token\s+(0x[0-9a-fA-F]{40})", out)
        if not match:
            raise CliError("could not find the token address in forge output")
        token = match.group(1)
        os.environ["TOKEN_ADDRESS"] = token
        print(f"token: {token}")

        step("3. simulate before deploying (sends nothing)")
        print(logs_section(forge_script("simulate", [run_dir], rpc=rpc, capture=True, quiet=True)))

        step("4. deploy the airdrop contract")
        forge_script("deploy", [run_dir], rpc=rpc, broadcast=True, wallet=["--private-key", ANVIL_EXECUTOR_KEY],
                     capture=True, quiet=True)
        deployment = json.loads((HERE / run_dir / "deployment-31337.json").read_text())
        airdrop = deployment["airdrop"]
        os.environ["AIRDROP_ADDRESS"] = airdrop
        print(f"airdrop: {airdrop}")

        step("5. verify the run directory against the contract, in Python and in Solidity")
        verify.main(["--run-dir", run_dir, "--airdrop", airdrop, "--rpc-url", rpc])
        print(logs_section(forge_script("verify-root", [run_dir], rpc=rpc, capture=True, quiet=True)))

        step("6. the reserve approves exactly the total (on Ethereum: the multisig's one transaction)")
        approve_calldata.main(["--run-dir", run_dir, "--airdrop", airdrop, "--token", token,
                               "--reserve", ANVIL_RESERVE, "--chain-id", "31337"])
        run(["cast", "send", token, "approve(address,uint256)", airdrop, str(manifest["total_amount"]),
             "--rpc-url", rpc, "--private-key", ANVIL_RESERVE_KEY], capture=True, quiet=True)
        print("approved")

        step("7. execute the first two batches, check status, then send the rest")
        for frm, to in ((0, 2), (0, 0)):
            out = forge_script("execute", [run_dir, str(frm), str(to)], rpc=rpc, sender=ANVIL_EXECUTOR, broadcast=True,
                               slow=True, wallet=["--private-key", ANVIL_EXECUTOR_KEY],
                               extra=["--gas-estimate-multiplier", GAS_ESTIMATE_MULTIPLIER], capture=True, quiet=True)
            print(logs_section(out))
            if to == 2:
                status.main(["--run-dir", run_dir, "--airdrop", airdrop, "--rpc-url", rpc])

        step("8. reconcile every recipient's balance")
        reconcile.main(["--run-dir", run_dir, "--airdrop", airdrop, "--rpc-url", rpc])
        print("\nlocal demo finished")
    finally:
        anvil.terminate()
        try:
            anvil.wait(timeout=5)
        except subprocess.TimeoutExpired:
            anvil.kill()


# ---------------------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------------------


# Commands whose arguments are handed straight to a tool's own argument parser.
PASSTHROUGH = {
    "build": (cmd_build, "options for tools/build.py: --input FILE|DIR --run-dir runs/NAME [--batch-size N] ..."),
    "approve-calldata": (cmd_approve_calldata, "options for tools/approve_calldata.py, e.g. --revoke"),
    "status": (cmd_status, "options for tools/status.py"),
    "reconcile": (cmd_reconcile, "options for tools/reconcile.py, e.g. --block N"),
    "test": (cmd_test, "forge test flags"),
}
FORGE_EXTRA_HELP = "Extra forge flags may follow a `--` separator."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airdrop.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str, func, forge_extra: bool = False):
        p = sub.add_parser(name, help=help_text, description=help_text + (" " + FORGE_EXTRA_HELP if forge_extra else ""))
        p.set_defaults(func=func)
        return p

    add("build", "Turn a recipient list into a run directory (canonical list, batches, proofs, root). "
        + PASSTHROUGH["build"][1], cmd_build)
    p = add("verify", "Recompute everything in Python and Solidity and compare with the files and the contract.",
            cmd_verify)
    p.add_argument("--run-dir", help="run directory (default: RUN_DIR from .env)")
    p.add_argument("--offline", action="store_true", help="skip the on-chain comparison")
    add("deploy", "Deploy CommittedBatchAirdrop for RUN_DIR (writes runs/<name>/deployment-<chain>.json).",
        cmd_deploy, True)
    add("verify-contract", "Publish the source of AIRDROP_ADDRESS on Etherscan.", cmd_verify_contract, True)
    add("approve-calldata", "Print the approve() call the reserve must send; write a Safe Transaction Builder file. "
        + PASSTHROUGH["approve-calldata"][1], cmd_approve_calldata)
    add("simulate", "Dress rehearsal on a fork of RPC_URL: approves and sends every batch in simulation. Sends nothing.",
        cmd_simulate, True)
    p = add("execute", "Send batches [from, to) one transaction at a time; to = 0 means through the last batch.",
            cmd_execute, True)
    p.add_argument("--dry-run", action="store_true", help="only simulate the transactions")
    p.add_argument("frm", nargs="?", type=int, default=0, metavar="from", help="first batch index (default 0)")
    p.add_argument("to", nargs="?", type=int, default=0, help="last batch index, exclusive (default 0 = to the end)")
    add("status", "Show contract state, reserve balance and allowance, executed and pending batches. "
        + PASSTHROUGH["status"][1], cmd_status)
    add("reconcile", "Read every recipient's balance at one block and write a reconciliation report. "
        + PASSTHROUGH["reconcile"][1], cmd_reconcile)
    p = add("deploy-rehearsal-token", "Testnet only: deploy the stand-in token (refuses to run on Ethereum mainnet).",
            cmd_deploy_rehearsal_token, True)
    p.add_argument("holder", help="address that receives the whole supply (use your rehearsal RESERVE_ADDRESS)")
    p.add_argument("supply", help="total supply in the smallest unit")
    p = add("local-demo", "Run the whole flow on a local Anvil node with the example list; no .env needed.",
            cmd_local_demo)
    p.add_argument("--port", type=int, default=8546)
    add("test", "Run the Solidity test suite. " + PASSTHROUGH["test"][1], cmd_test)
    add("help", "Show this help.", None)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv or argv[0] in ("help", "-h", "--help"):
        parser.print_help()
        return 0

    if argv[0] in PASSTHROUGH:  # including -h, so the tool's own full help is shown
        func = PASSTHROUGH[argv[0]][0]
        ns = argparse.Namespace(command=argv[0], func=func, args=argv[1:], extra=[])
    else:
        own, extra = split_extra(argv)
        ns = parser.parse_args(own)
        ns.args = []
        ns.extra = extra

    try:
        env = load_environment()
        ns.func(env, ns)
        return 0
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (common.InputError, RuntimeError) as exc:  # RuntimeError: RPC failures from tools/common.py
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
