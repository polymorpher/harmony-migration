#!/usr/bin/env python3

"""Fill missing shard-0 code metadata at an exact historical block."""

import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--rpc")
    parser.add_argument(
        "--code-cache",
        help="optional JSON address-to-eth_getCode map from the exact block",
    )
    parser.add_argument(
        "--preserve-code-cache",
        help="optional normalized copy of the supplied code cache",
    )
    parser.add_argument("--block", required=True, type=int)
    parser.add_argument("--block-hash")
    parser.add_argument("--state-root")
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="atomically replace existing output and summary files",
    )
    args = parser.parse_args()
    if not args.rpc and not args.code_cache:
        parser.error("one of --rpc or --code-cache is required")
    if args.code_cache and (not args.block_hash or not args.state_root):
        parser.error(
            "--code-cache requires --block-hash and --state-root"
        )
    return args


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_hash(code):
    raw = bytes.fromhex(code[2:] if code.startswith("0x") else code)
    return "0x" + lib.keccak256(raw).hex()


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path)
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)

    addresses = []
    seen = set()
    with open(args.input, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "address",
            "nonce_shard0",
            "code_hash_shard0",
            "total_claim_atto",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"input is missing fields: {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            if row["code_hash_shard0"]:
                continue
            address = lib.normalize_address(row["address"])
            if address in seen:
                raise ValueError(f"duplicate address at line {line}")
            seen.add(address)
            addresses.append(address)

    metadata = {}
    code_bearing = 0
    if args.code_cache:
        with open(args.code_cache, encoding="utf-8") as source:
            cached = {
                lib.normalize_address(address): code
                for address, code in json.load(source).items()
            }
        block = {"hash": args.block_hash, "stateRoot": args.state_root}
        source_kind = "recorded eth_getCode cache"
        if args.preserve_code_cache:
            if os.path.exists(args.preserve_code_cache) or os.path.exists(
                args.preserve_code_cache + ".partial"
            ):
                raise FileExistsError(args.preserve_code_cache)
            with open(
                args.preserve_code_cache + ".partial",
                "x",
                encoding="utf-8",
            ) as output:
                json.dump(cached, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(
                args.preserve_code_cache + ".partial",
                args.preserve_code_cache,
            )
        for address in addresses:
            if address not in cached:
                raise ValueError(f"code cache is missing {address}")
            code = cached[address]
            if not isinstance(code, str) or not code.startswith("0x"):
                raise ValueError(f"invalid cached code for {address}")
            code_bearing += int(code != "0x")
            metadata[address] = {
                "code_hash_shard0": code_hash(code),
                "nonce_shard0": None,
                "code_bytes": (len(code) - 2) // 2,
            }
    else:
        client = lib.RpcClient(args.rpc)
        block = client.call(
            "hmyv2_getBlockByNumber",
            [args.block, {"fullTx": False, "inclStaking": False}],
        )
        block_tag = hex(args.block)
        calls = []
        for address in addresses:
            calls.extend(
                (
                    ("eth_getCode", [address, block_tag]),
                    ("eth_getTransactionCount", [address, block_tag]),
                )
            )
        responses = client.batch(calls)
        source_kind = "live archival RPC"
        for index, address in enumerate(addresses):
            code, code_error = responses[index * 2]
            nonce, nonce_error = responses[index * 2 + 1]
            if code_error or nonce_error:
                raise ValueError(
                    f"RPC metadata lookup failed for {address}: "
                    f"code={code_error} nonce={nonce_error}"
                )
            if not isinstance(code, str) or not code.startswith("0x"):
                raise ValueError(f"invalid code result for {address}")
            if not isinstance(nonce, str) or not nonce.startswith("0x"):
                raise ValueError(f"invalid nonce result for {address}")
            code_bearing += int(code != "0x")
            metadata[address] = {
                "code_hash_shard0": code_hash(code),
                "nonce_shard0": str(int(nonce, 16)),
                "code_bytes": (len(code) - 2) // 2,
            }

    rows = 0
    updated = 0
    with open(args.input, newline="") as source, open(
        args.output + ".partial", "x", newline=""
    ) as output:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(
            output, fieldnames=reader.fieldnames, lineterminator="\n"
        )
        writer.writeheader()
        for row in reader:
            address = lib.normalize_address(row["address"])
            replacement = metadata.get(address)
            if replacement is not None:
                row["code_hash_shard0"] = replacement["code_hash_shard0"]
                if replacement["nonce_shard0"] is not None:
                    row["nonce_shard0"] = replacement["nonce_shard0"]
                updated += 1
            writer.writerow(row)
            rows += 1
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)

    result = {
        "status": "passed",
        "input": args.input,
        "input_sha256": file_sha256(args.input),
        "output": args.output,
        "output_sha256": file_sha256(args.output),
        "rows": rows,
        "queried_missing_metadata": len(addresses),
        "updated_rows": updated,
        "code_bearing_among_queried": code_bearing,
        "block": args.block,
        "block_hash": block["hash"],
        "state_root": block["stateRoot"],
        "rpc": args.rpc,
        "source_kind": source_kind,
    }
    if args.preserve_code_cache:
        result["preserved_code_cache"] = args.preserve_code_cache
        result["preserved_code_cache_sha256"] = file_sha256(
            args.preserve_code_cache
        )
    with open(args.summary + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
