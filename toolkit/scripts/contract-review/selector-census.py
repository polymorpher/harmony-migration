#!/usr/bin/env python3
"""Stage 1c: function-selector census for contracts that remain unidentified.

For every non-validator address (and the implementation behind any proxy it
points to), extract the 4-byte selectors that appear in the runtime bytecode
dispatcher (``PUSH4 <selector>``), then resolve them to human readable
signatures using:

1. a built-in dictionary of well-known interface signatures (ERC standards,
   Uniswap, Safe, Compound/Aave, MasterChef, ENS, LayerZero, WETH9, Harmony
   staking precompile wrappers); and
2. the public openchain.xyz signature database (labels only; never used as
   a source of balances or state).

Output: ``selectors.json`` keyed by lowercase address with the selector list,
resolved signatures, proxy implementation used, and implementation code hash.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contract_review_lib as lib  # noqa: E402

ZERO = "0x0000000000000000000000000000000000000000"

KNOWN_SIGNATURES = [
    # ERC-20 / 721 / 1155 / 165
    "name()", "symbol()", "decimals()", "totalSupply()", "balanceOf(address)", "transfer(address,uint256)",
    "transferFrom(address,address,uint256)", "approve(address,uint256)", "allowance(address,address)",
    "increaseAllowance(address,uint256)", "decreaseAllowance(address,uint256)", "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
    "nonces(address)", "DOMAIN_SEPARATOR()", "mint(address,uint256)", "burn(uint256)", "burnFrom(address,uint256)", "burn(address,uint256)",
    "ownerOf(uint256)", "safeTransferFrom(address,address,uint256)", "safeTransferFrom(address,address,uint256,bytes)",
    "setApprovalForAll(address,bool)", "getApproved(uint256)", "isApprovedForAll(address,address)", "tokenURI(uint256)",
    "tokenByIndex(uint256)", "tokenOfOwnerByIndex(address,uint256)", "supportsInterface(bytes4)", "baseURI()", "setBaseURI(string)",
    "safeMint(address)", "safeMint(address,uint256)", "mint(uint256)", "mint()", "totalMinted()", "maxSupply()", "MAX_SUPPLY()",
    "uri(uint256)", "balanceOf(address,uint256)", "balanceOfBatch(address[],uint256[])",
    "safeTransferFrom(address,address,uint256,uint256,bytes)", "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)",
    "onERC721Received(address,address,uint256,bytes)", "onERC1155Received(address,address,uint256,uint256,bytes)",
    "onERC1155BatchReceived(address,address,uint256[],uint256[],bytes)", "tokensReceived(address,address,address,uint256,bytes,bytes)",
    # Ownable / access / pausable / proxies
    "owner()", "transferOwnership(address)", "renounceOwnership()", "getOwner()", "admin()", "setAdmin(address)", "pendingAdmin()",
    "hasRole(bytes32,address)", "grantRole(bytes32,address)", "revokeRole(bytes32,address)", "getRoleAdmin(bytes32)", "DEFAULT_ADMIN_ROLE()",
    "paused()", "pause()", "unpause()", "implementation()", "upgradeTo(address)", "upgradeToAndCall(address,bytes)", "changeAdmin(address)",
    "proxiableUUID()", "initialize()", "initialize(address)", "initialize(address,address)", "masterCopy()", "changeMasterCopy(address)",
    "VERSION()", "version()",
    # Gnosis Safe
    "getOwners()", "getThreshold()", "isOwner(address)", "nonce()", "domainSeparator()", "getModules()", "enableModule(address)",
    "execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)",
    "execTransactionFromModule(address,uint256,bytes,uint8)", "addOwnerWithThreshold(address,uint256)", "removeOwner(address,address,uint256)",
    "swapOwner(address,address,address)", "changeThreshold(uint256)", "setup(address[],uint256,address,bytes,address,address,uint256,address)",
    "getTransactionHash(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,uint256)", "approveHash(bytes32)",
    "signedMessages(bytes32)", "isValidSignature(bytes,bytes)", "isValidSignature(bytes32,bytes)", "getMessageHash(bytes)",
    "createProxy(address,bytes)", "createProxyWithNonce(address,bytes,uint256)", "proxyCreationCode()",
    # Gnosis MultiSigWallet legacy
    "required()", "transactionCount()", "submitTransaction(address,uint256,bytes)", "confirmTransaction(uint256)", "executeTransaction(uint256)",
    "revokeConfirmation(uint256)", "isConfirmed(uint256)", "getConfirmations(uint256)", "owners(uint256)",
    # 1wallet
    "getInfo()", "getVersion()", "getForwardAddress()", "commit(bytes32,bytes32,bytes32)", "commit(bytes32)", "retire()", "getNonce()",
    "getAllCommits()", "getTrackedTokens()", "getBacklinks()", "identificationKey()", "getIdentificationKeys()", "getRootKey()",
    "getSpendingState()", "getCurrentSpendingState()", "getCurrentSpending()", "lastOperationTime()", "getOldInfos()", "getInnerCores()",
    # WETH9
    "deposit()", "withdraw(uint256)",
    # Uniswap V2
    "factory()", "token0()", "token1()", "getReserves()", "price0CumulativeLast()", "price1CumulativeLast()", "kLast()", "MINIMUM_LIQUIDITY()",
    "mint(address)", "burn(address)", "swap(uint256,uint256,address,bytes)", "skim(address)", "sync()", "initialize(address,address)",
    "allPairs(uint256)", "allPairsLength()", "createPair(address,address)", "feeTo()", "feeToSetter()", "getPair(address,address)", "setFeeTo(address)",
    "WETH()", "addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)", "addLiquidityETH(address,uint256,uint256,uint256,address,uint256)",
    "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)", "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)",
    "swapExactETHForTokens(uint256,address[],address,uint256)", "swapExactTokensForETH(uint256,uint256,address[],address,uint256)",
    "getAmountsOut(uint256,address[])", "getAmountsIn(uint256,address[])", "quote(uint256,uint256,uint256)",
    # Uniswap V3
    "slot0()", "liquidity()", "fee()", "tickSpacing()", "positions(bytes32)", "ticks(int24)", "observe(uint32[])",
    "swap(address,bool,int256,uint160,bytes)", "mint(address,int24,int24,uint128,bytes)", "collect(address,int24,int24,uint128,uint128)",
    "flash(address,uint256,uint256,bytes)", "getPool(address,address,uint24)", "createPool(address,address,uint24)",
    # MasterChef / staking
    "poolLength()", "poolInfo(uint256)", "userInfo(uint256,address)", "deposit(uint256,uint256)", "withdraw(uint256,uint256)",
    "emergencyWithdraw(uint256)", "pendingSushi(uint256,address)", "pendingReward(uint256,address)", "pendingRewards(uint256,address)", "sushi()", "SUSHI()", "rewardToken()",
    "govToken()", "add(uint256,address,bool)", "set(uint256,uint256,bool)", "massUpdatePools()", "updatePool(uint256)", "startBlock()", "rewardPerBlock()",
    "totalAllocPoint()", "stake(uint256)", "unstake(uint256)", "getReward()", "exit()", "earned(address)", "rewardRate()", "periodFinish()",
    "notifyRewardAmount(uint256)", "stakingToken()", "rewardsToken()", "lpToken()", "totalStaked()", "stakers(address)", "claim()", "claimRewards()",
    "harvest()", "compound()", "deposit(uint256)", "withdrawAll()", "emergencyWithdraw()", "stake()", "unstake()", "withdraw()",
    "lockedBalance(address)", "lock(uint256,uint256)", "unlock()", "release()", "releasable()", "vestedAmount(uint64)", "beneficiary()", "start()", "duration()", "cliff()",
    # Compound / Aave
    "comptroller()", "underlying()", "mint(uint256)", "redeem(uint256)", "redeemUnderlying(uint256)", "borrow(uint256)", "repayBorrow(uint256)",
    "liquidateBorrow(address,uint256,address)", "exchangeRateStored()", "exchangeRateCurrent()", "borrowRatePerBlock()", "supplyRatePerBlock()",
    "getCash()", "totalBorrows()", "totalReserves()", "reserveFactorMantissa()", "interestRateModel()", "isCToken()", "accrueInterest()",
    "getAllMarkets()", "enterMarkets(address[])", "exitMarket(address)", "getAccountLiquidity(address)", "oracle()", "closeFactorMantissa()",
    "POOL()", "UNDERLYING_ASSET_ADDRESS()", "scaledBalanceOf(address)", "scaledTotalSupply()", "getIncentivesController()", "ADDRESSES_PROVIDER()",
    "supply(address,uint256,address,uint16)", "borrow(address,uint256,uint256,uint16,address)", "repay(address,uint256,uint256,address)",
    "flashLoanSimple(address,address,uint256,bytes,uint16)", "getReserveData(address)", "getUserAccountData(address)",
    # Harmony staking precompile wrappers / liquid staking
    "delegate(address,uint256)", "undelegate(address,uint256)", "collectRewards()", "epoch()", "getDelegation(address)", "validator()",
    "validators(uint256)", "validatorAddress()", "getValidators()", "addValidator(address)", "removeValidator(address)", "totalDelegated()",
    "exchangeRate()", "getExchangeRate()", "stONE()", "stakingContract()", "rebalance()", "totalAssets()", "convertToShares(uint256)",
    "convertToAssets(uint256)", "asset()", "previewDeposit(uint256)", "maxWithdraw(address)", "redeem(uint256,address,address)",
    "deposit(uint256,address)", "withdraw(uint256,address,address)", "mint(uint256,address)",
    # Vaults / strategies
    "want()", "token()", "strategy()", "vault()", "getPricePerFullShare()", "balance()", "available()", "earn()", "depositAll()", "withdrawAll()",
    "setStrategy(address)", "proposeStrat(address)", "upgradeStrat()", "treasury()", "governance()", "keeper()", "strategist()",
    # ENS-like
    "commit(bytes32)", "makeCommitment(string,address,bytes32)", "register(string,address,uint256,bytes32)", "renew(string,uint256)",
    "available(string)", "rentPrice(string,uint256)", "valid(string)", "minCommitmentAge()", "maxCommitmentAge()", "commitments(bytes32)",
    "baseNode()", "ens()", "resolver()", "nameExpires(uint256)", "register(uint256,address,uint256)", "reclaim(uint256,address)",
    "setResolver(address)", "controllers(address)", "addController(address)", "removeController(address)", "GRACE_PERIOD()",
    "setSubnodeOwner(bytes32,bytes32,address)", "setSubnodeRecord(bytes32,bytes32,address,address,uint64)", "setRecord(bytes32,address,address,uint64)",
    "addr(bytes32)", "setAddr(bytes32,address)", "text(bytes32,string)", "setText(bytes32,string,string)", "contenthash(bytes32)",
    "registry()", "priceOracle()", "baseRegistrar()", "reverseRegistrar()", "nameWrapper()",
    # LayerZero
    "lzEndpoint()", "endpoint()", "lzReceive(uint16,bytes,uint64,bytes)", "lzReceive((uint32,bytes32,uint64),bytes32,bytes,address,bytes)",
    "setTrustedRemote(uint16,bytes)", "trustedRemoteLookup(uint16)", "estimateSendFee(uint16,bytes32,uint256,bool,bytes)",
    "sendFrom(address,uint16,bytes32,uint256,(address,address,bytes))", "peers(uint32)", "setPeer(uint32,bytes32)", "oApp()", "token()",
    "quoteSend((uint32,bytes32,uint256,uint256,bytes,bytes,bytes),bool)", "send((uint32,bytes32,uint256,uint256,bytes,bytes,bytes),(uint256,uint256),address)",
    "sharedDecimals()", "circulatingSupply()", "oftVersion()", "approvalRequired()", "setDelegate(address)",
    # Bridges / misc
    "wallet()", "rate()", "weiRaised()", "buyTokens(address)", "cap()", "openingTime()", "closingTime()", "hasClosed()", "finalize()",
    "multicall(bytes[])", "aggregate((address,bytes)[])", "tryAggregate(bool,(address,bytes)[])", "aggregate3((address,bool,bytes)[])",
    "execute(address,uint256,bytes)", "execute(bytes32,address,uint256,bytes,bytes32,uint256)", "queueTransaction(address,uint256,string,bytes,uint256)",
    "executeTransaction(address,uint256,string,bytes,uint256)", "delay()", "MINIMUM_DELAY()", "GRACE_PERIOD()", "setDelay(uint256)",
    "propose(address[],uint256[],string[],bytes[],string)", "castVote(uint256,bool)", "quorumVotes()", "proposalThreshold()",
    "lockTokens(address,uint256,address)", "unlockToken(address,uint256,address,bytes32)", "mintToken(address,uint256,address,bytes32)",
    "burnToken(address,uint256,address)", "lockOne(address)", "unlockOne(uint256,address,bytes32)", "lockOneFor(address,uint256)",
    "swap(address,uint256,address)", "swapETH(uint256,address)", "getVersion()", "claimTokens(address,uint256,bytes32[])", "isClaimed(uint256)",
    "merkleRoot()", "claim(uint256,address,uint256,bytes32[])", "cost()", "price()", "mintPrice()", "getPrice()", "setPrice(uint256)",
    "tokenPrice()", "buy(uint256)", "sell(uint256)", "buy()", "sell()", "withdrawFunds()", "withdrawETH()", "withdrawToken(address)", "rescueTokens(address)",
    "distribute()", "distributeRewards()", "airdrop(address[],uint256[])", "batchTransfer(address[],uint256[])", "multisend(address[],uint256[])",
    "createBet(uint256)", "placeBet(uint256)", "resolveBet(uint256)", "roll()", "spin()", "enter()", "draw()", "lotteryId()", "ticketPrice()",
    "getRandomNumber()", "fulfillRandomness(bytes32,uint256)", "rawFulfillRandomWords(uint256,uint256[])",
    "listItem(address,uint256,uint256)", "buyItem(address,uint256)", "cancelListing(address,uint256)", "createAuction(uint256,uint256,uint256)",
    "bid(uint256)", "settleAuction(uint256)", "listings(address,uint256)", "marketplaceFee()", "feeRecipient()",
    "TOKEN_DECIMALS()", "totalPoolAmount()", "rewardPool()", "getUserInfo(address)", "users(address)", "referrer(address)", "invest(address)", "withdraw(address)",
]
KNOWN = {lib.selector(sig)[2:]: sig for sig in KNOWN_SIGNATURES}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rpc", default="https://a.api.s0.t.hmny.io")
    parser.add_argument("--only", help="comma separated addresses (default: all non-validator addresses)")
    parser.add_argument("--no-openchain", action="store_true", help="do not query openchain.xyz for unknown selectors")
    return parser.parse_args()


def log(message):
    sys.stderr.write(time.strftime("%H:%M:%S ") + message + "\n")
    sys.stderr.flush()


def extract_selectors(code_hex):
    """Selectors pushed with PUSH4 in the dispatcher; filters out obvious non-selector constants."""
    raw = code_hex[2:] if code_hex.startswith("0x") else code_hex
    found = []
    for match in re.finditer(r"63([0-9a-f]{8})(?:14|81|1461|16)", raw):
        sel = match.group(1)
        if sel in ("ffffffff", "00000000"):
            continue
        found.append(sel)
    # also accept `63xxxxxxxx` followed within 6 bytes by `14` (EQ) for compilers that reorder
    for match in re.finditer(r"63([0-9a-f]{8})[0-9a-f]{0,12}?14", raw):
        sel = match.group(1)
        if sel not in ("ffffffff", "00000000"):
            found.append(sel)
    ordered = []
    seen = set()
    for sel in found:
        if sel not in seen:
            seen.add(sel)
            ordered.append(sel)
    return ordered


def clone_implementation(code_hex):
    raw = code_hex[2:] if code_hex.startswith("0x") else code_hex
    if raw.startswith("363d3d373d3d3d363d73") and len(raw) == 90:
        return "0x" + raw[20:60]
    # Gnosis proxy / minimal proxies that load slot0 are handled through storage, not here
    return None


def proxy_target(facts):
    basic = facts.get("basic") or {}
    for key in ("eip1967_impl", "eip1967_beacon"):
        value = basic.get(key)
        if value and value != ZERO:
            return value, key
    clone = clone_implementation(basic.get("code_cutoff") or "")
    if clone:
        return clone, "eip1167-clone"
    probes = facts.get("probes") or {}
    for label in ("safe_masterCopy", "misc_implementation"):
        data = (probes.get(label) or {}).get("data")
        if data:
            address = lib.decode_address(data)
            if address and address != ZERO:
                return address, label
    slot0 = basic.get("storage_slot0") or ""
    address = lib.decode_address(slot0) if slot0 else None
    if address and address != ZERO and (basic.get("code_cutoff_len") or 0) < 1200:
        return address, "storage-slot0"
    return None, None


def openchain_lookup(selectors, cache):
    unknown = [s for s in selectors if s not in cache]
    for i in range(0, len(unknown), 80):
        chunk = unknown[i:i + 80]
        query = ",".join("0x" + s for s in chunk)
        url = "https://api.openchain.xyz/signature-database/v1/lookup?filter=true&function=" + urllib.parse.quote(query)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "harmony-migration-contract-review"}), timeout=60) as response:
                data = json.load(response)
            functions = (data.get("result") or {}).get("function") or {}
            for s in chunk:
                entries = functions.get("0x" + s) or []
                cache[s] = entries[0]["name"] if entries else None
        except Exception as error:  # noqa: BLE001
            log(f"openchain lookup failed: {error}")
            for s in chunk:
                cache.setdefault(s, None)
            time.sleep(2)
        time.sleep(0.3)


def main():
    args = parse_args()
    facts_all = lib.load_json(args.facts, {})
    output = lib.load_json(args.output, {}) or {}
    sig_cache = output.get("_signature_cache", {})
    client = lib.RpcClient(args.rpc, batch_size=20, workers=3)
    addresses = [a for a, f in facts_all.items() if not (f.get("validator") or {}).get("is_validator")]
    if args.only:
        wanted = {lib.normalize_address(x) for x in args.only.split(",")}
        addresses = [a for a in addresses if a in wanted]
    log(f"selector census for {len(addresses)} contracts")

    # fetch implementation code where needed
    targets = {}
    for a in addresses:
        target, how = proxy_target(facts_all[a])
        if target:
            targets[a] = (target, how)
    impl_addresses = sorted({t for t, _ in targets.values()})
    impl_code = {}
    if impl_addresses:
        results = client.batch([("eth_getCode", [t, "latest"]) for t in impl_addresses])
        for t, (code, error) in zip(impl_addresses, results):
            impl_code[t] = code or "0x"

    all_selectors = set()
    entries = {}
    for a in addresses:
        facts = facts_all[a]
        own_code = (facts.get("basic") or {}).get("code_cutoff") or "0x"
        own = extract_selectors(own_code)
        entry = {"own_selectors": own}
        if a in targets:
            target, how = targets[a]
            code = impl_code.get(target, "0x")
            impl_sel = extract_selectors(code)
            code_bytes = bytes.fromhex(code[2:]) if code.startswith("0x") else b""
            entry.update({
                "proxy_kind": how,
                "implementation": target,
                "implementation_code_len": len(code_bytes),
                "implementation_code_hash": "0x" + lib.keccak256(code_bytes).hex(),
                "implementation_selectors": impl_sel,
            })
            all_selectors.update(impl_sel)
        all_selectors.update(own)
        entries[a] = entry

    unknown = sorted(s for s in all_selectors if s not in KNOWN)
    log(f"{len(all_selectors)} distinct selectors, {len(unknown)} not in built-in dictionary")
    if not args.no_openchain and unknown:
        openchain_lookup(unknown, sig_cache)

    def resolve(sels):
        out = []
        for s in sels:
            name = KNOWN.get(s) or sig_cache.get(s)
            out.append(f"{name}" if name else f"0x{s}")
        return out

    for a, entry in entries.items():
        entry["own_signatures"] = resolve(entry["own_selectors"])
        if "implementation_selectors" in entry:
            entry["implementation_signatures"] = resolve(entry["implementation_selectors"])
        output[a] = entry
    output["_signature_cache"] = sig_cache
    lib.dump_json(args.output, output)
    log(f"done; wrote {args.output}")


if __name__ == "__main__":
    main()
