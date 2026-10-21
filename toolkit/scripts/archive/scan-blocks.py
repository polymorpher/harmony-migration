#!/usr/bin/env python3
"""Scan one shard/branch block range of a Harmony node over RPC for the transaction archive.

Reads every block with hmyv2_getBlocks (full and staking transactions) and
hmyv2_getFullHeader (cross-shard receipt roots), and fetches receipts for every
exported transaction. Outputs in --out:

  transactions.csv   plain transactions with value >= --min-value-atto, all kinds
                     (transfer, call, create, xshard_native, xshard_precompile)
  staking.csv        staking transactions whose amount (message amount, or the
                     CollectRewards log) is >= --min-value-atto
  trace-blocks.csv   blocks that can hold internal transfers: a contract call or
                     creation, or a value transfer to an address with code
  xshard-blocks.csv  blocks whose header has a non-empty incoming/outgoing receipt root
  segments.csv       first parent hash and last hash of every task
  stage.json         shard, branch, range, endpoint host, boundary hashes, counters,
                     chain-continuity result and file hashes

The run is resumable (--resume). Every task checks parent-hash continuity, and
the finished run checks it across tasks, so the range is one connected chain.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402

BUCKETS = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000]
RPC = None
MIN_VALUE = lib.ONE
CODE_CACHE = {}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpc", default=os.environ.get("HARMONY_RPC"),
                        help="node JSON-RPC URL (default: $HARMONY_RPC)")
    parser.add_argument("--shard", type=int, required=True, choices=(0, 1))
    parser.add_argument("--branch", required=True, choices=lib.BRANCHES)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--block-list", help="read these blocks by hash instead of a canonical range: "
                        "find-abandoned-blocks.py output (block_number, block_hash, parent_hash)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--min-value-atto", type=int, default=lib.ONE)
    parser.add_argument("--expect", action="append", default=[], metavar="NUMBER:HASH",
                        help="require this block hash on the node before scanning (repeatable)")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--step", type=int, default=1000, help="blocks per task (max 1024)")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not args.rpc:
        parser.error("--rpc or $HARMONY_RPC is required")
    if args.block_list:
        rows = lib.read_csv(args.block_list)
        if not rows:
            parser.error("--block-list is empty")
        first, last = int(rows[0]["block_number"]), int(rows[-1]["block_number"])
        if (args.start not in (None, first)) or (args.end not in (None, last)):
            parser.error(f"--block-list covers {first}-{last}")
        args.start, args.end, args.rows = first, last, rows
    elif args.start is None or args.end is None:
        parser.error("--start and --end are required without --block-list")
    if args.start > args.end or not 1 <= args.step <= 1024 or args.workers < 1:
        parser.error("invalid range, --step or --workers")
    return args


def init_worker(url, min_value):
    global RPC, MIN_VALUE
    RPC = lib.Rpc(url)
    MIN_VALUE = min_value


def fetch_range(start, end):
    blocks = RPC.call("hmyv2_getBlocks", [start, end, {
        "fullTx": True, "inclTx": True, "inclStaking": True, "withSigners": False}])
    headers = RPC.batch([("hmyv2_getFullHeader", [n]) for n in range(start, end + 1)], size=1024)
    numbers = [lib.integer(block["number"]) for block in blocks]
    if numbers != list(range(start, end + 1)):
        raise lib.RpcError(f"blocks {start}-{end}: node returned {len(numbers)} blocks")
    for block, header in zip(blocks, headers):
        if lib.integer(header["number"]) != lib.integer(block["number"]):
            raise lib.RpcError(f"header/block mismatch at {block['number']}")
    return blocks, headers


def fetch_listed(rows):
    """Blocks read by hash. No RPC returns a header by hash, so receipt roots are unknown."""
    blocks = RPC.batch([("hmyv2_getBlockByHash", [row["block_hash"], {
        "fullTx": True, "inclTx": True, "inclStaking": True}]) for row in rows], size=100)
    for row, block in zip(rows, blocks):
        if (block is None or block["hash"].lower() != row["block_hash"].lower()
                or block["parentHash"].lower() != row["parent_hash"].lower()
                or lib.integer(block["number"]) != int(row["block_number"])):
            raise lib.RpcError(f"listed block {row['block_number']} {row['block_hash']} is missing or differs")
    return blocks, None


def bucket(value):
    label = None
    for limit in BUCKETS:
        if value >= limit * lib.ONE:
            label = f">={limit}"
    return label or f"<{BUCKETS[0]}"


def has_code(addresses):
    missing = [item for item in addresses if item not in CODE_CACHE]
    if missing:
        codes = RPC.batch([("eth_getCode", [item, "latest"]) for item in missing])
        for item, code in zip(missing, codes):
            CODE_CACHE[item] = bool(code) and code != "0x"
    return {item: CODE_CACHE[item] for item in addresses}


def scan(task):
    listed = isinstance(task, list)
    blocks, headers = fetch_listed(task) if listed else fetch_range(*task)
    start = lib.integer(blocks[0]["number"])
    counters = Counter()
    months = Counter()
    for previous, block in zip(blocks, blocks[1:]):
        if block["parentHash"] != previous["hash"]:
            raise lib.RpcError(f"block {block['number']}: parent hash does not link")

    tx_rows, staking_rows, trace_rows, xshard_rows = [], [], [], []
    pending = []
    plain_recipients = {}
    counters["blocks"] = len(blocks)
    for block, header in zip(blocks, headers or [None] * len(blocks)):
        number = lib.integer(block["number"])
        timestamp = lib.integer(block["timestamp"])
        if header is not None:
            incoming = header["incomingReceiptsRoot"] != lib.EMPTY_ROOT
            outgoing = header["outgoingReceiptsRoot"] != lib.EMPTY_ROOT
        else:
            # Receipt roots are unknown for blocks read by hash; flag transaction-level senders.
            incoming = False
            outgoing = any(lib.classify_tx(tx, lib.address(tx.get("to"))).startswith("xshard")
                           for tx in block.get("transactions") or [])
        if incoming or outgoing:
            xshard_rows.append({"block_number": number, "block_hash": block["hash"],
                                "timestamp": timestamp,
                                "epoch": lib.integer((header or block)["epoch"]),
                                "incoming": int(incoming), "outgoing": int(outgoing)})
            counters["blocks_incoming_receipts"] += incoming
            counters["blocks_outgoing_receipts"] += outgoing
        txs = block.get("transactions") or []
        stakings = block.get("stakingTransactions") or []
        if txs or stakings:
            counters["blocks_with_transactions"] += 1
        call_txs = 0
        for tx in txs:
            counters["tx_total"] += 1
            value = lib.integer(tx["value"])
            to = lib.address(tx.get("to"))
            data = tx.get("input") or "0x"
            kind = lib.classify_tx(tx, to)
            counters[f"tx_kind_{kind}"] += 1
            if kind in ("call", "create", "xshard_precompile"):
                call_txs += 1
            elif kind == "transfer" and value > 0:
                plain_recipients.setdefault(number, set()).add(to)
            if value > 0:
                counters["tx_value_gt0"] += 1
            if value < MIN_VALUE:
                continue
            counters["tx_ge_min"] += 1
            counters[f"tx_ge_min_kind_{kind}"] += 1
            counters[f"tx_ge_min_bucket_{bucket(value)}"] += 1
            months[datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")] += 1
            to_out, to_shard = to, lib.integer(tx["toShardID"])
            if kind == "xshard_precompile":
                decoded = lib.decode_xshard_input(data)
                if decoded:
                    to_out, to_shard = decoded["to"], decoded["to_shard"]
            row = {
                "kind": kind, "block_number": number, "block_hash": block["hash"],
                "timestamp": timestamp, "tx_index": lib.integer(tx["transactionIndex"]),
                "tx_hash": tx["hash"], "eth_hash": tx.get("ethHash", ""),
                "from": lib.address(tx["from"]), "to": to_out, "value_atto": value,
                "shard_id": lib.integer(tx["shardID"]), "to_shard_id": to_shard,
                "gas_price": lib.integer(tx["gasPrice"]),
                "input_selector": data[:10] if data != "0x" else "",
                "input_bytes": (len(data) - 2) // 2,
            }
            tx_rows.append(row)
            pending.append(("tx", row, tx))
        for tx in stakings:
            counters["staking_total"] += 1
            counters[f"staking_type_{tx.get('type', '')}"] += 1
            pending.append(("staking", None, (tx, block, timestamp)))
        if call_txs:
            trace_rows.append({"block_number": number, "tx_count": len(txs), "call_tx_count": call_txs,
                               "reason": "call", "block_hash": block["hash"]})

    block_hashes = {lib.integer(block["number"]): block["hash"] for block in blocks}
    if plain_recipients:
        codes = has_code(sorted({item for items in plain_recipients.values() for item in items}))
        traced = {row["block_number"] for row in trace_rows}
        for number, recipients in sorted(plain_recipients.items()):
            if number not in traced and any(codes[item] for item in recipients):
                trace_rows.append({"block_number": number, "tx_count": 0, "call_tx_count": 0,
                                   "reason": "transfer_to_contract", "block_hash": block_hashes[number]})
                counters["trace_blocks_transfer_to_contract"] += 1

    hashes = [item[2]["hash"] if item[0] == "tx" else item[2][0]["hash"] for item in pending]
    if listed:
        # A transaction lookup may point at the canonical re-inclusion, so read receipts by block.
        wanted = sorted({item[1]["block_hash"] if item[0] == "tx" else item[2][1]["hash"] for item in pending})
        by_hash = {}
        for block_receipts in RPC.batch([("hmyv2_getBlockReceipts", [h]) for h in wanted], size=50):
            for receipt in block_receipts or []:
                by_hash[receipt["transactionHash"]] = receipt
        receipts = [by_hash.get(h) for h in hashes]
    else:
        receipts = RPC.batch([("hmyv2_getTransactionReceipt", [h]) for h in hashes])
    for (kind, row, payload), receipt in zip(pending, receipts):
        if receipt is None:
            raise lib.RpcError(f"missing receipt for {payload['hash'] if kind == 'tx' else payload[0]['hash']}")
        if kind == "tx":
            row["status"] = lib.integer(receipt["status"])
            row["gas_used"] = lib.integer(receipt["gasUsed"])
            if row["status"] != 1:
                counters["tx_ge_min_failed"] += 1
            continue
        tx, block, timestamp = payload
        amount = lib.staking_amount(tx, receipt)
        if amount is None or amount < MIN_VALUE:
            continue
        msg = tx.get("msg") or {}
        counters[f"staking_ge_min_type_{tx.get('type', '')}"] += 1
        staking_rows.append({
            "type": tx.get("type", ""), "block_number": lib.integer(block["number"]),
            "block_hash": block["hash"], "timestamp": timestamp,
            "tx_index": lib.integer(tx["transactionIndex"]), "tx_hash": tx["hash"],
            "from": lib.address(tx["from"]),
            "validator": lib.address(msg.get("validatorAddress")),
            "amount_atto": amount, "status": lib.integer(receipt["status"]),
            "gas_used": lib.integer(receipt["gasUsed"]), "gas_price": lib.integer(tx["gasPrice"]),
        })

    counters["trace_blocks"] = len(trace_rows)
    end = lib.integer(blocks[-1]["number"])
    segment = {"start": start, "end": end, "first_parent_hash": blocks[0]["parentHash"],
               "first_hash": blocks[0]["hash"], "last_hash": blocks[-1]["hash"]}
    return {"start": start, "counters": dict(counters), "months": dict(months), "segment": segment,
            "tx": tx_rows, "staking": staking_rows, "trace": trace_rows, "xshard": xshard_rows}


def verify_continuity(segments):
    segments = sorted(segments, key=lambda item: item["start"])
    for previous, current in zip(segments, segments[1:]):
        if current["start"] != previous["end"] + 1:
            raise ValueError(f"gap between tasks at {previous['end']}..{current['start']}")
        if current["first_parent_hash"] != previous["last_hash"]:
            raise ValueError(f"chain does not link at block {current['start']}")
    return segments


def main():
    args = parse_args()
    rpc = lib.Rpc(args.rpc)
    if rpc.shard() != args.shard:
        raise SystemExit(f"{rpc.host} serves shard {rpc.shard()}, not {args.shard}")
    head = rpc.head()
    if args.end > head:
        raise SystemExit(f"--end {args.end} is above the node head {head}")
    anchors = []
    for text in args.expect:
        number, expected = lib.parse_block_pin(text)
        actual = rpc.block(number)["hash"].lower()
        if actual != expected:
            raise SystemExit(f"block {number} on {rpc.host} is {actual}, expected {expected}")
        anchors.append({"block": number, "hash": expected})

    identity = {"stage": "scan-blocks", "shard": args.shard, "branch": args.branch,
                "start": args.start, "end": args.end, "min_value_atto": str(args.min_value_atto),
                "step": args.step, "anchors": anchors}
    if args.block_list:
        fork = {a["block"]: a["hash"] for a in anchors}.get(args.start - 1)
        if fork != args.rows[0]["parent_hash"].lower():
            raise SystemExit(f"--block-list needs --expect {args.start - 1}:<hash of the block it attaches to>")
        identity["block_list_sha256"] = lib.file_sha256(args.block_list)
    out = Path(args.out)
    state = lib.open_stage(out, identity, args.resume)
    done_path = out / "done.jsonl"
    done, totals, months, segments = set(), Counter(), Counter(), []
    if done_path.exists():
        with open(done_path, encoding="utf-8") as source:
            for line in source:
                item = json.loads(line)
                if item["start"] in done:
                    continue
                done.add(item["start"])
                totals.update(item["counters"])
                months.update(item["months"])
                segments.append(item["segment"])
    if args.block_list:
        chunks = [args.rows[i:i + args.step] for i in range(0, len(args.rows), args.step)]
        tasks = [chunk for chunk in chunks if int(chunk[0]["block_number"]) not in done]
    else:
        tasks = [(s, min(s + args.step - 1, args.end)) for s in range(args.start, args.end + 1, args.step)
                 if s not in done]
    files = {
        "tx": lib.AppendCsv(out / "transactions.csv", lib.TX_FIELDS),
        "staking": lib.AppendCsv(out / "staking.csv", lib.STAKING_FIELDS),
        "trace": lib.AppendCsv(out / "trace-blocks.csv", lib.TRACE_BLOCK_FIELDS),
        "xshard": lib.AppendCsv(out / "xshard-blocks.csv", lib.XSHARD_BLOCK_FIELDS),
    }
    print(f"scan shard {args.shard} {args.branch} {args.start}-{args.end} via {rpc.host}: "
          f"{len(tasks)} tasks to run, {len(done)} done", flush=True)
    with open(done_path, "a", encoding="utf-8") as done_file, \
            Pool(args.workers, init_worker, (args.rpc, args.min_value_atto)) as pool:
        for index, result in enumerate(pool.imap_unordered(scan, tasks, chunksize=1), 1):
            for key, handle in files.items():
                handle.write(result[key])
                handle.flush()
            done_file.write(json.dumps({key: result[key] for key in
                                        ("start", "counters", "months", "segment")}) + "\n")
            done_file.flush()
            os.fsync(done_file.fileno())
            totals.update(result["counters"])
            months.update(result["months"])
            segments.append(result["segment"])
            if index % 500 == 0 or index == len(tasks):
                print(f"{index}/{len(tasks)} tasks; tx={totals['tx_total']} "
                      f"ge_min={totals['tx_ge_min']} trace_blocks={totals['trace_blocks']}", flush=True)
    for handle in files.values():
        handle.close()

    segments = verify_continuity(segments)
    lib.write_csv(out / "segments.csv", lib.SEGMENT_FIELDS, segments)
    if args.block_list:
        first = rpc.call("hmyv2_getBlockByHash", [args.rows[0]["block_hash"], {}])
        last = rpc.call("hmyv2_getBlockByHash", [args.rows[-1]["block_hash"], {}])
    else:
        first, last = rpc.block(args.start), rpc.block(args.end)
    lib.finish_stage(out, state, {
        "read_by": "block hash (receipt roots unavailable)" if args.block_list else "block number",
        "rpc_host": rpc.host,
        "boundaries": {
            "start": {"block": args.start, "hash": first["hash"], "parent_hash": first["parentHash"],
                      "timestamp": lib.integer(first["timestamp"]),
                      "time_utc": lib.utc(first["timestamp"])},
            "end": {"block": args.end, "hash": last["hash"], "timestamp": lib.integer(last["timestamp"]),
                    "time_utc": lib.utc(last["timestamp"])},
        },
        "chain_continuity": "verified",
        "counters": dict(sorted(totals.items())),
        "tx_ge_min_by_month": dict(sorted(months.items())),
        "files": lib.output_files(out, ["transactions.csv", "staking.csv", "trace-blocks.csv",
                                        "xshard-blocks.csv", "segments.csv"]),
    })
    print(f"complete: {out / 'stage.json'}", flush=True)


if __name__ == "__main__":
    main()
