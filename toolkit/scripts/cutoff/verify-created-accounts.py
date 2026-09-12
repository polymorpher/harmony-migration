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
    parser.add_argument("--state-diff", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--old-block", type=int, required=True)
    parser.add_argument("--new-block", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
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
    for attempt in range(6):
        try:
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result["result"]
        except Exception as error:
            last_error = error
            if attempt == 5:
                break
            time.sleep(attempt + 1)
    raise RuntimeError(f"{method} failed: {last_error}")


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
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            by_id = {item["id"]: item for item in result}
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
    raise RuntimeError(f"batch failed: {last_error}")


def exists(balance, nonce, code):
    return int(balance, 16) != 0 or int(nonce, 16) != 0 or code not in ("0x", "0x0")


def address_values(value, field=""):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in (
                "from",
                "to",
                "address",
                "refundaddress",
                "author",
                "beneficiary",
            ):
                if isinstance(item, str) and len(item) == 42 and item.startswith("0x"):
                    result.add(item.lower())
            result.update(address_values(item, key))
    elif isinstance(value, list):
        for item in value:
            result.update(address_values(item, field))
    return result


def transaction_hash_for_address(traces, address):
    address = address.lower()
    for trace in traces:
        if address in address_values(trace):
            return trace.get("transactionHash") or "", "trace"
    return "", ""


def main():
    args = parse_args()
    for path in (args.output, args.summary):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(path)
    with open(args.state_diff, newline="") as source:
        created = [
            row for row in csv.DictReader(source) if row["change_type"] == "created"
        ]
    bounds = {
        row["address"].lower(): [args.old_block + 1, args.new_block]
        for row in created
    }
    by_address = {row["address"].lower(): row for row in created}

    while any(low < high for low, high in bounds.values()):
        active = [
            (address, low + (high - low) // 2)
            for address, (low, high) in bounds.items()
            if low < high
        ]
        calls = []
        for address, block in active:
            tag = hex(block)
            calls.extend(
                (
                    ("eth_getBalance", [address, tag]),
                    ("eth_getTransactionCount", [address, tag]),
                    ("eth_getCode", [address, tag]),
                )
            )
        values = rpc_batch(args.rpc, calls)
        for index, (address, block) in enumerate(active):
            present = exists(
                values[index * 3],
                values[index * 3 + 1],
                values[index * 3 + 2],
            )
            if present:
                bounds[address][1] = block
            else:
                bounds[address][0] = block + 1

    output_rows = []
    trace_matches = 0
    block_matches = 0
    protocol_only = 0
    for address in sorted(bounds):
        block = bounds[address][0]
        traces = rpc(args.rpc, "trace_block", [hex(block)])
        transaction_hash, evidence = transaction_hash_for_address(traces, address)
        if evidence:
            trace_matches += 1
        else:
            full_block = rpc(
                args.rpc, "eth_getBlockByNumber", [hex(block), True]
            )
            for transaction in full_block.get("transactions") or []:
                if address in (
                    (transaction.get("from") or "").lower(),
                    (transaction.get("to") or "").lower(),
                ):
                    transaction_hash = transaction.get("hash") or ""
                    evidence = "block_transaction"
                    block_matches += 1
                    break
        if not evidence:
            evidence = "protocol_state_transition"
            protocol_only += 1
        row = by_address[address]
        output_rows.append(
            {
                "secure_key": row["secure_key"],
                "address": row["address"],
                "transition_block": block,
                "transaction_hash": transaction_hash,
                "evidence": evidence,
                "new_balance_atto": row["new_balance_atto"],
                "new_nonce": row["new_nonce"],
                "new_code_hash": row["new_code_hash"],
            }
        )

    fields = list(output_rows[0])
    with open(args.output + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output_rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    result = {
        "status": "passed",
        "created_accounts": len(created),
        "trace_matches": trace_matches,
        "block_transaction_matches": block_matches,
        "protocol_state_transitions": protocol_only,
        "output": args.output,
        "output_sha256": file_sha256(args.output),
        "state_diff_sha256": file_sha256(args.state_diff),
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
