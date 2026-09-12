#!/usr/bin/env python3

"""Export per-validator active delegation principal from historical RPC state."""

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
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--block", required=True, type=int)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--max-pages", type=int, default=10000)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def secure_key(address):
    raw = bytes.fromhex(lib.normalize_address(address)[2:])
    return "0x" + lib.keccak256(raw).hex()


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(path)

    client = lib.RpcClient(args.rpc)
    block = client.call(
        "hmyv2_getBlockByNumber",
        [args.block, {"fullTx": False, "inclStaking": False}],
    )
    records = []
    seen_validators = set()
    seen_pairs = set()
    for page in range(args.max_pages):
        validators = client.call(
            "hmyv2_getAllValidatorInformationByBlockNumber",
            [page, args.block],
        )
        if not validators:
            break
        for info in validators:
            validator = lib.any_to_hex(info["validator"]["address"])
            if validator in seen_validators:
                raise ValueError(f"duplicate validator: {validator}")
            seen_validators.add(validator)
            for delegation in info["validator"]["delegations"]:
                delegator = lib.any_to_hex(
                    delegation["delegator-address"]
                )
                amount = int(delegation["amount"])
                if amount < 0:
                    raise ValueError(
                        f"negative active delegation: {validator} {delegator}"
                    )
                if amount == 0:
                    continue
                pair = (validator, delegator)
                if pair in seen_pairs:
                    raise ValueError(
                        f"duplicate validator/delegator pair: {pair}"
                    )
                seen_pairs.add(pair)
                records.append((validator, delegator, amount))
    else:
        raise ValueError("validator pagination exceeded --max-pages")

    records.sort(key=lambda record: (record[0], record[1]))
    partial = args.output + ".partial"
    with open(partial, "x", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            (
                "validator_address",
                "validator_secure_key",
                "delegator_address",
                "delegator_secure_key",
                "staked_to_vault_atto",
                "is_self_delegation",
            )
        )
        for validator, delegator, amount in records:
            writer.writerow(
                (
                    lib.to_checksum(validator),
                    secure_key(validator),
                    lib.to_checksum(delegator),
                    secure_key(delegator),
                    str(amount),
                    str(validator == delegator).lower(),
                )
            )
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, args.output)

    result = {
        "source": "Harmony archival RPC",
        "rpc": args.rpc,
        "block": args.block,
        "block_hash": block["hash"],
        "state_root": block["stateRoot"],
        "epoch": block["epoch"],
        "validators": len(seen_validators),
        "active_delegation_rows": len(records),
        "staked_to_vault_atto": str(
            sum(record[2] for record in records)
        ),
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
