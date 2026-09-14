#!/usr/bin/env python3
"""Stage 2: classify code-bearing claim accounts from the collected facts.

Reads the facts JSON produced by ``fetch-contract-facts.py`` plus the
claims CSV and the known-apps registry, and writes:

- ``contract-review-all.csv``       one row per address, primary category
- ``validator-accounts.csv``        validator EOAs (ValidatorWrapper in code field)
- ``multisig-wallets.csv``          Gnosis Safe / Safe proxies and other multisigs
- ``onewallets.csv``                1wallet (Modulo OTP wallet) contracts
- ``erc20-tokens.csv``              ERC-20 token contracts (any app)
- ``nft-contracts.csv``             ERC-721 / ERC-1155 / other NFT contracts
- ``known-app-contracts.csv``       contracts attributed to well-known apps
- ``unidentified-contracts.csv``    everything else
- ``summary.json``                  category statistics

Classification is purely from on-chain evidence: eth_call probe results,
runtime bytecode selectors, storage slots, and on-chain parent relationships
(factory / pool / comptroller / singleton), matched against the registry.
"""

import argparse
import csv
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract_review_lib as lib  # noqa: E402
from contract_review_lib import decode_address, decode_address_array, decode_bool, decode_string, decode_uint  # noqa: E402

ONEWALLET_TREASURIES = {
    "0x7534978f9fa903150ed429c486d1f42b7fdb7a61",
    "0x02f2cf45dd4bacba091d78502dba3b2f431a54d3",
}
ZERO = "0x0000000000000000000000000000000000000000"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--claims", required=True, help="claims-at-least-1000-one-contract-review.csv")
    parser.add_argument("--registry", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "known-apps.json"))
    parser.add_argument("--extra", help="optional JSON with extra per-address facts (token0/token1 symbols, nft owner counts, singleton labels)")
    parser.add_argument("--selectors", help="selectors.json from selector-census.py (dispatcher signatures, proxy implementations)")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


SELECTORS = {}


def signatures(address):
    entry = SELECTORS.get(address) or {}
    return set(entry.get("own_signatures") or []) | set(entry.get("implementation_signatures") or [])


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def iso(ts):
    if ts in (None, "", 0):
        return ""
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def one(atto):
    if atto in (None, ""):
        return ""
    return lib.atto_to_one_str(atto)


def probe_data(facts, label):
    return ((facts.get("probes") or {}).get(label) or {}).get("data")


def probe_string(facts, label):
    data = probe_data(facts, label)
    if not data:
        return None
    value = decode_string(data)
    if value is None:
        return None
    value = value.replace("\x00", "").strip()
    return value or None


def probe_uint(facts, label, index=0):
    data = probe_data(facts, label)
    return decode_uint(data, index) if data else None


def probe_address(facts, label, index=0):
    data = probe_data(facts, label)
    if not data:
        return None
    value = decode_address(data, index)
    return value if value and value != ZERO else None


def probe_bool(facts, label):
    data = probe_data(facts, label)
    if not data or lib.word_count(data) != 1:
        return None
    return decode_bool(data)


def csv_addr(value):
    if not value:
        return ""
    hexed = lib.any_to_hex(value)
    return lib.to_checksum(hexed) if hexed else value


def sels(facts):
    return set((facts.get("basic") or {}).get("bytecode_selectors") or [])


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------


def detect_safe(facts, registry):
    """Gnosis Safe (any version) or legacy Gnosis MultiSigWallet."""
    owners_data = probe_data(facts, "safe_getOwners")
    owners = decode_address_array(owners_data) if owners_data else None
    threshold = probe_uint(facts, "safe_getThreshold")
    version = probe_string(facts, "safe_VERSION")
    nonce = probe_uint(facts, "safe_nonce")
    slot0 = (facts.get("basic") or {}).get("storage_slot0") or ""
    master = decode_address(slot0) if slot0 else None
    if master == ZERO:
        master = None
    required = probe_uint(facts, "msw_required")
    if owners and threshold and 1 <= threshold <= len(owners) and version:
        label = registry["safe_singletons"].get(master or "", None)
        return {
            "kind": "gnosis-safe",
            "version": version,
            "owners": owners,
            "threshold": threshold,
            "nonce": nonce,
            "master_copy": master,
            "master_copy_label": label,
        }
    if owners and required and 1 <= required <= len(owners) and not version:
        return {
            "kind": "gnosis-multisigwallet-legacy",
            "version": "MultiSigWallet",
            "owners": owners,
            "threshold": required,
            "nonce": probe_uint(facts, "msw_transactionCount"),
            "master_copy": master,
            "master_copy_label": None,
        }
    return None


def detect_onewallet(facts):
    """1wallet: getInfo() returns (root, height, interval, t0, lifespan, maxOps, lastResort, dailyLimit)."""
    data = probe_data(facts, "ow_getInfo")
    if not data or lib.word_count(data) != 8:
        return None
    root = data[2:66]
    height = decode_uint(data, 1)
    interval = decode_uint(data, 2)
    t0 = decode_uint(data, 3)
    lifespan = decode_uint(data, 4)
    max_ops = decode_uint(data, 5)
    last_resort = decode_address(data, 6)
    daily_limit = decode_uint(data, 7)
    if int(root, 16) == 0 or not (1 <= height <= 64) or not (1 <= interval <= 86400):
        return None
    if last_resort is None or not (1 <= max_ops <= 255):
        return None
    effective = t0 * interval
    if not (1_500_000_000 <= effective <= 2_000_000_000):
        return None
    version_data = probe_data(facts, "ow_getVersion")
    major = minor = None
    if version_data and lib.word_count(version_data) == 2:
        major = decode_uint(version_data, 0)
        minor = decode_uint(version_data, 1)
    forward = probe_address(facts, "ow_getForwardAddress")
    recovery_set = last_resort not in ONEWALLET_TREASURIES and last_resort != ZERO
    return {
        "root": "0x" + root,
        "height": height,
        "interval": interval,
        "t0": t0,
        "effective_time": effective,
        "expiry_time": (t0 + lifespan) * interval,
        "lifespan": lifespan,
        "max_ops": max_ops,
        "last_resort": last_resort,
        "recovery_address_set": recovery_set,
        "daily_limit_atto": daily_limit,
        "major": major,
        "minor": minor,
        "forward_address": forward,
        "last_operation_time": probe_uint(facts, "ow_lastOperationTime"),
        "has_commit3": "sel_ow_commit3" in sels(facts),
        "has_commit1": "sel_ow_commit1" in sels(facts),
    }


def detect_token(address, facts):
    """Return token standard evidence: erc20 / erc721 / erc1155 / None.

    Uses ERC-165 answers first, then the contract's (or its implementation's)
    dispatcher signatures. Bytecode-anywhere selector hits are deliberately
    not used because contracts that *call* tokens embed the same selectors.
    """
    sigs = signatures(address)
    name = probe_string(facts, "erc20_name")
    symbol = probe_string(facts, "erc20_symbol")
    decimals_data = probe_data(facts, "erc20_decimals")
    decimals = decode_uint(decimals_data) if decimals_data and lib.word_count(decimals_data) == 1 else None
    total_supply_data = probe_data(facts, "erc20_totalSupply")
    total_supply = decode_uint(total_supply_data) if total_supply_data and lib.word_count(total_supply_data) == 1 else None
    balance_dead = probe_data(facts, "erc20_balanceOf_dead")
    balance_ok = bool(balance_dead) and lib.word_count(balance_dead) == 1

    supports_165 = probe_bool(facts, "erc165_165")
    supports_ffff = probe_bool(facts, "erc165_ffffffff")
    erc165_sane = supports_165 is True and supports_ffff is False
    is721 = is1155 = False
    if erc165_sane:
        is721 = probe_bool(facts, "erc165_721") is True
        is1155 = probe_bool(facts, "erc165_1155") is True
    else:
        has_owner_of = "ownerOf(uint256)" in sigs
        has_safe_transfer = "safeTransferFrom(address,address,uint256)" in sigs or "safeTransferFrom(address,address,uint256,bytes)" in sigs
        is721 = has_owner_of and has_safe_transfer and "setApprovalForAll(address,bool)" in sigs and "transferFrom(address,address,uint256)" in sigs
        is1155 = "balanceOf(address,uint256)" in sigs and "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)" in sigs
    nft_other = "punksOfferedForSale(uint256)" in sigs and "getPunk(uint256)" in sigs

    info = {
        "name": name,
        "symbol": symbol,
        "decimals": decimals,
        "total_supply": total_supply,
        "erc721": is721,
        "erc1155": is1155,
        "nft_other": nft_other,
        "erc721_enumerable": probe_bool(facts, "erc165_721enum") is True or "tokenByIndex(uint256)" in sigs,
        "erc721_metadata": probe_bool(facts, "erc165_721meta") is True or "tokenURI(uint256)" in sigs,
    }
    if is721 or is1155:
        info["standard"] = "ERC-1155" if is1155 and not is721 else "ERC-721"
        if is721 and is1155:
            info["standard"] = "ERC-721+ERC-1155"
        return info
    if nft_other:
        info["standard"] = "NFT (CryptoPunks-style, pre-ERC-721)"
        return info
    staking_pool = "rewardPerToken()" in sigs and "stakeFor(address,uint128)" in sigs
    erc20_core = {"transfer(address,uint256)", "approve(address,uint256)", "balanceOf(address)", "totalSupply()"} & sigs
    erc20_ext = {"allowance(address,address)", "transferFrom(address,address,uint256)"} & sigs
    if staking_pool:
        return None
    if total_supply is not None and balance_ok and (name or symbol) and len(erc20_core) >= 3 and erc20_ext:
        info["standard"] = "ERC-20"
        return info
    if total_supply is not None and balance_ok and (name or symbol) and decimals is not None and not sigs:
        # no dispatcher could be extracted (unusual compiler); rely on probes only
        info["standard"] = "ERC-20 (probe-only)"
        return info
    return None


def match_rules(rules, sigs):
    for rule in rules:
        if all(sig in sigs for sig in rule["all_of"]):
            return rule
    return None


def detect_known_app(address, facts, token, registry, extra):
    """Attribute to a well-known app by address, on-chain parent, or name pattern."""
    entry = registry["addresses"].get(address)
    if entry:
        return {"app": entry["app"], "role": entry["role"], "evidence": "registry address match"}
    sigs = signatures(address)
    rule = match_rules(registry.get("signature_rules") or [], sigs)
    if rule:
        role = rule["role"]
        if rule["app"] == "layerzero" and token and (token.get("erc721") or token.get("erc1155")):
            role = f"LayerZero ONFT (omnichain NFT) {token.get('symbol') or ''}".strip()
        return {"app": rule["app"], "role": role, "evidence": "function-signature fingerprint: " + " & ".join(rule["all_of"])}
    factory = probe_address(facts, "amm_factory")
    token0 = probe_address(facts, "amm_token0")
    token1 = probe_address(facts, "amm_token1")
    if factory and factory in registry["univ2_factories"] and token0 and token1:
        app = registry["univ2_factories"][factory]
        symbols = (extra.get(address) or {}).get("pair_symbols")
        kind = "UniswapV3 pool" if probe_data(facts, "univ3_fee") and probe_data(facts, "univ3_slot0") else "UniswapV2 LP pair"
        role = f"{kind} {symbols}" if symbols else f"{kind} token0={token0} token1={token1}"
        return {"app": app, "role": role, "evidence": f"factory() == {factory}"}
    pool = probe_address(facts, "aave_POOL")
    if pool and pool in registry["aave_pools"]:
        app = registry["aave_pools"][pool]
        symbol = (token or {}).get("symbol") or ""
        if symbol.lower().startswith("variabledebt"):
            role = f"VariableDebtToken {symbol}"
        elif symbol.lower().startswith("stabledebt"):
            role = f"StableDebtToken {symbol}"
        else:
            role = f"AToken {symbol}"
        underlying = probe_address(facts, "aave_UNDERLYING")
        if underlying:
            role += f" (underlying {underlying})"
        return {"app": app, "role": role, "evidence": f"POOL() == {pool}"}
    comptroller = probe_address(facts, "comp_comptroller")
    if comptroller and comptroller in registry["comptrollers"]:
        app = registry["comptrollers"][comptroller]
        symbol = (token or {}).get("symbol") or ""
        underlying = probe_address(facts, "comp_underlying")
        role = f"cToken market {symbol}" + (f" (underlying {underlying})" if underlying else " (native ONE)")
        return {"app": app, "role": role, "evidence": f"comptroller() == {comptroller}"}
    if token and (token.get("name") or token.get("symbol")):
        for rule in registry["name_patterns"]:
            haystack = f"{token.get('name') or ''} | {token.get('symbol') or ''}"
            if re.search(rule["pattern"], haystack):
                role_source = token.get("symbol") if rule.get("role_from") == "symbol" else token.get("name")
                std = token.get("standard") or "token"
                return {"app": rule["app"], "role": f"{std} {role_source}", "evidence": f"name/symbol pattern {rule['pattern']!r}"}
    return None


def proxy_info(address, facts):
    basic = facts.get("basic") or {}
    entry = SELECTORS.get(address) or {}
    if entry.get("implementation"):
        return {"type": entry.get("proxy_kind"), "implementation": entry["implementation"], "admin": basic.get("eip1967_admin")}
    impl = basic.get("eip1967_impl")
    if impl and impl != ZERO:
        return {"type": "eip1967_impl", "implementation": impl, "admin": basic.get("eip1967_admin")}
    beacon = basic.get("eip1967_beacon")
    if beacon and beacon != ZERO:
        return {"type": "eip1967_beacon", "implementation": beacon, "admin": basic.get("eip1967_admin")}
    impl_probe = probe_address(facts, "misc_implementation")
    if impl_probe and basic.get("code_cutoff_len", 0) < 2000:
        return {"type": "implementation()", "implementation": impl_probe, "admin": None}
    return None


def detect_smartvault(address, facts, extra):
    sigs = signatures(address)
    if "setupHOTP2FA(string,bytes32,uint8,uint256)" not in sigs and "getRootHashes()" not in sigs:
        return None
    ex = extra.get(address) or {}
    return {
        "owner": ex.get("smartvault_owner") or probe_address(facts, "owner") or probe_address(facts, "getOwner"),
        "guardians": ex.get("smartvault_guardians") or [],
        "factory": ex.get("smartvault_factory"),
        "is_recovering": ex.get("smartvault_is_recovering"),
        "locked": ex.get("smartvault_locked"),
        "recovery_delay": ex.get("smartvault_recovery_delay"),
        "master_copy": (SELECTORS.get(address) or {}).get("implementation"),
        "version_hint": "early masterCopy (getRootHashes/startSession)" if "getRootHashes()" in sigs else "later masterCopy (setupHOTP2FA/guardians/metaTx)",
    }


# --------------------------------------------------------------------------
# main classification
# --------------------------------------------------------------------------


def classify(address, facts, claim, registry, extra):
    basic = facts.get("basic") or {}
    total_claim = claim["total_claim_one"]
    staked_to_vault = claim["staked_to_vault_one"]
    wallet_airdrop = claim["wallet_airdrop_one"]
    if lib.one_str_to_atto(total_claim) != (
        lib.one_str_to_atto(wallet_airdrop)
        + lib.one_str_to_atto(staked_to_vault)
    ):
        raise ValueError(f"allocation component mismatch for {address}")
    row = {
        "address": lib.to_checksum(address),
        "address_bech32": lib.hex_to_bech32(address),
        "total_claim_one": total_claim,
        "wallet_airdrop_one": wallet_airdrop,
        "staked_to_vault_one": staked_to_vault,
        "liquid_total_one": claim["liquid_total_one"],
        "active_staked_or_delegated_one": claim["active_staked_or_delegated_one"],
        "pending_undelegation_one": claim["pending_undelegation_one"],
        "unclaimed_staking_reward_one": claim["unclaimed_staking_reward_one"],
        "total_usd_at_cutoff_price": claim["total_usd"],
        "wallet_airdrop_usd_at_cutoff_price": claim.get(
            "wallet_airdrop_usd", ""
        ),
        "balance_latest_one": one(basic.get("balance_latest_atto")),
        "latest_block_at_fetch": basic.get("latest_block_at_fetch"),
        "code_size_bytes": basic.get("code_cutoff_len"),
        "code_hash": basic.get("code_cutoff_hash"),
        "nonce": basic.get("nonce_latest"),
        "direct_tx_count_all": basic.get("tx_count_all"),
        "direct_tx_count_sent": basic.get("tx_count_sent"),
        "direct_tx_count_received": basic.get("tx_count_received"),
        "staking_tx_count": basic.get("staking_tx_count_all"),
    }
    creation = facts.get("creation") or {}
    row.update({
        "creation_block": creation.get("block"),
        "creation_time_utc": iso(creation.get("timestamp")),
        "creation_tx_hash": creation.get("tx_hash"),
        "creation_tx_sender": csv_addr(creation.get("tx_sender")),
        "creation_direct_creator": csv_addr(creation.get("direct_creator")),
        "creation_method": creation.get("method"),
        "creation_note": creation.get("trace_error") or creation.get("error") or "",
    })
    funding = facts.get("funding") or {}
    row.update({
        "first_funding_block": funding.get("block"),
        "first_funding_time_utc": iso(funding.get("timestamp")),
        "first_funding_tx_hash": funding.get("tx_hash"),
        "first_funder": csv_addr(funding.get("funder")),
        "first_funding_tx_sender": csv_addr(funding.get("tx_sender")),
        "first_funding_value_one": one(funding.get("value_atto")),
        "first_funding_method": (funding.get("method") or "") + (("; " + funding["note"]) if funding.get("note") else ""),
        "first_funding_note": funding.get("trace_error") or "",
    })
    history = facts.get("history") or {}
    last = history.get("last_direct_tx") or {}
    first = history.get("first_direct_tx") or {}
    row.update({
        "first_direct_tx_block": first.get("block"),
        "first_direct_tx_time_utc": iso(first.get("timestamp")),
        "first_direct_tx_hash": first.get("hash"),
        "last_direct_tx_block": last.get("block"),
        "last_direct_tx_time_utc": iso(last.get("timestamp")),
        "last_direct_tx_hash": last.get("hash"),
        "last_direct_tx_from": csv_addr(last.get("from")),
    })
    delegations = facts.get("delegations") or {}
    row.update({
        "current_delegation_count": delegations.get("count"),
        "current_delegated_one": one(delegations.get("active_atto")) if delegations.get("active_atto") is not None else "",
    })

    validator = facts.get("validator") or {}
    token = None
    safe = None
    wallet = None
    vault = None
    app = None
    proxy = None
    pattern = None
    if validator.get("is_validator") and validator.get("rlp_wrapper_matches"):
        primary, sub = "validator-account", "Harmony validator (EOA; ValidatorWrapper RLP stored in code field)"
        identity = validator.get("name") or ""
    else:
        safe = detect_safe(facts, registry)
        wallet = detect_onewallet(facts) if not safe else None
        vault = detect_smartvault(address, facts, extra) if not (safe or wallet) else None
        token = detect_token(address, facts)
        proxy = proxy_info(address, facts)
        app = detect_known_app(address, facts, token, registry, extra)
        pattern = match_rules(registry.get("pattern_rules") or [], signatures(address))
        if safe:
            primary = "multisig-wallet"
            label = safe.get("master_copy_label") or (extra.get(safe.get("master_copy") or "", {}) or {}).get("singleton_label")
            sub = f"{safe['kind']} v{safe['version']}" + (f" [{label}]" if label else f" [singleton {safe.get('master_copy')}]")
            identity = f"{safe['threshold']}-of-{len(safe['owners'])} {safe['kind']} v{safe['version']}"
        elif wallet:
            primary = "onewallet"
            sub = f"1wallet v{wallet['major']}.{wallet['minor']}" if wallet.get("major") is not None else "1wallet (pre-getVersion)"
            identity = sub
        elif vault:
            primary = "smartvault-wallet"
            sub = "SmartVault (harmony-totp) OTP smart wallet"
            identity = vault["version_hint"]
        elif app and app["app"] not in ("safe", "onewallet") and not (
            app["app"] in (registry.get("infrastructure_apps") or []) and token and app["evidence"].startswith("function-signature")
        ):
            primary = "known-app"
            sub = registry["apps"][app["app"]]["name"]
            identity = app["role"]
        elif token and token.get("standard", "").startswith("ERC-20"):
            primary = "erc20-token"
            sub = token["standard"]
            identity = f"{token.get('name') or ''} ({token.get('symbol') or ''})".strip()
        elif token:
            primary = "nft-contract"
            sub = token["standard"]
            identity = f"{token.get('name') or ''} ({token.get('symbol') or ''})".strip()
        elif pattern:
            primary = "pattern-identified"
            sub = pattern.get("category", "pattern")
            identity = pattern["label"]
        elif proxy:
            primary = "unidentified"
            sub = f"proxy ({proxy['type']}) -> {proxy['implementation']}"
            identity = ""
        else:
            primary = "unidentified"
            sub = "unidentified contract"
            identity = ""
    sigs = sorted(signatures(address))
    row.update({
        "primary_category": primary,
        "subcategory": sub,
        "identity": identity,
        "is_erc20": bool(token and token.get("standard", "").startswith("ERC-20")),
        "is_nft": bool(token and (token.get("erc721") or token.get("erc1155") or token.get("nft_other"))),
        "is_multisig": bool(safe),
        "is_onewallet": bool(wallet),
        "is_smartvault": bool(vault),
        "known_app": (registry["apps"][app["app"]]["name"] if app else ""),
        "known_app_role": app["role"] if app else "",
        "known_app_evidence": app["evidence"] if app else "",
        "pattern_label": pattern["label"] if pattern else "",
        "pattern_category": pattern.get("category", "") if pattern else "",
        "token_name": (token or {}).get("name") or "",
        "token_symbol": (token or {}).get("symbol") or "",
        "proxy_type": (proxy or {}).get("type") or "",
        "proxy_implementation": csv_addr((proxy or {}).get("implementation")),
        "owner_probe": csv_addr(probe_address(facts, "owner") or probe_address(facts, "getOwner")),
        "dispatcher_signatures": " ".join(sigs)[:4000],
    })
    return row, {"safe": safe, "wallet": wallet, "vault": vault, "token": token, "app": app, "proxy": proxy, "validator": validator, "pattern": pattern}


# --------------------------------------------------------------------------
# per-category writers
# --------------------------------------------------------------------------


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


COMMON_FIELDS = [
    "address", "address_bech32", "total_claim_one", "wallet_airdrop_one",
    "staked_to_vault_one", "liquid_total_one",
    "active_staked_or_delegated_one", "pending_undelegation_one",
    "unclaimed_staking_reward_one", "total_usd_at_cutoff_price",
    "wallet_airdrop_usd_at_cutoff_price", "balance_latest_one",
]

CREATION_FIELDS = [
    "creation_block", "creation_time_utc", "creation_tx_hash", "creation_tx_sender", "creation_direct_creator",
    "creation_method", "creation_note",
]

FUNDING_FIELDS = [
    "first_funding_block", "first_funding_time_utc", "first_funding_tx_hash", "first_funder",
    "first_funding_tx_sender", "first_funding_value_one", "first_funding_method", "first_funding_note",
]

ACTIVITY_FIELDS = [
    "direct_tx_count_all", "direct_tx_count_sent", "direct_tx_count_received", "staking_tx_count",
    "first_direct_tx_block", "first_direct_tx_time_utc", "first_direct_tx_hash",
    "last_direct_tx_block", "last_direct_tx_time_utc", "last_direct_tx_hash", "last_direct_tx_from",
]


def main():
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    facts_all = lib.load_json(args.facts, {})
    registry = lib.load_json(args.registry, {})
    extra = lib.load_json(args.extra, {}) if args.extra else {}
    if args.selectors:
        SELECTORS.update({k: v for k, v in (lib.load_json(args.selectors, {}) or {}).items() if k.startswith("0x")})
    claims = {}
    with open(args.claims, newline="") as handle:
        for row in csv.DictReader(handle):
            claims[lib.normalize_address(row["address"])] = row

    all_rows = []
    details = {}
    for address, claim in claims.items():
        facts = facts_all.get(address) or {}
        row, det = classify(address, facts, claim, registry, extra)
        all_rows.append(row)
        details[address] = det

    all_rows.sort(
        key=lambda row: -lib.one_str_to_atto(
            row["total_claim_one"]
        )
    )
    write_csv(
        os.path.join(args.output_dir, "contract-review-all.csv"),
        all_rows,
        ["primary_category", "subcategory", "identity"] + COMMON_FIELDS + [
            "latest_block_at_fetch",
            "is_erc20", "is_nft", "is_multisig", "is_onewallet", "is_smartvault", "known_app", "known_app_role", "known_app_evidence",
            "pattern_label", "pattern_category",
            "token_name", "token_symbol", "proxy_type", "proxy_implementation", "owner_probe",
            "code_size_bytes", "code_hash", "nonce", "current_delegation_count", "current_delegated_one",
        ] + CREATION_FIELDS + FUNDING_FIELDS + ACTIVITY_FIELDS + ["dispatcher_signatures"],
    )

    # SmartVault (harmony-totp) wallets
    sv_rows = []
    for row in all_rows:
        det = details[row["address"].lower()]
        v = det["vault"]
        if not v:
            continue
        sv_rows.append(dict(row, **{
            "smartvault_owner": csv_addr(v.get("owner")),
            "guardian_count": len(v.get("guardians") or []),
            "guardians": ",".join(lib.to_checksum(g) for g in (v.get("guardians") or [])),
            "wallet_factory": csv_addr(v.get("factory")),
            "master_copy": csv_addr(v.get("master_copy")),
            "version_hint": v.get("version_hint"),
            "is_recovering": v.get("is_recovering"),
            "locked": v.get("locked"),
            "recovery_delay_seconds": v.get("recovery_delay"),
        }))
    write_csv(
        os.path.join(args.output_dir, "smartvault-wallets.csv"),
        sv_rows,
        COMMON_FIELDS + ["smartvault_owner", "guardian_count", "guardians", "wallet_factory", "master_copy", "version_hint", "is_recovering", "locked", "recovery_delay_seconds"]
        + CREATION_FIELDS + FUNDING_FIELDS + ACTIVITY_FIELDS,
    )

    # validators
    validator_rows = []
    for row in all_rows:
        det = details[row["address"].lower()]
        v = det["validator"]
        if not v.get("is_validator"):
            continue
        validator_rows.append(dict(row, **{
            "validator_name": v.get("name") or "",
            "validator_identity": v.get("identity") or "",
            "validator_website": v.get("website") or "",
            "active_status": v.get("active_status") or "",
            "epos_status": v.get("epos_status") or "",
            "validator_creation_height": v.get("creation_height"),
            "validator_total_delegation_one": one(v.get("total_delegation_atto")),
            "validator_delegation_count": v.get("delegation_count"),
            "bls_key_count": v.get("bls_key_count"),
        }))
    write_csv(
        os.path.join(args.output_dir, "validator-accounts.csv"),
        validator_rows,
        ["validator_name", "validator_identity", "validator_website", "active_status", "epos_status"] + COMMON_FIELDS + [
            "validator_creation_height", "creation_time_utc", "creation_tx_hash", "validator_total_delegation_one",
            "validator_delegation_count", "bls_key_count", "code_size_bytes",
        ] + ACTIVITY_FIELDS,
    )

    # multisigs
    ms_rows = []
    for row in all_rows:
        det = details[row["address"].lower()]
        safe = det["safe"]
        if not safe:
            continue
        ms_rows.append(dict(row, **{
            "multisig_kind": safe["kind"],
            "safe_version": safe["version"],
            "threshold": safe["threshold"],
            "owner_count": len(safe["owners"]),
            "owners": ",".join(lib.to_checksum(o) for o in safe["owners"]),
            "safe_nonce": safe.get("nonce"),
            "master_copy": csv_addr(safe.get("master_copy")),
            "master_copy_label": safe.get("master_copy_label") or (extra.get(safe.get("master_copy") or "", {}) or {}).get("singleton_label") or "",
        }))
    write_csv(
        os.path.join(args.output_dir, "multisig-wallets.csv"),
        ms_rows,
        COMMON_FIELDS + ["multisig_kind", "safe_version", "master_copy", "master_copy_label", "threshold", "owner_count", "owners", "safe_nonce", "known_app", "known_app_role"]
        + CREATION_FIELDS + FUNDING_FIELDS + ACTIVITY_FIELDS,
    )

    # 1wallets
    ow_rows = []
    for row in all_rows:
        det = details[row["address"].lower()]
        w = det["wallet"]
        if not w:
            continue
        ow_rows.append(dict(row, **{
            "onewallet_version": f"{w['major']}.{w['minor']}" if w.get("major") is not None else "",
            "recovery_address": csv_addr(w["last_resort"]) if w["recovery_address_set"] else "",
            "recovery_address_raw": csv_addr(w["last_resort"]),
            "recovery_is_treasury": (w["last_resort"] in ONEWALLET_TREASURIES),
            "forward_address": csv_addr(w.get("forward_address")),
            "wallet_effective_time_utc": iso(w["effective_time"]),
            "wallet_expiry_time_utc": iso(w["expiry_time"]),
            "otp_interval_seconds": w["interval"],
            "merkle_height": w["height"],
            "daily_limit_one": one(w["daily_limit_atto"]),
            "last_operation_time_utc": iso(w.get("last_operation_time")) if w.get("last_operation_time") else "",
        }))
    write_csv(
        os.path.join(args.output_dir, "onewallets.csv"),
        ow_rows,
        COMMON_FIELDS + ["onewallet_version", "recovery_address", "recovery_address_raw", "recovery_is_treasury", "forward_address",
                         "wallet_effective_time_utc", "wallet_expiry_time_utc", "otp_interval_seconds", "merkle_height",
                         "daily_limit_one", "last_operation_time_utc"]
        + CREATION_FIELDS + FUNDING_FIELDS + ACTIVITY_FIELDS,
    )

    # ERC-20
    erc20_rows = []
    for row in all_rows:
        det = details[row["address"].lower()]
        t = det["token"]
        if not t or not t.get("standard", "").startswith("ERC-20"):
            continue
        decimals = t.get("decimals")
        supply = t.get("total_supply")
        supply_units = ""
        if supply is not None and decimals is not None and 0 <= decimals <= 36:
            whole, frac = divmod(supply, 10 ** decimals)
            supply_units = f"{whole}.{frac:0{decimals}d}" if decimals else str(whole)
        erc20_rows.append(dict(row, **{
            "token_standard": t["standard"],
            "decimals": decimals if decimals is not None else "",
            "total_supply_raw": supply if supply is not None else "",
            "total_supply_units": supply_units,
            "primary_category_note": row["primary_category"],
        }))
    write_csv(
        os.path.join(args.output_dir, "erc20-tokens.csv"),
        erc20_rows,
        ["token_name", "token_symbol", "token_standard", "decimals", "total_supply_raw", "total_supply_units", "known_app", "known_app_role", "primary_category_note", "owner_probe", "proxy_type", "proxy_implementation"]
        + COMMON_FIELDS + CREATION_FIELDS + ACTIVITY_FIELDS,
    )

    # NFT
    nft_rows = []
    for row in all_rows:
        det = details[row["address"].lower()]
        t = det["token"]
        if not t or not (t.get("erc721") or t.get("erc1155") or t.get("nft_other")):
            continue
        ex = extra.get(row["address"].lower()) or {}
        nft_rows.append(dict(row, **{
            "token_standard": t["standard"],
            "erc721_enumerable": t.get("erc721_enumerable"),
            "erc721_metadata": t.get("erc721_metadata"),
            "total_supply_raw": t.get("total_supply") if t.get("total_supply") is not None else "",
            "owner_count": ex.get("nft_owner_count", ""),
            "owner_count_method": ex.get("nft_owner_method", ""),
            "tokens_enumerated": ex.get("nft_tokens_enumerated", ""),
        }))
    write_csv(
        os.path.join(args.output_dir, "nft-contracts.csv"),
        nft_rows,
        ["token_name", "token_symbol", "token_standard", "erc721_enumerable", "erc721_metadata", "total_supply_raw", "owner_count", "owner_count_method", "tokens_enumerated", "known_app", "known_app_role", "owner_probe"]
        + COMMON_FIELDS + CREATION_FIELDS + ACTIVITY_FIELDS,
    )

    # known apps
    app_rows = []
    for row in all_rows:
        det = details[row["address"].lower()]
        a = det["app"]
        if not a:
            continue
        meta = registry["apps"][a["app"]]
        app_rows.append(dict(row, **{
            "app_id": a["app"],
            "app_name": meta["name"],
            "role": a["role"],
            "evidence": a["evidence"],
            "reference_docs": meta.get("docs", ""),
            "reference_source": meta.get("source", ""),
            "reference_address_book": meta.get("address_book", ""),
        }))
    write_csv(
        os.path.join(args.output_dir, "known-app-contracts.csv"),
        app_rows,
        ["app_name", "role", "evidence", "reference_docs", "reference_source", "reference_address_book", "token_name", "token_symbol", "primary_category"]
        + COMMON_FIELDS + CREATION_FIELDS + ACTIVITY_FIELDS,
    )

    # pattern-identified (type known from function fingerprints, operator unknown)
    pattern_rows = [r for r in all_rows if r["primary_category"] == "pattern-identified"]
    write_csv(
        os.path.join(args.output_dir, "pattern-identified-contracts.csv"),
        pattern_rows,
        ["pattern_category", "pattern_label", "owner_probe", "proxy_type", "proxy_implementation", "code_size_bytes"]
        + COMMON_FIELDS + CREATION_FIELDS + FUNDING_FIELDS + ACTIVITY_FIELDS + ["dispatcher_signatures"],
    )

    # unidentified
    unid_rows = [r for r in all_rows if r["primary_category"] == "unidentified"]
    write_csv(
        os.path.join(args.output_dir, "unidentified-contracts.csv"),
        unid_rows,
        ["subcategory", "token_name", "token_symbol", "owner_probe", "proxy_type", "proxy_implementation", "code_size_bytes", "code_hash"]
        + COMMON_FIELDS + CREATION_FIELDS + FUNDING_FIELDS + ACTIVITY_FIELDS + ["dispatcher_signatures"],
    )

    # summary
    def bucket(rows, key):
        out = {}
        for r in rows:
            k = r[key]
            b = out.setdefault(k, {
                "count": 0,
                "total_claim_atto": 0,
                "wallet_airdrop_atto": 0,
                "staked_to_vault_atto": 0,
                "liquid_atto": 0,
                "staked_atto": 0,
                "latest_balance_atto": 0,
            })
            b["count"] += 1
            b["total_claim_atto"] += lib.one_str_to_atto(
                r["total_claim_one"]
            )
            b["wallet_airdrop_atto"] += lib.one_str_to_atto(
                r["wallet_airdrop_one"]
            )
            b["staked_to_vault_atto"] += lib.one_str_to_atto(
                r["staked_to_vault_one"]
            )
            b["liquid_atto"] += lib.one_str_to_atto(
                r["liquid_total_one"]
            )
            b["staked_atto"] += lib.one_str_to_atto(
                r["active_staked_or_delegated_one"]
            )
            b["latest_balance_atto"] += lib.one_str_to_atto(
                r["balance_latest_one"]
            )
        for b in out.values():
            for k in list(b):
                if k.endswith("_atto"):
                    b[k.replace("_atto", "_one")] = lib.atto_to_one_str(b[k])
                    del b[k]
        return out

    summary = {
        "input_rows": len(all_rows),
        "primary_categories": bucket(all_rows, "primary_category"),
        "subcategories": bucket(all_rows, "subcategory"),
        "overlapping": {
            "validator_accounts": len(validator_rows),
            "multisig_wallets": len(ms_rows),
            "onewallets": len(ow_rows),
            "erc20_tokens": len(erc20_rows),
            "nft_contracts": len(nft_rows),
            "known_app_contracts": len(app_rows),
            "smartvault_wallets": len(sv_rows),
            "pattern_identified": len(pattern_rows),
            "unidentified": len(unid_rows),
        },
        "pattern_categories": bucket(pattern_rows, "pattern_category"),
        "known_apps": bucket(app_rows, "app_name"),
        "erc20_by_primary": bucket(erc20_rows, "primary_category"),
        "nft_by_standard": bucket(nft_rows, "token_standard"),
        "multisig_by_version": bucket(ms_rows, "subcategory"),
        "onewallet_by_version": bucket(ow_rows, "subcategory"),
    }
    lib.dump_json(os.path.join(args.output_dir, "summary.json"), summary)
    print(json.dumps({k: {kk: vv["count"] for kk, vv in v.items()} if isinstance(v, dict) and k in ("primary_categories",) else v for k, v in summary.items() if k in ("input_rows", "primary_categories", "overlapping")}, indent=1))


if __name__ == "__main__":
    main()
