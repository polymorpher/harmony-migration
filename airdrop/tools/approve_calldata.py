#!/usr/bin/env python3
"""Prepare the one transaction the reserve has to send: approve the airdrop contract for exactly
the total amount of the run.

    python3 tools/approve_calldata.py               # RUN_DIR, AIRDROP_ADDRESS, TOKEN_ADDRESS from .env

Prints the call data for `approve(address,uint256)` and writes a file that can be imported into the
Safe "Transaction Builder" app, so the multisig signers see exactly one call with two arguments:
the airdrop contract and the total. Nothing is sent by this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, help="run directory (default: RUN_DIR from .env)")
    parser.add_argument("--airdrop", help="deployed contract address (default: AIRDROP_ADDRESS from .env)")
    parser.add_argument("--token", help="token address (default: TOKEN_ADDRESS from .env)")
    parser.add_argument("--reserve", help="Safe address, for the file header (default: RESERVE_ADDRESS from .env)")
    parser.add_argument("--chain-id", type=int, help="chain id, for the file header (default: CHAIN_ID from .env)")
    parser.add_argument("--revoke", action="store_true", help="prepare approve(airdrop, 0) instead, to stop a run")
    parser.add_argument("--env", type=Path, default=common.default_env_path())
    args = parser.parse_args(argv)

    env = common.load_env(args.env)
    run_dir = args.run_dir or (Path(env["RUN_DIR"]) if env.get("RUN_DIR") else None)
    airdrop = args.airdrop or env.get("AIRDROP_ADDRESS", "")
    token = args.token or env.get("TOKEN_ADDRESS", "")
    reserve = args.reserve or env.get("RESERVE_ADDRESS", "")
    chain_id = args.chain_id or int(env.get("CHAIN_ID") or 0)
    if run_dir is None or not airdrop or not token:
        common.die("need RUN_DIR, AIRDROP_ADDRESS and TOKEN_ADDRESS (in .env or as flags)")

    try:
        manifest = common.read_manifest(run_dir)
        airdrop = common.normalize_address(airdrop, "--airdrop")
        token = common.normalize_address(token, "--token")
    except common.InputError as exc:
        common.die(str(exc))
        return

    amount = 0 if args.revoke else int(manifest["total_amount"])
    calldata = common.encode_call("approve(address,uint256)", airdrop, amount)

    print(f"token (to)        : {token}")
    print(f"spender (airdrop) : {airdrop}")
    print(f"amount (atto)     : {amount}")
    print(f"amount (tokens)   : {common.format_one(amount)}")
    print(f"call data         : {calldata}")
    print(f"root in manifest  : {manifest['root']}")
    print("signers should confirm on Etherscan that the spender's root() equals the root above")

    safe_tx = {
        "version": "1.0",
        "chainId": str(chain_id) if chain_id else "",
        "createdAt": int(time.time() * 1000),
        "meta": {
            "name": f"{'Revoke' if args.revoke else 'Approve'} airdrop {airdrop}",
            "description": f"approve(spender, amount) on the token; root {manifest['root']}",
            "txBuilderVersion": "1.16.0",
            "createdFromSafeAddress": reserve,
        },
        "transactions": [
            {
                "to": token,
                "value": "0",
                "data": None,
                "contractMethod": {
                    "inputs": [
                        {"name": "spender", "type": "address", "internalType": "address"},
                        {"name": "value", "type": "uint256", "internalType": "uint256"},
                    ],
                    "name": "approve",
                    "payable": False,
                },
                "contractInputsValues": {"spender": airdrop, "value": str(amount)},
            }
        ],
    }
    suffix = "revoke" if args.revoke else "approve"
    out = run_dir / f"safe-{suffix}-{chain_id or 'chain'}.json"
    out.write_text(json.dumps(safe_tx, indent=1) + "\n", encoding="utf-8")
    print(f"Safe tx builder   : {out}")


if __name__ == "__main__":
    main()
