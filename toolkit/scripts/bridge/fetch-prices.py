#!/usr/bin/env python3

"""Snapshot USD prices for every bridge pool asset from CoinMarketCap.

USD stablecoins named in `pricing.stablecoins` are valued at 1.00. Native
coins use the chain's coin id. Every other asset is matched to the
CoinMarketCap id whose listed platform contracts include the pool's escrowed
token on that chain, so a symbol alone never decides the match; an asset
with no such listing needs an explicit `pricing.cmc_ids` entry, verified the
same way when possible. Bridged ONE is priced as Harmony ONE.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402

API = "https://api.coinmarketcap.com/data-api/v3"
USD = 2781
NATIVE_COINS = {1: 1027, 56: 1839, 42161: 1027, 8453: 1027, 59144: 1027}
HARMONY_ONE = 3945
DEFAULT_STABLECOINS = ("USDC", "USDT", "DAI", "BUSD")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def get(path, params):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(6):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read())
            if str(body.get("status", {}).get("error_code")) in ("0", "None"):
                return body.get("data")
            raise ValueError(body.get("status"))
        except (OSError, ValueError) as error:
            last = error
            time.sleep(2 * (attempt + 1))
    raise lib.RpcError(f"CoinMarketCap {path} failed: {last}")


def coin_map():
    coins, start = [], 1
    while True:
        page = get("map/all", {
            "listing_status": "active,untracked",
            "start": start,
            "limit": 5000,
        })["cryptoCurrencyMap"]
        coins += page
        if len(page) < 5000:
            return coins
        start += 5000


def platforms(cmc_id, cache):
    if cmc_id not in cache:
        detail = get("cryptocurrency/detail", {"id": cmc_id}) or {}
        cache[cmc_id] = {
            "name": detail.get("name"),
            "symbol": detail.get("symbol"),
            "slug": detail.get("slug"),
            "platforms": {
                (int(item["contractChainId"]), item["contractAddress"].lower())
                for item in detail.get("platforms") or []
                if item.get("contractChainId") and item.get("contractAddress")
            },
        }
        time.sleep(0.3)
    return cache[cmc_id]


def main():
    args = parse_args()
    lib.prepare_outputs([args.output], args.replace)
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    pricing = config.get("pricing") or {}
    stablecoins = {symbol.upper() for symbol in pricing.get("stablecoins", DEFAULT_STABLECOINS)}
    overrides, override_notes = {}, {}
    for key, value in (pricing.get("cmc_ids") or {}).items():
        if isinstance(value, dict):
            overrides[key] = int(value["id"])
            override_notes[key] = value.get("note", "")
        else:
            overrides[key] = int(value)

    coins = coin_map()
    by_symbol = {}
    for coin in coins:
        by_symbol.setdefault(coin["symbol"].upper(), []).append(coin)
    details = {}
    assets, unresolved = {}, []
    one_contract = next(
        ((routes["chains"][pool["chain"]]["chain_id"], pool["remote_token"])
         for pool in routes["pools"]
         if pool["claim_asset"] == "one" and not lib.is_native(pool["remote_token"])),
        None,
    )
    for pool in routes["pools"]:
        if pool["claim_asset"] == "unsupported":
            continue
        pid, symbol = pool["pool_id"], pool["symbol"]
        chain_id = int(routes["chains"][pool["chain"]]["chain_id"])
        token = pool["remote_token"]
        entry = {"symbol": symbol, "chain": pool["chain"], "token": token}
        if pool["claim_asset"] == "one":
            info = platforms(HARMONY_ONE, details)
            matched = one_contract in info["platforms"]
            entry.update(basis="cmc", cmc_id=HARMONY_ONE, match=(
                f"Harmony ONE; lists {one_contract[1]} on chain {one_contract[0]}"
                if matched else "Harmony ONE (claims are paid in ERC-20 ONE)"
            ))
        elif symbol.upper() in stablecoins:
            entry.update(basis="stablecoin_par", price_usd="1", match="USD stablecoin at par")
        elif lib.is_native(token):
            entry.update(basis="cmc", cmc_id=NATIVE_COINS[chain_id], match="native coin")
        else:
            candidates = [overrides[pid]] if pid in overrides else [
                coin["id"] for coin in sorted(
                    by_symbol.get(symbol.upper(), []),
                    key=lambda coin: coin.get("rank") or 10 ** 9,
                )[:15]
            ]
            chosen = None
            for cmc_id in candidates:
                if (chain_id, token) in platforms(cmc_id, details)["platforms"]:
                    chosen = (cmc_id, f"lists {token} on chain {chain_id}")
                    break
            if chosen is None and pid in overrides:
                note = override_notes.get(pid) or "contract not listed"
                chosen = (overrides[pid], f"configured CoinMarketCap id: {note}")
            if chosen is None:
                unresolved.append({"pool_id": pid, "symbol": symbol, "chain_id": chain_id,
                                   "token": token, "candidates": candidates})
                entry.update(basis="unresolved")
            else:
                entry.update(basis="cmc", cmc_id=chosen[0], match=chosen[1])
        assets[pid] = entry

    ids = sorted({entry["cmc_id"] for entry in assets.values() if entry.get("cmc_id")})
    quotes = {}
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        for item in get("cryptocurrency/quote/latest", {
            "id": ",".join(map(str, batch)), "convertId": USD,
        }) or []:
            quote = next((q for q in item.get("quotes") or [] if str(q.get("name")) == str(USD)), None)
            quotes[item["id"]] = {
                "name": item.get("name"),
                "symbol": item.get("symbol"),
                "slug": item.get("slug"),
                "price_usd": None if quote is None or quote.get("price") is None
                else format(Decimal(str(quote["price"])), "f"),
                "price_updated": (quote or {}).get("lastUpdatedTime") or item.get("lastUpdatedTime"),
            }
    for pid, entry in assets.items():
        if entry.get("cmc_id"):
            quote = quotes.get(entry["cmc_id"]) or {}
            info = platforms(entry["cmc_id"], details)
            entry.update(
                cmc_name=quote.get("name") or info["name"],
                cmc_symbol=quote.get("symbol") or info["symbol"],
                cmc_slug=quote.get("slug") or info["slug"],
                price_usd=quote.get("price_usd"),
                price_updated=quote.get("price_updated"),
            )
            if entry["price_usd"] is None:
                entry["basis"] = "unpriced"
                unresolved.append({"pool_id": pid, "symbol": entry["symbol"], "reason": "no USD quote"})
    for item in unresolved:
        if item.get("candidates") is not None:
            assets[item["pool_id"]]["basis"] = "unpriced"
            assets[item["pool_id"]]["match"] = "no CoinMarketCap listing with this contract"
    result = {
        "schema_version": 1,
        "source": f"CoinMarketCap {API}/cryptocurrency/quote/latest (USD)",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stablecoins_at_par": sorted(stablecoins),
        "assets": assets,
        "unresolved": unresolved,
    }
    lib.write_json(args.output, result)
    print(f"wrote {args.output}: {len(assets)} assets, {len(unresolved)} unresolved")
    for item in unresolved:
        print("  unresolved:", json.dumps(item))


if __name__ == "__main__":
    main()
