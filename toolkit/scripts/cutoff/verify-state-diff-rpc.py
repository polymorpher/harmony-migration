#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import time
import urllib.request


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--old-block", required=True, type=int)
    parser.add_argument("--new-block", required=True, type=int)
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rpc_batch(url, calls):
    payload = [
        {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
        for index, (method, params) in enumerate(calls)
    ]
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    last_error = None
    for attempt in range(6):
        try:
            request = urllib.request.Request(
                url, data=encoded, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
            by_id = {item["id"]: item for item in result}
            if len(by_id) != len(calls):
                raise RuntimeError("incomplete batch response")
            values = []
            for index in range(len(calls)):
                item = by_id[index]
                if item.get("error"):
                    raise RuntimeError(item["error"])
                values.append(item["result"])
            return values
        except Exception as error:
            last_error = error
            if attempt == 5:
                break
            time.sleep(attempt + 1)
    raise RuntimeError(f"RPC batch failed: {last_error}")


def main():
    args = parse_args()
    if os.path.exists(args.output) or os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)
    with open(args.input, newline="") as source:
        rows = list(csv.DictReader(source))
    counts = {"created": 0, "changed": 0, "deleted": 0}
    positive_delta = 0
    negative_delta = 0
    old_tag = hex(args.old_block)
    new_tag = hex(args.new_block)

    for start in range(0, len(rows), args.batch_size):
        batch = rows[start : start + args.batch_size]
        calls = []
        for row in batch:
            address = row["address"]
            if not address:
                raise ValueError(f"missing address for {row['secure_key']}")
            calls.extend(
                (
                    ("eth_getBalance", [address, old_tag]),
                    ("eth_getBalance", [address, new_tag]),
                    ("eth_getTransactionCount", [address, old_tag]),
                    ("eth_getTransactionCount", [address, new_tag]),
                )
            )
        values = rpc_batch(args.rpc, calls)
        for index, row in enumerate(batch):
            old_balance = int(values[index * 4], 16)
            new_balance = int(values[index * 4 + 1], 16)
            old_nonce = int(values[index * 4 + 2], 16)
            new_nonce = int(values[index * 4 + 3], 16)
            if old_balance != int(row["old_balance_atto"]):
                raise ValueError(f"old balance mismatch for {row['address']}")
            if new_balance != int(row["new_balance_atto"]):
                raise ValueError(f"new balance mismatch for {row['address']}")
            if old_nonce != int(row["old_nonce"]):
                raise ValueError(f"old nonce mismatch for {row['address']}")
            if new_nonce != int(row["new_nonce"]):
                raise ValueError(f"new nonce mismatch for {row['address']}")
            delta = new_balance - old_balance
            if delta != int(row["balance_delta_atto"]):
                raise ValueError(f"delta mismatch for {row['address']}")
            if delta > 0:
                positive_delta += delta
            elif delta < 0:
                negative_delta -= delta
            counts[row["change_type"]] += 1
        print(f"progress verified={min(start + len(batch), len(rows))}/{len(rows)}")

    result = {
        "status": "passed",
        "input": args.input,
        "input_sha256": file_sha256(args.input),
        "rpc": args.rpc,
        "old_block": args.old_block,
        "new_block": args.new_block,
        "rows": len(rows),
        "change_counts": counts,
        "positive_delta_atto": str(positive_delta),
        "negative_delta_atto": str(negative_delta),
        "net_delta_atto": str(positive_delta - negative_delta),
        "balance_checks": len(rows) * 2,
        "nonce_checks": len(rows) * 2,
    }
    with open(args.output + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
