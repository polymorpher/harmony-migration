#!/usr/bin/env python3
"""Stage 1b: derived enrichment that needs a second round of RPC calls.

- AMM pairs: resolve token0/token1 symbols so LP roles are readable.
- Safe proxies: resolve singleton (masterCopy) VERSION() and code hash so
  every Safe can be grouped by singleton even when it is not a canonical
  Safe deployment (e.g. the Harmony multisig.harmony.one fork).
- NFT contracts: count distinct policy-state owners by enumerating token ids
  through Multicall3 ``aggregate3`` (ERC721Enumerable ``tokenByIndex`` when
  available, otherwise sequential ids 0..N and 1..N probed with
  ``ownerOf``). ERC-1155 balances cannot be enumerated from state without
  logs and are reported as not enumerable.

Output: an ``extra.json`` keyed by lowercase address consumed by
``classify-contracts.py``.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract_review_lib as lib  # noqa: E402
from contract_review_lib import decode_address, decode_bool, decode_string, decode_uint  # noqa: E402

MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
ZERO = "0x0000000000000000000000000000000000000000"
ENRICHMENT_EVIDENCE_VERSION = 2


def is_verified_validator(facts):
    validator = facts.get("validator") or {}
    return bool(
        validator.get("is_validator")
        and validator.get("rlp_wrapper_matches")
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--selectors", help="selectors.json from selector-census.py (enables SmartVault enrichment)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--rpc", default="https://a.api.s0.t.hmny.io")
    parser.add_argument("--state-block", required=True, type=int)
    parser.add_argument("--max-tokens", type=int, default=20000, help="max token ids enumerated per NFT contract")
    parser.add_argument("--chunk", type=int, default=400, help="ownerOf calls per Multicall3 aggregate3")
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def log(message):
    sys.stderr.write(time.strftime("%H:%M:%S ") + message + "\n")
    sys.stderr.flush()


def probe_data(facts, label):
    return ((facts.get("probes") or {}).get(label) or {}).get("data")


def probe_address(facts, label):
    data = probe_data(facts, label)
    value = decode_address(data) if data else None
    return value if value and value != ZERO else None


def probe_bool(facts, label):
    data = probe_data(facts, label)
    return decode_bool(data) if data and lib.word_count(data) == 1 else None


# ---- Multicall3 aggregate3 ---------------------------------------------------


def encode_aggregate3(calls):
    """calls: list of (target, calldata_hex). allowFailure = true for all."""
    head = lib.selector("aggregate3((address,bool,bytes)[])")
    # offset to array
    body = lib.encode_uint(32)
    body += lib.encode_uint(len(calls))
    # dynamic tuple array: offsets for each element relative to array data start
    tails = []
    offsets = []
    running = 32 * len(calls)
    for target, data in calls:
        raw = data[2:] if data.startswith("0x") else data
        payload_len = len(raw) // 2
        padded = raw + "0" * ((64 - len(raw) % 64) % 64)
        tuple_enc = lib.encode_address(target) + lib.encode_uint(1) + lib.encode_uint(96) + lib.encode_uint(payload_len) + padded
        offsets.append(running)
        tails.append(tuple_enc)
        running += len(tuple_enc) // 2
    body += "".join(lib.encode_uint(o) for o in offsets) + "".join(tails)
    return head + body


def decode_aggregate3(hexdata):
    """Returns list of (success, returndata_hex)."""
    raw = hexdata[2:] if hexdata.startswith("0x") else hexdata
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    if len(words) < 2:
        return []
    arr = int(words[0], 16) // 32
    count = int(words[arr], 16)
    base = arr + 1
    out = []
    for i in range(count):
        off = int(words[base + i], 16) // 32
        pos = base + off
        success = int(words[pos], 16) == 1
        data_off = int(words[pos + 1], 16) // 32
        data_pos = pos + data_off
        length = int(words[data_pos], 16)
        start = (data_pos + 1) * 64
        out.append((success, "0x" + raw[start:start + 2 * length]))
    return out


def multicall(client, calls, state_tag):
    data = encode_aggregate3(calls)
    result, error = client.call_soft(
        "eth_call",
        [{"to": MULTICALL3, "data": data}, state_tag],
    )
    if error or not result or result == "0x":
        return None
    return decode_aggregate3(result)


# ---- enrichment tasks ----------------------------------------------------------


def resolve_symbols(client, addresses, state_tag):
    out = {}
    addresses = sorted(set(a for a in addresses if a))
    if not addresses:
        return out
    calls = []
    for a in addresses:
        calls.append(
            (
                "eth_call",
                [{"to": a, "data": lib.selector("symbol()")}, state_tag],
            )
        )
    results = client.batch(calls)
    for a, (r, e) in zip(addresses, results):
        out[a] = (decode_string(r) or "").replace("\x00", "").strip() if r and r != "0x" else ""
    return out


def nft_owner_census(
    client,
    address,
    facts,
    max_tokens,
    chunk,
    state_tag,
):
    """Count distinct policy-state owners for an ERC-721 contract."""
    total_data = probe_data(facts, "erc20_totalSupply")
    total = decode_uint(total_data) if total_data and lib.word_count(total_data) == 1 else None
    enumerable = probe_bool(facts, "erc165_721enum") is True or bool(probe_data(facts, "nft_tokenByIndex_0"))
    owners = set()
    enumerated = 0
    method = ""
    if total is None or total == 0:
        # unknown supply: probe sequential ids until a long run of failures
        method = "sequential ownerOf probing (unknown totalSupply)"
        token_ids = list(range(0, min(max_tokens, 5000)))
    elif enumerable:
        method = "ERC721Enumerable tokenByIndex + ownerOf"
        n = min(total, max_tokens)
        ids = []
        for start in range(0, n, chunk):
            calls = [(address, lib.encode_call("tokenByIndex(uint256)", ("uint256", i))) for i in range(start, min(n, start + chunk))]
            res = multicall(client, calls, state_tag)
            if res is None:
                method += " (multicall failed)"
                break
            for ok, data in res:
                if ok and data and data != "0x":
                    ids.append(decode_uint(data))
        token_ids = ids
    else:
        method = "sequential ownerOf probing ids 0..totalSupply and 1..totalSupply"
        n = min(total, max_tokens)
        token_ids = list(range(0, n + 1))
    failures = 0
    for start in range(0, len(token_ids), chunk):
        batch_ids = token_ids[start:start + chunk]
        calls = [(address, lib.encode_call("ownerOf(uint256)", ("uint256", i))) for i in batch_ids]
        res = multicall(client, calls, state_tag)
        if res is None:
            method += " (multicall failed)"
            break
        got = 0
        for ok, data in res:
            if ok and data and data != "0x":
                owner = decode_address(data)
                if owner and owner != ZERO:
                    owners.add(owner)
                    got += 1
        enumerated += got
        if got == 0:
            failures += 1
            if failures >= 3 and (total is None or total == 0):
                break
        else:
            failures = 0
    truncated = total is not None and total > max_tokens
    if enumerated == 0 and not enumerable:
        method += " [no sequential ids found: token ids are likely non-sequential (e.g. name hashes); owners not enumerable from state]"
    return {
        "nft_owner_count": len(owners) if enumerated else "",
        "nft_owner_method": method + (" [truncated at max-tokens]" if truncated else ""),
        "nft_tokens_enumerated": enumerated,
        "nft_total_supply": total,
    }


def main():
    args = parse_args()
    facts_all = lib.load_json(args.facts, {})
    extra = lib.load_json(args.output, {}) or {}
    client = lib.RpcClient(args.rpc, batch_size=40, workers=args.workers)
    state_tag = hex(args.state_block)
    policy_block = client.call(
        "hmyv2_getBlockByNumber",
        [args.state_block, {"fullTx": False, "inclStaking": False}],
    )
    if not policy_block or not policy_block.get("hash"):
        raise ValueError("could not resolve policy-state block")
    metadata = extra.get("_policy_state") or {}
    if (
        metadata.get("schema_version") != ENRICHMENT_EVIDENCE_VERSION
        or metadata.get("block") != args.state_block
        or metadata.get("block_hash") != policy_block["hash"]
        or metadata.get("state_root") != policy_block["stateRoot"]
    ):
        extra = {}
    extra["_policy_state"] = {
        "schema_version": ENRICHMENT_EVIDENCE_VERSION,
        "block": args.state_block,
        "block_hash": policy_block["hash"],
        "state_root": policy_block["stateRoot"],
    }

    # 1) AMM pair token symbols
    pair_tokens = set()
    pairs = {}
    for address, facts in facts_all.items():
        if is_verified_validator(facts):
            continue
        t0 = probe_address(facts, "amm_token0")
        t1 = probe_address(facts, "amm_token1")
        if t0 and t1 and probe_address(facts, "amm_factory"):
            pairs[address] = (t0, t1)
            pair_tokens.update([t0, t1])
    log(f"resolving symbols for {len(pair_tokens)} pair tokens")
    symbols = resolve_symbols(client, pair_tokens, state_tag)
    for address, (t0, t1) in pairs.items():
        extra.setdefault(address, {})["pair_symbols"] = f"{symbols.get(t0) or t0}/{symbols.get(t1) or t1}"
        extra[address]["pair_token0"] = t0
        extra[address]["pair_token1"] = t1

    # 2) Safe singleton metadata
    singletons = set()
    for address, facts in facts_all.items():
        basic = facts.get("basic") or {}
        if not probe_data(facts, "safe_getOwners"):
            continue
        master = decode_address(basic.get("storage_slot0") or "")
        if master and master != ZERO:
            singletons.add(master)
    log(f"resolving {len(singletons)} Safe singletons")
    if singletons:
        singletons = sorted(singletons)
        calls = []
        for s in singletons:
            calls.append(
                (
                    "eth_call",
                    [{"to": s, "data": lib.selector("VERSION()")}, state_tag],
                )
            )
            calls.append(("eth_getCode", [s, state_tag]))
        results = client.batch(calls)
        for i, s in enumerate(singletons):
            version, _ = results[2 * i]
            code, _ = results[2 * i + 1]
            code_bytes = bytes.fromhex(code[2:]) if code and code.startswith("0x") else b""
            extra.setdefault(s, {})["singleton_version"] = decode_string(version) if version and version != "0x" else None
            extra[s]["singleton_code_len"] = len(code_bytes)
            extra[s]["singleton_code_hash"] = "0x" + lib.keccak256(code_bytes).hex()
            extra[s]["singleton_label"] = (
                f"singleton {s} VERSION={extra[s]['singleton_version']} code={len(code_bytes)}B"
            )

    # 2b) SmartVault (harmony-totp) wallets: owner, guardians, factory
    selectors = lib.load_json(args.selectors, {}) if args.selectors else {}
    vaults = []
    for address, facts in facts_all.items():
        entry = selectors.get(address) or {}
        sigs = set(
            entry.get("implementation_policy_signatures") or []
        ) | set(entry.get("own_policy_signatures") or [])
        if "setupHOTP2FA(string,bytes32,uint8,uint256)" in sigs or "getRootHashes()" in sigs:
            vaults.append(address)
    log(f"resolving {len(vaults)} SmartVault wallets")
    if vaults:
        calls = []
        for a in vaults:
            for sig in ("owner()", "getOwner()", "getGuardians()", "walletFactory()", "wallet()", "isRecovering()", "locked()", "recoveryDelay()"):
                calls.append(
                    (
                        "eth_call",
                        [{"to": a, "data": lib.selector(sig)}, state_tag],
                    )
                )
        results = client.batch(calls)
        per = 8
        for i, a in enumerate(vaults):
            chunk = [r for r, _ in results[i * per:(i + 1) * per]]
            owner = decode_address(chunk[0]) if chunk[0] and chunk[0] != "0x" else None
            owner2 = decode_address(chunk[1]) if chunk[1] and chunk[1] != "0x" else None
            guardians = lib.decode_address_array(chunk[2]) if chunk[2] and chunk[2] != "0x" else None
            extra.setdefault(a, {}).update({
                "smartvault_owner": owner or owner2,
                "smartvault_guardians": guardians or [],
                "smartvault_factory": decode_address(chunk[3]) if chunk[3] and chunk[3] != "0x" else (decode_address(chunk[4]) if chunk[4] and chunk[4] != "0x" else None),
                "smartvault_is_recovering": decode_bool(chunk[5]) if chunk[5] and chunk[5] != "0x" else None,
                "smartvault_locked": decode_bool(chunk[6]) if chunk[6] and chunk[6] != "0x" else None,
                "smartvault_recovery_delay": decode_uint(chunk[7]) if chunk[7] and chunk[7] != "0x" else None,
            })
    lib.dump_json(args.output, extra)

    # 3) NFT owner census
    nfts = []
    for address, facts in facts_all.items():
        if is_verified_validator(facts):
            continue
        s = set((facts.get("basic") or {}).get("bytecode_selectors") or [])
        is721 = probe_bool(facts, "erc165_721") is True or ("sel_ownerOf" in s and "sel_setApprovalForAll" in s and ("sel_safeTransferFrom721" in s or "sel_safeTransferFrom721data" in s))
        is1155 = probe_bool(facts, "erc165_1155") is True or ("sel_balanceOf1155" in s and "sel_safeBatchTransferFrom" in s)
        # skip 1wallets (they implement ERC-721/1155 receiver hooks and can trip selector heuristics)
        if probe_data(facts, "ow_getInfo") and lib.word_count(probe_data(facts, "ow_getInfo")) == 8:
            continue
        if probe_data(facts, "safe_getOwners"):
            continue
        entry = selectors.get(address) or {}
        sigs = set(
            entry.get("implementation_policy_signatures") or []
        ) | set(entry.get("own_policy_signatures") or [])
        if "punkIndexToAddress(uint256)" in sigs and "nft_owner_count" not in (extra.get(address) or {}):
            # CryptoPunks-style market: owners live in punkIndexToAddress(0..totalSupply-1)
            total_data = probe_data(facts, "erc20_totalSupply")
            total = decode_uint(total_data) if total_data and lib.word_count(total_data) == 1 else 10000
            owners = set()
            enumerated = 0
            for start in range(0, min(total, args.max_tokens), args.chunk):
                calls = [(address, lib.encode_call("punkIndexToAddress(uint256)", ("uint256", i))) for i in range(start, min(total, start + args.chunk))]
                res = multicall(client, calls, state_tag)
                if res is None:
                    break
                for ok, data in res:
                    owner = decode_address(data) if ok and data and data != "0x" else None
                    if owner and owner != ZERO:
                        owners.add(owner)
                        enumerated += 1
            extra.setdefault(address, {}).update({
                "nft_owner_count": len(owners),
                "nft_owner_method": "CryptoPunks-style punkIndexToAddress(0..totalSupply-1) via Multicall3",
                "nft_tokens_enumerated": enumerated,
            })
            log(f"  punks-style {address}: owners={len(owners)} assigned={enumerated}")
            continue
        if is721 and "nft_owner_count" not in (extra.get(address) or {}):
            nfts.append(address)
        elif is1155 and not is721:
            extra.setdefault(address, {}).update({
                "nft_owner_count": "",
                "nft_owner_method": "ERC-1155: holders not enumerable from state (requires full TransferSingle/TransferBatch log scan)",
                "nft_tokens_enumerated": "",
            })
    log(f"NFT owner census for {len(nfts)} ERC-721 contracts")
    for i, address in enumerate(nfts):
        try:
            result = nft_owner_census(
                client,
                address,
                facts_all[address],
                args.max_tokens,
                args.chunk,
                state_tag,
            )
        except lib.RpcError as error:
            result = {"nft_owner_count": "", "nft_owner_method": f"error: {error}", "nft_tokens_enumerated": ""}
        extra.setdefault(address, {}).update(result)
        log(f"  [{i + 1}/{len(nfts)}] {address}: owners={result.get('nft_owner_count')} tokens={result.get('nft_tokens_enumerated')} ({result.get('nft_owner_method')})")
        lib.dump_json(args.output, extra)

    lib.dump_json(args.output, extra)
    log(f"done; http requests={client.requests_sent}")


if __name__ == "__main__":
    main()
