#!/usr/bin/env python3
"""Fetch and verify the full calldata of a run's exported transactions.

For every row of <scan>/transactions.csv with input_bytes > 0, read the
transaction (hmyv2_getTransactionByHash, or from its block by hash when the
node has no lookup entry) and require that:

  - the input has the length and 4-byte selector the scan recorded;
  - with the row's value, the transaction reproduces its own signed hash, so
    the calldata, recipient and value are exactly the signed ones.

Writes <scan>/calldata/calldata.csv (tx_hash, input) and calldata/stage.json.
"""

import argparse
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scan", required=True)
    parser.add_argument("--rpc", default=os.environ.get("HARMONY_RPC"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--batch", type=int, default=200)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if not args.rpc:
        parser.error("--rpc or $HARMONY_RPC is required")
    return args


def verify(item):
    tx, value = item
    return tx["hash"], lib.signed_value_matches(tx, value)


def check_row(row, tx):
    data = tx.get("input") or "0x"
    if tx["hash"].lower() != row["tx_hash"].lower():
        raise SystemExit(f"{row['tx_hash']}: node returned {tx['hash']}")
    if (len(data) - 2) // 2 != int(row["input_bytes"]) or (row["input_selector"] and data[:10] != row["input_selector"]):
        raise SystemExit(f"{row['tx_hash']}: input length or selector differs from the scan")
    return data


def main():
    args = parse_args()
    scan_dir = Path(args.scan)
    scan_state = lib.load_json(scan_dir / "stage.json")
    if not scan_state.get("complete"):
        raise SystemExit(f"{scan_dir} is not a completed scan")
    out = scan_dir / "calldata"
    if (out / "stage.json").exists() and not args.replace:
        raise SystemExit(f"{out} exists; pass --replace")
    wanted = {}
    for row in lib.iter_csv(scan_dir / "transactions.csv"):
        if int(row["input_bytes"] or 0) > 0:
            wanted[row["tx_hash"]] = row
    rpc = lib.Rpc(args.rpc)
    hashes = sorted(wanted)
    chunks = [hashes[i:i + args.batch] for i in range(0, len(hashes), args.batch)]
    counters = Counter()

    def fetch(chunk):
        found = rpc.batch([("hmyv2_getTransactionByHash", [h]) for h in chunk])
        out_rows = []
        for tx_hash, tx in zip(chunk, found):
            row = wanted[tx_hash]
            by_block = False
            if tx is None:
                block = rpc.call("hmyv2_getBlockByHash", [row["block_hash"], {"fullTx": True, "inclTx": True}])
                tx = next((t for t in (block or {}).get("transactions") or [] if t["hash"] == tx_hash), None)
                by_block = True
                if tx is None:
                    raise SystemExit(f"{tx_hash}: not found by hash or in block {row['block_hash']}")
            out_rows.append((tx_hash, check_row(row, tx), tx, by_block))
        return out_rows

    fetched = []
    with ThreadPoolExecutor(args.workers) as pool:
        for part in pool.map(fetch, chunks):
            fetched += part
    counters["rows"] = len(fetched)
    counters["fetched_from_block"] = sum(by_block for *_, by_block in fetched)
    counters["bytes"] = sum((len(data) - 2) // 2 for _, data, _, _ in fetched)
    with Pool(args.workers) as pool:
        results = dict(pool.imap_unordered(verify, [(tx, int(wanted[h]["value_atto"])) for h, _, tx, _ in fetched],
                                           chunksize=64))
    failed = sorted(h for h, ok in results.items() if not ok)
    if failed:
        raise SystemExit(f"{len(failed)} transactions do not reproduce their signed hash, e.g. {failed[:5]}")
    counters["signature_verified"] = len(results)
    count = lib.write_calldata_stage(out, scan_state["identity"], lib.file_sha256(scan_dir / "transactions.csv"),
                                     [{"tx_hash": h, "input": data} for h, data, _, _ in sorted(fetched)], counters)
    print(f"{count} calldata rows, {counters['bytes']} bytes, all signature-verified "
          f"({counters['fetched_from_block']} read from their block)", flush=True)


if __name__ == "__main__":
    main()
