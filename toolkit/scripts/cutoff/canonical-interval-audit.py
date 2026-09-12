#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import time
import urllib.request


EMPTY_ROOT = "0x56e81f171bcc55a6ff8345e692c0f86e5b48e01b996cadc001622fb5e363b421"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--shard", required=True, type=int)
    parser.add_argument("--start", required=True, type=int)
    parser.add_argument("--end", required=True, type=int)
    parser.add_argument("--material-blocks", required=True)
    parser.add_argument("--transactions", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--chunk-size", type=int, default=512)
    return parser.parse_args()


def rpc(url, method, params, timeout=120):
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        separators=(",", ":"),
    ).encode()
    last_error = None
    for attempt in range(6):
        try:
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result["result"]
        except Exception as error:
            last_error = error
            if attempt == 5:
                break
            time.sleep(attempt + 1)
    raise RuntimeError(f"{method} failed after retries: {last_error}")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def transaction_value(transaction, *names):
    for name in names:
        if name in transaction and transaction[name] is not None:
            return transaction[name]
    return ""


def write_transaction(writer, shard, block, kind, index, transaction):
    writer.writerow(
        {
            "shard": shard,
            "block_number": block["number"],
            "block_hash": block["hash"],
            "timestamp": block["timestamp"],
            "kind": kind,
            "index": index,
            "transaction_hash": transaction_value(
                transaction, "hash", "transactionHash"
            ),
            "from": transaction_value(transaction, "from"),
            "to": transaction_value(transaction, "to"),
            "from_shard": transaction_value(
                transaction, "shardID", "fromShard"
            ),
            "to_shard": transaction_value(
                transaction, "toShardID", "toShard"
            ),
            "value": transaction_value(transaction, "value", "amount"),
            "nonce": transaction_value(transaction, "nonce"),
            "transaction_json": json.dumps(
                transaction, sort_keys=True, separators=(",", ":")
            ),
        }
    )


def main():
    args = parse_args()
    if args.shard not in (0, 1):
        raise ValueError("shard must be 0 or 1")
    if args.start <= 0 or args.end < args.start:
        raise ValueError("invalid block range")
    if args.chunk_size < 1 or args.chunk_size > 1024:
        raise ValueError("chunk size must be between 1 and 1024")

    outputs = (args.material_blocks, args.transactions, args.summary)
    for output in outputs:
        if os.path.exists(output) or os.path.exists(output + ".partial"):
            raise FileExistsError(output)

    predecessor = rpc(
        args.rpc,
        "hmyv2_getBlockByNumber",
        [args.start - 1, {"fullTx": False, "inclStaking": False}],
    )
    expected_parent = predecessor["hash"]
    previous_root = predecessor["stateRoot"]
    previous_timestamp = predecessor["timestamp"]
    header_digest = hashlib.sha256()
    distinct_roots = {previous_root}

    material_fields = [
        "shard",
        "number",
        "hash",
        "parent_hash",
        "timestamp",
        "state_root",
        "transactions_root",
        "receipts_root",
        "outgoing_receipts_root",
        "incoming_receipts_root",
        "gas_used",
        "transaction_count",
        "staking_transaction_count",
        "state_root_changed",
    ]
    transaction_fields = [
        "shard",
        "block_number",
        "block_hash",
        "timestamp",
        "kind",
        "index",
        "transaction_hash",
        "from",
        "to",
        "from_shard",
        "to_shard",
        "value",
        "nonce",
        "transaction_json",
    ]

    summary = {
        "rpc": args.rpc,
        "shard": args.shard,
        "start": args.start,
        "end": args.end,
        "predecessor_number": args.start - 1,
        "predecessor_hash": predecessor["hash"],
        "predecessor_state_root": predecessor["stateRoot"],
        "blocks": 0,
        "material_blocks": 0,
        "state_root_changes": 0,
        "normal_transactions": 0,
        "staking_transactions": 0,
        "cross_shard_transactions": 0,
        "nonempty_receipts_roots": 0,
        "nonempty_outgoing_receipts_roots": 0,
        "nonempty_incoming_receipts_roots": 0,
        "gas_used": 0,
    }

    material_partial = args.material_blocks + ".partial"
    transactions_partial = args.transactions + ".partial"
    try:
        with open(material_partial, "x", newline="") as material_file, open(
            transactions_partial, "x", newline=""
        ) as transaction_file:
            material_writer = csv.DictWriter(
                material_file, fieldnames=material_fields, lineterminator="\n"
            )
            transaction_writer = csv.DictWriter(
                transaction_file,
                fieldnames=transaction_fields,
                lineterminator="\n",
            )
            material_writer.writeheader()
            transaction_writer.writeheader()

            next_number = args.start
            while next_number <= args.end:
                chunk_end = min(args.end, next_number + args.chunk_size - 1)
                blocks = rpc(
                    args.rpc,
                    "hmyv2_getBlocks",
                    [
                        next_number,
                        chunk_end,
                        {
                            "fullTx": True,
                            "inclStaking": True,
                            "withSigners": False,
                        },
                    ],
                    timeout=180,
                )
                if len(blocks) != chunk_end - next_number + 1:
                    raise RuntimeError(
                        f"range {next_number}..{chunk_end} returned "
                        f"{len(blocks)} blocks"
                    )
                for block in blocks:
                    number = int(block["number"])
                    if number != next_number:
                        raise RuntimeError(
                            f"expected block {next_number}, got {number}"
                        )
                    if block["parentHash"].lower() != expected_parent.lower():
                        raise RuntimeError(
                            f"parent mismatch at block {number}: "
                            f"{block['parentHash']} != {expected_parent}"
                        )
                    timestamp = int(block["timestamp"])
                    if timestamp < int(previous_timestamp):
                        raise RuntimeError(f"timestamp regressed at block {number}")

                    transactions = block.get("transactions") or []
                    staking = block.get("stakingTransactions") or []
                    state_root = block["stateRoot"]
                    state_changed = state_root.lower() != previous_root.lower()
                    tx_root = block.get("transactionsRoot") or ""
                    receipt_root = block.get("receiptsRoot") or ""
                    outgoing_root = block.get("outgoingReceiptsRoot") or ""
                    incoming_root = block.get("incomingReceiptsRoot") or ""
                    gas_used = int(block.get("gasUsed") or 0)

                    summary["blocks"] += 1
                    summary["normal_transactions"] += len(transactions)
                    summary["staking_transactions"] += len(staking)
                    summary["gas_used"] += gas_used
                    summary["state_root_changes"] += int(state_changed)
                    summary["nonempty_receipts_roots"] += int(
                        bool(receipt_root)
                        and receipt_root.lower() != EMPTY_ROOT.lower()
                    )
                    summary["nonempty_outgoing_receipts_roots"] += int(
                        bool(outgoing_root)
                        and outgoing_root.lower() != EMPTY_ROOT.lower()
                    )
                    summary["nonempty_incoming_receipts_roots"] += int(
                        bool(incoming_root)
                        and incoming_root.lower() != EMPTY_ROOT.lower()
                    )
                    distinct_roots.add(state_root)

                    for index, transaction in enumerate(transactions):
                        write_transaction(
                            transaction_writer,
                            args.shard,
                            block,
                            "regular",
                            index,
                            transaction,
                        )
                        from_shard = transaction_value(
                            transaction, "shardID", "fromShard"
                        )
                        to_shard = transaction_value(
                            transaction, "toShardID", "toShard"
                        )
                        summary["cross_shard_transactions"] += int(
                            from_shard != ""
                            and to_shard != ""
                            and int(from_shard) != int(to_shard)
                        )
                    for index, transaction in enumerate(staking):
                        write_transaction(
                            transaction_writer,
                            args.shard,
                            block,
                            "staking",
                            index,
                            transaction,
                        )

                    material = (
                        state_changed
                        or transactions
                        or staking
                        or gas_used
                        or (
                            receipt_root
                            and receipt_root.lower() != EMPTY_ROOT.lower()
                        )
                        or (
                            outgoing_root
                            and outgoing_root.lower() != EMPTY_ROOT.lower()
                        )
                        or (
                            incoming_root
                            and incoming_root.lower() != EMPTY_ROOT.lower()
                        )
                    )
                    if material:
                        material_writer.writerow(
                            {
                                "shard": args.shard,
                                "number": number,
                                "hash": block["hash"],
                                "parent_hash": block["parentHash"],
                                "timestamp": timestamp,
                                "state_root": state_root,
                                "transactions_root": tx_root,
                                "receipts_root": receipt_root,
                                "outgoing_receipts_root": outgoing_root,
                                "incoming_receipts_root": incoming_root,
                                "gas_used": gas_used,
                                "transaction_count": len(transactions),
                                "staking_transaction_count": len(staking),
                                "state_root_changed": str(state_changed).lower(),
                            }
                        )
                        summary["material_blocks"] += 1

                    header_digest.update(
                        (
                            f"{number}|{block['hash'].lower()}|"
                            f"{block['parentHash'].lower()}|{timestamp}|"
                            f"{state_root.lower()}|{tx_root.lower()}|"
                            f"{receipt_root.lower()}\n"
                        ).encode()
                    )
                    expected_parent = block["hash"]
                    previous_root = state_root
                    previous_timestamp = timestamp
                    next_number += 1

                print(
                    f"progress shard={args.shard} block={chunk_end} "
                    f"transactions={summary['normal_transactions']} "
                    f"staking={summary['staking_transactions']} "
                    f"state_changes={summary['state_root_changes']}",
                    flush=True,
                )

            material_file.flush()
            os.fsync(material_file.fileno())
            transaction_file.flush()
            os.fsync(transaction_file.fileno())

        os.replace(material_partial, args.material_blocks)
        os.replace(transactions_partial, args.transactions)
        summary.update(
            {
                "first_hash": rpc(
                    args.rpc,
                    "hmyv2_getBlockByNumber",
                    [args.start, {"fullTx": False, "inclStaking": False}],
                )["hash"],
                "last_hash": expected_parent,
                "last_state_root": previous_root,
                "last_timestamp": previous_timestamp,
                "distinct_state_roots": len(distinct_roots),
                "canonical_header_digest_sha256": header_digest.hexdigest(),
                "material_blocks_path": args.material_blocks,
                "material_blocks_sha256": file_sha256(args.material_blocks),
                "transactions_path": args.transactions,
                "transactions_sha256": file_sha256(args.transactions),
            }
        )
        summary_partial = args.summary + ".partial"
        with open(summary_partial, "x") as summary_file:
            json.dump(summary, summary_file, indent=2, sort_keys=True)
            summary_file.write("\n")
            summary_file.flush()
            os.fsync(summary_file.fileno())
        os.replace(summary_partial, args.summary)
        print(json.dumps(summary, sort_keys=True))
    except BaseException:
        for path in (
            material_partial,
            transactions_partial,
            args.summary + ".partial",
        ):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        raise


if __name__ == "__main__":
    main()
