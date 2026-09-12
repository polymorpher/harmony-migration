#!/usr/bin/env python3
"""Stage 1: collect primary-source facts for every code-bearing claim address.

Everything is read from a Harmony shard-0 archival node over JSON-RPC:

- runtime code at the cutoff block and at the latest block;
- latest liquid balance, nonce, storage slot 0 and EIP-1967 slots;
- direct transaction counts and first/last direct transactions
  (``hmyv2_getTransactionsCount`` / ``hmyv2_getTransactionsHistory``);
- creation block located by binary search on ``eth_getCode`` and the
  creating transaction recovered from ``trace_block`` (falls back to block
  receipts for plain CREATE transactions);
- the first inbound value transfer (funding) recovered from the earliest
  direct value transfer plus a balance binary search and ``trace_block``;
- a fixed set of ``eth_call`` ABI probes used later for classification;
- current delegations through ``hmyv2_getDelegationsByDelegator``.

The output is a single JSON document keyed by lowercase address. The script
is resumable: facts already present are not fetched again unless
``--refresh`` is given.
"""

import argparse
import concurrent.futures
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract_review_lib as lib  # noqa: E402

EMPTY_CODE_HASH = "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
DEAD = "0x000000000000000000000000000000000000dead"
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
EIP1967_BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"

# (label, signature, [(type, value), ...])
PROBES = [
    # Gnosis Safe / legacy Gnosis MultiSigWallet
    ("safe_getOwners", "getOwners()", []),
    ("safe_getThreshold", "getThreshold()", []),
    ("safe_VERSION", "VERSION()", []),
    ("safe_nonce", "nonce()", []),
    ("safe_masterCopy", "masterCopy()", []),
    ("safe_domainSeparator", "domainSeparator()", []),
    ("msw_required", "required()", []),
    ("msw_transactionCount", "transactionCount()", []),
    # 1wallet (all versions expose getInfo(); getVersion() since v2+)
    ("ow_getInfo", "getInfo()", []),
    ("ow_getVersion", "getVersion()", []),
    ("ow_getForwardAddress", "getForwardAddress()", []),
    ("ow_getIdentificationKeys", "getIdentificationKeys()", []),
    ("ow_identificationKey", "identificationKey()", []),
    ("ow_getRootKey", "getRootKey()", []),
    ("ow_lastOperationTime", "lastOperationTime()", []),
    ("ow_getSpendingState", "getSpendingState()", []),
    ("ow_getCurrentSpendingState", "getCurrentSpendingState()", []),
    ("ow_getCurrentSpending", "getCurrentSpending()", []),
    ("ow_getBacklinks", "getBacklinks()", []),
    ("ow_getNonce", "getNonce()", []),
    # ERC-20 / generic token metadata
    ("erc20_name", "name()", []),
    ("erc20_symbol", "symbol()", []),
    ("erc20_decimals", "decimals()", []),
    ("erc20_totalSupply", "totalSupply()", []),
    ("erc20_balanceOf_dead", "balanceOf(address)", [("address", DEAD)]),
    ("erc20_allowance_dead", "allowance(address,address)", [("address", DEAD), ("address", DEAD)]),
    ("owner", "owner()", []),
    ("getOwner", "getOwner()", []),
    # ERC-165 / NFT
    ("erc165_165", "supportsInterface(bytes4)", [("bytes4", "0x01ffc9a7")]),
    ("erc165_721", "supportsInterface(bytes4)", [("bytes4", "0x80ac58cd")]),
    ("erc165_721enum", "supportsInterface(bytes4)", [("bytes4", "0x780e9d63")]),
    ("erc165_721meta", "supportsInterface(bytes4)", [("bytes4", "0x5b5e139f")]),
    ("erc165_1155", "supportsInterface(bytes4)", [("bytes4", "0xd9b67a26")]),
    ("erc165_1155meta", "supportsInterface(bytes4)", [("bytes4", "0x0e89341c")]),
    ("erc165_ffffffff", "supportsInterface(bytes4)", [("bytes4", "0xffffffff")]),
    ("nft_ownerOf_1", "ownerOf(uint256)", [("uint256", 1)]),
    ("nft_ownerOf_0", "ownerOf(uint256)", [("uint256", 0)]),
    ("nft_tokenURI_1", "tokenURI(uint256)", [("uint256", 1)]),
    ("nft_uri_1", "uri(uint256)", [("uint256", 1)]),
    ("nft_tokenByIndex_0", "tokenByIndex(uint256)", [("uint256", 0)]),
    ("erc1155_balanceOf_dead_1", "balanceOf(address,uint256)", [("address", DEAD), ("uint256", 1)]),
    # AMM pairs / pools
    ("amm_factory", "factory()", []),
    ("amm_token0", "token0()", []),
    ("amm_token1", "token1()", []),
    ("amm_getReserves", "getReserves()", []),
    ("amm_kLast", "kLast()", []),
    ("univ3_fee", "fee()", []),
    ("univ3_slot0", "slot0()", []),
    ("univ3_liquidity", "liquidity()", []),
    ("univ3_tickSpacing", "tickSpacing()", []),
    ("amm_WETH", "WETH()", []),
    ("amm_router_factory", "factory()", []),
    ("univ2_factory_allPairsLength", "allPairsLength()", []),
    ("univ2_factory_feeTo", "feeTo()", []),
    ("univ2_factory_feeToSetter", "feeToSetter()", []),
    # Lending
    ("aave_POOL", "POOL()", []),
    ("aave_UNDERLYING", "UNDERLYING_ASSET_ADDRESS()", []),
    ("aave_ADDRESSES_PROVIDER", "ADDRESSES_PROVIDER()", []),
    ("aave_getPool", "getPool()", []),
    ("aave_getPoolDataProvider", "getPoolDataProvider()", []),
    ("comp_comptroller", "comptroller()", []),
    ("comp_underlying", "underlying()", []),
    ("comp_isCToken", "isCToken()", []),
    ("comp_isComptroller", "isComptroller()", []),
    ("comp_interestRateModel", "interestRateModel()", []),
    ("comp_admin", "admin()", []),
    ("comp_oracle", "oracle()", []),
    ("comp_getAllMarkets", "getAllMarkets()", []),
    # Farming / staking
    ("chef_sushi", "sushi()", []),
    ("chef_SUSHI", "SUSHI()", []),
    ("chef_rewardToken", "rewardToken()", []),
    ("chef_govToken", "govToken()", []),
    ("chef_poolLength", "poolLength()", []),
    ("chef_MASTER_CHEF", "MASTER_CHEF()", []),
    ("chef_stakingToken", "stakingToken()", []),
    ("chef_rewardsToken", "rewardsToken()", []),
    ("chef_lpToken", "lpToken()", []),
    ("chef_startBlock", "startBlock()", []),
    # Vaults / misc
    ("vault_want", "want()", []),
    ("vault_token", "token()", []),
    ("vault_asset", "asset()", []),
    ("vault_strategy", "strategy()", []),
    ("vault_vault", "vault()", []),
    ("vault_getPricePerFullShare", "getPricePerFullShare()", []),
    ("misc_paused", "paused()", []),
    ("misc_beneficiary", "beneficiary()", []),
    ("misc_start", "start()", []),
    ("misc_duration", "duration()", []),
    ("misc_cliff", "cliff()", []),
    ("misc_delay", "delay()", []),
    ("misc_MINIMUM_DELAY", "MINIMUM_DELAY()", []),
    ("misc_implementation", "implementation()", []),
    ("misc_wallet", "wallet()", []),
    ("misc_treasury", "treasury()", []),
    ("misc_governance", "governance()", []),
    ("misc_operator", "operator()", []),
    ("misc_epoch", "epoch()", []),
    ("misc_baseURI", "baseURI()", []),
    ("misc_contractURI", "contractURI()", []),
    ("misc_maxSupply", "maxSupply()", []),
    ("misc_MAX_SUPPLY", "MAX_SUPPLY()", []),
    ("misc_cost", "cost()", []),
    ("misc_price", "price()", []),
    ("misc_mintPrice", "mintPrice()", []),
    ("misc_getPrice", "getPrice()", []),
    ("misc_totalStaked", "totalStaked()", []),
    ("misc_registry", "registry()", []),
    ("misc_resolver", "resolver()", []),
    ("misc_ens", "ens()", []),
    ("misc_priceOracle", "priceOracle()", []),
    ("misc_baseNode", "baseNode()", []),
    ("misc_lzEndpoint", "lzEndpoint()", []),
    ("misc_endpoint", "endpoint()", []),
    ("misc_bridge", "bridge()", []),
    ("misc_WONE", "WONE()", []),
    ("misc_wone", "wone()", []),
    ("misc_jewel", "jewel()", []),
    ("misc_heroCore", "heroCoreContract()", []),
    ("misc_profiles", "profiles()", []),
    ("misc_quest", "questCore()", []),
    ("misc_PROXY_ADMIN", "proxyAdmin()", []),
    ("misc_stakingPool", "stakingPool()", []),
    ("misc_exchangeRate", "exchangeRate()", []),
    ("misc_getExchangeRate", "getExchangeRate()", []),
    ("misc_totalAssets", "totalAssets()", []),
    ("misc_lockedUntil", "lockedUntil()", []),
    ("misc_unlockTime", "unlockTime()", []),
    ("misc_releaseTime", "releaseTime()", []),
    ("misc_end", "end()", []),
    ("misc_token0Symbol", "getToken0Symbol()", []),
    ("staking_precompile_users_validator", "validator()", []),
    ("staking_validatorAddress", "validatorAddress()", []),
    ("staking_delegations", "getDelegations()", []),
]

PROBE_CALLDATA = {label: lib.encode_call(sig, *args) for label, sig, args in PROBES}

# Bytecode selector heuristics, checked locally against runtime code
BYTECODE_SELECTORS = {
    "transfer(address,uint256)": "sel_transfer",
    "transferFrom(address,address,uint256)": "sel_transferFrom",
    "approve(address,uint256)": "sel_approve",
    "allowance(address,address)": "sel_allowance",
    "balanceOf(address)": "sel_balanceOf",
    "totalSupply()": "sel_totalSupply",
    "ownerOf(uint256)": "sel_ownerOf",
    "safeTransferFrom(address,address,uint256)": "sel_safeTransferFrom721",
    "safeTransferFrom(address,address,uint256,bytes)": "sel_safeTransferFrom721data",
    "safeTransferFrom(address,address,uint256,uint256,bytes)": "sel_safeTransferFrom1155",
    "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)": "sel_safeBatchTransferFrom",
    "balanceOf(address,uint256)": "sel_balanceOf1155",
    "balanceOfBatch(address[],uint256[])": "sel_balanceOfBatch",
    "setApprovalForAll(address,bool)": "sel_setApprovalForAll",
    "isApprovedForAll(address,address)": "sel_isApprovedForAll",
    "tokenURI(uint256)": "sel_tokenURI",
    "uri(uint256)": "sel_uri",
    "supportsInterface(bytes4)": "sel_supportsInterface",
    "commit(bytes32,bytes32,bytes32)": "sel_ow_commit3",
    "commit(bytes32)": "sel_ow_commit1",
    "getInfo()": "sel_ow_getInfo",
    "reveal(bytes32[],uint32,bytes32,uint8,uint8,address,uint256,address,uint256,bytes)": "sel_ow_reveal_legacy",
    "getOwners()": "sel_getOwners",
    "getThreshold()": "sel_getThreshold",
    "execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)": "sel_safe_execTransaction",
    "masterCopy()": "sel_masterCopy",
    "swap(uint256,uint256,address,bytes)": "sel_univ2_swap",
    "swap(address,bool,int256,uint160,bytes)": "sel_univ3_swap",
    "mint(address)": "sel_univ2_mint",
    "burn(address)": "sel_univ2_burn",
    "getReserves()": "sel_getReserves",
    "createPair(address,address)": "sel_createPair",
    "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)": "sel_router_swapExact",
    "deposit(uint256,uint256)": "sel_chef_deposit",
    "withdraw(uint256,uint256)": "sel_chef_withdraw",
    "emergencyWithdraw(uint256)": "sel_chef_emergencyWithdraw",
    "mint(uint256)": "sel_ctoken_mint",
    "redeem(uint256)": "sel_ctoken_redeem",
    "borrow(uint256)": "sel_ctoken_borrow",
    "supply(address,uint256,address,uint16)": "sel_aave_supply",
    "flashLoanSimple(address,address,uint256,bytes,uint16)": "sel_aave_flashLoanSimple",
    "deposit()": "sel_weth_deposit",
    "withdraw(uint256)": "sel_weth_withdraw",
    "execute(address,uint256,bytes)": "sel_execute",
    "delegate(address,uint256)": "sel_stk_delegate",
    "undelegate(address,uint256)": "sel_stk_undelegate",
    "collectRewards()": "sel_stk_collectRewards",
    "multicall(bytes[])": "sel_multicall",
    "initialize()": "sel_initialize",
    "upgradeTo(address)": "sel_upgradeTo",
    "release()": "sel_release",
    "claim()": "sel_claim",
}
BYTECODE_SELECTOR_HEX = {lib.selector(sig)[2:]: name for sig, name in BYTECODE_SELECTORS.items()}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="claims CSV with an `address` column (code-bearing rows)")
    parser.add_argument("--rpc", default="https://a.api.s0.t.hmny.io", help="shard-0 archival RPC endpoint")
    parser.add_argument("--cutoff-block", type=int, default=93623067)
    parser.add_argument("--output", required=True, help="facts JSON output")
    parser.add_argument("--refresh", action="store_true", help="ignore cached facts and refetch everything")
    parser.add_argument("--only", help="comma separated stage list: basic,validator,history,creation,funding,probes,delegations")
    parser.add_argument("--limit", type=int, default=0, help="only process the first N addresses (debug)")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=40)
    return parser.parse_args()


def load_addresses(path, limit):
    out = []
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            address = lib.normalize_address(row.get("address"))
            if address:
                out.append(address)
    if limit:
        out = out[:limit]
    return out


def log(message):
    sys.stderr.write(time.strftime("%H:%M:%S ") + message + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Stage: basic
# --------------------------------------------------------------------------


def stage_basic(client, facts, addresses, cutoff_hex):
    todo = [a for a in addresses if "basic" not in facts[a]]
    if not todo:
        return
    log(f"basic: {len(todo)} addresses")
    calls = []
    for a in todo:
        calls.extend([
            ("eth_getCode", [a, cutoff_hex]),
            ("eth_getCode", [a, "latest"]),
            ("eth_getBalance", [a, "latest"]),
            ("eth_getBalance", [a, cutoff_hex]),
            ("eth_getTransactionCount", [a, "latest"]),
            ("eth_getStorageAt", [a, "0x0", "latest"]),
            ("eth_getStorageAt", [a, EIP1967_IMPL_SLOT, "latest"]),
            ("eth_getStorageAt", [a, EIP1967_ADMIN_SLOT, "latest"]),
            ("eth_getStorageAt", [a, EIP1967_BEACON_SLOT, "latest"]),
            ("hmyv2_getTransactionsCount", [a, "ALL"]),
            ("hmyv2_getTransactionsCount", [a, "SENT"]),
            ("hmyv2_getTransactionsCount", [a, "RECEIVED"]),
            ("hmyv2_getStakingTransactionsCount", [a, "ALL"]),
        ])
    per = 13
    results = client.batch(calls)
    latest_block = client.call("eth_blockNumber", [])
    for idx, a in enumerate(todo):
        chunk = results[idx * per:(idx + 1) * per]
        values = [r for r, _ in chunk]
        errors = [e for _, e in chunk]
        code_cutoff = values[0] or "0x"
        code_latest = values[1] or "0x"
        code_bytes = bytes.fromhex(code_cutoff[2:]) if code_cutoff.startswith("0x") else b""
        code_hash = "0x" + lib.keccak256(code_bytes).hex()
        code_lower = code_cutoff.lower()
        selectors_present = sorted(
            name for sel, name in BYTECODE_SELECTOR_HEX.items() if ("63" + sel) in code_lower
        )
        facts[a]["basic"] = {
            "code_cutoff_len": len(code_bytes),
            "code_cutoff_hash": code_hash,
            "code_cutoff": code_cutoff,
            "code_latest_len": (len(code_latest) - 2) // 2 if code_latest.startswith("0x") else 0,
            "code_latest_same": code_latest.lower() == code_lower,
            "balance_latest_atto": str(lib.hex_to_int(values[2])),
            "balance_cutoff_atto": str(lib.hex_to_int(values[3])),
            "nonce_latest": lib.hex_to_int(values[4]),
            "storage_slot0": values[5],
            "eip1967_impl": lib.decode_address(values[6]) if values[6] else None,
            "eip1967_admin": lib.decode_address(values[7]) if values[7] else None,
            "eip1967_beacon": lib.decode_address(values[8]) if values[8] else None,
            "tx_count_all": values[9],
            "tx_count_sent": values[10],
            "tx_count_received": values[11],
            "staking_tx_count_all": values[12],
            "latest_block_at_fetch": lib.hex_to_int(latest_block),
            "bytecode_selectors": selectors_present,
            "errors": [e for e in errors if e],
        }


# --------------------------------------------------------------------------
# Stage: history (first / last direct transactions)
# --------------------------------------------------------------------------


def history_params(address, order, page_size=1, page_index=0, tx_type="ALL"):
    return [{
        "address": address,
        "pageIndex": page_index,
        "pageSize": page_size,
        "fullTx": True,
        "txType": tx_type,
        "order": order,
    }]


def summarize_tx(tx):
    if not tx:
        return None
    return {
        "hash": tx.get("ethHash") or tx.get("hash"),
        "hmy_hash": tx.get("hash"),
        "block": tx.get("blockNumber"),
        "timestamp": tx.get("timestamp"),
        "from": lib.any_to_hex(tx.get("from")),
        "to": lib.any_to_hex(tx.get("to")),
        "value_atto": str(tx.get("value", 0)),
        "input_selector": (tx.get("input") or "0x")[:10],
        "shard": tx.get("shardID"),
        "to_shard": tx.get("toShardID"),
        "type": tx.get("type"),
    }


# --------------------------------------------------------------------------
# Stage: validator (Harmony stores ValidatorWrapper RLP in the code field)
# --------------------------------------------------------------------------


def stage_validator(client, facts, addresses):
    todo = [a for a in addresses if "validator" not in facts[a]]
    if not todo:
        return
    log(f"validator: {len(todo)} addresses")
    calls = [("hmyv2_getValidatorInformation", [a]) for a in todo]
    results = client.batch(calls)
    validators = []
    for a, (result, error) in zip(todo, results):
        rlp_address = lib.rlp_validator_wrapper_address(facts[a]["basic"]["code_cutoff"])
        entry = {"rlp_wrapper_address": rlp_address, "rlp_wrapper_matches": rlp_address == a}
        if error or not result:
            entry["is_validator"] = False
            entry["error"] = (error or {}).get("message", "")[:160] if isinstance(error, dict) else str(error)[:160]
        else:
            validator = result.get("validator") or {}
            entry.update({
                "is_validator": True,
                "name": validator.get("name"),
                "identity": validator.get("identity"),
                "website": validator.get("website"),
                "details": (validator.get("details") or "")[:200],
                "creation_height": validator.get("creation-height"),
                "last_epoch_in_committee": validator.get("last-epoch-in-committee"),
                "active_status": result.get("active-status"),
                "epos_status": result.get("epos-status"),
                "total_delegation_atto": str(result.get("total-delegation", 0)),
                "delegation_count": len(validator.get("delegations") or []),
                "bls_key_count": len(validator.get("bls-public-keys") or []),
                "commission_rate": ((validator.get("rate") or "")),
            })
            validators.append(a)
        facts[a]["validator"] = entry
    if validators:
        calls = [("hmyv2_getStakingTransactionsHistory", history_params(a, "ASC")) for a in validators]
        results = client.batch(calls)
        for a, (result, error) in zip(validators, results):
            txs = (result or {}).get("staking_transactions") or []
            facts[a]["validator"]["first_staking_tx"] = summarize_tx(txs[0]) if txs else None
            if error:
                facts[a]["validator"]["staking_history_error"] = error


def is_validator(facts, address):
    return bool(facts[address].get("validator", {}).get("is_validator"))


HASH_PREFIX_CAP = 400
VALUE_SCAN_CAP = 1500


def hash_sorted_prefix(hashes):
    """Length of the leading run of ascending hashes.

    The node's per-address index key is (block, index, hash); legacy entries
    migrated with an unknown block are stored with block 0 and therefore sort
    first, ordered by hash. The rest of the list is ordered by block.
    """
    n = 0
    while n + 1 < len(hashes) and hashes[n].lower() < hashes[n + 1].lower():
        n += 1
    return n + 1 if hashes else 0


def fetch_full_txs(client, hashes):
    if not hashes:
        return []
    results = client.batch([("hmyv2_getTransactionByHash", [h]) for h in hashes])
    return [r for r, e in results if r]


def tx_sort_key(tx):
    return (tx.get("blockNumber") or 0, tx.get("transactionIndex") or 0)


def earliest_direct_tx(client, address, tx_type, require_value):
    """Earliest direct tx (optionally with value > 0) for an address.

    Returns (tx_or_None, info dict). Fetches the complete hash list (cheap),
    resolves the hash-sorted legacy prefix fully, then walks the block-sorted
    remainder in order until a qualifying tx is found.
    """
    listing, error = client.call_soft(
        "hmyv2_getTransactionsHistory",
        [{"address": address, "pageIndex": 0, "pageSize": 5_000_000, "fullTx": False, "txType": tx_type, "order": "ASC"}],
    )
    info = {"tx_type": tx_type}
    if error:
        info["error"] = error
        return None, info
    hashes = (listing or {}).get("transactions") or []
    info["hash_count"] = len(hashes)
    if not hashes:
        return None, info
    prefix_len = hash_sorted_prefix(hashes)
    info["legacy_prefix_len"] = prefix_len
    truncated_prefix = prefix_len > HASH_PREFIX_CAP
    prefix = hashes[:min(prefix_len, HASH_PREFIX_CAP)]
    candidates = [tx for tx in fetch_full_txs(client, prefix) if not require_value or int(tx.get("value") or 0) > 0]
    remainder = hashes[prefix_len:]
    scanned = 0
    best_remainder = None
    for offset in range(0, min(len(remainder), VALUE_SCAN_CAP), 50):
        chunk = fetch_full_txs(client, remainder[offset:offset + 50])
        scanned += len(chunk)
        chunk.sort(key=tx_sort_key)
        for tx in chunk:
            if not require_value or int(tx.get("value") or 0) > 0:
                best_remainder = tx
                break
        if best_remainder:
            break
    info["remainder_scanned"] = scanned
    info["remainder_exhausted"] = scanned >= len(remainder)
    info["reliable"] = (not truncated_prefix) and (best_remainder is not None or scanned >= len(remainder))
    if best_remainder:
        candidates.append(best_remainder)
    if not candidates:
        return None, info
    return min(candidates, key=tx_sort_key), info


def stage_history(client, facts, addresses):
    todo = [a for a in addresses if "history" not in facts[a]]
    if not todo:
        return
    log(f"history: {len(todo)} addresses")
    calls = []
    for a in todo:
        calls.append(("hmyv2_getTransactionsHistory", history_params(a, "DESC")))
        calls.append(("hmyv2_getStakingTransactionsHistory", history_params(a, "DESC")))
    results = client.batch(calls)

    def earliest_pair(a):
        try:
            first, first_info = earliest_direct_tx(client, a, "ALL", False)
            if is_validator(facts, a):
                return first, first_info, None, {"skipped": "validator account"}
            value, value_info = earliest_direct_tx(client, a, "RECEIVED", True)
            return first, first_info, value, value_info
        except lib.RpcError as error:
            return None, {"error": str(error)}, None, {"error": str(error)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=client.workers) as pool:
        earliest = dict(zip(todo, pool.map(earliest_pair, todo)))

    for idx, a in enumerate(todo):
        last, e2 = results[idx * 2]
        stk, e3 = results[idx * 2 + 1]
        last_tx = (last or {}).get("transactions") or []
        stk_tx = (stk or {}).get("staking_transactions") or []
        first, first_info, value, value_info = earliest[a]
        if first_info.get("error") and "transport" in str(first_info.get("error")):
            # leave unset so a resumed run retries this address
            continue
        facts[a]["history"] = {
            "first_direct_tx": summarize_tx(first),
            "first_direct_tx_info": first_info,
            "first_direct_value_received_tx": summarize_tx(value),
            "first_direct_value_received_info": value_info,
            "last_direct_tx": summarize_tx(last_tx[0]) if last_tx else None,
            "last_staking_tx": summarize_tx(stk_tx[0]) if stk_tx else None,
            "errors": [e for e in (e2, e3) if e],
        }


# --------------------------------------------------------------------------
# Stage: creation (binary search on eth_getCode + trace_block)
# --------------------------------------------------------------------------


def binary_search_rounds(client, items, make_call, predicate, label):
    """Generic parallel binary search.

    items: dict address -> (lo, hi) where predicate(lo) is False (or lo == -1)
    and predicate(hi) is True. Finds the smallest block where predicate holds.
    Returns dict address -> block.
    """
    state = {a: [lo, hi] for a, (lo, hi) in items.items()}
    rounds = 0
    while True:
        active = [a for a, (lo, hi) in state.items() if hi - lo > 1]
        if not active:
            break
        rounds += 1
        calls = []
        mids = {}
        for a in active:
            lo, hi = state[a]
            mid = (lo + hi) // 2
            mids[a] = mid
            calls.append(make_call(a, mid))
        results = client.batch(calls)
        for a, (result, error) in zip(active, results):
            if error:
                # keep hi as-is but remember the error; retry this address next round
                state[a].append(("error", mids[a], error))
                continue
            if predicate(result):
                state[a][1] = mids[a]
            else:
                state[a][0] = mids[a]
        log(f"{label}: round {rounds}, {len(active)} active")
        if rounds > 60:
            break
    return {a: st[1] for a, st in state.items()}


def has_code(result):
    return bool(result) and result not in ("0x", "0x0")


def trace_block_cached(client, block, cache):
    if block in cache:
        return cache[block]
    traces, error = client.call_soft("trace_block", [hex(block)])
    if error:
        cache[block] = {"error": error}
    else:
        cache[block] = {"traces": traces or []}
    return cache[block]


def block_header_cached(client, block, cache):
    if block in cache:
        return cache[block]
    header, error = client.call_soft("eth_getBlockByNumber", [hex(block), False])
    cache[block] = header or {"error": error}
    return cache[block]


def find_create_trace(traces, address):
    address = address.lower()
    for trace in traces:
        if trace.get("type") != "create":
            continue
        result = trace.get("result") or {}
        if (result.get("address") or "").lower() == address:
            return trace
    return None


def top_level_trace(traces, tx_position):
    for trace in traces:
        if trace.get("transactionPosition") == tx_position and not trace.get("traceAddress"):
            return trace
    return None


def validator_creation(client, facts, address):
    """Creation of a validator account = its CreateValidator staking transaction.

    The creation height comes from the validator wrapper itself
    (`hmyv2_getValidatorInformation`), which is exact; the transaction is
    then located in that block's staking transactions.
    """
    height = (facts[address].get("validator") or {}).get("creation_height")
    entry = {"method": "validator account: CreateValidator staking transaction at wrapper creation-height"}
    if height is None:
        entry["error"] = "validator information lacks creation-height"
        return entry
    height = int(height)
    entry["block"] = height
    block, error = client.call_soft("hmyv2_getBlockByNumber", [height, {"fullTx": True, "inclStaking": True}])
    if error or not block:
        entry["trace_error"] = f"block fetch failed: {error}"
        return entry
    entry["timestamp"] = int(block.get("timestamp") or 0) or None
    entry["block_hash"] = block.get("hash")
    for tx in block.get("stakingTransactions") or []:
        if (tx.get("type") or "").lower() == "createvalidator" and lib.any_to_hex(tx.get("from")) == address:
            entry.update({
                "tx_hash": tx.get("hash"),
                "tx_position": tx.get("transactionIndex"),
                "tx_sender": address,
                "direct_creator": address,
                "tx_top_type": "CreateValidator",
            })
            break
    if "tx_hash" not in entry:
        entry["trace_error"] = "no CreateValidator staking tx from this address found in creation-height block"
    return entry


def stage_creation(client, facts, addresses, cutoff_block):
    todo = [a for a in addresses if "creation" not in facts[a]]
    if not todo:
        return
    log(f"creation: {len(todo)} addresses")
    # predicate: has_code(block). lo = -1 (no code before genesis), hi = cutoff (code present per claims CSV)
    items = {}
    for a in todo:
        if facts[a]["basic"]["code_cutoff_len"] == 0:
            facts[a]["creation"] = {"error": "no code at cutoff block"}
            continue
        if is_validator(facts, a):
            facts[a]["creation"] = validator_creation(client, facts, a)
            continue
        items[a] = (-1, cutoff_block)
    found = binary_search_rounds(
        client,
        items,
        lambda a, mid: ("eth_getCode", [a, hex(mid)]),
        has_code,
        "creation-search",
    )
    trace_cache = {}
    header_cache = {}
    blocks = sorted(set(found.values()))
    log(f"creation: tracing {len(blocks)} distinct creation blocks")
    # prefetch traces and headers with bounded parallelism
    with concurrent.futures.ThreadPoolExecutor(max_workers=client.workers) as pool:
        list(pool.map(lambda b: trace_block_cached(client, b, trace_cache), blocks))
        list(pool.map(lambda b: block_header_cached(client, b, header_cache), blocks))
    for a, block in found.items():
        header = header_cache.get(block) or {}
        timestamp = lib.hex_to_int(header.get("timestamp")) if header.get("timestamp") else None
        entry = {
            "block": block,
            "block_hash": header.get("hash"),
            "timestamp": timestamp,
            "method": "eth_getCode binary search + trace_block",
        }
        traced = trace_cache.get(block) or {}
        traces = traced.get("traces")
        if traces is None:
            entry["trace_error"] = traced.get("error")
        else:
            create = find_create_trace(traces, a)
            if create:
                top = top_level_trace(traces, create.get("transactionPosition"))
                action = create.get("action") or {}
                entry.update({
                    "tx_hash": create.get("transactionHash"),
                    "tx_position": create.get("transactionPosition"),
                    "direct_creator": action.get("from"),
                    "create_value_atto": str(lib.hex_to_int(action.get("value"))),
                    "create_depth": len(create.get("traceAddress") or []),
                    "tx_sender": (top or {}).get("action", {}).get("from"),
                    "tx_to": (top or {}).get("action", {}).get("to"),
                    "tx_top_type": (top or {}).get("type"),
                    "init_code_len": (len((action.get("init") or "0x")) - 2) // 2,
                })
            else:
                entry["trace_error"] = "no create trace matched address in creation block"
        if "tx_hash" not in entry:
            # fallback: plain CREATE transactions expose contractAddress in receipts
            receipts, error = client.call_soft("hmyv2_getBlockByNumber", [block, {"fullTx": True, "inclStaking": False}])
            if receipts and receipts.get("transactions"):
                for tx in receipts["transactions"]:
                    if tx.get("to") in (None, ""):
                        receipt, _ = client.call_soft("hmyv2_getTransactionReceipt", [tx.get("hash")])
                        if receipt and (receipt.get("contractAddress") or "").lower() == a:
                            entry.update({
                                "tx_hash": tx.get("ethHash") or tx.get("hash"),
                                "tx_position": tx.get("transactionIndex"),
                                "direct_creator": tx.get("from"),
                                "tx_sender": tx.get("from"),
                                "tx_to": None,
                                "create_value_atto": str(tx.get("value", 0)),
                                "create_depth": 0,
                                "method": "eth_getCode binary search + receipt contractAddress",
                            })
                            break
        facts[a]["creation"] = entry


# --------------------------------------------------------------------------
# Stage: funding (first inbound value transfer)
# --------------------------------------------------------------------------


def find_first_value_transfer_trace(traces, address):
    address = address.lower()
    ordered = sorted(traces, key=lambda t: (t.get("transactionPosition") or 0, t.get("traceAddress") or []))
    for trace in ordered:
        action = trace.get("action") or {}
        value = lib.hex_to_int(action.get("value"))
        if value <= 0:
            continue
        if trace.get("error"):
            continue
        kind = trace.get("type")
        target = None
        if kind == "call":
            if action.get("callType") in ("call", "callcode", None):
                target = action.get("to")
        elif kind == "create":
            target = (trace.get("result") or {}).get("address")
        elif kind == "suicide":
            target = action.get("refundAddress")
        if target and target.lower() == address:
            return trace
    return None


def stage_funding(client, facts, addresses, cutoff_block):
    todo = [
        a for a in addresses
        if "funding" not in facts[a]
        and "creation" in facts[a]
        and facts[a]["creation"].get("block") is not None
        and not is_validator(facts, a)
    ]
    if not todo:
        return
    log(f"funding: {len(todo)} addresses")
    # 1) balance at creation block, and balance at cutoff
    calls = []
    for a in todo:
        calls.append(("eth_getBalance", [a, hex(facts[a]["creation"]["block"])]))
    results = client.batch(calls)
    balance_at_creation = {a: lib.hex_to_int(r) if not e else None for a, (r, e) in zip(todo, results)}

    # 2) earliest direct RECEIVED value transfer (resolved in the history stage)
    direct = {}
    for a in todo:
        history = facts[a].get("history") or {}
        direct[a] = (history.get("first_direct_value_received_tx"), history.get("first_direct_value_received_info") or {})

    # 3) balance binary search for addresses not funded at creation
    items = {}
    for a in todo:
        creation_block = facts[a]["creation"]["block"]
        if balance_at_creation[a]:
            continue
        cutoff_balance = int(facts[a]["basic"]["balance_cutoff_atto"])
        tx, _ = direct[a]
        hi = None
        if cutoff_balance > 0:
            hi = cutoff_block
        if tx is not None:
            hi = tx["block"] if hi is None else min(hi, tx["block"])
        if hi is None:
            continue
        if hi <= creation_block:
            continue
        items[a] = (creation_block, hi)
    searched = binary_search_rounds(
        client,
        items,
        lambda a, mid: ("eth_getBalance", [a, hex(mid)]),
        lambda r: lib.hex_to_int(r) > 0,
        "funding-search",
    )

    # 4) resolve funding transaction per address via trace_block
    trace_cache = {}
    header_cache = {}
    blocks = set()
    for a in todo:
        if balance_at_creation[a]:
            blocks.add(facts[a]["creation"]["block"])
        elif a in searched:
            blocks.add(searched[a])
    blocks = sorted(blocks)
    log(f"funding: tracing {len(blocks)} distinct funding blocks")
    with concurrent.futures.ThreadPoolExecutor(max_workers=client.workers) as pool:
        list(pool.map(lambda b: trace_block_cached(client, b, trace_cache), blocks))
        list(pool.map(lambda b: block_header_cached(client, b, header_cache), blocks))

    for a in todo:
        tx, direct_info = direct[a]
        entry = {"direct_first_value_tx": tx, "direct_reliable": direct_info.get("reliable")}
        if direct_info.get("error"):
            entry["direct_error"] = direct_info["error"]
        if balance_at_creation[a]:
            block = facts[a]["creation"]["block"]
            entry["method"] = "balance > 0 at creation block; trace_block"
        elif a in searched:
            block = searched[a]
            entry["method"] = "eth_getBalance binary search; trace_block"
        else:
            block = None
            entry["method"] = "no positive balance boundary found"
        if block is not None:
            header = header_cache.get(block) or {}
            entry["block"] = block
            entry["timestamp"] = lib.hex_to_int(header.get("timestamp")) if header.get("timestamp") else None
            traced = trace_cache.get(block) or {}
            traces = traced.get("traces")
            if traces is None:
                entry["trace_error"] = traced.get("error")
            else:
                trace = find_first_value_transfer_trace(traces, a)
                if trace:
                    top = top_level_trace(traces, trace.get("transactionPosition"))
                    action = trace.get("action") or {}
                    entry.update({
                        "tx_hash": trace.get("transactionHash"),
                        "funder": action.get("from"),
                        "value_atto": str(lib.hex_to_int(action.get("value"))),
                        "trace_type": trace.get("type"),
                        "trace_depth": len(trace.get("traceAddress") or []),
                        "tx_sender": (top or {}).get("action", {}).get("from"),
                        "tx_to": (top or {}).get("action", {}).get("to"),
                    })
                else:
                    entry["trace_error"] = "no inbound value trace found (possible cross-shard receipt or staking payout)"
        # prefer the traced result; if trace found nothing but a direct tx exists, use it
        if "tx_hash" not in entry and tx:
            entry.update({
                "block": tx["block"],
                "timestamp": tx.get("timestamp"),
                "tx_hash": tx.get("hash"),
                "funder": tx.get("from"),
                "value_atto": tx.get("value_atto"),
                "trace_type": "direct",
                "trace_depth": 0,
                "tx_sender": tx.get("from"),
                "tx_to": tx.get("to"),
                "method": entry.get("method", "") + "; fallback to earliest direct RECEIVED value tx",
            })
        # consistency flag: traced funding should not be later than the earliest direct value tx
        if tx and entry.get("block") is not None and tx["block"] < entry["block"]:
            entry["note"] = "earliest direct value tx precedes traced funding block; using direct tx"
            entry.update({
                "block": tx["block"],
                "timestamp": tx.get("timestamp"),
                "tx_hash": tx.get("hash"),
                "funder": tx.get("from"),
                "value_atto": tx.get("value_atto"),
                "trace_type": "direct",
                "trace_depth": 0,
                "tx_sender": tx.get("from"),
                "tx_to": tx.get("to"),
            })
        facts[a]["funding"] = entry


# --------------------------------------------------------------------------
# Stage: probes (eth_call)
# --------------------------------------------------------------------------


def stage_probes(client, facts, addresses):
    todo = [a for a in addresses if "probes" not in facts[a] and not is_validator(facts, a)]
    if not todo:
        return
    log(f"probes: {len(todo)} addresses x {len(PROBES)} probes")
    calls = []
    labels = [label for label, _, _ in PROBES]
    for a in todo:
        for label in labels:
            calls.append(("eth_call", [{"to": a, "data": PROBE_CALLDATA[label]}, "latest"]))
    results = client.batch(calls)
    per = len(labels)
    for idx, a in enumerate(todo):
        chunk = results[idx * per:(idx + 1) * per]
        probes = {}
        for label, (result, error) in zip(labels, chunk):
            if error:
                probes[label] = {"error": (error.get("message") if isinstance(error, dict) else str(error))[:120]}
            elif result in (None, "0x", ""):
                probes[label] = {"empty": True}
            else:
                probes[label] = {"data": result}
        facts[a]["probes"] = probes


# --------------------------------------------------------------------------
# Stage: delegations
# --------------------------------------------------------------------------


def stage_delegations(client, facts, addresses):
    todo = [a for a in addresses if "delegations" not in facts[a]]
    if not todo:
        return
    log(f"delegations: {len(todo)} addresses")
    calls = [("hmyv2_getDelegationsByDelegator", [a]) for a in todo]
    results = client.batch(calls)
    for a, (result, error) in zip(todo, results):
        if error:
            facts[a]["delegations"] = {"error": error}
            continue
        items = result or []
        total = sum(int(d.get("amount", 0)) for d in items)
        reward = sum(int(d.get("reward", 0)) for d in items)
        undel = sum(int(u.get("amount", 0)) for d in items for u in (d.get("Undelegations") or []))
        facts[a]["delegations"] = {
            "count": len(items),
            "active_atto": str(total),
            "reward_atto": str(reward),
            "undelegating_atto": str(undel),
            "validators": [d.get("validator_address") for d in items][:20],
        }


def main():
    args = parse_args()
    addresses = load_addresses(args.input, args.limit)
    facts = {} if args.refresh else (lib.load_json(args.output, {}) or {})
    for a in addresses:
        facts.setdefault(a, {})
    client = lib.RpcClient(args.rpc, batch_size=args.batch_size, workers=args.workers)
    stages = args.only.split(",") if args.only else ["basic", "validator", "history", "creation", "funding", "probes", "delegations"]
    cutoff_hex = hex(args.cutoff_block)
    started = time.time()
    try:
        if "basic" in stages:
            stage_basic(client, facts, addresses, cutoff_hex)
            lib.dump_json(args.output, facts)
        if "validator" in stages:
            stage_validator(client, facts, addresses)
            lib.dump_json(args.output, facts)
        if "history" in stages:
            stage_history(client, facts, addresses)
            lib.dump_json(args.output, facts)
        if "creation" in stages:
            stage_creation(client, facts, addresses, args.cutoff_block)
            lib.dump_json(args.output, facts)
        if "funding" in stages:
            stage_funding(client, facts, addresses, args.cutoff_block)
            lib.dump_json(args.output, facts)
        if "probes" in stages:
            stage_probes(client, facts, addresses)
            lib.dump_json(args.output, facts)
        if "delegations" in stages:
            stage_delegations(client, facts, addresses)
            lib.dump_json(args.output, facts)
    finally:
        lib.dump_json(args.output, facts)
        log(
            f"done in {time.time() - started:.0f}s; http requests={client.requests_sent} rpc items={client.items_sent}; output={args.output}"
        )


if __name__ == "__main__":
    main()
