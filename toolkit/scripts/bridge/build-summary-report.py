#!/usr/bin/env python3

"""Summarize a built bridge allocation directory per chain and per asset.

Reads only verified outputs (pools, allocations, claim lists, custody plan,
snapshot plan, message summary) and writes a Markdown overview: per-chain
rollups, per-asset collateral and allocation shares, bridged-ONE detail,
same-symbol totals across chains, holder classes, the largest held
positions, claim-size distributions, and review items. Amounts are token
units; USD sections are added when a `fetch-prices.py` snapshot is given.
"""

import argparse
import csv
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402

ONE_BUCKETS = (1, 100, 1_000, 10_000, 100_000)
LOW_ADDRESS_LIMIT = 0xFFFF


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--messages-summary", required=True)
    parser.add_argument("--allocations-dir", required=True)
    parser.add_argument("--prices", help="prices.json from fetch-prices.py; adds USD values")
    parser.add_argument("--output", required=True)
    parser.add_argument("--top-held", type=int, default=15)
    parser.add_argument("--top-accounts", type=int, default=20)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def units(raw, decimals):
    return Decimal(int(raw)).scaleb(-int(decimals))


def amount(raw, decimals):
    value = units(raw, decimals)
    if value == 0:
        return "0"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.4g}"


def share(part, whole):
    if not int(whole):
        return "–"
    return f"{Decimal(int(part)) * 100 / Decimal(int(whole)):.2f}%"


def quantile(values, fraction):
    if not values:
        return 0
    index = min(len(values) - 1, int(fraction * (len(values) - 1) + Decimal("0.5")))
    return values[index]


def table(headers, rows, align=None):
    align = align or ["---"] * len(headers)
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(align) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def read_pool_rows(allocations_dir):
    with open(os.path.join(allocations_dir, "pools.csv"), newline="") as source:
        return {row["pool_id"]: row for row in csv.DictReader(source)}


def scan_allocations(allocations_dir, top_held):
    stats = {}
    held_positions = []
    low_payees = []
    held_accounts = {}
    held_by_account = {}
    with open(os.path.join(allocations_dir, "allocations.csv"), newline="") as source:
        for row in csv.DictReader(source):
            pool = stats.setdefault(row["pool_id"], {
                "status_rows": {}, "status_amount": {}, "class_rows": {},
                "class_amount": {}, "claimable": [],
            })
            status, account_class = row["status"], row["class"] or "unclassified"
            value = int(row["allocation_raw"]) if not status.startswith("excluded_") else int(row["gross_raw"])
            pool["status_rows"][status] = pool["status_rows"].get(status, 0) + 1
            pool["status_amount"][status] = pool["status_amount"].get(status, 0) + value
            pool["class_rows"][account_class] = pool["class_rows"].get(account_class, 0) + 1
            pool["class_amount"][account_class] = pool["class_amount"].get(account_class, 0) + int(row["gross_raw"])
            if status == "claimable" and int(row["allocation_raw"]):
                pool["claimable"].append(int(row["allocation_raw"]))
                if int(row["payee"], 16) <= LOW_ADDRESS_LIMIT:
                    low_payees.append((row["pool_id"], row["payee"], int(row["allocation_raw"])))
            if status.startswith("held_"):
                held_accounts.setdefault(row["claim_chain"], set()).add(row["account"])
                held_by_account.setdefault(row["account"], []).append(
                    (row["pool_id"], account_class, int(row["allocation_raw"]))
                )
                if int(row["allocation_raw"]):
                    held_positions.append((int(row["allocation_raw"]), row["pool_id"], row["account"], account_class))
    for pool in stats.values():
        pool["claimable"].sort()
    held_positions.sort(reverse=True)
    return stats, held_positions, low_payees, held_accounts, held_by_account


def usd(value):
    if value is None:
        return "unpriced"
    if value == 0:
        return "$0"
    if abs(value) < Decimal("0.01"):
        return "<$0.01"
    return f"${value:,.2f}"


def load_prices(path, pools):
    """Return {pool_id: Decimal price or None} and the price snapshot."""
    if not path:
        return None, None
    snapshot = lib.load_json(path)
    prices = {}
    for pid in pools:
        entry = snapshot["assets"].get(pid) or {}
        value = entry.get("price_usd")
        prices[pid] = Decimal(value) if value not in (None, "") else None
    return prices, snapshot


def pool_usd(row, price, field):
    if price is None:
        return None
    return units(row[field], row["decimals"]) * price


def scan_usd(allocations_dir, pools, prices):
    """Aggregate claimable and held USD per account, per claim chain and overall."""
    per_chain, overall = {}, {}
    with open(os.path.join(allocations_dir, "allocations.csv"), newline="") as source:
        for row in csv.DictReader(source):
            status = row["status"]
            if status != "claimable" and not status.startswith("held_"):
                continue
            price = prices.get(row["pool_id"])
            raw = int(row["allocation_raw"])
            if price is None or not raw:
                continue
            value = units(raw, pools[row["pool_id"]]["decimals"]) * price
            kind = "claimable" if status == "claimable" else "held"
            for key, book in (((row["claim_chain"], row["account"]), per_chain),
                              (row["account"], overall)):
                item = book.setdefault(key, {"claimable": Decimal(0), "held": Decimal(0),
                                             "classes": set(), "pools": {}})
                item[kind] += value
                item["classes"].add(row["class"] or "unclassified")
                item["pools"][row["pool_id"]] = item["pools"].get(row["pool_id"], Decimal(0)) + value
    return per_chain, overall


def top_accounts_rows(items, limit):
    ranked = sorted(
        items, key=lambda pair: (-(pair[1]["claimable"] + pair[1]["held"]), pair[0])
    )[:limit]
    rows = []
    for rank, (account, item) in enumerate(ranked, 1):
        biggest = sorted(item["pools"].items(), key=lambda pool: -pool[1])[:3]
        rows.append([
            rank,
            f"`{account}`",
            ", ".join(sorted(item["classes"])),
            usd(item["claimable"]),
            usd(item["held"]),
            usd(item["claimable"] + item["held"]),
            len(item["pools"]),
            ", ".join(f"{pid} {usd(value)}" for pid, value in biggest),
        ])
    return rows


TOP_ACCOUNT_HEADERS = ["#", "Account", "Class", "Claimable", "Held", "Total", "Pools",
                       "Largest pools"]
TOP_ACCOUNT_ALIGN = ["---:", "---", "---", "---:", "---:", "---:", "---:", "---"]


def usd_sections(pools, registry, prices, snapshot, usd_chain, usd_overall, chains, top):
    priced = [pid for pid in pools if prices.get(pid) is not None]
    unpriced = [pid for pid in pools if prices.get(pid) is None]
    lines = [
        "",
        "## USD values",
        "",
        f"Prices are a CoinMarketCap snapshot taken {snapshot['fetched_at']} (see Prices).",
        f"USD stablecoins ({', '.join(snapshot['stablecoins_at_par'])}) are valued at $1.00.",
        "Pools without a CoinMarketCap price are left out of every USD total: "
        + (", ".join(f"`{pid}`" for pid in unpriced) or "none") + ".",
        "",
        "### Per chain",
        "",
    ]
    rows = []
    totals = {key: Decimal(0) for key in ("collateral", "claimable", "held", "retained", "shortfall")}
    for chain in chains:
        chain_pools = [pid for pid in priced if pools[pid]["claim_chain"] == chain]
        values = {
            key: sum((pool_usd(pools[pid], prices[pid], f"{key}_raw") for pid in chain_pools), Decimal(0))
            for key in totals
        }
        for key in totals:
            totals[key] += values[key]
        one = sum(
            (pool_usd(pools[pid], prices[pid], "collateral_raw") for pid in chain_pools
             if pools[pid]["claim_asset"] == "one"),
            Decimal(0),
        )
        rows.append([
            chain, usd(values["collateral"]), usd(one) if one else "–", usd(values["claimable"]),
            usd(values["held"]), usd(values["retained"]), usd(values["shortfall"]),
            ", ".join(registry[pid]["symbol"] for pid in pools
                      if pools[pid]["claim_chain"] == chain and pid in unpriced) or "none",
        ])
    rows.append([
        "**all**", usd(totals["collateral"]), "", usd(totals["claimable"]), usd(totals["held"]),
        usd(totals["retained"]), usd(totals["shortfall"]), "",
    ])
    lines += table(
        ["Claim chain", "Collateral", "of which bridged ONE", "Claimable", "Held", "Retained",
         "Shortfall", "Unpriced pools"],
        rows,
        ["---", "---:", "---:", "---:", "---:", "---:", "---:", "---"],
    )
    lines += definitions([
        ("Claim chain", "where the claims are paid; bridged ONE is paid on Ethereum"),
        ("Collateral", "USD value of the collateral of the priced pools paid on this chain"),
        ("of which bridged ONE", "the part backed by ONE locked on Harmony and funded in "
         "ERC-20 ONE from the reserve"),
        ("Claimable / Held / Retained", "USD value of each part of that collateral (see Terms)"),
        ("Shortfall", "USD value of the shortfall of pools paid pro rata"),
        ("Unpriced pools", "pools on this chain left out of these totals"),
    ])
    lines += ["", "### Per pool", "", "Sorted by collateral value.", ""]
    rows = []
    for pid in sorted(pools, key=lambda pid: -(pool_usd(pools[pid], prices.get(pid), "collateral_raw") or Decimal(-1))):
        price = prices.get(pid)
        row = pools[pid]
        rows.append([
            pid, registry[pid]["symbol"],
            "unpriced" if price is None else f"${price:,.6g}",
            usd(pool_usd(row, price, "collateral_raw")),
            usd(pool_usd(row, price, "claimable_raw")),
            usd(pool_usd(row, price, "held_raw")),
            usd(pool_usd(row, price, "retained_raw")),
            usd(pool_usd(row, price, "shortfall_raw")),
        ])
    lines += table(
        ["Pool", "Asset", "Price", "Collateral", "Claimable", "Held", "Retained", "Shortfall"],
        rows,
        ["---", "---", "---:", "---:", "---:", "---:", "---:", "---:"],
    )
    lines += definitions([
        ("Price", "USD per token from the price snapshot"),
        ("Collateral / Claimable / Held / Retained / Shortfall",
         "the per-pool amounts from the units tables, times the price"),
    ])
    lines += [
        "",
        f"### Top accounts by USD (all chains, top {top})",
        "",
        "Each account's claimable and held allocations across every pool and chain,",
        "valued at the snapshot prices. Excluded system addresses are not listed.",
        "",
    ]
    lines += table(TOP_ACCOUNT_HEADERS, top_accounts_rows(usd_overall.items(), top), TOP_ACCOUNT_ALIGN)
    lines += definitions([
        ("Account", "the holder at the snapshot block (or the message-adjustment address)"),
        ("Class", "account class or classes across its pools (see Holder classes)"),
        ("Claimable", "USD value paid through the claim lists"),
        ("Held", "USD value reserved for the account pending review"),
        ("Total", "claimable plus held"),
        ("Pools", "pools in which the account has a priced allocation"),
        ("Largest pools", "its three largest pools by USD value"),
    ])
    for chain in chains:
        items = [(account, item) for (claim_chain, account), item in usd_chain.items() if claim_chain == chain]
        if not items:
            continue
        lines += ["", f"### Top accounts by USD on {chain} (top {min(top, 10)})", ""]
        lines += table(TOP_ACCOUNT_HEADERS, top_accounts_rows(items, min(top, 10)), TOP_ACCOUNT_ALIGN)
    lines += ["", "Columns are the same as in the all-chains table, limited to claims paid on that chain."]
    return lines, totals


def price_section(pools, registry, snapshot):
    lines = [
        "",
        "## Prices",
        "",
        f"Snapshot: {snapshot['fetched_at']} from {snapshot['source']}.",
        "",
    ]
    rows = []
    for pid in sorted(pools):
        entry = snapshot["assets"].get(pid) or {}
        rows.append([
            pid, registry[pid]["symbol"], f"`{entry.get('token', '')}`",
            {"stablecoin_par": "par", "cmc": "CoinMarketCap", "unpriced": "unpriced"}.get(entry.get("basis"), entry.get("basis", "")),
            entry.get("cmc_id", ""),
            f"{entry.get('cmc_name', '')} ({entry.get('cmc_symbol', '')})" if entry.get("cmc_id") else "",
            "unpriced" if entry.get("price_usd") in (None, "") else f"${Decimal(entry['price_usd']):,.8g}",
            entry.get("price_updated", "") or "",
            entry.get("match", ""),
        ])
    lines += table(
        ["Pool", "Asset", "Token", "Basis", "CMC id", "CMC asset", "Price (USD)", "Quote time", "Match"],
        rows,
        ["---", "---", "---", "---", "---:", "---", "---:", "---", "---"],
    )
    lines += definitions([
        ("Token", "the escrowed token on the pool's chain (`native` for ETH and BNB); for ONE "
         "pools the bridged token, priced as Harmony ONE because claims are paid in ERC-20 ONE"),
        ("Basis", "`par`: USD stablecoin at $1.00; `CoinMarketCap`: latest USD quote; "
         "`unpriced`: CoinMarketCap has no quote for this asset"),
        ("CMC id / CMC asset", "the CoinMarketCap listing used"),
        ("Price (USD)", "USD per token"),
        ("Quote time", "CoinMarketCap's last update time for the quote"),
        ("Match", "why that listing is this token: it lists this contract on this chain, it is "
         "the chain's native coin, or a configured id with its reason"),
    ])
    return lines


def held_section(held_by_account, held_accounts, pools):
    """Explain held positions versus held accounts with this run's numbers."""
    positions = sum(len(items) for items in held_by_account.values())
    per_chain = sum(len(accounts) for accounts in held_accounts.values())
    ranked = sorted(held_by_account.items(), key=lambda item: (-len(item[1]), item[0]))
    spread = {"1 pool": 0, "2–4 pools": 0, "5 or more pools": 0}
    for items in held_by_account.values():
        key = "1 pool" if len(items) == 1 else "2–4 pools" if len(items) < 5 else "5 or more pools"
        spread[key] += 1
    lines = [
        "",
        "## Held positions and held accounts",
        "",
        "Allocations are computed per pool (one asset on one chain), so the hold",
        "is per pool too.",
        "",
        "- A **held position** is one pool's allocation to one address that is",
        "  held instead of paid: one row of `allocations.csv` with a `held_*` status.",
        "- A **held account** is the address itself, counted once no matter how",
        "  many pools hold it.",
        "",
        "An address that holds several bridged tokens is one held account with one",
        "held position per token. For example, a Harmony DEX pair holding 1USDC and",
        "1USDT is one account and two positions, one in `ethereum-usdc` and one in",
        "`ethereum-usdt`; each position has its own amount and is resolved by the",
        "same routing decision for that address. Within a single pool the two",
        "numbers are the same, which is why the per-asset and bridged-ONE tables",
        "show only held accounts.",
        "",
        f"In this run: {positions:,} held positions across {len(held_by_account):,} "
        f"held accounts. The per-chain table counts an account once per claim "
        f"chain, so an address held on two claim chains (for example a Harmony "
        f"contract holding wrapped tokens escrowed on both Ethereum and BNB Chain) "
        f"appears on both rows; those rows add up to {per_chain:,}.",
        "",
    ]
    lines += table(
        ["Held in", "Accounts"],
        [[label, f"{count:,}"] for label, count in spread.items()],
        ["---", "---:"],
    )
    def weight(items):
        return sum(
            Decimal(value) / Decimal(int(pools[pid]["collateral_raw"]))
            for pid, _, value in items if int(pools[pid]["collateral_raw"])
        )

    multi = [item for item in held_by_account.items() if len(item[1]) > 1]
    if multi:
        account, items = max(multi, key=lambda item: (weight(item[1]), item[0]))
        lines += [
            "",
            f"Example: `{account}` ({items[0][1]}) is one held account with "
            f"{len(items)} held positions, the multi-pool account whose holds are "
            "the largest share of their pools:",
            "",
        ]
        lines += table(
            ["Pool", "Held", "Share of collateral"],
            [
                [pid, amount(value, pools[pid]["decimals"]),
                 share(value, pools[pid]["collateral_raw"])]
                for pid, _, value in sorted(items, key=lambda item: item[0])
            ],
            ["---", "---:", "---:"],
        )
    lines += ["", "Accounts with the most held positions:", ""]
    lines += table(
        ["Account", "Class", "Held positions", "Pools"],
        [
            [f"`{account}`", items[0][1], len(items), ", ".join(sorted(pid for pid, _, _ in items))]
            for account, items in ranked[:10]
        ],
        ["---", "---", "---:", "---"],
    )
    return lines


def definitions(items):
    lines = ["", "Columns:", ""]
    lines += [f"- **{name}** — {text}" for name, text in items]
    return lines


def scan_claims(allocations_dir, chains):
    claims = {}
    for chain in chains:
        tokens, accounts = {}, set()
        with open(os.path.join(allocations_dir, "claims", f"{chain}.csv"), newline="") as source:
            for row in csv.DictReader(source):
                accounts.add(row["account"])
                item = tokens.setdefault(row["token"], {"rows": 0, "amounts": []})
                item["rows"] += 1
                item["amounts"].append(int(row["amount_raw"]))
        claims[chain] = {"tokens": tokens, "accounts": len(accounts)}
    return claims


def main():
    args = parse_args()
    lib.prepare_outputs([args.output], args.replace)
    routes = lib.load_routes(args.routes)
    registry = {pool["pool_id"]: pool for pool in routes["pools"]}
    plan = lib.load_json(args.plan)
    messages = lib.load_json(args.messages_summary)
    out = args.allocations_dir
    summary = lib.load_json(os.path.join(out, "summary.json"))
    custody = lib.load_json(os.path.join(out, "custody-plan.json"))
    pools = read_pool_rows(out)
    stats, held_positions, low_payees, held_accounts, held_by_account = scan_allocations(
        out, args.top_held
    )
    claims = scan_claims(out, summary["claim_lists"])
    prices, price_snapshot = load_prices(args.prices, pools)
    if prices is not None:
        usd_chain, usd_overall = scan_usd(out, pools, prices)
        usd_lines, usd_totals = usd_sections(
            pools, registry, prices, price_snapshot, usd_chain, usd_overall,
            summary["claim_lists"], args.top_accounts,
        )
    one_pools = [pid for pid in pools if pools[pid]["claim_asset"] == "one"]
    escrow_pools = [pid for pid in pools if pools[pid]["claim_asset"] == "escrow"]
    one_token = pools[one_pools[0]]["claim_token"] if one_pools else None
    funding = custody["one_funding"]

    lines = [
        "# Bridge allocation summary",
        "",
        "Per-chain and per-asset overview of the verified bridge allocation run in",
        "this directory. Amounts are token units"
        + (
            "; USD values use the price\nsnapshot listed under Prices."
            if prices is not None
            else " (no USD: no price snapshot\nwas supplied)."
        ),
        "",
        "## Terms",
        "",
        "- **Pool** — one bridged asset on one chain: the collateral in one bridge",
        "  escrow (or, for ONE, one Harmony lock) and the holders it backs. It is",
        "  finer than an asset: USDC escrowed on Ethereum, BNB Chain, Arbitrum,",
        "  Base, and Linea is five pools with separate collateral, holders, and",
        "  scale; bridged ONE is two pools (BNB Chain and Ethereum routes) paid in",
        "  the same Ethereum ONE. Pool ids are `<chain>-<symbol>`.",
        "- **Collateral** — what backs the pool: the remote escrow balance, or for",
        "  ONE the native ONE plus WONE locked on Harmony at the cutoff.",
        "- **Claimable** — allocated to an address that can be paid directly; it is",
        "  in the claim list.",
        "- **Held** — allocated and reserved in custody, but not in the claim list,",
        "  because paying the address as-is may be unsafe or unspendable: contracts",
        "  (DEX pairs, vaults, other bridges), multisigs whose address does not",
        "  exist on the claim chain, 1wallets, and unclassified accounts. Held",
        "  accounts are not excluded: they keep their share and need a routing",
        "  decision (pay the underlying owners, redirect, or release).",
        "- **Excluded** — system addresses that are not owners: bridge contracts,",
        "  tokens, endpoints, Horizon managers, burn and zero addresses, the",
        "  deployer, and the custody and reserve Safes. They get nothing and do not",
        "  count against the collateral.",
        "- **Eligible** — claimable plus held balances before any scaling.",
        "- **Retained** — collateral minus claimable minus held: surplus, the",
        "  backing of excluded balances, and rounding dust. It stays in custody.",
        "- **Shortfall** — eligible minus collateral when eligible is larger; the",
        "  pool then pays every claimable and held allocation pro rata.",
        "- **Payee** — an address paid on a claim chain.",
        "- **Claim row** — one (payee, token) entry in `claims/<chain>.csv`; the",
        "  claim contracts will have one leaf per row. A payee owed three assets",
        "  on a chain has three claim rows.",
        "- **Position** — one (pool, account) pair. The same address in two pools",
        "  is two positions.",
        "",
        "## At a glance",
        "",
        f"- Pools: {len(pools)} paid ({len(escrow_pools)} remote-escrow assets, "
        f"{len(one_pools)} bridged-ONE routes), {len(summary['unsupported_pools'])} unsupported "
        f"({', '.join(item['pool_id'] for item in summary['unsupported_pools']) or 'none'}).",
        f"- Claim lists: {', '.join(summary['claim_lists'])}; "
        f"{sum(c['accounts'] for c in claims.values()):,} payees (an address paid on two "
        f"chains counts twice), "
        f"{sum(t['rows'] for c in claims.values() for t in c['tokens'].values()):,} claim rows.",
        f"- Held: {summary['held_accounts']:,} positions across "
        f"{len(held_by_account):,} distinct accounts (see Held positions and held accounts).",
        f"- ONE funding: {amount(funding['amount_raw'], 18)} ERC-20 ONE from the reserve Safe "
        f"to `{funding['to']}` on {funding['chain']} (reserve sufficient: {funding['reserve_sufficient']}).",
        "- Custody: " + "; ".join(
            f"{chain} {item['status']}" for chain, item in custody["chains"].items()
        ) + ".",
    ]
    if prices is not None:
        one_price = prices.get(one_pools[0]) if one_pools else None
        lines += [
            f"- USD (CoinMarketCap snapshot {price_snapshot['fetched_at']}, priced pools only): "
            f"collateral {usd(usd_totals['collateral'])}, claimable {usd(usd_totals['claimable'])}, "
            f"held {usd(usd_totals['held'])}, retained {usd(usd_totals['retained'])}.",
            f"- ONE funding at that price: "
            f"{usd(units(funding['amount_raw'], 18) * one_price) if one_price else 'unpriced'}.",
        ]
    lines += [
        "",
        "## Per chain",
        "",
        "Rows are grouped by the chain where claims are paid. Bridged ONE from BNB",
        "Chain and Ethereum is paid on Ethereum and is counted there.",
        "",
    ]
    chain_rows = []
    for chain in summary["claim_lists"]:
        chain_pools = [pid for pid in pools if pools[pid]["claim_chain"] == chain]
        claim = claims[chain]
        held = sum(int(pools[pid]["held_accounts"]) for pid in chain_pools)
        shortfall = [registry[pid]["symbol"] for pid in chain_pools if int(pools[pid]["shortfall_raw"])]
        item = custody["chains"].get(chain, {})
        chain_rows.append([
            chain,
            len(chain_pools),
            item.get("status", "–"),
            f"{len(item.get('transfers', []))}",
            f"{claim['accounts']:,}",
            f"{sum(t['rows'] for t in claim['tokens'].values()):,}",
            f"{held:,}",
            f"{len(held_accounts.get(chain, ())):,}",
            ", ".join(shortfall) or "none",
            ", ".join(sorted({registry[pid]["symbol"] for pid in chain_pools})),
        ])
    lines += table(
        ["Claim chain", "Pools", "Custody", "Escrows to move", "Payees", "Claim rows",
         "Held positions", "Held accounts", "Shortfall pools", "Assets"],
        chain_rows,
        ["---", "---:", "---", "---:", "---:", "---:", "---:", "---:", "---", "---"],
    )
    lines += definitions([
        ("Claim chain", "where the claims are paid"),
        ("Pools", "bridged assets paid on this chain"),
        ("Custody", "`ready`: a custody Safe is configured and has the required owners "
         "and threshold; `missing`: no custody multisig exists on this chain yet; "
         "`not_qualified`: the configured address is not a qualifying Safe"),
        ("Escrows to move", "escrow contracts on this chain whose balances move to custody "
         "(ONE collateral is on Harmony and is funded from the reserve instead)"),
        ("Payees", "distinct addresses with at least one claim on this chain"),
        ("Claim rows", "rows in `claims/<chain>.csv`, one per payee and token"),
        ("Held positions", "(pool, account) pairs on this chain whose allocation is held "
         "for review; an address held in three pools is three positions"),
        ("Held accounts", "distinct addresses behind those positions; the same address "
         "in three pools is one account"),
        ("Shortfall pools", "assets on this chain whose eligible claims exceed the "
         "collateral, so they are paid pro rata"),
        ("Assets", "symbols of the pools paid on this chain"),
    ])
    if prices is not None:
        lines += usd_lines
    lines += held_section(held_by_account, held_accounts, pools)

    if one_pools:
        lines += [
            "",
            "## Bridged ONE",
            "",
            "Collateral is the native ONE (plus WONE) locked on Harmony at the cutoff;",
            "claims are paid in Ethereum ERC-20 ONE. Holder snapshots use each",
            "chain's rollback block because both chains received rollback-orphaned",
            "deliveries; those mints are excluded.",
            "",
        ]
        one_rows = []
        for pid in one_pools:
            row = pools[pid]
            exposure = plan["exposure"][registry[pid]["chain"]]
            orphaned = messages["pools"].get(pid, {}).get("categories", {}).get("orphaned_mint", {})
            one_rows.append([
                registry[pid]["chain"],
                f"{row['snapshot_label']} {int(row['snapshot_block']):,}",
                f"{exposure['orphaned_one_deliveries']} ({amount(orphaned.get('amount_raw', 0), 18)})",
                amount(row["collateral_raw"], 18),
                amount(row["eligible_raw"], 18),
                f"{amount(row['claimable_raw'], 18)} ({share(row['claimable_raw'], row['collateral_raw'])})",
                f"{amount(row['held_raw'], 18)} ({share(row['held_raw'], row['collateral_raw'])})",
                f"{amount(row['retained_raw'], 18)} ({share(row['retained_raw'], row['collateral_raw'])})",
                f"{int(row['holders']):,}",
                f"{int(row['claimable_accounts']):,}",
                f"{int(row['held_accounts']):,}",
            ])
        lines += table(
            ["Source chain", "Snapshot", "Orphaned deliveries excluded", "Collateral", "Eligible",
             "Claimable", "Held", "Retained", "Holders", "Claimable accounts", "Held accounts"],
            one_rows,
            ["---", "---", "---:", "---:", "---:", "---:", "---:", "---:", "---:", "---:", "---:"],
        )
        lines += definitions([
            ("Source chain", "where the bridged ONE was held"),
            ("Snapshot", "block whose bridged-ONE balances are used"),
            ("Orphaned deliveries excluded", "remote mints whose Harmony lock the rollback "
             "undid (count and ONE); they are after the snapshot block, so they are not counted"),
            ("Collateral", "native ONE plus WONE locked for this route on Harmony at the cutoff"),
            ("Eligible", "claimable plus held bridged-ONE balances before scaling"),
            ("Claimable / Held / Retained", "ONE and share of collateral (see Terms)"),
            ("Holders", "addresses with a positive balance at the snapshot block, "
             "including excluded system addresses"),
            ("Claimable accounts", "holders paid directly (some allocations may round to zero)"),
            ("Held accounts", "holders whose allocation in this pool is held for review; "
             "within one pool this equals the pool's held positions"),
        ])
        one_claims = claims[pools[one_pools[0]]["claim_chain"]]["tokens"].get(one_token, {"rows": 0, "amounts": []})
        amounts = one_claims["amounts"]
        buckets = []
        lower = 0
        for upper in (*ONE_BUCKETS, None):
            selected = [
                value for value in amounts
                if units(value, 18) >= lower and (upper is None or units(value, 18) < upper)
            ]
            label = f"≥ {lower:,}" if upper is None else f"{lower:,} – {upper:,}"
            buckets.append([label, f"{len(selected):,}", amount(sum(selected), 18)])
            lower = upper
        lines += [
            "",
            f"Combined Ethereum ERC-20 ONE claims: {one_claims['rows']:,} rows, "
            f"{amount(sum(amounts), 18)} ONE.",
            "",
        ]
        lines += table(["Claim size (ONE)", "Claims", "ONE"], buckets, ["---", "---:", "---:"])
        lines += definitions([
            ("Claim size (ONE)", "range of one claim row's amount, lower bound inclusive"),
            ("Claims", "Ethereum ERC-20 ONE claim rows in that range (BNB Chain and Ethereum "
             "allocations of the same address are one row)"),
            ("ONE", "total of those rows"),
        ])

    lines += [
        "",
        "## Per asset and chain (pools)",
        "",
        "One row per pool, so an asset escrowed on several chains has several rows;",
        "their combined units are under Same symbol on several chains. Shares are",
        "of the pool's collateral. Scale is 1:1 unless eligible claims exceed",
        "collateral, in which case every allocation in that pool is cut pro rata.",
        "",
    ]
    asset_rows = []
    for pid in escrow_pools:
        row = pools[pid]
        decimals = int(row["decimals"])
        scale = "1:1" if row["scale_numerator"] == row["scale_denominator"] else (
            f"pro rata {share(row['scale_numerator'], row['scale_denominator'])}"
        )
        asset_rows.append([
            registry[pid]["symbol"],
            registry[pid]["chain"],
            amount(row["collateral_raw"], decimals),
            share(row["claimable_raw"], row["collateral_raw"]),
            share(row["held_raw"], row["collateral_raw"]),
            share(row["retained_raw"], row["collateral_raw"]),
            scale,
            amount(row["shortfall_raw"], decimals),
            f"{int(row['holders']):,}",
            f"{int(row['claimable_accounts']):,}",
            f"{int(row['held_accounts']):,}",
        ])
    lines += table(
        ["Asset", "Chain", "Collateral", "Claimable", "Held", "Retained", "Scale",
         "Shortfall", "Holders", "Claimable accounts", "Held accounts"],
        asset_rows,
        ["---", "---", "---:", "---:", "---:", "---:", "---", "---:", "---:", "---:", "---:"],
    )
    lines += definitions([
        ("Asset / Chain", "the escrowed asset and the chain where it is escrowed and paid"),
        ("Collateral", "escrow balance at the pinned state block, in the asset's units"),
        ("Claimable", "share of collateral allocated to payees in the claim list"),
        ("Held", "share of collateral reserved for held accounts"),
        ("Retained", "share of collateral left in custody (surplus, excluded balances, dust)"),
        ("Scale", "`1:1` when collateral covers every eligible balance; otherwise the "
         "pro-rata factor applied to every claimable and held allocation"),
        ("Shortfall", "eligible minus collateral, in the asset's units"),
        ("Holders", "Harmony addresses with a positive balance of the wrapped token(s) "
         "at the cutoff, including excluded system addresses"),
        ("Claimable accounts", "holders paid directly (some allocations may round to zero)"),
        ("Held accounts", "holders whose allocation in this pool is held for review; within "
         "one pool this equals the pool's held positions"),
    ])

    by_symbol = {}
    for pid in escrow_pools:
        symbol = registry[pid]["symbol"]
        by_symbol.setdefault(symbol, []).append(pid)
    multi = {symbol: pids for symbol, pids in by_symbol.items() if len(pids) > 1}
    if multi:
        lines += [
            "",
            "## Same symbol on several chains",
            "",
            "Units are added across chains; the tokens are different contracts.",
            "",
        ]
        rows = []
        for symbol in sorted(multi):
            pids = multi[symbol]
            collateral = sum(units(pools[pid]["collateral_raw"], pools[pid]["decimals"]) for pid in pids)
            claimable = sum(units(pools[pid]["claimable_raw"], pools[pid]["decimals"]) for pid in pids)
            held = sum(units(pools[pid]["held_raw"], pools[pid]["decimals"]) for pid in pids)
            rows.append([
                symbol,
                ", ".join(registry[pid]["chain"] for pid in pids),
                f"{collateral:,.2f}",
                f"{claimable:,.2f}",
                f"{held:,.2f}",
            ])
        lines += table(["Symbol", "Chains", "Collateral", "Claimable", "Held"], rows,
                       ["---", "---", "---:", "---:", "---:"])
        lines += definitions([
            ("Symbol / Chains", "assets with the same symbol escrowed on several chains"),
            ("Collateral / Claimable / Held", "sums of the per-chain amounts, in units"),
        ])

    lines += [
        "",
        "## Holder classes",
        "",
        "Accounts per class across all pools (an account appears once per pool).",
        "",
    ]
    class_totals = {}
    for pid, pool in stats.items():
        chain = pools[pid]["snapshot_chain"]
        for account_class, count in pool["class_rows"].items():
            key = (chain, account_class)
            class_totals[key] = class_totals.get(key, 0) + count
    lines += table(
        ["Holder chain", "Class", "Positions"],
        [[chain, account_class, f"{count:,}"] for (chain, account_class), count in sorted(class_totals.items())],
        ["---", "---", "---:"],
    )
    lines += definitions([
        ("Holder chain", "chain of the holder snapshot (Harmony for escrowed assets, "
         "BNB Chain or Ethereum for bridged ONE)"),
        ("Class", "`eoa`: no code; `eoa_7702`: key-controlled account with an EIP-7702 "
         "delegation; `validator_wrapper`: Harmony validator account; `safe` and "
         "`multisig`: Safe or MultiSigWallet; `onewallet`; `contract`: any other code; "
         "`adjustment_target`: address with only a message adjustment"),
        ("Positions", "(pool, account) pairs of that class"),
    ])
    lines += [
        "",
        f"## Largest held positions (top {args.top_held})",
        "",
    ]
    rows = []
    for value, pid, account, account_class in held_positions[: args.top_held]:
        decimals = int(pools[pid]["decimals"])
        rows.append([
            pid, f"`{account}`", account_class, amount(value, decimals),
            share(value, pools[pid]["collateral_raw"]),
        ])
    lines += table(["Pool", "Account", "Class", "Held", "Share of collateral"], rows,
                   ["---", "---", "---", "---:", "---:"])
    lines += definitions([
        ("Account", "holder whose allocation is held"),
        ("Class", "why it is held (see Holder classes)"),
        ("Held", "allocation reserved for it, in the pool's units"),
        ("Share of collateral", "that allocation as a share of the pool's collateral"),
    ])

    lines += [
        "",
        "## Claim sizes per pool",
        "",
        "Claimable allocations per pool, in token units.",
        "",
    ]
    rows = []
    for pid in pools:
        values = stats.get(pid, {}).get("claimable", [])
        if not values:
            continue
        decimals = int(pools[pid]["decimals"])
        rows.append([
            pid, f"{len(values):,}",
            amount(quantile(values, Decimal("0.5")), decimals),
            amount(quantile(values, Decimal("0.9")), decimals),
            amount(quantile(values, Decimal("0.99")), decimals),
            amount(values[-1], decimals),
        ])
    lines += table(["Pool", "Claims", "Median", "p90", "p99", "Largest"], rows,
                   ["---", "---:", "---:", "---:", "---:", "---:"])
    lines += definitions([
        ("Claims", "claimable allocations above zero in this pool"),
        ("Median / p90 / p99", "allocation size that 50%, 90%, and 99% of those claims "
         "are at or below"),
        ("Largest", "largest single allocation"),
    ])

    review = []
    for pid in pools:
        if int(pools[pid]["shortfall_raw"]):
            review.append(
                f"`{pid}` is short {amount(pools[pid]['shortfall_raw'], pools[pid]['decimals'])} "
                f"{registry[pid]['symbol']} and is paid pro rata."
            )
        if int(pools[pid]["unrecovered_debit_raw"]):
            review.append(
                f"`{pid}` has an unrecovered debit of "
                f"{amount(pools[pid]['unrecovered_debit_raw'], pools[pid]['decimals'])} {registry[pid]['symbol']}."
            )
    for chain, item in custody["chains"].items():
        if item["status"] != "ready":
            review.append(f"{chain} has no qualifying custody multisig ({item['status']}).")
        single = sorted({
            transfer["escrow_owner"] for transfer in item["transfers"]
            if not transfer["escrow_owner_is_safe"]
        })
        if single:
            review.append(
                f"{chain} escrows owned by a single key: "
                + ", ".join(f"`{owner}`" for owner in single) + "."
            )
    if low_payees:
        total = {}
        for pid, payee, value in low_payees:
            total.setdefault(pid, []).append((payee, value))
        review.append(
            f"{len(low_payees)} claimable rows pay a precompile or other reserved low address "
            "(at or below 0x…ffff), which no key controls: "
            + "; ".join(
                f"`{pid}` " + ", ".join(
                    f"`{payee}` {amount(value, pools[pid]['decimals'])}" for payee, value in items
                )
                for pid, items in sorted(total.items())
            )
            + "."
        )
    for item in summary["unsupported_pools"]:
        review.append(f"`{item['pool_id']}` is unsupported: {item['reason']}.")
    if prices is not None:
        for pid in pools:
            if prices.get(pid) is None:
                review.append(
                    f"`{pid}` has no CoinMarketCap price; its "
                    f"{amount(pools[pid]['collateral_raw'], pools[pid]['decimals'])} "
                    f"{registry[pid]['symbol']} are left out of USD totals."
                )
        lines += price_section(pools, registry, price_snapshot)
    lines += ["", "## Review items", ""]
    lines += [f"- {item}" for item in review] or ["- none"]
    lines.append("")
    lib.write_text(args.output, "\n".join(lines))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
