#!/usr/bin/env python3

import argparse
import copy
import hashlib
import json
import os
import time
import urllib.request


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--shard0-interval-summary", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--shard0-cutoff", type=int, required=True)
    parser.add_argument("--shard1-cutoff", type=int, required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rpc(url, method, params):
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    last_error = None
    for attempt in range(5):
        try:
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result["result"]
        except Exception as error:
            last_error = error
            if attempt == 4:
                break
            time.sleep(attempt + 1)
    raise RuntimeError(f"RPC failed: {last_error}")


def block_number(receipt):
    for name in ("blockNumber", "block_number"):
        if name in receipt and receipt[name] is not None:
            value = receipt[name]
            return int(value, 16) if isinstance(value, str) else int(value)
    raise ValueError(f"CX receipt has no block number: {receipt}")


def main():
    args = parse_args()
    if os.path.exists(args.output) or os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)
    with open(args.input) as source:
        original = json.load(source)
    with open(args.shard0_interval_summary) as source:
        interval = json.load(source)
    if interval["start"] != 93529888 or interval["end"] != args.shard0_cutoff:
        raise ValueError("unexpected shard-0 interval")
    if interval["cross_shard_transactions"] != 0:
        raise ValueError("new cross-shard source transactions require full reclassification")

    checked = []
    for direction in original["directions"]:
        for group in direction.get("pending_groups") or []:
            for receipt in group.get("receipts") or []:
                tx_hash = receipt["transaction_hash"]
                consumed = rpc(args.rpc, "hmyv2_getCXReceiptByHash", [tx_hash])
                consumed_block = block_number(consumed) if consumed else None
                if consumed_block is not None and consumed_block <= args.shard0_cutoff:
                    raise ValueError(
                        f"previously pending receipt {tx_hash} was consumed "
                        f"at block {consumed_block}"
                    )
                checked.append(
                    {
                        "transaction_hash": tx_hash,
                        "result": (
                            "not_found"
                            if consumed is None
                            else "consumed_after_cutoff"
                        ),
                        "consumed_block": consumed_block,
                    }
                )

    result = copy.deepcopy(original)
    result["cutoff"] = {
        "shard0_block": args.shard0_cutoff,
        "shard1_block": args.shard1_cutoff,
    }
    result["cutoff_verification"] = {
        "method": "canonical interval plus destination CX lookup",
        "source_report_sha256": file_sha256(args.input),
        "shard0_interval_summary_sha256": file_sha256(
            args.shard0_interval_summary
        ),
        "new_cross_shard_transactions": 0,
        "pending_receipts_checked": len(checked),
        "pending_receipt_results": checked,
    }
    with open(args.output + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(
        json.dumps(
            {
                "output": args.output,
                "output_sha256": file_sha256(args.output),
                "pending_active_atto": result["pending_active_atto"],
                "pending_receipts_checked": len(checked),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
