#!/usr/bin/env python3

"""Resolve cutoff metadata for WONE-only addresses above the threshold."""

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


ATTO_PER_ONE = 10**18
EMPTY_CODE_HASH = (
    "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
)
WONE_ADDRESS = "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a"
FIELDS = (
    "address",
    "wone_balance_atto",
    "combined_total_atto",
    "claim_row_present",
    "nonce_shard0",
    "code_hash_shard0",
    "code_status",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-claims", required=True)
    parser.add_argument("--wone-holders", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--block", required=True, type=int)
    parser.add_argument("--block-hash", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--minimum-one", type=int, default=1000)
    parser.add_argument("--exclude-address", action="append", default=[])
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_address(value):
    address = lib.normalize_address(value)
    if address is None or len(address) != 42:
        raise ValueError(f"invalid address: {value!r}")
    return address


def code_hash(code):
    raw = bytes.fromhex(code[2:])
    return "0x" + lib.keccak256(raw).hex()


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path + ".partial")
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    threshold = args.minimum_one * ATTO_PER_ONE
    excluded = {
        normalize_address(address) for address in args.exclude_address
    }
    excluded.add(WONE_ADDRESS)
    candidates = {}
    with open(args.wone_holders, newline="") as source:
        reader = csv.DictReader(source)
        for line, row in enumerate(reader, start=2):
            address = normalize_address(row["address"])
            amount = int(row["wone_balance_atto"])
            if amount < 0:
                raise ValueError(f"negative WONE at line {line}")
            if amount >= threshold and address not in excluded:
                candidates[address] = amount

    native_matches = 0
    with open(args.native_claims, newline="") as source:
        reader = csv.DictReader(source)
        if "address" not in set(reader.fieldnames or ()):
            raise ValueError("native claim input is missing address")
        for row in reader:
            address = normalize_address(row["address"])
            if address in candidates:
                candidates.pop(address)
                native_matches += 1

    client = lib.RpcClient(args.rpc)
    block = client.call(
        "hmyv2_getBlockByNumber",
        [args.block, {"fullTx": False, "inclStaking": False}],
    )
    if (
        not block
        or str(block.get("hash", "")).lower()
        != args.block_hash.lower()
        or str(block.get("stateRoot", "")).lower()
        != args.state_root.lower()
    ):
        raise ValueError("archival RPC cutoff identity mismatch")
    addresses = sorted(candidates)
    responses = client.batch(
        [
            call
            for address in addresses
            for call in (
                ("eth_getCode", [address, hex(args.block)]),
                ("eth_getTransactionCount", [address, hex(args.block)]),
            )
        ]
    )
    rows = []
    code_bearing = 0
    for index, address in enumerate(addresses):
        code, code_error = responses[index * 2]
        nonce, nonce_error = responses[index * 2 + 1]
        if code_error or nonce_error:
            raise ValueError(
                f"metadata lookup failed for {address}: "
                f"code={code_error} nonce={nonce_error}"
            )
        if not isinstance(code, str) or not code.startswith("0x"):
            raise ValueError(f"invalid code for {address}")
        if not isinstance(nonce, str) or not nonce.startswith("0x"):
            raise ValueError(f"invalid nonce for {address}")
        digest = code_hash(code)
        status = "code_less" if digest == EMPTY_CODE_HASH else "code_bearing"
        code_bearing += int(status == "code_bearing")
        rows.append(
            {
                "address": lib.to_checksum(address),
                "wone_balance_atto": str(candidates[address]),
                "combined_total_atto": str(candidates[address]),
                "claim_row_present": "false",
                "nonce_shard0": str(int(nonce, 16)),
                "code_hash_shard0": digest,
                "code_status": status,
            }
        )

    with open(args.output + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    result = {
        "schema_version": 1,
        "status": "passed",
        "native_claims": args.native_claims,
        "native_claims_sha256": file_sha256(args.native_claims),
        "wone_holders": args.wone_holders,
        "wone_holders_sha256": file_sha256(args.wone_holders),
        "minimum_atto": str(threshold),
        "wone_holders_at_or_above_threshold": len(candidates) + native_matches,
        "native_claim_matches": native_matches,
        "wone_only_rows": len(rows),
        "code_bearing_rows": code_bearing,
        "block": args.block,
        "block_hash": block["hash"],
        "state_root": block["stateRoot"],
        "rpc": args.rpc,
        "output": args.output,
        "output_sha256": file_sha256(args.output),
    }
    with open(args.summary + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
