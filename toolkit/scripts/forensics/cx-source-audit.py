#!/usr/bin/env python3

import argparse
import csv
import json
import time
import urllib.error
import urllib.request


CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
CX_PRECOMPILE = "0x00000000000000000000000000000000000000f9"


def bech32_polymod(values):
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def hrp_expand(hrp):
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def convert_bits(values, from_bits, to_bits):
    accumulator = 0
    bits = 0
    output = []
    maximum = (1 << to_bits) - 1
    for value in values:
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            output.append((accumulator >> bits) & maximum)
    if bits and ((accumulator << (to_bits - bits)) & maximum):
        raise ValueError("nonzero address padding")
    return bytes(output)


def decode_address(value):
    if value is None:
        return ""
    if value.startswith("0x"):
        return value.lower()
    if value != value.lower() or not value.startswith("one1"):
        raise ValueError(f"invalid Harmony address {value!r}")
    separator = value.rfind("1")
    hrp = value[:separator]
    data = [CHARSET.index(char) for char in value[separator + 1 :]]
    if len(data) < 6 or bech32_polymod(hrp_expand(hrp) + data) != 1:
        raise ValueError(f"invalid Harmony address checksum {value!r}")
    decoded = convert_bits(data[:-6], 5, 8)
    if len(decoded) != 20:
        raise ValueError(f"invalid Harmony address length {value!r}")
    return "0x" + decoded.hex()


def rpc(url, method, params):
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    for attempt in range(5):
        try:
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result["result"]
        except (OSError, RuntimeError, urllib.error.HTTPError):
            if attempt == 4:
                raise
            time.sleep(2**attempt)


def inspect_trace(root):
    precompile_calls = 0
    failed_precompile_paths = 0

    def visit(node, failed_ancestor):
        nonlocal precompile_calls, failed_precompile_paths
        failed = failed_ancestor or bool(node.get("error"))
        if node.get("to", "").lower() == CX_PRECOMPILE:
            precompile_calls += 1
            if failed:
                failed_precompile_paths += 1
        for child in node.get("calls", []):
            visit(child, failed)

    visit(root, False)
    return precompile_calls, failed_precompile_paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-source-block", type=int, default=0)
    parser.add_argument("--max-source-block", type=int)
    parser.add_argument("--source-shard", type=int)
    parser.add_argument("--destination-shard", type=int)
    parser.add_argument("--address", action="append", default=[])
    parser.add_argument("receipts", nargs="+")
    args = parser.parse_args()
    addresses = {address.lower() for address in args.address}

    rows = []
    for path in args.receipts:
        with open(path, newline="") as source:
            for row in csv.DictReader(source):
                source_block = int(
                    row.get("signed_source_block") or row["source_block"]
                )
                if source_block < args.min_source_block:
                    continue
                if (
                    args.max_source_block is not None
                    and source_block > args.max_source_block
                ):
                    continue
                if (
                    args.source_shard is not None
                    and int(
                        row.get("signed_source_shard")
                        or row.get("source_shard", 0)
                    )
                    != args.source_shard
                ):
                    continue
                if (
                    args.destination_shard is not None
                    and int(row.get("destination_shard", 0))
                    != args.destination_shard
                ):
                    continue
                if addresses and row["from"].lower() not in addresses:
                    continue
                row["receipt_file"] = path
                row["audit_source_block"] = str(source_block)
                rows.append(row)
    rows.sort(key=lambda row: int(row["audit_source_block"]))

    totals = {"rollback_leak": 0, "valid_source_debit": 0, "unclassified": 0}
    output_rows = []
    for index, row in enumerate(rows, 1):
        transaction = rpc(
            args.rpc, "hmyv2_getTransactionByHash", [row["tx_hash"]]
        )
        if transaction is None:
            raise RuntimeError(f"source transaction missing: {row['tx_hash']}")
        transaction_hashes = {
            transaction["hash"].lower(),
            transaction["ethHash"].lower(),
        }
        if row["tx_hash"].lower() not in transaction_hashes:
            raise RuntimeError(f"source transaction hash mismatch: {row['tx_hash']}")
        amount = int(row["amount_atto"])
        source_to = decode_address(transaction.get("to"))
        direct_source_debit = (
            source_to == CX_PRECOMPILE
            or transaction["shardID"] != transaction["toShardID"]
        )
        if direct_source_debit:
            receipt_status = ""
            trace = {}
            precompile_calls = int(source_to == CX_PRECOMPILE)
            failed_paths = 0
            classification = "valid_source_debit"
        else:
            receipt = rpc(
                args.rpc, "hmyv2_getTransactionReceipt", [transaction["hash"]]
            )
            receipt_status = receipt["status"]
            trace = rpc(
                args.rpc,
                "debug_traceTransaction",
                [transaction["hash"], {"tracer": "callTracer", "timeout": "60s"}],
            )
            precompile_calls, failed_paths = inspect_trace(trace)
            if failed_paths:
                classification = "rollback_leak"
            elif precompile_calls:
                classification = "valid_source_debit"
            else:
                classification = "unclassified"
        totals[classification] += amount
        output_rows.append(
            {
                **row,
                "source_transaction_hash": transaction["hash"],
                "source_transaction_from": decode_address(transaction["from"]),
                "source_transaction_to": source_to,
                "source_transaction_value_atto": transaction["value"],
                "source_transaction_status": receipt_status,
                "source_input_selector": transaction["input"][:10],
                "trace_root_error": trace.get("error", ""),
                "precompile_call_count": precompile_calls,
                "failed_precompile_path_count": failed_paths,
                "classification": classification,
            }
        )
        print(
            f"{index}/{len(rows)} source_block={row['audit_source_block']} "
            f"amount={amount} classification={classification}",
            flush=True,
        )

    fields = list(output_rows[0]) if output_rows else []
    with open(args.output, "x", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output_rows)

    print(
        json.dumps(
            {
                "receipts": len(rows),
                "rollback_leak_atto": str(totals["rollback_leak"]),
                "valid_source_debit_atto": str(totals["valid_source_debit"]),
                "unclassified_atto": str(totals["unclassified"]),
                "output": args.output,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
