#!/usr/bin/env python3

"""Resolve the rollback and cutoff blocks on every bridge chain.

Each label resolves to the last block whose timestamp is at or before the
configured UTC time. Pinned blocks (Harmony) are verified instead of searched:
the hash must match, the block must not be after the time, and the next block
must be after it.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--chain", action="append", default=[])
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


class Timeline:
    def __init__(self, rpc):
        self.rpc = rpc
        self.cache = {}

    def block(self, number):
        if number not in self.cache:
            self.cache[number] = self.rpc.block(number)
        return self.cache[number]

    def timestamp(self, number):
        return int(self.block(number)["timestamp"], 16)

    def last_at_or_before(self, target, head):
        if self.timestamp(0) > target:
            raise ValueError(f"{self.rpc.label}: genesis is after {target}")
        if self.timestamp(head) <= target:
            raise ValueError(
                f"{self.rpc.label}: head {head} has not passed the target time"
            )
        low, high = 0, head
        while high - low > 1:
            middle = (low + high) // 2
            if self.timestamp(middle) <= target:
                low = middle
            else:
                high = middle
        return low


def record(timeline, number, head):
    block = timeline.block(number)
    entry = {
        "number": number,
        "hash": block["hash"],
        "state_root": block.get("stateRoot"),
        "timestamp": int(block["timestamp"], 16),
        "timestamp_utc": lib.utc_text(int(block["timestamp"], 16)),
    }
    if number < head:
        following = timeline.timestamp(number + 1)
        entry["next_timestamp"] = following
        entry["next_timestamp_utc"] = lib.utc_text(following)
    return entry


def resolve_chain(chain, config, times):
    entry = config["chains"][chain]
    rpc = lib.chain_rpc(config, chain)
    chain_id = rpc.chain_id()
    if chain_id != int(entry["chain_id"]):
        raise ValueError(f"{chain}: RPC chain id {chain_id} != {entry['chain_id']}")
    head = rpc.head()
    timeline = Timeline(rpc)
    blocks = {}
    for label, target in times.items():
        pin = (entry.get("pinned_blocks") or {}).get(label)
        if pin:
            number = pin["number"]
            found = record(timeline, number, head)
            if found["hash"].lower() != pin["hash"].lower():
                raise ValueError(f"{chain} {label}: pinned hash mismatch")
            if found["timestamp"] > target:
                raise ValueError(f"{chain} {label}: pinned block is after the time")
            if found.get("next_timestamp", target + 1) <= target:
                raise ValueError(
                    f"{chain} {label}: a later block is still at or before the time"
                )
            found["source"] = "pinned"
        else:
            number = timeline.last_at_or_before(target, head)
            found = record(timeline, number, head)
            found["source"] = "timestamp_search"
        blocks[label] = found
        print(
            f"{chain} {label}: block {found['number']} "
            f"{found['timestamp_utc']} ({found['source']})",
            flush=True,
        )
    return {
        "chain_id": chain_id,
        "rpc": rpc.host,
        "head": head,
        "blocks": blocks,
    }


def main():
    args = parse_args()
    lib.prepare_outputs([args.output], args.replace)
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    times = {label: lib.parse_utc(config["times"][label]) for label in lib.TIME_LABELS}
    chains = args.chain or [lib.HARMONY, *routes["chains"]]
    result = {
        "schema_version": 1,
        "times": {label: config["times"][label] for label in lib.TIME_LABELS},
        "chains": {chain: resolve_chain(chain, config, times) for chain in chains},
    }
    lib.write_json(args.output, result)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
