#!/usr/bin/env python3
"""Record where each outgoing receipt was applied on its destination shard.

    fetch-cx-destination-status.py --outgoing shard0-outgoing-receipts.csv \
        --source-shard 0 --destination-shard 1 --source-cutoff 93623067 \
        --rpc https://a.api.s1.t.hmny.io --output destination-status.csv

For every canonical receipt from the source shard to the destination shard at
or before the source cutoff, hmyv2_getCXReceiptByHash on the destination
returns the destination block that applied it, or null. Each returned block is
checked against the destination's canonical block at that height. The rebuild
step decides spent or pending from this file offline.
"""

import argparse
import csv
import json
import os
import time
import urllib.request

FIELDS = (
    "transaction_hash",
    "source_block",
    "found",
    "destination_block",
    "destination_block_hash",
    "destination_canonical_hash",
    "canonical",
)


class Rpc:
    def __init__(self, url, batch):
        self.url, self.batch = url, batch

    def calls(self, requests):
        out = []
        for start in range(0, len(requests), self.batch):
            out.extend(self._batch(requests[start:start + self.batch]))
        return out

    def _batch(self, requests):
        body = json.dumps([
            {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
            for i, (method, params) in enumerate(requests)
        ]).encode()
        for attempt in range(6):
            try:
                request = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    replies = json.load(response)
                by_id = {reply["id"]: reply for reply in replies}
                errors = [reply["error"] for reply in replies if "error" in reply]
                if errors:
                    raise RuntimeError(errors[0])
                return [by_id[i].get("result") for i in range(len(requests))]
            except Exception as error:
                if attempt == 5:
                    raise RuntimeError(f"{self.url}: {error}") from error
                time.sleep(2 * (attempt + 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outgoing", required=True, help="outgoing-cx-scan CSV")
    parser.add_argument("--source-shard", type=int, required=True)
    parser.add_argument("--destination-shard", type=int, required=True)
    parser.add_argument("--source-cutoff", type=int, required=True)
    parser.add_argument("--rpc", required=True, help="destination-shard archival RPC")
    parser.add_argument("--batch", type=int, default=40)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if os.path.exists(args.output):
        raise FileExistsError(args.output)

    receipts = []
    with open(args.outgoing, newline="") as source:
        for row in csv.DictReader(source):
            if (int(row["source_shard"]) == args.source_shard
                    and int(row["destination_shard"]) == args.destination_shard
                    and int(row["source_block"]) <= args.source_cutoff):
                receipts.append((row["tx_hash"].lower(), int(row["source_block"])))
    rpc = Rpc(args.rpc, args.batch)
    results = rpc.calls([("hmyv2_getCXReceiptByHash", [tx]) for tx, _ in receipts])
    blocks = sorted({int(result["blockNumber"]) for result in results if result})
    canonical = dict(zip(blocks, (
        (block or {}).get("hash", "").lower()
        for block in rpc.calls([("hmyv2_getBlockByNumber", [n, {"fullTx": False}]) for n in blocks])
    )))

    with open(args.output + ".partial", "w", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(FIELDS)
        for (tx, source_block), result in zip(receipts, results):
            if not result:
                writer.writerow([tx, source_block, "false", "", "", "", ""])
                continue
            number = int(result["blockNumber"])
            block_hash = result["blockHash"].lower()
            writer.writerow([tx, source_block, "true", number, block_hash, canonical[number],
                             "true" if canonical[number] == block_hash else "false"])
    os.replace(args.output + ".partial", args.output)
    found = sum(1 for result in results if result)
    print(json.dumps({"receipts": len(receipts), "found": found, "not_found": len(receipts) - found,
                      "destination_blocks": len(blocks)}))


if __name__ == "__main__":
    main()
