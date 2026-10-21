#!/usr/bin/env python3

"""List bridge messages that a holder snapshot alone would misstate.

For every planned pool the nonce analysis in the snapshot plan gives exact
nonce ranges. This script finds the matching OFT events, assigns each event
its nonce from the endpoint counter before and after its block, and turns
each message into a signed per-address adjustment for the claimant ledger:

remote_escrow pools (claimants hold the wrapped token on Harmony at cutoff)
  orphaned_payout       remote escrow paid a rollback-orphaned Harmony burn;
                        the burn was restored on Harmony, so the recipient is
                        debited (or the loss stays with the pool)
  undelivered_burn      canonical Harmony burn the escrow never paid; the
                        Harmony sender is credited
  undelivered_deposit   remote deposit never minted on canonical Harmony; the
                        remote sender is credited

harmony_origin pools (claimants hold the bridged token on the remote chain at
the snapshot block S)
  orphaned_mint         remote mint for a rollback-orphaned Harmony lock;
                        excluded when S precedes it, otherwise debited
  canonical_mint_after_snapshot
                        canonical Harmony lock minted remotely after S; the
                        recipient is credited
  undelivered_lock      canonical Harmony lock never minted remotely; the
                        Harmony sender is credited
  undelivered_return    remote burn at or before S that canonical Harmony
                        never released; the remote sender is credited (later
                        undelivered burns are already undone by S, so they
                        are only counted)
  canonical_release_after_snapshot
                        remote burn after S that canonical Harmony released;
                        the remote sender is debited

Every pool also lists MessageFailed events seen in the scanned windows,
because a failed delivery consumes a nonce without crediting tokens.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402

FIELDS = (
    "pool_id",
    "path_index",
    "direction",
    "nonce",
    "category",
    "source_chain",
    "source_block",
    "source_tx",
    "destination_chain",
    "destination_block",
    "destination_tx",
    "sender",
    "recipient",
    "amount_raw",
    "adjustment_account",
    "adjustment_raw",
    "note",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--pool", action="append", default=[])
    parser.add_argument("--range-size", type=int, default=5000)
    parser.add_argument(
        "--cache-dir",
        help="per-pool results reused while state, plan, and policy are unchanged; "
        "default is message-cache next to --output",
    )
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


class Counter:
    """An endpoint nonce counter that can be read at any block."""

    def __init__(self, rpc, endpoint, data):
        self.rpc = rpc
        self.endpoint = endpoint
        self.data = data
        self.cache = {}

    def at(self, block):
        if block < 0:
            return 0
        if block not in self.cache:
            result, error = self.rpc.call_soft(
                "eth_call",
                [{"to": self.endpoint, "data": self.data}, lib.block_tag(block)],
            )
            if error is not None:
                raise lib.RpcError(
                    f"{self.rpc.label}: nonce unreadable at block {block}: {error}"
                )
            self.cache[block] = 0 if result in (None, "0x") else int(result, 16)
        return self.cache[block]

    def first_block_reaching(self, target, low, high):
        """Smallest block in [low, high] whose counter is at least target."""
        if self.at(high) < target:
            raise ValueError(
                f"{self.rpc.label}: counter {self.at(high)} never reaches "
                f"{target} by block {high}"
            )
        while low < high:
            middle = (low + high) // 2
            if self.at(middle) >= target:
                high = middle
            else:
                low = middle + 1
        return low


def decode_event(log, kind):
    topics = log["topics"]
    data = log["data"]
    words = [data[2 + i:2 + i + 64] for i in range(0, len(data) - 2, 64)]
    if kind == "send":
        return {
            "sender": lib.topic_address(topics[2]),
            "recipient": "",
            "amount": int(words[0], 16),
        }
    if kind == "native_send":
        return {
            "sender": lib.topic_address(topics[1]),
            "recipient": "",
            "amount": int(words[0], 16),
        }
    if kind == "receive":
        return {
            "sender": lib.packed_address(lib.decode_dynamic_bytes(data, 0)) or "",
            "recipient": lib.topic_address(topics[2]),
            "amount": int(words[1], 16),
        }
    if kind == "failed":
        source, recipient, amount = lib.decode_oft_payload(
            lib.decode_dynamic_bytes(data, 3)
        )
        return {
            "source_chain_id": int(words[0], 16),
            "path": lib.decode_dynamic_bytes(data, 1),
            "nonce": int(words[2], 16),
            "sender": source or "",
            "recipient": recipient or "",
            "amount": amount,
        }
    raise ValueError(kind)


def collect(
    counter, rpc, contract, topic_sets, low_nonce, high_nonce, search, range_size,
    accept=None,
):
    """Return events for nonces in (low_nonce, high_nonce], keyed by nonce.

    topic_sets maps an event kind to the topic filter for that kind; accept
    can drop logs (for example MessageFailed from another path) before
    counting. Events in one block are numbered in log order from the counter
    before the block.
    """
    if high_nonce <= low_nonce:
        return {}, []
    start = counter.first_block_reaching(low_nonce + 1, *search)
    end = counter.first_block_reaching(high_nonce, start, search[1])
    count = high_nonce - low_nonce
    range_cost = (end - start) // range_size + 1
    search_cost = count * max(1, (end - start).bit_length())
    if range_cost <= search_cost:
        windows = [(start, end)]
    else:
        blocks, low = set(), start
        for nonce in range(low_nonce + 1, high_nonce + 1):
            low = counter.first_block_reaching(nonce, low, end)
            blocks.add(low)
        windows = [(block, block) for block in sorted(blocks)]
    logs = []
    for kind, topics in topic_sets.items():
        for first, last in windows:
            for log in rpc.logs(contract, topics, first, last, range_size):
                if accept is not None and not accept(kind, log):
                    continue
                logs.append((int(log["blockNumber"], 16), int(log["logIndex"], 16), kind, log))
    logs.sort(key=lambda item: (item[0], item[1]))
    by_block = {}
    for item in logs:
        by_block.setdefault(item[0], []).append(item)
    events = {}
    problems = []
    for block in sorted(by_block):
        before, after = counter.at(block - 1), counter.at(block)
        items = by_block[block]
        if after - before != len(items):
            problems.append(
                f"block {block}: counter moved {after - before} for {len(items)} events"
            )
            continue
        for offset, (_, _, kind, log) in enumerate(items):
            nonce = before + offset + 1
            if low_nonce < nonce <= high_nonce:
                event = decode_event(log, kind)
                event.update({
                    "kind": kind,
                    "block": block,
                    "tx": log["transactionHash"],
                })
                events[nonce] = event
    missing = [n for n in range(low_nonce + 1, high_nonce + 1) if n not in events]
    if missing:
        problems.append(
            f"no event for {len(missing)} nonces in ({low_nonce}, {high_nonce}]"
        )
    return events, problems


def send_topics(destination_lz_chain_id, contract):
    """Topic filters for a source-side send.

    OFT and ProxyOFT contracts emit SendToChain. NativeOFT contracts emit no
    SendToChain; their debit is a Transfer of the internal token from the
    sender to the contract itself, which no other path emits.
    """
    return {
        "send": [lib.TOPIC["SendToChain"], "0x" + lib.word(destination_lz_chain_id)],
        "native_send": [lib.TOPIC["Transfer"], None, "0x" + lib.address_word(contract)],
    }


def accept_native_send(kind, log):
    return kind != "native_send" or lib.topic_address(log["topics"][1]) != lib.ZERO_ADDRESS


def rows_for(events, base, category, chooser, note="", failed_credit=False):
    """Turn events into ledger rows.

    A failed delivery moved no tokens: in a canonical range the recipient is
    still owed (failed_credit), in an orphaned range nothing is adjusted.
    """
    rows = []
    for nonce in sorted(events):
        event = events[nonce]
        account, sign = chooser(event)
        row = dict(base)
        row.update({
            "nonce": nonce,
            "category": category,
            "sender": event["sender"],
            "recipient": event["recipient"],
            "amount_raw": event["amount"],
            "adjustment_account": account or "",
            "adjustment_raw": sign * event["amount"] if account else 0,
            "note": note,
        })
        if event["kind"] == "failed":
            row["category"] = f"{category}_failed"
            if failed_credit and event["recipient"]:
                row["adjustment_account"] = event["recipient"]
                row["adjustment_raw"] = event["amount"]
                row["note"] = "delivery failed; credited unless later retried"
            else:
                row["adjustment_account"] = ""
                row["adjustment_raw"] = 0
                row["note"] = "delivery failed; no tokens moved"
        if base["direction"] == "harmony_to_remote":
            if event["kind"] in ("send", "native_send"):
                row.update(source_block=event["block"], source_tx=event["tx"])
            else:
                row.update(destination_block=event["block"], destination_tx=event["tx"])
        else:
            row.update(source_block=event["block"], source_tx=event["tx"])
        rows.append(row)
    return rows


def scan_pool(pool, plan, state, routes, config, rpcs, range_size, policy):
    pid = pool["pool_id"]
    chain = pool["chain"]
    harmony_lz = routes["harmony"]["layerzero_chain_id"]
    remote_lz = routes["chains"][chain]["layerzero_chain_id"]
    remote_rpc, harmony_rpc = rpcs[chain], rpcs[lib.HARMONY]
    remote_endpoint = state["pools"][pid]["remote"]["endpoint"]
    harmony_endpoint = routes["harmony"]["endpoint"]
    blocks = state["blocks"]
    remote_search = (0, blocks[chain]["state"]["number"])
    harmony_search = (0, blocks[lib.HARMONY]["cutoff"]["number"])
    analysis = plan["nonce_analysis"]
    snapshot = plan["snapshot_block"]["number"]
    adapter = pool["remote_adapter"]
    rows, problems = [], []

    receive_topics = {
        "receive": [lib.TOPIC["ReceiveFromChain"], "0x" + lib.word(harmony_lz)],
        "failed": [lib.TOPIC["MessageFailed"]],
    }

    def from_path(proxy):
        expected = "0x" + proxy[2:] + adapter[2:]

        def accept(kind, log):
            if kind != "failed":
                return True
            event = decode_event(log, "failed")
            return (
                event["source_chain_id"] == harmony_lz
                and (event["path"] or "").lower() == expected
            )

        return accept

    for index, entry in enumerate(pool["harmony"]):
        h2r = analysis["harmony_to_remote"][index]
        nonces = h2r["nonces"]
        h_out_rb = nonces["harmony_outbound"]["rollback"]
        h_out_cut = nonces["harmony_outbound"]["cutoff"]
        r_in_state = nonces["remote_inbound"]["state"]
        delivered_state = h2r["delivered_total"]["state"]
        offset = h2r["remote_path_offset"]

        def global_nonces(events):
            return {nonce + offset: event for nonce, event in events.items()}
        base = {
            "pool_id": pid,
            "path_index": index,
            "direction": "harmony_to_remote",
            "source_chain": lib.HARMONY,
            "destination_chain": chain,
        }
        inbound = Counter(
            remote_rpc, remote_endpoint,
            lib.inbound_nonce_data(harmony_lz, entry["proxy"], adapter),
        )
        harmony_out = Counter(
            harmony_rpc, harmony_endpoint,
            lib.outbound_nonce_data(remote_lz, entry["proxy"]),
        )
        harmony_sends = send_topics(remote_lz, entry["proxy"])

        accept = from_path(entry["proxy"])
        orphaned, found = collect(
            inbound, remote_rpc, adapter, receive_topics,
            max(0, h_out_rb - offset), r_in_state, remote_search, range_size, accept,
        )
        orphaned = global_nonces(orphaned)
        problems += [f"{pid} orphaned: {item}" for item in found]
        undelivered, found = collect(
            harmony_out, harmony_rpc, entry["proxy"], harmony_sends,
            delivered_state, h_out_cut, harmony_search, range_size, accept_native_send,
        )
        problems += [f"{pid} undelivered: {item}" for item in found]

        if pool["kind"] == "remote_escrow":
            debit = policy["orphaned_payouts"] == "debit_recipient"
            rows += rows_for(
                orphaned, base, "orphaned_payout",
                lambda event: (event["recipient"], -1) if debit else (None, 0),
                "" if debit else "loss stays with the pool",
            )
            rows += rows_for(
                undelivered, base, "undelivered_burn",
                lambda event: (event["sender"], 1),
            )
        else:
            rows += rows_for(
                {n: e for n, e in orphaned.items() if e["block"] > snapshot},
                base, "orphaned_mint", lambda event: (None, 0),
                "after the snapshot block, so already excluded",
            )
            rows += rows_for(
                {n: e for n, e in orphaned.items() if e["block"] <= snapshot},
                base, "orphaned_mint_in_snapshot",
                lambda event: (event["recipient"], -1),
                "inside the snapshot, so debited",
            )
            r_in_snapshot = inbound.at(snapshot)
            after, found = collect(
                inbound, remote_rpc, adapter, receive_topics,
                r_in_snapshot, min(r_in_state, max(0, h_out_rb - offset)), remote_search,
                range_size, accept,
            )
            after = global_nonces(after)
            problems += [f"{pid} after snapshot: {item}" for item in found]
            rows += rows_for(
                after, base, "canonical_mint_after_snapshot",
                lambda event: (event["recipient"], 1),
                failed_credit=True,
            )
            rows += rows_for(
                undelivered, base, "undelivered_lock",
                lambda event: (event["sender"], 1),
            )

    counts = {}
    for r2h in analysis["remote_to_harmony"]:
        source = r2h["adapter"]
        outbound = Counter(
            remote_rpc, remote_endpoint, lib.outbound_nonce_data(harmony_lz, source)
        )
        remote_sends = send_topics(harmony_lz, source)
        h_in_rb = r2h["harmony_inbound_total"]["rollback"]
        h_in_cut = r2h["harmony_inbound_total"]["cutoff"]
        r_out_state = r2h["remote_outbound"]["state"]
        base = {
            "pool_id": pid,
            "path_index": source,
            "direction": "remote_to_harmony",
            "source_chain": chain,
            "destination_chain": lib.HARMONY,
        }
        if pool["kind"] == "remote_escrow":
            undelivered, found = collect(
                outbound, remote_rpc, source, remote_sends,
                h_in_cut, r_out_state, remote_search, range_size, accept_native_send,
            )
            problems += [f"{pid} undelivered deposits from {source}: {item}" for item in found]
            rows += rows_for(
                undelivered, base, "undelivered_deposit",
                lambda event: (event["sender"], 1),
            )
            continue
        r_out_snapshot = outbound.at(snapshot)
        undelivered, found = collect(
            outbound, remote_rpc, source, remote_sends,
            h_in_cut, min(r_out_state, r_out_snapshot), remote_search, range_size,
            accept_native_send,
        )
        problems += [f"{pid} undelivered returns from {source}: {item}" for item in found]
        rows += rows_for(
            undelivered, base, "undelivered_return",
            lambda event: (event["sender"], 1),
            "sent at or before the snapshot block",
        )
        counts["undelivered_return_after_snapshot"] = counts.get(
            "undelivered_return_after_snapshot", 0
        ) + max(0, r_out_state - max(h_in_cut, r_out_snapshot))
        released, found = collect(
            outbound, remote_rpc, source, remote_sends,
            h_in_rb, h_in_cut, remote_search, range_size, accept_native_send,
        )
        problems += [f"{pid} released after rollback from {source}: {item}" for item in found]
        late = {nonce: event for nonce, event in released.items() if event["block"] > snapshot}
        rows += rows_for(
            late, base, "canonical_release_after_snapshot",
            lambda event: (event["sender"], -1),
        )
    return rows, problems, counts


def main():
    args = parse_args()
    lib.prepare_outputs([args.output, args.summary], args.replace)
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    state = lib.load_json(args.state)
    plan = lib.load_json(args.plan)
    policy = config.get("allocation_policy") or {}
    policy.setdefault("orphaned_payouts", "debit_recipient")
    if policy["orphaned_payouts"] not in ("debit_recipient", "pool_loss"):
        raise ValueError("allocation_policy.orphaned_payouts is invalid")
    selected = set(args.pool)
    cache_dir = args.cache_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.output)), "message-cache"
    )
    os.makedirs(cache_dir, exist_ok=True)
    fingerprint = {
        "state_sha256": lib.file_sha256(args.state),
        "plan_sha256": lib.file_sha256(args.plan),
        "policy": {"orphaned_payouts": policy["orphaned_payouts"]},
    }
    rpcs = {}
    rows, summary_pools = [], {}
    for pool in routes["pools"]:
        pid = pool["pool_id"]
        pool_plan = plan["pools"][pid]
        if selected and pid not in selected:
            continue
        if pool_plan["status"] != "planned":
            summary_pools[pid] = {"status": "skipped", "plan_status": pool_plan["status"]}
            continue
        cache_path = os.path.join(cache_dir, f"{pid}.json")
        cached = lib.load_json(cache_path) if os.path.exists(cache_path) else None
        if cached and cached["fingerprint"] == fingerprint and not cached["problems"]:
            pool_rows, problems, counts = cached["rows"], [], cached["counts"]
            print(f"{pid}: reused {len(pool_rows)} cached messages", flush=True)
        else:
            for chain in (lib.HARMONY, pool["chain"]):
                if chain not in rpcs:
                    rpcs[chain] = lib.chain_rpc(config, chain)
            pool_rows, problems, counts = scan_pool(
                pool, pool_plan, state, routes, config, rpcs, args.range_size, policy
            )
            if os.path.exists(cache_path):
                os.remove(cache_path)
            lib.write_json(cache_path, {
                "fingerprint": fingerprint,
                "rows": pool_rows,
                "problems": problems,
                "counts": counts,
            })
        rows += pool_rows
        totals = {}
        for row in pool_rows:
            item = totals.setdefault(row["category"], {"messages": 0, "amount_raw": 0, "adjustment_raw": 0})
            item["messages"] += 1
            item["amount_raw"] += int(row["amount_raw"])
            item["adjustment_raw"] += int(row["adjustment_raw"])
        summary_pools[pid] = {
            "status": "complete" if not problems else "incomplete",
            "problems": problems,
            "counted_without_events": counts,
            "categories": {
                key: {name: str(value) for name, value in item.items()}
                for key, item in sorted(totals.items())
            },
        }
        print(
            f"{pid}: {len(pool_rows)} messages"
            + (f"; problems: {problems}" if problems else ""),
            flush=True,
        )
    rows.sort(key=lambda row: (row["pool_id"], row["direction"], str(row["path_index"]), int(row["nonce"])))
    digest = lib.write_csv(args.output, FIELDS, rows)
    incomplete = [pid for pid, item in summary_pools.items() if item["status"] == "incomplete"]
    summary = {
        "schema_version": 1,
        "status": "passed" if not incomplete else "incomplete",
        "incomplete_pools": incomplete,
        "policy": {"orphaned_payouts": policy["orphaned_payouts"]},
        "messages": len(rows),
        "output": os.path.basename(args.output),
        "output_sha256": digest,
        "pools": summary_pools,
    }
    lib.write_json(args.summary, summary)
    print(f"wrote {args.output} ({len(rows)} rows) status={summary['status']}")


if __name__ == "__main__":
    main()
