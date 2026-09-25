#!/usr/bin/env python3
"""Recompute initial-distribution wallet amounts from Harmony's public RPC.

    python3 tools/verify_cutoff_amounts.py --list selected.csv --evidence evidence.csv \
        --out report.csv [--head]

Standard library only. For every address in --list the script reads, at the cutoff blocks,

  liquid ONE    hmyv2_getBalanceByBlockNumber on shard 0 (block 93,623,067) and shard 1 (95,882,100)
  WONE          eth_call balanceOf(address) on the WONE contract at the shard-0 cutoff block
  staking       hmyv2_getDelegationsByDelegatorByBlockNumber at the shard-0 cutoff block:
                unclaimed rewards, pending undelegations and active delegations

and checks

  amount        list amount == liquid s0 + liquid s1 + WONE + rewards + pending undelegations
                + pending cross-shard - deduction (active delegations go to validator vaults,
                not to the wallet)
  threshold     the same total plus active delegations is at least 1,000 ONE after the deduction
  activity      the evidence transaction exists on the canonical chain at its block, involves
                the address, and is at or after the six-month window start and before the cutoff

Only deductions and pending cross-shard amounts come from --evidence (the not-issued
inventories); everything else is read from the chain. With --head the script also reads the
same values at the current head and reports whether they are unchanged. The chain stopped
shortly after the cutoff, so a wallet whose liquid ONE and WONE are unchanged shows its cutoff
balances in any explorer (staking rewards kept accruing for the last blocks, so the explorer's
reward figures are slightly higher than at the cutoff).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ATTO = 10**18
MINIMUM = 1000 * ATTO
WONE = "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a"
CUTOFF = {
    0: (93623067, "0x23572e11f6ef9afe4c27ab3102f15b99fd7277ae5f685ccaef0ae571fe7ee0b6"),
    1: (95882100, "0xf801577a5480175c05a63c8e4e5cf3d2623e1a01bdf176e16b46967405ae02ee"),
}
CUTOFF_TIME = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
WINDOW_START = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def one1_to_hex(value: str) -> str:
    value = value.lower()
    if value.startswith("0x"):
        return value
    data = [BECH32.index(c) for c in value[value.rindex("1") + 1:-6]]
    acc, bits, out = 0, 0, []
    for d in data:
        acc = (acc << 5) | d
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return "0x" + bytes(out).hex()


class Rpc:
    def __init__(self, url: str, batch: int):
        self.url, self.batch = url, batch

    def calls(self, requests: list[tuple[str, list]]) -> list:
        out = []
        for start in range(0, len(requests), self.batch):
            out.extend(self._batch(requests[start:start + self.batch]))
        return out

    def _batch(self, requests):
        body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(requests)])
        for attempt in range(6):
            try:
                req = urllib.request.Request(self.url, body.encode(), {"Content-Type": "application/json"})
                replies = json.load(urllib.request.urlopen(req, timeout=120))
                by_id = {r["id"]: r for r in replies}
                if any("error" in r for r in replies):
                    raise RuntimeError(next(r["error"] for r in replies if "error" in r))
                return [by_id[i].get("result") for i in range(len(requests))]
            except Exception as error:
                if attempt == 5:
                    raise RuntimeError(f"{self.url}: {error}") from error
                time.sleep(2 * (attempt + 1))


def staking_parts(delegations) -> tuple[int, int, int]:
    reward = undelegating = active = 0
    for d in delegations or []:
        reward += int(d.get("reward", 0))
        active += int(d.get("amount", 0))
        for u in d.get("Undelegations") or d.get("undelegations") or []:
            undelegating += int(u.get("Amount", u.get("amount", 0)))
    return reward, undelegating, active


def balance_call(address: str, block) -> tuple[str, list]:
    data = "0x70a08231" + address[2:].rjust(64, "0")
    return "eth_call", [{"to": WONE, "data": data}, hex(block) if isinstance(block, int) else block]


def read_state(rpc0: Rpc, rpc1: Rpc, addresses: list[str], head: bool) -> list[dict]:
    b0 = "latest" if head else CUTOFF[0][0]
    b1 = "latest" if head else CUTOFF[1][0]
    if head:
        s0 = rpc0.calls([(m, p) for a in addresses for m, p in (
            ("hmyv2_getBalance", [a]), balance_call(a, "latest"), ("hmyv2_getDelegationsByDelegator", [a]))])
        s1 = rpc1.calls([("hmyv2_getBalance", [a]) for a in addresses])
    else:
        s0 = rpc0.calls([(m, p) for a in addresses for m, p in (
            ("hmyv2_getBalanceByBlockNumber", [a, b0]), balance_call(a, b0),
            ("hmyv2_getDelegationsByDelegatorByBlockNumber", [a, b0]))])
        s1 = rpc1.calls([("hmyv2_getBalanceByBlockNumber", [a, b1]) for a in addresses])
    states = []
    for i, a in enumerate(addresses):
        reward, undelegating, active = staking_parts(s0[3 * i + 2])
        states.append({
            "liquid_shard0_atto": int(s0[3 * i]), "liquid_shard1_atto": int(s1[i]),
            "wone_atto": int(s0[3 * i + 1], 16), "unclaimed_reward_atto": reward,
            "pending_undelegation_atto": undelegating, "active_delegation_atto": active,
        })
    return states


def check_activity(rpcs: dict, rows: list[dict]) -> list[str]:
    """Returns '' when the evidence transaction proves in-window activity, else the reason."""
    found = [None] * len(rows)
    for shard in (0, 1):
        pending = [i for i, r in enumerate(rows) if found[i] is None and r["activity_tx_hash"]]
        if not pending:
            break
        method = lambda r: "hmyv2_getStakingTransactionByHash" if r["activity_type"] == "staking" else "hmyv2_getTransactionByHash"
        replies = rpcs[shard].calls([(method(rows[i]), [rows[i]["activity_tx_hash"]]) for i in pending])
        eth = [i for i, reply in zip(pending, replies) if not reply and rows[i]["activity_type"] != "staking"]
        eth_replies = dict(zip(eth, rpcs[shard].calls([("eth_getTransactionByHash", [rows[i]["activity_tx_hash"]]) for i in eth])))
        for i, reply in zip(pending, replies):
            reply = reply or eth_replies.get(i)
            if reply:
                found[i] = (shard, reply)
    blocks = {}
    for shard in (0, 1):
        numbers = sorted({int(str(f[1]["blockNumber"]), 0) for f in found if f and f[0] == shard})
        for n, block in zip(numbers, rpcs[shard].calls([("hmyv2_getBlockByNumber", [n, {"fullTx": False}]) for n in numbers])):
            blocks[(shard, n)] = block
    reasons = []
    for row, f in zip(rows, found):
        if not row["activity_tx_hash"]:
            reasons.append("no evidence transaction")
            continue
        if not f:
            reasons.append("evidence transaction not found")
            continue
        shard, tx = f
        number = int(str(tx["blockNumber"]), 0)
        block = blocks[(shard, number)]
        when = datetime.fromtimestamp(int(str(block["timestamp"]), 0), timezone.utc)
        parties = {one1_to_hex(str(tx.get(k) or "")) for k in ("from", "to") if tx.get(k)}
        msg = tx.get("msg") or {}
        parties |= {one1_to_hex(str(msg[k])) for k in ("delegatorAddress", "validatorAddress") if msg.get(k)}
        if block["hash"].lower() != str(tx["blockHash"]).lower():
            reasons.append("evidence block is not canonical")
        elif row["address"] not in parties:
            reasons.append("evidence transaction does not involve the address")
        elif not (WINDOW_START <= when < CUTOFF_TIME) or number > CUTOFF[shard][0]:
            reasons.append(f"evidence transaction at {when:%Y-%m-%dT%H:%M:%SZ} is outside the window")
        else:
            reasons.append("")
    return reasons


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", required=True, help="CSV with the addresses and amounts to check")
    p.add_argument("--address-column", default="address")
    p.add_argument("--amount-column", default="amount_atto")
    p.add_argument("--evidence", required=True, help="CSV: address, deduction_atto, pending_cross_shard_atto, activity_*")
    p.add_argument("--out", required=True)
    p.add_argument("--rpc0", default="https://a.api.s0.t.hmny.io")
    p.add_argument("--rpc1", default="https://a.api.s1.t.hmny.io")
    p.add_argument("--head", action="store_true", help="also compare with the current head (explorer view)")
    p.add_argument("--batch", type=int, default=60, help="JSON-RPC requests per HTTP call")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()

    rpcs = {0: Rpc(args.rpc0, args.batch), 1: Rpc(args.rpc1, args.batch)}
    for shard, (number, expected) in CUTOFF.items():
        block = rpcs[shard].calls([("hmyv2_getBlockByNumber", [number, {"fullTx": False}])])[0]
        if block["hash"].lower() != expected:
            sys.exit(f"shard {shard} block {number} is {block['hash']}, expected {expected}")

    listed = [(r[args.address_column].lower(), int(r[args.amount_column])) for r in csv.DictReader(open(args.list))]
    evidence = {r["address"].lower(): r for r in csv.DictReader(open(args.evidence))}
    missing = [a for a, _ in listed if a not in evidence]
    if missing:
        sys.exit(f"{len(missing)} listed addresses have no evidence row, first {missing[0]}")

    chunks = [listed[i:i + 40] for i in range(0, len(listed), 40)]

    def work(chunk):
        addresses = [a for a, _ in chunk]
        cutoff = read_state(rpcs[0], rpcs[1], addresses, False)
        now = read_state(rpcs[0], rpcs[1], addresses, True) if args.head else [None] * len(chunk)
        rows = [dict(evidence[a], address=a) for a in addresses]
        activity = check_activity(rpcs, rows)
        return list(zip(chunk, cutoff, now, rows, activity))

    results = []
    with ThreadPoolExecutor(args.workers) as pool:
        for n, part in enumerate(pool.map(work, chunks), 1):
            results.extend(part)
            print(f"\r{min(n * 40, len(listed))}/{len(listed)} addresses", end="", file=sys.stderr)
    print(file=sys.stderr)

    fields = ["address", "list_amount_atto", "liquid_shard0_atto", "liquid_shard1_atto", "wone_atto",
              "unclaimed_reward_atto", "pending_undelegation_atto", "pending_cross_shard_atto", "deduction_atto",
              "computed_amount_atto", "amount_ok", "active_delegation_atto", "qualification_after_deduction_atto",
              "threshold_ok", "activity_tx_hash", "activity_ok", "activity_problem",
              "head_wallet_unchanged", "head_staking_unchanged"]
    wallet_keys = ("liquid_shard0_atto", "liquid_shard1_atto", "wone_atto")
    totals = {"rows": 0, "amount_ok": 0, "threshold_ok": 0, "activity_ok": 0, "head_wallet_unchanged": 0,
              "list_atto": 0, "computed_atto": 0, "wone_atto": 0, "wone_rows": 0}
    with open(args.out, "w", newline="") as out:
        w = csv.DictWriter(out, fieldnames=fields)
        w.writeheader()
        for (address, amount), s, now, row, problem in results:
            deduction = int(row.get("deduction_atto") or 0)
            cx = int(row.get("pending_cross_shard_atto") or 0)
            computed = (s["liquid_shard0_atto"] + s["liquid_shard1_atto"] + s["wone_atto"] + s["unclaimed_reward_atto"]
                        + s["pending_undelegation_atto"] + cx - deduction)
            qualification = computed + s["active_delegation_atto"]
            record = dict(address=address, list_amount_atto=amount, **{k: v for k, v in s.items() if k != "active_delegation_atto"},
                          pending_cross_shard_atto=cx, deduction_atto=deduction, computed_amount_atto=computed,
                          amount_ok=computed == amount, active_delegation_atto=s["active_delegation_atto"],
                          qualification_after_deduction_atto=qualification, threshold_ok=qualification >= MINIMUM,
                          activity_tx_hash=row["activity_tx_hash"], activity_ok=problem == "", activity_problem=problem,
                          head_wallet_unchanged="" if now is None else all(now[k] == s[k] for k in wallet_keys),
                          head_staking_unchanged="" if now is None else all(
                              now[k] == s[k] for k in s if k not in wallet_keys))
            w.writerow(record)
            totals["rows"] += 1
            totals["list_atto"] += amount
            totals["computed_atto"] += computed
            totals["wone_atto"] += s["wone_atto"]
            totals["wone_rows"] += s["wone_atto"] > 0
            for key in ("amount_ok", "threshold_ok", "activity_ok"):
                totals[key] += record[key]
            totals["head_wallet_unchanged"] += record["head_wallet_unchanged"] is True
    for key in ("list_atto", "computed_atto", "wone_atto"):
        atto = totals.pop(key)
        totals[key.replace("_atto", "_one")] = f"{atto // ATTO:,}.{atto % ATTO:018d}"
    print(json.dumps(totals, indent=1))
    ok = totals["amount_ok"] == totals["threshold_ok"] == totals["activity_ok"] == totals["rows"]
    print("PASS" if ok else "FAIL: see the report for rows with a false check")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
