#!/usr/bin/env python3

"""List every remote adapter a registered Harmony proxy ever trusted.

Reads SetTrustedRemote and SetTrustedRemoteAddress events emitted by all
registered Harmony proxies, in one multi-address eth_getLogs pass up to the
Harmony cutoff block. A remote adapter that is not registered for the pool
(or as a previous adapter) fails the check, because an older escrow can
still hold collateral or carry undelivered messages.
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
    parser.add_argument("--blocks", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--from-block", type=int, default=0)
    parser.add_argument("--range-size", type=int, default=1000)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    lib.prepare_outputs([args.output], args.replace)
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    cutoff = lib.load_json(args.blocks)["chains"][lib.HARMONY]["blocks"]["cutoff"]["number"]
    rpc = lib.chain_rpc(config, lib.HARMONY)
    chain_by_lz = {
        entry["layerzero_chain_id"]: name for name, entry in routes["chains"].items()
    }
    owners = {}
    for pool in routes["pools"]:
        for entry in pool["harmony"]:
            owners.setdefault(entry["proxy"], []).append(pool)
    start = args.from_block or min(
        lib.deployment_block(rpc, proxy, cutoff) for proxy in sorted(owners)
    )
    print(f"scanning {len(owners)} Harmony proxies from block {start}", flush=True)
    logs = rpc.logs(
        sorted(owners),
        [[lib.TOPIC["SetTrustedRemote"], lib.TOPIC["SetTrustedRemoteAddress"]]],
        start, cutoff, args.range_size,
    )
    events, unknown = [], []
    for log in sorted(logs, key=lambda item: (int(item["blockNumber"], 16), int(item["logIndex"], 16))):
        proxy = lib.address(log["address"])
        data = log["data"]
        lz_id = int(data[2:66], 16)
        value = lib.decode_dynamic_bytes(data, 1) or "0x"
        raw = value[2:]
        remote = "0x" + raw[:40].lower() if len(raw) >= 40 and int(raw[:40], 16) else ""
        chain = chain_by_lz.get(lz_id)
        event = {
            "block": int(log["blockNumber"], 16),
            "transaction": log["transactionHash"],
            "harmony_proxy": proxy,
            "layerzero_chain_id": lz_id,
            "chain": chain,
            "remote_peer": remote,
        }
        events.append(event)
        if not remote:
            continue
        known = any(
            pool["chain"] == chain
            and remote in {
                pool["remote_adapter"],
                *pool["previous_remote_adapters"],
                *(item["address"] for item in pool["ignored_remote_peers"]),
            }
            for pool in owners[proxy]
        )
        if not known:
            unknown.append(event)
    lib.write_json(args.output, {
        "schema_version": 1,
        "status": "passed" if not unknown else "review",
        "rpc_host": rpc.host,
        "from_block": start,
        "to_block": cutoff,
        "proxies": len(owners),
        "events": events,
        "unregistered_remote_peers": unknown,
    })
    print(f"wrote {args.output}: {len(events)} events, {len(unknown)} unregistered remote peers")
    for event in unknown:
        print(
            f"  {event['harmony_proxy']} trusted {event['chain'] or event['layerzero_chain_id']} "
            f"{event['remote_peer']} at block {event['block']}"
        )


if __name__ == "__main__":
    main()
