#!/usr/bin/env python3
"""Show where a deployed airdrop stands: what is fixed in the contract, who the executor is,
whether it is paused, the reserve's balance and allowance, and which batches remain.

    python3 tools/status.py                      # uses RUN_DIR, AIRDROP_ADDRESS, RPC_URL from .env
    python3 tools/status.py --run-dir runs/x --airdrop 0x... --rpc-url https://...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def read_state(rpc: common.Rpc, airdrop: str, block: str) -> dict:
    sigs = [
        ("token", "token()", common.decode_address),
        ("reserve", "reserve()", common.decode_address),
        ("admin", "admin()", common.decode_address),
        ("executor", "executor()", common.decode_address),
        ("root", "root()", common.decode_bytes32),
        ("listHash", "listHash()", common.decode_bytes32),
        ("batchCount", "batchCount()", common.decode_word),
        ("totalAmount", "totalAmount()", common.decode_word),
        ("executedBatches", "executedBatches()", common.decode_word),
        ("distributedAmount", "distributedAmount()", common.decode_word),
        ("paused", "paused()", lambda v: bool(common.decode_word(v))),
    ]
    raw = rpc.batch([("eth_call", [{"to": airdrop, "data": common.encode_call(sig)}, block]) for _, sig, _ in sigs])
    state = {name: decode(value) for (name, _, decode), value in zip(sigs, raw)}

    words = (state["batchCount"] + 255) // 256
    raw_words = rpc.batch(
        [("eth_call", [{"to": airdrop, "data": common.encode_call("executedBitmap(uint256)", w)}, block]) for w in range(words)]
    )
    executed = []
    for w, value in enumerate(raw_words):
        bits = common.decode_word(value)
        for i in range(256):
            index = w * 256 + i
            if index < state["batchCount"] and bits >> i & 1:
                executed.append(index)
    state["executed"] = executed
    state["pending"] = [i for i in range(state["batchCount"]) if i not in set(executed)]

    token = state["token"]
    balance, allowance = rpc.batch([
        ("eth_call", [{"to": token, "data": common.encode_call("balanceOf(address)", state["reserve"])}, block]),
        ("eth_call", [{"to": token, "data": common.encode_call("allowance(address,address)", state["reserve"], airdrop)}, block]),
    ])
    state["reserveBalance"] = common.decode_word(balance)
    state["allowance"] = common.decode_word(allowance)
    return state


def ranges(indexes: list[int]) -> str:
    if not indexes:
        return "none"
    out = []
    start = prev = indexes[0]
    for i in indexes[1:]:
        if i == prev + 1:
            prev = i
            continue
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = i
    out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, help="run directory (default: RUN_DIR from .env)")
    parser.add_argument("--rpc-url", help="JSON-RPC endpoint (default: RPC_URL from .env)")
    parser.add_argument("--airdrop", help="deployed contract address (default: AIRDROP_ADDRESS from .env)")
    parser.add_argument("--env", type=Path, default=common.default_env_path())
    args = parser.parse_args(argv)

    env = common.load_env(args.env)
    rpc_url = args.rpc_url or env.get("RPC_URL", "")
    airdrop = args.airdrop or env.get("AIRDROP_ADDRESS", "")
    run_dir = args.run_dir or (Path(env["RUN_DIR"]) if env.get("RUN_DIR") else None)
    if not airdrop:
        common.die("pass --airdrop or set AIRDROP_ADDRESS in .env")

    rpc = common.Rpc(rpc_url)
    block_number = rpc.block_number()
    state = read_state(rpc, airdrop, hex(block_number))

    print(f"chain id          : {rpc.chain_id()} (block {block_number})")
    print(f"contract          : {airdrop}")
    print(f"token             : {state['token']}")
    print(f"reserve           : {state['reserve']}")
    print(f"admin             : {state['admin']}")
    print(f"executor          : {state['executor']}")
    print(f"paused            : {state['paused']}")
    print(f"root              : {state['root']}")
    print(f"list sha256       : {state['listHash']}")
    print(f"batches           : {state['executedBatches']} of {state['batchCount']} executed")
    print(f"distributed       : {common.format_one(state['distributedAmount'])} of {common.format_one(state['totalAmount'])}")
    remaining = state["totalAmount"] - state["distributedAmount"]
    print(f"remaining         : {common.format_one(remaining)}")
    print(f"reserve balance   : {common.format_one(state['reserveBalance'])}")
    print(f"reserve allowance : {common.format_one(state['allowance'])}"
          + ("" if state["allowance"] == remaining else "   <-- differs from remaining amount"))
    print(f"executed batches  : {ranges(state['executed'])}")
    print(f"pending batches   : {ranges(state['pending'])}")

    if run_dir is not None and (run_dir / "manifest.json").is_file():
        manifest = common.read_manifest(run_dir)
        if manifest["root"].lower() != state["root"].lower():
            print(f"\nWARNING: {run_dir}/manifest.json root {manifest['root']} differs from the contract root")
            sys.exit(2)
        print(f"run directory     : {run_dir} (root matches)")


if __name__ == "__main__":
    main()
