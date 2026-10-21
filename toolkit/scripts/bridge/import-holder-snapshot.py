#!/usr/bin/env python3

"""Import an externally produced holder list as a pipeline holder snapshot.

The list covers one token at one block. When that block (--list-block)
differs from the snapshot block, the list must first sum to totalSupply() at
its own block; Transfer events between the two blocks are then replayed
forward or reversed to reach the snapshot block. The result is summed and
compared with totalSupply() at the snapshot block, and the largest holders
are checked with balanceOf(), so an imported list meets the same checks as
token-holders.
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--address-column", default="address")
    parser.add_argument("--balance-column", default="balance_raw")
    parser.add_argument("--chain", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--block-hash", required=True)
    parser.add_argument("--source", required=True, help="where the list came from")
    parser.add_argument("--list-block", type=int, help="block the list describes")
    parser.add_argument("--list-block-hash")
    parser.add_argument("--range-size", type=int, default=5000)
    parser.add_argument("--holders-dir", required=True)
    parser.add_argument("--verify-top", type=int, default=50)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def roll(rpc, token, balances, list_block, block, range_size):
    """Move balances from list_block to block by replaying Transfer events."""
    low, high = sorted((list_block, block))
    sign = 1 if block > list_block else -1
    logs = rpc.logs(token, [lib.TOPIC["Transfer"]], low + 1, high, range_size)
    for log in logs:
        if lib.address(log["address"]) != token or len(log["topics"]) != 3:
            raise ValueError(f"unexpected Transfer log {log.get('transactionHash')}")
        source = lib.topic_address(log["topics"][1])
        destination = lib.topic_address(log["topics"][2])
        amount = int(log["data"], 16) * sign
        if source != lib.ZERO_ADDRESS:
            balances[source] = balances.get(source, 0) - amount
        if destination != lib.ZERO_ADDRESS:
            balances[destination] = balances.get(destination, 0) + amount
    negative = [account for account, value in balances.items() if value < 0]
    if negative:
        raise ValueError(f"replay left {len(negative)} negative balances")
    return {account: value for account, value in balances.items() if value}, len(logs)


def main():
    args = parse_args()
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    token = lib.address(args.token, "--token")
    stem = lib.holder_file_stem(args.chain, token, args.block)
    csv_path = os.path.join(args.holders_dir, stem + ".csv")
    summary_path = os.path.join(args.holders_dir, stem + ".summary.json")
    lib.prepare_outputs([csv_path, summary_path], args.replace)

    balances = {}
    with open(args.input, newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            account = lib.address(row[args.address_column], "holder")
            amount = int(row[args.balance_column])
            if amount < 0:
                raise ValueError(f"negative balance for {account}")
            if account in balances:
                raise ValueError(f"duplicate holder {account}")
            if amount:
                balances[account] = amount

    rpc = lib.chain_rpc(config, args.chain)
    if rpc.chain_id() != int(config["chains"][args.chain]["chain_id"]):
        raise ValueError("RPC chain id does not match the config")
    block = rpc.block(args.block)
    if block["hash"].lower() != args.block_hash.lower():
        raise ValueError("block hash mismatch")
    replayed = 0
    if args.list_block is not None and args.list_block != args.block:
        listed = rpc.block(args.list_block)
        if args.list_block_hash and listed["hash"].lower() != args.list_block_hash.lower():
            raise ValueError("list block hash mismatch")
        list_supply = lib.decode_uint(rpc.eth_call(token, lib.SEL["totalSupply"], args.list_block))
        if list_supply != sum(balances.values()):
            raise SystemExit("the list does not sum to totalSupply at its own block")
        balances, replayed = roll(
            rpc, token, balances, args.list_block, args.block, args.range_size
        )
    supply = lib.decode_uint(rpc.eth_call(token, lib.SEL["totalSupply"], args.block))
    decimals = lib.decode_uint(rpc.eth_call(token, lib.SEL["decimals"], args.block))
    total = sum(balances.values())
    problems = []
    if supply != total:
        problems.append(f"totalSupply {supply} minus holder total {total} is {supply - total}")
    top = sorted(balances, key=lambda account: (-balances[account], account))[: args.verify_top]
    outcomes = rpc.batch([
        ("eth_call", [{"to": token, "data": lib.call_data("balanceOf", lib.address_word(account))}, lib.block_tag(args.block)])
        for account in top
    ])
    checks = []
    for account, (result, error) in zip(top, outcomes):
        on_chain = lib.decode_uint(result) if error is None else None
        checks.append({"address": account, "derived_raw": str(balances[account]), "balance_of_raw": str(on_chain)})
        if on_chain != balances[account]:
            problems.append(f"balanceOf {account} is {on_chain}, list gives {balances[account]}")
    if problems:
        raise SystemExit("imported list failed checks:\n" + "\n".join(problems))

    rows = [
        {
            "address": account,
            "balance_raw": balances[account],
            "balance": lib.format_units(balances[account], decimals) if decimals is not None else "",
        }
        for account in sorted(balances)
    ]
    digest = lib.write_csv(csv_path, ("address", "balance_raw", "balance"), rows)
    lib.write_json(summary_path, {
        "schema_version": 1,
        "status": "passed",
        "source_kind": "imported holder list checked against totalSupply and balanceOf",
        "source": args.source,
        "input_sha256": lib.file_sha256(args.input),
        "list_block": args.list_block if args.list_block is not None else args.block,
        "replayed_transfer_events": replayed,
        "rpc_host": rpc.host,
        "chain_id": int(config["chains"][args.chain]["chain_id"]),
        "token": token,
        "decimals": decimals,
        "block": args.block,
        "block_hash": block["hash"].lower(),
        "block_timestamp": int(block["timestamp"], 16),
        "holder_count": len(rows),
        "holder_total_raw": str(total),
        "total_supply_raw": str(supply),
        "supply_minus_holder_total_raw": "0",
        "spot_checks": checks,
        "problems": [],
        "output": os.path.basename(csv_path),
        "output_sha256": digest,
    })
    print(f"wrote {csv_path} holders={len(rows)}")


if __name__ == "__main__":
    main()
