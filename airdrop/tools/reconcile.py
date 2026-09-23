#!/usr/bin/env python3
"""After the batches have been sent, check every recipient's on-chain balance against the list.

    python3 tools/reconcile.py                      # RUN_DIR, AIRDROP_ADDRESS, RPC_URL from .env
    python3 tools/reconcile.py --run-dir runs/x --airdrop 0x... --rpc-url https://...

All balances are read at one block so the picture is consistent. For each recipient the tool
records the balance and whether it is at least the published amount (a recipient may legitimately
hold more if they received tokens from elsewhere). It also checks the contract's counters and the
reserve's remaining allowance, and writes a report next to the run files.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import status as status_tool  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, help="run directory (default: RUN_DIR from .env)")
    parser.add_argument("--rpc-url", help="JSON-RPC endpoint (default: RPC_URL from .env)")
    parser.add_argument("--airdrop", help="deployed contract address (default: AIRDROP_ADDRESS from .env)")
    parser.add_argument("--block", type=int, help="block number to read at (default: latest)")
    parser.add_argument("--rpc-batch", type=int, default=100, help="balance reads per JSON-RPC request")
    parser.add_argument("--env", type=Path, default=common.default_env_path())
    args = parser.parse_args(argv)

    env = common.load_env(args.env)
    rpc_url = args.rpc_url or env.get("RPC_URL", "")
    airdrop = args.airdrop or env.get("AIRDROP_ADDRESS", "")
    run_dir = args.run_dir or (Path(env["RUN_DIR"]) if env.get("RUN_DIR") else None)
    if not airdrop or run_dir is None:
        common.die("need --run-dir and --airdrop (or RUN_DIR and AIRDROP_ADDRESS in .env)")

    try:
        manifest = common.read_manifest(run_dir)
        rows = common.read_distribution(run_dir)
    except common.InputError as exc:
        common.die(str(exc))
        return

    rpc = common.Rpc(rpc_url)
    chain_id = rpc.chain_id()
    block_number = args.block or rpc.block_number()
    block = hex(block_number)
    state = status_tool.read_state(rpc, airdrop, block)
    if state["root"].lower() != manifest["root"].lower():
        common.die(f"contract root {state['root']} differs from {run_dir}/manifest.json root {manifest['root']}")

    token = state["token"]
    balances: list[int] = []
    for start in range(0, len(rows), args.rpc_batch):
        part = rows[start:start + args.rpc_batch]
        raw = rpc.batch([("eth_call", [{"to": token, "data": common.encode_call("balanceOf(address)", r.address)}, block]) for r in part])
        balances.extend(common.decode_word(v) for v in raw)
        done = start + len(part)
        print(f"\rread {done}/{len(rows)} balances", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    short = []
    exact = 0
    for row, balance in zip(rows, balances):
        if balance < row.amount:
            short.append({"address": row.address, "expected": str(row.amount), "balance": str(balance)})
        elif balance == row.amount:
            exact += 1

    problems = []
    if short:
        problems.append(f"{len(short)} recipient(s) hold less than the published amount")
    if state["executedBatches"] != state["batchCount"]:
        problems.append(f"{state['batchCount'] - state['executedBatches']} batch(es) not executed yet")
    if state["distributedAmount"] != state["totalAmount"]:
        problems.append("distributedAmount differs from totalAmount")
    if state["allowance"] != state["totalAmount"] - state["distributedAmount"]:
        problems.append("reserve allowance differs from the remaining amount")

    report = {
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chain_id": chain_id,
        "block": block_number,
        "airdrop": airdrop,
        "token": token,
        "run_dir": str(run_dir),
        "root": state["root"],
        "recipients": len(rows),
        "recipients_with_exact_balance": exact,
        "recipients_with_higher_balance": len(rows) - exact - len(short),
        "recipients_short": short,
        "executed_batches": state["executedBatches"],
        "batch_count": state["batchCount"],
        "distributed_amount": str(state["distributedAmount"]),
        "total_amount": str(state["totalAmount"]),
        "reserve_allowance_remaining": str(state["allowance"]),
        "problems": problems,
    }
    out = run_dir / f"reconciliation-{chain_id}-{block_number}.json"
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")

    print(f"chain id / block  : {chain_id} / {block_number}")
    print(f"recipients        : {len(rows)}")
    print(f"exact balance     : {exact}")
    print(f"higher balance    : {len(rows) - exact - len(short)}")
    print(f"short             : {len(short)}")
    print(f"batches executed  : {state['executedBatches']} of {state['batchCount']}")
    print(f"distributed       : {common.format_one(state['distributedAmount'])} of {common.format_one(state['totalAmount'])}")
    print(f"allowance left    : {common.format_one(state['allowance'])}")
    print(f"report            : {out}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nOK: every recipient holds at least the published amount and the contract is fully executed")


if __name__ == "__main__":
    main()
