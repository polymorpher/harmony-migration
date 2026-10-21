#!/usr/bin/env python3
"""List the abandoned (rolled-back) blocks that a node still stores, for a by-hash scan.

After a rollback a node keeps abandoned blocks under their hashes but drops
their canonical number-to-hash mapping, so they cannot be read by number. This
stage finds them through the node's own per-address transaction index
(hmyv2_getTransactionsHistory and hmyv2_getStakingTransactionsHistory, served
by explorer-mode nodes): every indexed transaction above the fork whose block
hash differs from the canonical hash at that height lies on an abandoned
block. Addresses seen in the abandoned blocks are queried again until no new
block appears. Starting from the highest abandoned block found, the chain is
walked back through parent hashes to the canonical fork block, so every block
in between is listed, including blocks without transactions.

Writes --out/abandoned-blocks.csv (block_number, block_hash, parent_hash, in
ascending order) for scan-blocks.py --block-list, and --out/discovery.json.
Blocks above the highest abandoned block that holds an indexed transaction
cannot be reached this way; discovery.json reports that bound.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402

ADDRESS_COLUMNS = ("from", "to", "delegator", "validator")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpc", default=os.environ.get("HARMONY_RPC"))
    parser.add_argument("--fork", required=True, metavar="NUMBER:HASH",
                        help="last block shared with the canonical chain and its canonical hash")
    parser.add_argument("--max-block", type=int, help="highest block the node ever stored on the abandoned branch")
    parser.add_argument("--addresses", action="append", required=True,
                        help="CSV whose from/to/delegator/validator columns seed the index queries (repeatable)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if not args.rpc:
        parser.error("--rpc or $HARMONY_RPC is required")
    return args


def main():
    args = parse_args()
    out = Path(args.out)
    output = out / "abandoned-blocks.csv"
    if output.exists() and not args.replace:
        raise SystemExit(f"{output} exists; pass --replace")
    out.mkdir(parents=True, exist_ok=True)
    rpc = lib.Rpc(args.rpc)
    fork_number, fork_hash = lib.parse_block_pin(args.fork)
    if rpc.block(fork_number)["hash"].lower() != fork_hash:
        raise SystemExit(f"block {fork_number} on {rpc.host} is not {fork_hash}")
    head = rpc.head()
    upper = min(args.max_block or head, head)
    canonical = {}

    def canonical_hash(number):
        if number not in canonical:
            canonical[number] = rpc.block(number)["hash"].lower()
        return canonical[number]

    def history(address):
        """(stale block hash -> number) for one address.

        The index lists an address's transactions in the order the node processed
        them, so newest first is: re-synced canonical blocks, abandoned blocks, then
        blocks at or below the fork, where the walk stops.
        """
        found = {}
        for method, key, lookup in (
                ("hmyv2_getTransactionsHistory", "transactions", "hmyv2_getTransactionByHash"),
                ("hmyv2_getStakingTransactionsHistory", "staking_transactions",
                 "hmyv2_getStakingTransactionByHash")):
            for page in range(args.max_pages):
                result = rpc.call(method, [{"address": address, "pageIndex": page, "pageSize": args.page_size,
                                            "fullTx": False, "txType": "ALL", "order": "DESC"}])
                hashes = (result or {}).get(key) or []
                below_fork = False
                for tx in rpc.batch([(lookup, [h]) for h in hashes]):
                    if tx is None:
                        continue
                    number = lib.integer(tx["blockNumber"])
                    if number <= fork_number:
                        below_fork = True
                        break
                    if number <= upper and tx["blockHash"].lower() != canonical_hash(number):
                        found[tx["blockHash"].lower()] = number
                if below_fork or len(hashes) < args.page_size:
                    break
        return found

    seeds = set()
    for path in args.addresses:
        for row in lib.iter_csv(path):
            for column in ADDRESS_COLUMNS:
                if row.get(column):
                    seeds.add(row[column].lower())
    queried, stale, rounds = set(), {}, []
    pending = sorted(seeds)
    while pending:
        before = len(stale)
        with ThreadPoolExecutor(args.workers) as pool:
            for found in pool.map(history, pending):
                stale.update(found)
        queried.update(pending)
        new_addresses = set()
        for block_hash in [h for h in stale if h not in {b for r in rounds for b in r["blocks"]}]:
            block = rpc.call("hmyv2_getBlockByHash", [block_hash, {"fullTx": True, "inclStaking": True}])
            for tx in (block.get("transactions") or []) + (block.get("stakingTransactions") or []):
                for value in (tx.get("from"), tx.get("to"), (tx.get("msg") or {}).get("validatorAddress")):
                    if value:
                        new_addresses.add(lib.address(value))
        rounds.append({"queried": len(pending), "new_blocks": len(stale) - before, "blocks": list(stale)})
        print(f"round {len(rounds)}: queried {len(pending)} addresses, {len(stale)} abandoned blocks", flush=True)
        pending = sorted(new_addresses - queried)

    if not stale:
        raise SystemExit("no abandoned block found through the index")
    top_hash = max(stale, key=lambda h: stale[h])
    chain = []
    cursor = top_hash
    while True:
        block = rpc.call("hmyv2_getBlockByHash", [cursor, {"fullTx": False}])
        if block is None:
            raise SystemExit(f"abandoned block {cursor} is missing; the walk back cannot reach the fork")
        number = lib.integer(block["number"])
        chain.append({"block_number": number, "block_hash": block["hash"].lower(),
                      "parent_hash": block["parentHash"].lower()})
        if number == fork_number + 1:
            if block["parentHash"].lower() != fork_hash:
                raise SystemExit("the abandoned branch does not attach to the fork block")
            break
        cursor = block["parentHash"].lower()
    chain.reverse()
    on_chain = {row["block_hash"] for row in chain}
    off_chain = sorted(h for h in stale if h not in on_chain)
    count, digest = lib.write_csv(output, ["block_number", "block_hash", "parent_hash"], chain)
    lib.write_json(out / "discovery.json", {
        "stage": "find-abandoned-blocks", "rpc_host": rpc.host, "fork": {"block": fork_number, "hash": fork_hash},
        "upper_bound": upper, "seed_addresses": len(seeds), "queried_addresses": len(queried),
        "rounds": [{k: r[k] for k in ("queried", "new_blocks")} for r in rounds],
        "abandoned_blocks_with_indexed_transactions": len(stale),
        "listed_blocks": count, "highest_listed_block": chain[-1]["block_number"],
        "blocks_off_the_listed_branch": off_chain,
        "output": {"path": output.name, "sha256": digest},
    })
    print(f"{count} abandoned blocks {chain[0]['block_number']}-{chain[-1]['block_number']}; "
          f"{len(off_chain)} indexed blocks off the walked branch", flush=True)
    if off_chain:
        raise SystemExit("indexed abandoned blocks lie on more than one branch; see discovery.json")


if __name__ == "__main__":
    main()
