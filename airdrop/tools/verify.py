#!/usr/bin/env python3
"""Check a run directory from scratch, and optionally check it against the deployed contract.

    python3 tools/verify.py --run-dir runs/mainnet-priority
    python3 tools/verify.py --run-dir runs/mainnet-priority --rpc-url https://... --airdrop 0x...

Starting from distribution.csv alone, the tool rebuilds every batch, every fingerprint and the root,
and compares them with manifest.json and with each batch file. With --rpc-url and --airdrop (or the
values in .env) it also reads root, batchCount, totalAmount and listHash from the contract.

This is the check anyone can run after downloading the published run directory. It does not need a
wallet and sends no transactions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def verify_run(run_dir: Path) -> dict:
    manifest = common.read_manifest(run_dir)
    rows = common.read_distribution(run_dir)
    problems = []

    seen = set()
    for row in rows:
        if row.address != common.to_checksum(row.address):
            problems.append(f"{row.source}: address is not in checksum form")
        if row.address.lower() in seen:
            problems.append(f"{row.source}: duplicate address {row.address}")
        seen.add(row.address.lower())
        if row.amount <= 0:
            problems.append(f"{row.source}: non-positive amount")

    batch_size = int(manifest["batch_size"])
    batches = common.chunk(rows, batch_size)
    leaves = [common.batch_leaf(i, [r.address for r in b], [r.amount for r in b]) for i, b in enumerate(batches)]
    levels = common.tree_levels(leaves)
    root = common.hex0x(common.tree_root(levels))
    total = sum(r.amount for r in rows)
    list_sha256 = common.sha256_file(run_dir / "distribution.csv")

    if len(batches) != int(manifest["batch_count"]):
        problems.append(f"batch_count: manifest says {manifest['batch_count']}, recomputed {len(batches)}")
    if len(rows) != int(manifest["recipient_count"]):
        problems.append(f"recipient_count: manifest says {manifest['recipient_count']}, recomputed {len(rows)}")
    if str(total) != str(manifest["total_amount"]):
        problems.append(f"total_amount: manifest says {manifest['total_amount']}, recomputed {total}")
    if root != manifest["root"]:
        problems.append(f"root: manifest says {manifest['root']}, recomputed {root}")
    if list_sha256 != manifest["list_sha256"]:
        problems.append(f"list_sha256: manifest says {manifest['list_sha256']}, file is {list_sha256}")

    for i, b in enumerate(batches):
        try:
            payload = common.read_batch(run_dir, i)
        except (FileNotFoundError, common.InputError) as exc:
            problems.append(f"batch {i}: {exc}")
            continue
        if payload["recipients"] != [r.address for r in b]:
            problems.append(f"batch {i}: recipients differ from distribution.csv")
        if [int(a) for a in payload["amounts"]] != [r.amount for r in b]:
            problems.append(f"batch {i}: amounts differ from distribution.csv")
        if payload["leaf"] != common.hex0x(leaves[i]):
            problems.append(f"batch {i}: leaf differs ({payload['leaf']} vs {common.hex0x(leaves[i])})")
        proof = [common.unhex(p) for p in payload["proof"]]
        if proof != common.tree_proof(levels, i):
            problems.append(f"batch {i}: proof differs from recomputed proof")
        if not common.tree_verify(proof, common.tree_root(levels), leaves[i]):
            problems.append(f"batch {i}: proof does not reach the root")

    return {
        "manifest": manifest,
        "root": root,
        "batch_count": len(batches),
        "recipient_count": len(rows),
        "total_amount": total,
        "list_sha256": list_sha256,
        "problems": problems,
    }


def verify_contract(rpc: common.Rpc, airdrop: str, result: dict) -> list[str]:
    problems = []
    reads = {
        "root": ("root()", common.decode_bytes32, result["root"]),
        "batchCount": ("batchCount()", common.decode_word, result["batch_count"]),
        "totalAmount": ("totalAmount()", common.decode_word, result["total_amount"]),
        "listHash": ("listHash()", common.decode_bytes32, result["list_sha256"]),
    }
    raw = rpc.batch([("eth_call", [{"to": airdrop, "data": common.encode_call(sig)}, "latest"]) for sig, _, _ in reads.values()])
    for (name, (_, decode, expected)), value in zip(reads.items(), raw):
        got = decode(value)
        if str(got).lower() != str(expected).lower():
            problems.append(f"on-chain {name} is {got}, run directory has {expected}")
    for name, sig in (("token", "token()"), ("reserve", "reserve()"), ("admin", "admin()"), ("executor", "executor()")):
        print(f"{name:<17}: {common.decode_address(rpc.eth_call(airdrop, common.encode_call(sig)))}")
    return problems


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, help="run directory (default: RUN_DIR from .env)")
    parser.add_argument("--rpc-url", help="JSON-RPC endpoint (default: RPC_URL from .env)")
    parser.add_argument("--airdrop", help="deployed contract address (default: AIRDROP_ADDRESS from .env)")
    parser.add_argument("--offline", action="store_true", help="skip the on-chain comparison even if .env has an address")
    parser.add_argument("--env", type=Path, default=common.default_env_path(), help="path to .env")
    args = parser.parse_args(argv)

    env = common.load_env(args.env)
    run_dir = args.run_dir or Path(env.get("RUN_DIR", ""))
    if not str(run_dir):
        common.die("pass --run-dir or set RUN_DIR in .env")

    try:
        result = verify_run(run_dir)
    except (common.InputError, FileNotFoundError) as exc:
        common.die(str(exc))
        return

    print(f"run directory    : {run_dir}")
    print(f"root             : {result['root']}")
    print(f"batches          : {result['batch_count']}")
    print(f"recipients       : {result['recipient_count']}")
    print(f"total (atto)     : {result['total_amount']}")
    print(f"total (tokens)   : {common.format_one(result['total_amount'])}")
    print(f"list sha256      : {result['list_sha256']}")

    problems = list(result["problems"])
    airdrop = args.airdrop or env.get("AIRDROP_ADDRESS", "")
    rpc_url = args.rpc_url or env.get("RPC_URL", "")
    if airdrop and rpc_url and not args.offline:
        rpc = common.Rpc(rpc_url)
        print(f"chain id         : {rpc.chain_id()}")
        print(f"contract         : {airdrop}")
        problems += verify_contract(rpc, airdrop, result)
    else:
        print("contract         : (not checked; pass --airdrop and --rpc-url or set them in .env)")

    if problems:
        print(f"\nFAILED: {len(problems)} problem(s)")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nOK: the run directory is internally consistent" + (" and matches the contract" if airdrop and rpc_url and not args.offline else ""))


if __name__ == "__main__":
    main()
