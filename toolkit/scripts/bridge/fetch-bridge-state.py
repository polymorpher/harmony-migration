#!/usr/bin/env python3

"""Read bridge collateral, supply, nonce, owner, and custody state.

Harmony is read at its rollback and cutoff blocks. Every remote chain is read
at its rollback and cutoff blocks and at a pinned state block (the current
head unless --state-block is given), which is the collateral that would move
to custody. All values are raw integers read with eth_call or eth_getBalance.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402

EIP7702_PREFIX = "0xef0100"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--blocks", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--state-block",
        action="append",
        default=[],
        help="CHAIN=NUMBER; default is each remote chain's current head",
    )
    parser.add_argument("--discovery-max-chain-id", type=int, default=1000)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


class Calls:
    """Collect keyed RPC calls, run them in batches, and keep the results."""

    def __init__(self, rpc):
        self.rpc = rpc
        self.keys = []
        self.calls = []
        self.results = {}

    def add(self, key, method, params):
        self.keys.append(key)
        self.calls.append((method, params))

    def eth_call(self, key, to, data, block):
        self.add(key, "eth_call", [{"to": to, "data": data}, lib.block_tag(block)])

    def balance(self, key, holder, block):
        self.add(key, "eth_getBalance", [holder, lib.block_tag(block)])

    def code(self, key, holder, block):
        self.add(key, "eth_getCode", [holder, lib.block_tag(block)])

    def run(self):
        outcomes = self.rpc.batch(self.calls)
        for key, (method, _), (result, error) in zip(self.keys, self.calls, outcomes):
            if error is not None:
                self.results[key] = None
            elif method == "eth_getBalance" and isinstance(result, str):
                self.results[key] = "0x" + format(int(result, 16), "064x")
            else:
                self.results[key] = result
        self.keys, self.calls = [], []

    def uint(self, key):
        return lib.decode_uint(self.results.get(key))

    def text(self, key):
        value = self.uint(key)
        return None if value is None else str(value)

    def address(self, key):
        return lib.decode_address(self.results.get(key))

    def raw(self, key):
        return self.results.get(key)


def asset_balance(calls, key, asset, holder, block):
    if lib.is_native(asset):
        calls.balance(key, holder, block)
    else:
        calls.eth_call(
            key, asset, lib.call_data("balanceOf", lib.address_word(holder)), block
        )


def queue_account(calls, prefix, holder, block):
    calls.code((prefix, "code"), holder, block)
    for name in ("getOwners", "getThreshold", "required", "VERSION"):
        calls.eth_call((prefix, name), holder, lib.SEL[name], block)


def account_facts(calls, prefix):
    code = calls.raw((prefix, "code")) or "0x"
    owners = calls.raw((prefix, "getOwners"))
    facts = {
        "code_size": (len(code) - 2) // 2,
        "eip7702_delegated": code.lower().startswith(EIP7702_PREFIX),
        "owners": lib.crl.decode_address_array(owners) if owners else None,
        "threshold": calls.uint((prefix, "getThreshold")),
        "required": calls.uint((prefix, "required")),
        "version": lib.crl.decode_string(calls.raw((prefix, "VERSION")) or "0x"),
    }
    facts["is_safe"] = bool(
        facts["code_size"]
        and not facts["eip7702_delegated"]
        and facts["owners"]
        and facts["threshold"]
    )
    return facts


def custody_check(facts, requirements):
    if not facts["is_safe"]:
        return False
    if requirements.get("owner_count") is not None and len(facts["owners"]) != int(
        requirements["owner_count"]
    ):
        return False
    if requirements.get("threshold") is not None and facts["threshold"] != int(
        requirements["threshold"]
    ):
        return False
    return True


def trusted_remote_history(rpc, adapter, harmony_lz, state_block, range_size):
    """Every Harmony peer the adapter was ever configured to trust."""
    start = lib.deployment_block(rpc, adapter, state_block)
    logs = rpc.logs(
        adapter,
        [[lib.TOPIC["SetTrustedRemote"], lib.TOPIC["SetTrustedRemoteAddress"]]],
        start, state_block, range_size,
    )
    history = []
    for log in logs:
        data = log["data"]
        chain_id = int(data[2:66], 16)
        if chain_id != harmony_lz:
            continue
        value = lib.decode_dynamic_bytes(data, 1) or "0x"
        raw = value[2:]
        peer = "0x" + raw[:40] if len(raw) >= 40 else ""
        history.append({
            "block": int(log["blockNumber"], 16),
            "transaction": log["transactionHash"],
            "event": "SetTrustedRemote" if log["topics"][0] == lib.TOPIC["SetTrustedRemote"] else "SetTrustedRemoteAddress",
            "value": value,
            "harmony_peer": peer.lower() if peer and int(peer, 16) else "",
        })
    return {"from_block": start, "events": history}


def remote_state(routes, config, chain, blocks, state_block):
    rpc = lib.chain_rpc(config, chain)
    harmony_lz = routes["harmony"]["layerzero_chain_id"]
    pools = [pool for pool in routes["pools"] if pool["chain"] == chain]
    labels = {label: blocks[label]["number"] for label in lib.TIME_LABELS}
    labels["state"] = state_block
    calls = Calls(rpc)
    for pool in pools:
        pid, adapter = pool["pool_id"], pool["remote_adapter"]
        calls.code((pid, "adapter_code"), adapter, state_block)
        for name in ("owner", "lzEndpoint", "token"):
            calls.eth_call((pid, name), adapter, lib.SEL[name], state_block)
        if not lib.is_native(pool["remote_token"]):
            for name in ("decimals", "symbol"):
                calls.eth_call((pid, name), pool["remote_token"], lib.SEL[name], state_block)
    calls.run()

    endpoints = {}
    owners = {}
    for pool in pools:
        pid = pool["pool_id"]
        endpoints[pid] = calls.address((pid, "lzEndpoint"))
        owner = calls.address((pid, "owner"))
        owners[pid] = owner
    for pool in pools:
        for previous in pool["previous_remote_adapters"]:
            calls.eth_call((pool["pool_id"], "previous_owner", previous), previous, lib.SEL["owner"], state_block)
    for owner in sorted({owner for owner in owners.values() if owner}):
        queue_account(calls, ("owner", owner), owner, state_block)
    for pool in pools:
        pid = pool["pool_id"]
        for label, number in labels.items():
            asset_balance(
                calls, (pid, label, "escrow"), pool["remote_token"],
                pool["remote_adapter"], number,
            )
            if not lib.is_native(pool["remote_token"]):
                calls.eth_call(
                    (pid, label, "supply"), pool["remote_token"],
                    lib.SEL["totalSupply"], number,
                )
            endpoint = endpoints[pid]
            if endpoint:
                calls.eth_call(
                    (pid, label, "outbound"), endpoint,
                    lib.outbound_nonce_data(harmony_lz, pool["remote_adapter"]),
                    number,
                )
                for index, entry in enumerate(pool["harmony"]):
                    calls.eth_call(
                        (pid, label, "inbound", index), endpoint,
                        lib.inbound_nonce_data(
                            harmony_lz, entry["proxy"], pool["remote_adapter"]
                        ),
                        number,
                    )
            for previous in pool["previous_remote_adapters"]:
                asset_balance(
                    calls, (pid, label, "previous", previous),
                    pool["remote_token"], previous, number,
                )
                if endpoint:
                    calls.eth_call(
                        (pid, label, "previous_outbound", previous), endpoint,
                        lib.outbound_nonce_data(harmony_lz, previous), number,
                    )
                    for index, entry in enumerate(pool["harmony"]):
                        calls.eth_call(
                            (pid, label, "previous_inbound", previous, index), endpoint,
                            lib.inbound_nonce_data(harmony_lz, entry["proxy"], previous),
                            number,
                        )
    custody = config["chains"][chain].get("custody_multisig")
    custody_targets = {address for address in (custody,) if address}
    one_custody = lib.one_custody(config)
    if chain == config["one_claims"]["claim_chain"]:
        one = config["one_claims"]
        custody_targets.update(
            address for address in (one_custody, one["reserve_safe"]) if address
        )
        calls.code(("claim_token", "code"), one["claim_token"], state_block)
        for name in ("totalSupply", "decimals", "symbol"):
            calls.eth_call(("claim_token", name), one["claim_token"], lib.SEL[name], state_block)
    for target in sorted(custody_targets):
        queue_account(calls, ("account", target), target, state_block)
        for pool in pools:
            asset_balance(
                calls, ("account", target, pool["pool_id"]),
                pool["remote_token"], target, state_block,
            )
        if chain == config["one_claims"]["claim_chain"]:
            asset_balance(
                calls, ("account", target, "claim_token"),
                config["one_claims"]["claim_token"], target, state_block,
            )
    calls.run()

    range_size = int((config["chains"][chain].get("rpc_settings") or {}).get("log_range", 100000))
    histories = {
        pool["pool_id"]: trusted_remote_history(
            rpc, pool["remote_adapter"], harmony_lz, state_block, range_size
        )
        for pool in pools
    }
    result = {}
    for pool in pools:
        pid = pool["pool_id"]
        code = calls.raw((pid, "adapter_code")) or "0x"
        owner = owners[pid]
        entry = {
            "adapter_code_size": (len(code) - 2) // 2,
            "adapter_code_hash": "0x" + lib.crl.keccak256(bytes.fromhex(code[2:])).hex(),
            "endpoint": endpoints[pid],
            "owner": owner,
            "owner_facts": account_facts(calls, ("owner", owner)) if owner else None,
            "token_getter": calls.address((pid, "token")),
            "decimals": calls.uint((pid, "decimals")),
            "symbol": lib.crl.decode_string(calls.raw((pid, "symbol")) or "0x"),
            "trusted_remote_history": histories[pid],
            "previous_adapter_owners": {
                previous: calls.address((pid, "previous_owner", previous))
                for previous in pool["previous_remote_adapters"]
            },
            "by_label": {},
        }
        for label in labels:
            entry["by_label"][label] = {
                "escrow_balance": calls.text((pid, label, "escrow")),
                "token_total_supply": calls.text((pid, label, "supply")),
                "outbound_nonce": calls.uint((pid, label, "outbound")),
                "inbound_nonce": [
                    calls.uint((pid, label, "inbound", index))
                    for index in range(len(pool["harmony"]))
                ],
                "previous_adapter_balances": {
                    previous: calls.text((pid, label, "previous", previous))
                    for previous in pool["previous_remote_adapters"]
                },
                "previous_adapter_nonces": {
                    previous: {
                        "outbound_nonce": calls.uint((pid, label, "previous_outbound", previous)),
                        "inbound_nonce": [
                            calls.uint((pid, label, "previous_inbound", previous, index))
                            for index in range(len(pool["harmony"]))
                        ],
                    }
                    for previous in pool["previous_remote_adapters"]
                },
            }
        result[pid] = entry
    accounts = {}
    for target in sorted(custody_targets):
        facts = account_facts(calls, ("account", target))
        facts["balances"] = {
            pool["pool_id"]: calls.text(("account", target, pool["pool_id"]))
            for pool in pools
        }
        if chain == config["one_claims"]["claim_chain"]:
            facts["claim_token_balance"] = calls.text(("account", target, "claim_token"))
        if target == config["one_claims"]["reserve_safe"] and target not in (
            custody, one_custody
        ):
            facts["role"] = "reserve"
        else:
            facts["role"] = "custody"
            facts["meets_requirements"] = custody_check(
                facts, config.get("custody_requirements") or {}
            )
        accounts[target] = facts
    claim_token = None
    if chain == config["one_claims"]["claim_chain"]:
        code = calls.raw(("claim_token", "code")) or "0x"
        claim_token = {
            "address": config["one_claims"]["claim_token"],
            "code_size": (len(code) - 2) // 2,
            "total_supply": calls.text(("claim_token", "totalSupply")),
            "decimals": calls.uint(("claim_token", "decimals")),
            "symbol": lib.crl.decode_string(calls.raw(("claim_token", "symbol")) or "0x"),
        }
    return result, accounts, claim_token, rpc.host


def harmony_state(routes, config, blocks, max_chain_id):
    rpc = lib.chain_rpc(config, lib.HARMONY)
    endpoint = routes["harmony"]["endpoint"]
    wone = routes["harmony"]["wone"]
    labels = {label: blocks[label]["number"] for label in lib.TIME_LABELS}
    cutoff = labels["cutoff"]
    calls = Calls(rpc)
    for pool in routes["pools"]:
        lz_id = routes["chains"][pool["chain"]]["layerzero_chain_id"]
        for index, entry in enumerate(pool["harmony"]):
            key = (pool["pool_id"], index)
            proxy = entry["proxy"]
            calls.code(key + ("code",), proxy, cutoff)
            for name in ("owner", "lzEndpoint", "token"):
                calls.eth_call(key + (name,), proxy, lib.SEL[name], cutoff)
            if not lib.is_native(entry["token"]):
                calls.eth_call(key + ("decimals",), entry["token"], lib.SEL["decimals"], cutoff)
            for label, number in labels.items():
                calls.eth_call(
                    key + (label, "outbound"), endpoint,
                    lib.outbound_nonce_data(lz_id, proxy), number,
                )
                calls.eth_call(
                    key + (label, "inbound"), endpoint,
                    lib.inbound_nonce_data(lz_id, pool["remote_adapter"], proxy),
                    number,
                )
                for previous in pool["previous_remote_adapters"]:
                    calls.eth_call(
                        key + (label, "inbound_previous", previous), endpoint,
                        lib.inbound_nonce_data(lz_id, previous, proxy), number,
                    )
                if lib.is_native(entry["token"]):
                    calls.balance(key + (label, "native"), proxy, number)
                    calls.eth_call(key + (label, "proxy_supply"), proxy, lib.SEL["totalSupply"], number)
                    calls.eth_call(
                        key + (label, "proxy_self"), proxy,
                        lib.call_data("balanceOf", lib.address_word(proxy)), number,
                    )
                    calls.eth_call(
                        key + (label, "wone"), wone,
                        lib.call_data("balanceOf", lib.address_word(proxy)), number,
                    )
                else:
                    calls.eth_call(key + (label, "supply"), entry["token"], lib.SEL["totalSupply"], number)
                    calls.eth_call(
                        key + (label, "proxy_balance"), entry["token"],
                        lib.call_data("balanceOf", lib.address_word(proxy)), number,
                    )
    origin_proxies = sorted({
        entry["proxy"]
        for pool in routes["pools"] if pool["kind"] == "harmony_origin"
        for entry in pool["harmony"]
    })
    for proxy in origin_proxies:
        for lz_id in range(1, max_chain_id + 1):
            calls.eth_call(
                ("discovery", proxy, lz_id), endpoint,
                lib.outbound_nonce_data(lz_id, proxy), cutoff,
            )
    calls.run()

    result = {}
    for pool in routes["pools"]:
        entries = []
        for index, entry in enumerate(pool["harmony"]):
            key = (pool["pool_id"], index)
            code = calls.raw(key + ("code",)) or "0x"
            item = {
                "proxy": entry["proxy"],
                "token": entry["token"],
                "code_size": (len(code) - 2) // 2,
                "owner": calls.address(key + ("owner",)),
                "endpoint": calls.address(key + ("lzEndpoint",)),
                "token_getter": calls.address(key + ("token",)),
                "decimals": 18 if lib.is_native(entry["token"]) else calls.uint(key + ("decimals",)),
                "by_label": {},
            }
            for label in labels:
                values = {
                    "outbound_nonce": calls.uint(key + (label, "outbound")),
                    "inbound_nonce": calls.uint(key + (label, "inbound")),
                    "inbound_from_previous": {
                        previous: calls.uint(key + (label, "inbound_previous", previous))
                        for previous in pool["previous_remote_adapters"]
                    },
                }
                if lib.is_native(entry["token"]):
                    values.update({
                        "native_balance": calls.text(key + (label, "native")),
                        "proxy_total_supply": calls.text(key + (label, "proxy_supply")),
                        "proxy_self_balance": calls.text(key + (label, "proxy_self")),
                        "wone_balance": calls.text(key + (label, "wone")),
                    })
                else:
                    values.update({
                        "token_total_supply": calls.text(key + (label, "supply")),
                        "proxy_token_balance": calls.text(key + (label, "proxy_balance")),
                    })
                item["by_label"][label] = values
            entries.append(item)
        result[pool["pool_id"]] = entries
    declared = {
        (entry["proxy"], routes["chains"][pool["chain"]]["layerzero_chain_id"])
        for pool in routes["pools"] for entry in pool["harmony"]
    }
    discovery = {}
    undeclared = []
    for proxy in origin_proxies:
        found = {}
        for lz_id in range(1, max_chain_id + 1):
            nonce = calls.uint(("discovery", proxy, lz_id))
            if nonce:
                found[str(lz_id)] = nonce
                if (proxy, lz_id) not in declared:
                    undeclared.append({"proxy": proxy, "layerzero_chain_id": lz_id, "outbound_nonce": nonce})
        discovery[proxy] = found
    return result, {
        "max_chain_id": max_chain_id,
        "block": cutoff,
        "outbound_nonces": discovery,
        "undeclared_destinations": undeclared,
    }, rpc.host


def checks_for(pool, remote, harmony, endpoint):
    checks = []

    def add(name, passed, detail="", failure="fail"):
        checks.append({"name": name, "status": "pass" if passed else failure, "detail": detail})

    add("remote adapter has code", remote["adapter_code_size"] > 0)
    registered = {entry["proxy"] for entry in pool["harmony"]}
    registered |= {item["address"] for item in pool["ignored_peers"]}
    peers = sorted({
        event["harmony_peer"]
        for event in remote["trusted_remote_history"]["events"]
        if event["harmony_peer"]
    })
    unknown = [peer for peer in peers if peer not in registered]
    add(
        "every Harmony peer the adapter ever trusted is registered",
        not unknown,
        ", ".join(unknown),
    )
    add("remote endpoint readable", remote["endpoint"] is not None)
    if not lib.is_native(pool["remote_token"]) and remote["token_getter"] is not None:
        add(
            "remote adapter token() matches registry",
            remote["token_getter"] == pool["remote_token"],
            str(remote["token_getter"]),
        )
    if not lib.is_native(pool["remote_token"]):
        add(
            "remote decimals match registry",
            remote["decimals"] == pool["decimals"],
            str(remote["decimals"]),
        )
    for registered_entry, entry in zip(pool["harmony"], harmony):
        historical = bool(registered_entry.get("historical_peer"))
        add(f"Harmony proxy {entry['proxy']} has code", entry["code_size"] > 0)
        add(
            f"Harmony proxy {entry['proxy']} uses the registry endpoint",
            entry["endpoint"] == endpoint,
            str(entry["endpoint"]),
        )
        add(
            f"Harmony token decimals match for {entry['proxy']}",
            entry["decimals"] == pool["decimals"],
            str(entry["decimals"]),
        )
        cutoff = entry["by_label"]["cutoff"]
        if lib.is_native(entry["token"]):
            add(
                f"NativeOFT {entry['proxy']} has no Harmony-side wrapped holders",
                cutoff["proxy_total_supply"] == cutoff["proxy_self_balance"],
                f"supply {cutoff['proxy_total_supply']} self {cutoff['proxy_self_balance']}",
            )
            add(
                f"NativeOFT {entry['proxy']} native balance equals its supply",
                cutoff["native_balance"] is not None
                and cutoff["native_balance"] == cutoff["proxy_total_supply"],
                f"native {cutoff['native_balance']} supply {cutoff['proxy_total_supply']}",
            )
        elif pool["kind"] == "remote_escrow":
            add(
                f"Harmony proxy {entry['proxy']} holds no wrapped tokens",
                cutoff["proxy_token_balance"] in ("0", None),
                str(cutoff["proxy_token_balance"]),
                "warn" if historical else "fail",
            )
        elif not historical:
            add(
                f"Harmony proxy {entry['proxy']} holds the locked Harmony asset",
                int(cutoff["proxy_token_balance"] or 0) > 0,
                str(cutoff["proxy_token_balance"]),
            )
    return checks


def main():
    args = parse_args()
    lib.prepare_outputs([args.output], args.replace)
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    blocks = lib.load_json(args.blocks)["chains"]
    overrides = {}
    for item in args.state_block:
        chain, _, number = item.partition("=")
        overrides[chain] = int(number)

    harmony, discovery, harmony_host = harmony_state(
        routes, config, blocks[lib.HARMONY]["blocks"], args.discovery_max_chain_id
    )
    print(
        "Harmony ONE destinations: "
        + ", ".join(
            f"{proxy}->{sorted(found)}" for proxy, found in discovery["outbound_nonces"].items()
        ),
        flush=True,
    )
    block_records = {
        lib.HARMONY: {
            label: blocks[lib.HARMONY]["blocks"][label] for label in lib.TIME_LABELS
        }
    }
    remote = {}
    custody = {}
    claim_token = None
    hosts = {lib.HARMONY: harmony_host}
    for chain in routes["chains"]:
        rpc = lib.chain_rpc(config, chain)
        number = overrides.get(chain) or rpc.head()
        block = rpc.block(number)
        block_records[chain] = {
            label: blocks[chain]["blocks"][label] for label in lib.TIME_LABELS
        }
        block_records[chain]["state"] = {
            "number": number,
            "hash": block["hash"],
            "timestamp": int(block["timestamp"], 16),
            "timestamp_utc": lib.utc_text(int(block["timestamp"], 16)),
            "source": "override" if chain in overrides else "head",
        }
        values, accounts, token, host = remote_state(
            routes, config, chain, blocks[chain]["blocks"], number
        )
        remote.update(values)
        custody[chain] = accounts
        hosts[chain] = host
        if token:
            claim_token = token
        print(f"{chain}: read {len(values)} pools at state block {number}", flush=True)

    pools = {}
    failures = 0
    for pool in routes["pools"]:
        pid = pool["pool_id"]
        checks = checks_for(pool, remote[pid], harmony[pid], routes["harmony"]["endpoint"])
        failures += sum(1 for check in checks if check["status"] == "fail")
        pools[pid] = {"remote": remote[pid], "harmony": harmony[pid], "checks": checks}
    if discovery["undeclared_destinations"]:
        failures += 1
    result = {
        "schema_version": 1,
        "status": "passed" if failures == 0 else "review",
        "check_failures": failures,
        "rpc_hosts": hosts,
        "blocks": block_records,
        "pools": pools,
        "harmony_origin_discovery": discovery,
        "custody": custody,
        "claim_token": claim_token,
    }
    lib.write_json(args.output, result)
    print(f"wrote {args.output} status={result['status']} failures={failures}")


if __name__ == "__main__":
    main()
