#!/usr/bin/env python3

"""Choose the claimant snapshot block for every bridge pool.

Remote-escrow pools pay Harmony holders of the wrapped token at the Harmony
cutoff. Harmony-origin ONE pools pay remote holders of the bridged token at
the remote cutoff, unless the remote chain received rollback-orphaned
deliveries from Harmony, in which case the remote rollback block is used.

A delivery is rollback-orphaned when the remote endpoint consumed a nonce
that canonical Harmony never sent: remote inbound nonce at the state block
exceeds the Harmony outbound nonce at the rollback block, the last block both
histories share. Only endpoint nonce counters read over RPC are compared.
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
    parser.add_argument("--state", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


REMOTE_LABELS = (*lib.TIME_LABELS, "state")


def pool_nonces(pool, state):
    """Collect every endpoint counter for the pool's Harmony proxies and
    remote adapters (current and previous)."""
    harmony = state["pools"][pool["pool_id"]]["harmony"]
    remote = state["pools"][pool["pool_id"]]["remote"]["by_label"]
    previous = pool["previous_remote_adapters"]
    paths = []
    for index in range(len(pool["harmony"])):
        labels = harmony[index]["by_label"]
        paths.append({
            "harmony_outbound": {label: labels[label]["outbound_nonce"] for label in lib.TIME_LABELS},
            "remote_inbound": {label: remote[label]["inbound_nonce"][index] for label in REMOTE_LABELS},
            "remote_inbound_previous": {
                label: [remote[label]["previous_adapter_nonces"][item]["inbound_nonce"][index] for item in previous]
                for label in REMOTE_LABELS
            },
        })
    adapters = []
    for position, adapter in enumerate([pool["remote_adapter"], *previous]):
        if position == 0:
            outbound = {label: remote[label]["outbound_nonce"] for label in REMOTE_LABELS}
            inbound = {
                label: [harmony[index]["by_label"][label]["inbound_nonce"] for index in range(len(pool["harmony"]))]
                for label in lib.TIME_LABELS
            }
        else:
            outbound = {
                label: remote[label]["previous_adapter_nonces"][adapter]["outbound_nonce"]
                for label in REMOTE_LABELS
            }
            inbound = {
                label: [
                    harmony[index]["by_label"][label]["inbound_from_previous"][adapter]
                    for index in range(len(pool["harmony"]))
                ]
                for label in lib.TIME_LABELS
            }
        adapters.append({"adapter": adapter, "remote_outbound": outbound, "harmony_inbound": inbound})
    return paths, adapters


def values_of(item):
    if isinstance(item, dict):
        for value in item.values():
            yield from values_of(value)
    elif isinstance(item, list):
        for value in item:
            yield from values_of(value)
    else:
        yield item


def analyze_pool(pool, state):
    """Return nonce findings for each Harmony proxy and each remote adapter.

    A Harmony proxy's outbound counter covers every remote path it ever used,
    so its deliveries are summed over the current and previous adapters.
    Likewise each remote adapter's outbound counter is compared with the sum
    of Harmony inbound counters over every proxy. remote_path_offset converts
    a proxy's nonces to the current adapter's path counter.
    """
    paths, adapters = pool_nonces(pool, state)
    if any(value is None for value in values_of([paths, adapters])):
        return {"status": "review", "reason": "unreadable nonce", "paths": paths, "adapters": adapters}
    reasons = []
    h2r = []
    for nonces in paths:
        h_out = nonces["harmony_outbound"]
        delivered = {
            label: nonces["remote_inbound"][label] + sum(nonces["remote_inbound_previous"][label])
            for label in REMOTE_LABELS
        }
        item = {
            "orphaned_harmony_to_remote": max(0, delivered["state"] - h_out["rollback"]),
            "canonical_harmony_sends_after_rollback": h_out["cutoff"] - h_out["rollback"],
            "undelivered_harmony_to_remote": max(0, h_out["cutoff"] - delivered["state"]),
            "delivered_total": delivered,
            "remote_path_offset": sum(nonces["remote_inbound_previous"]["state"]),
            "nonces": nonces,
        }
        if delivered["rollback"] > h_out["rollback"]:
            reasons.append(
                "remote inbound at the rollback time exceeds Harmony outbound "
                "at the rollback block; nonce counters are not comparable"
            )
        if item["orphaned_harmony_to_remote"] and item["canonical_harmony_sends_after_rollback"]:
            reasons.append(
                "canonical Harmony sends after the rollback collide with "
                "rollback-orphaned remote nonces"
            )
        if item["orphaned_harmony_to_remote"] and item["remote_path_offset"] != sum(
            nonces["remote_inbound_previous"]["rollback"]
        ):
            reasons.append("a previous adapter received messages after the rollback")
        h2r.append(item)
    r2h = []
    for adapter in adapters:
        r_out = adapter["remote_outbound"]
        h_in = {label: sum(adapter["harmony_inbound"][label]) for label in lib.TIME_LABELS}
        if h_in["rollback"] > r_out["rollback"]:
            reasons.append(
                "Harmony inbound at the rollback block exceeds remote outbound at "
                "the rollback time; nonce counters are not comparable"
            )
        r2h.append({
            "adapter": adapter["adapter"],
            "delivered_after_harmony_rollback": h_in["cutoff"] - h_in["rollback"],
            "undelivered": max(0, r_out["state"] - h_in["cutoff"]),
            "harmony_inbound_total": h_in,
            "remote_outbound": r_out,
        })
    result = {
        "status": "review" if reasons else "ok",
        "harmony_to_remote": h2r,
        "remote_to_harmony": r2h,
        "orphaned_harmony_to_remote": sum(item["orphaned_harmony_to_remote"] for item in h2r),
    }
    if reasons:
        result["reason"] = "; ".join(sorted(set(reasons)))
    return result


def main():
    args = parse_args()
    lib.prepare_outputs([args.output], args.replace)
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    state = lib.load_json(args.state)
    blocks = state["blocks"]
    overrides = config["snapshot_policy"].get("remote_holder_overrides") or {}

    analyses = {
        pool["pool_id"]: analyze_pool(pool, state) for pool in routes["pools"]
    }

    exposure = {}
    for chain in routes["chains"]:
        one_pools = [
            pool for pool in routes["pools"]
            if pool["chain"] == chain and pool["claim_asset"] == "one"
        ]
        orphaned = sum(
            analyses[pool["pool_id"]].get("orphaned_harmony_to_remote", 0)
            for pool in one_pools
        )
        orphaned_other = {
            pool["pool_id"]: analyses[pool["pool_id"]]["orphaned_harmony_to_remote"]
            for pool in routes["pools"]
            if pool["chain"] == chain
            and pool["claim_asset"] != "one"
            and analyses[pool["pool_id"]].get("orphaned_harmony_to_remote")
        }
        review = [
            pool["pool_id"] for pool in one_pools
            if analyses[pool["pool_id"]]["status"] != "ok"
        ]
        exposed = orphaned > 0
        label = overrides.get(chain) or ("rollback" if exposed else "cutoff")
        exposure[chain] = {
            "has_one_pool": bool(one_pools),
            "orphaned_one_deliveries": orphaned,
            "orphaned_other_pool_deliveries": orphaned_other,
            "exposed": exposed,
            "review_pools": review,
            "snapshot_label": label,
            "basis": (
                "configured override"
                if chain in overrides
                else "rollback-orphaned ONE deliveries"
                if exposed
                else "no rollback-orphaned ONE deliveries"
            ),
        }

    plans = {}
    snapshots = {}
    for pool in routes["pools"]:
        pid = pool["pool_id"]
        analysis = analyses[pid]
        if pool["claim_asset"] == "unsupported":
            plans[pid] = {
                "status": "unsupported",
                "reason": "Harmony-origin asset with no claim asset off Harmony",
                "nonce_analysis": analysis,
            }
            continue
        if pool["kind"] == "remote_escrow":
            label = "cutoff"
            chain = lib.HARMONY
        else:
            chain = pool["chain"]
            label = exposure[chain]["snapshot_label"]
        block = blocks[chain][label]
        tokens = []
        for token_chain, token in lib.claimant_tokens(pool):
            stem = lib.holder_file_stem(token_chain, token, block["number"])
            tokens.append({"chain": token_chain, "token": token, "stem": stem})
            snapshots[stem] = {
                "chain": token_chain,
                "token": token,
                "block": block["number"],
                "block_hash": block["hash"],
                "label": label,
                "stem": stem,
            }
        plans[pid] = {
            "status": "planned" if analysis["status"] == "ok" else "review",
            "claimant_chain": chain,
            "snapshot_label": label,
            "snapshot_block": block,
            "tokens": tokens,
            "nonce_analysis": analysis,
        }
    result = {
        "schema_version": 1,
        "exposure": exposure,
        "pools": plans,
        "holder_snapshots": [snapshots[key] for key in sorted(snapshots)],
    }
    lib.write_json(args.output, result)
    for chain, item in exposure.items():
        if item["has_one_pool"]:
            print(
                f"{chain}: orphaned ONE deliveries={item['orphaned_one_deliveries']} "
                f"snapshot={item['snapshot_label']} ({item['basis']})"
            )
    review = [pid for pid, plan in plans.items() if plan["status"] == "review"]
    print(f"wrote {args.output}; pools needing review: {', '.join(review) or 'none'}")


if __name__ == "__main__":
    main()
