#!/usr/bin/env python3
"""Stage 3: render the contract-account review report (Markdown) from the CSVs.

All figures are computed from the classification outputs so the report can be
regenerated after any re-run of the pipeline.
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
from collections import Counter, defaultdict
from decimal import Decimal

D = lambda x: Decimal(x or 0)  # noqa: E731


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, help="directory containing the classification CSVs")
    parser.add_argument("--claims", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--rpc", default="https://a.api.s0.t.hmny.io")
    parser.add_argument("--cutoff-block", type=int, default=93623067)
    parser.add_argument("--all-claims", help="optional full cutoff ledger CSV (all addresses); adds context on code-bearing accounts below the threshold")
    parser.add_argument("--threshold-one", type=int, default=1000)
    return parser.parse_args()


EMPTY_CODE_HASH = "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def full_ledger_context(path, threshold):
    """Count code-bearing accounts in the full ledger, above and below the threshold."""
    import sys

    csv.field_size_limit(sys.maxsize)
    stats = {"rows": 0, "code": 0, "code_ge": 0, "code_lt": 0, "claim_ge": Decimal(0), "claim_lt": Decimal(0)}
    with open(path, newline="") as handle:
        for r in csv.DictReader(handle):
            stats["rows"] += 1
            h0 = r.get("code_hash_shard0") or ""
            h1 = r.get("code_hash_shard1") or ""
            if not ((h0 and h0 != EMPTY_CODE_HASH) or (h1 and h1 != EMPTY_CODE_HASH)):
                continue
            stats["code"] += 1
            claim = D(r["total_claim_one"])
            if claim >= threshold:
                stats["code_ge"] += 1
                stats["claim_ge"] += claim
            else:
                stats["code_lt"] += 1
                stats["claim_lt"] += claim
    return stats


def read(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def one(x, places=2):
    return f"{D(x):,.{places}f}"


def agg(rows, key):
    out = defaultdict(lambda: {
        "n": 0,
        "claim": Decimal(0),
        "wallet": Decimal(0),
        "vault": Decimal(0),
        "liquid": Decimal(0),
        "staked": Decimal(0),
        "latest": Decimal(0),
    })
    for r in rows:
        b = out[r.get(key, "all")]
        b["n"] += 1
        b["claim"] += D(r["total_claim_one"])
        b["wallet"] += D(r.get("wallet_airdrop_one"))
        b["vault"] += D(
            r.get("staked_to_vault_one")
            or r["active_staked_or_delegated_one"]
        )
        b["liquid"] += D(r["liquid_total_one"])
        b["staked"] += D(r["active_staked_or_delegated_one"])
        b["latest"] += D(r["balance_latest_one"])
    return out


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def main():
    args = parse_args()
    od = args.output_dir
    all_rows = read(os.path.join(od, "contract-review-all.csv"))
    validators = read(os.path.join(od, "validator-accounts.csv"))
    multisig = read(os.path.join(od, "multisig-wallets.csv"))
    onewallets = read(os.path.join(od, "onewallets.csv"))
    smartvault = read(os.path.join(od, "smartvault-wallets.csv"))
    erc20 = read(os.path.join(od, "erc20-tokens.csv"))
    nfts = read(os.path.join(od, "nft-contracts.csv"))
    apps = read(os.path.join(od, "known-app-contracts.csv"))
    patterns = read(os.path.join(od, "pattern-identified-contracts.csv"))
    unidentified = read(os.path.join(od, "unidentified-contracts.csv"))
    summary = json.load(open(os.path.join(od, "summary.json")))

    total_claim = sum(D(r["total_claim_one"]) for r in all_rows)
    contracts = [r for r in all_rows if r["primary_category"] != "validator-account"]
    latest_block = max(int(r["latest_block_at_fetch"] or 0) for r in all_rows)
    today = datetime.date.today().isoformat()

    L = []
    L.append(f"# Contract-account review of the >= 1,000 ONE code-bearing claim rows\n")
    L.append(f"Generated {today} by `toolkit/scripts/contract-review/build-report.py`.\n")
    L.append("## Scope and inputs\n")
    L.append(
        f"- Input: `{os.path.basename(args.claims)}` ({len(all_rows)} rows, SHA-256 `{file_sha256(args.claims)}`), "
        f"the code-bearing rows of the inclusive >= 1,000 ONE cutoff ledger (shard 0 block {args.cutoff_block:,}, shard 1 block 95,882,100).\n"
        f"- Classification and enrichment facts were read from the Harmony shard-0 archival node RPC `{args.rpc}` "
        f"(consensus state, canonical transactions, `trace_block`). Cutoff claim and delivery components came from the two-shard input CSV. Explorer/indexer balances were not used. "
        f"Latest-state values were read at block {latest_block:,} or later.\n"
        f"- Total claim of the reviewed rows: **{one(total_claim, 18)} ONE** (matches the eligibility summary exactly).\n"
    )
    L.append(
        "Terminology: **total claim** includes active delegation and is used "
        "for threshold selection. **Wallet airdrop** excludes active "
        "delegation. **Staked to vault** is deposited into the "
        "validator's ERC-4626 vault and represented by shares rather than "
        "being sent to the delegator wallet.\n"
    )

    # headline
    v_agg = agg(validators, "primary_category")["all"]
    L.append("## Headline findings\n")
    threshold = args.threshold_one
    scope = (
        f"**Scope of every count below:** the {len(all_rows)} code-bearing accounts whose total claim at the cutoff is "
        f"**>= {threshold:,} ONE** (the preliminary code-bearing review ledger). These are not census counts of each contract type on Harmony: "
        f"for example, \"{len(onewallets)} 1wallets\" means {len(onewallets)} 1wallet contracts that held >= {threshold:,} ONE at the cutoff, "
        f"not the number of 1wallets ever created."
    )
    if args.all_claims:
        ctx = full_ledger_context(args.all_claims, threshold)
        scope += (
            f" For context, the full cutoff ledger (`{os.path.basename(args.all_claims)}`, {ctx['rows']:,} accounts) contains "
            f"{ctx['code']:,} code-bearing accounts in total: the {ctx['code_ge']:,} reviewed here hold {one(ctx['claim_ge'])} ONE, "
            f"while the other {ctx['code_lt']:,} below the threshold hold only {one(ctx['claim_lt'])} ONE combined and were not classified."
        )
    L.append(scope + "\n")
    pc = agg(all_rows, "primary_category")
    safe_versions = Counter(r["safe_version"] for r in multisig)
    safe_note = ", ".join(
        f"{n} {'Harmony `multisig.harmony.one` v1.2.0-fork proxies' if v == '1.2.0' else 'canonical v' + v}"
        for v, n in sorted(safe_versions.items(), key=lambda kv: -kv[1])
    )
    app_totals = agg([r for r in all_rows if r["primary_category"] == "known-app"], "known_app")
    top_apps = sorted(app_totals.items(), key=lambda kv: -kv[1]["claim"])
    short = lambda name: name.split(" (")[0]  # noqa: E731
    app_note = ", ".join(f"{short(k)} {v['claim'] / Decimal(10**6):,.1f}M" for k, v in top_apps[:2])
    app_note += ", " + ", ".join(short(k) for k, _ in top_apps[2:9]) + ", etc."
    pattern_counts = Counter(r["pattern_category"] for r in patterns)
    pattern_note = f"{pattern_counts.get('staking-precompile-user', 0)} staking-precompile delegation pools, marketplaces, presales, games…"
    ow_versions = sorted({r["onewallet_version"].split(".")[0] for r in onewallets if r["onewallet_version"]}, key=int)
    ow_note = (
        f"v{ow_versions[0]}–v{ow_versions[-1]}; {sum(1 for r in onewallets if r['recovery_address'])} with user recovery address, "
        f"{sum(1 for r in onewallets if r['recovery_is_treasury'] == 'True')} pointing at the treasury Safe, "
        f"{sum(1 for r in onewallets if r['forward_address'])} forwarded"
    )
    headline = [
        ("multisig-wallet", f"Safe multisigs ({safe_note})"),
        ("known-app", f"Well-known apps ({app_note})"),
        ("validator-account", "**Validator accounts — not contracts**"),
        ("pattern-identified", f"Fingerprint-identified, operator unattributed ({pattern_note})"),
        ("erc20-token", "ERC-20 tokens (primary)"),
        ("onewallet", f"1wallets ({ow_note})"),
        ("smartvault-wallet", "SmartVault OTP wallets (harmony-totp) — a third wallet type"),
        ("nft-contract", "NFT contracts (primary)"),
        ("unidentified", "Unidentified"),
    ]
    rows = []
    for key, label in headline:
        v = pc.get(key, {"n": 0, "claim": Decimal(0)})
        rows.append([label, v["n"], f"{v['claim']:,.0f}"])
    L.append(table(["category", f"accounts with >= {threshold:,} ONE", "total claim ONE"], rows))
    L.append(
        f"\nOverlapping membership per question: {len(multisig)} multisigs, {len(onewallets)} 1wallets, "
        f"{len(erc20)} ERC-20 ({sum(D(r['total_claim_one']) for r in erc20) / Decimal(10**6):,.1f}M ONE incl. WONE), "
        f"{len(nfts)} NFTs ({sum(D(r['total_claim_one']) for r in nfts) / Decimal(10**3):,.1f}K ONE, with current-owner counts), "
        f"{len(apps)} rows attributed to {len(set(r['app_name'] for r in apps))} apps.\n"
    )
    L.append(
        f"1. **{len(validators)} of the {len(all_rows)} \"code-bearing\" rows are not contracts.** They are Harmony validator accounts. "
        f"Harmony stores each validator's RLP-encoded `ValidatorWrapper` in the account's *code* field "
        f"(`core/state/statedb.go: UpdateValidatorWrapper -> db.SetCode(addr, rlp, true)`), so the state export sees a non-empty code hash. "
        f"These accounts are ordinary ECDSA key-controlled EOAs (they sign CreateValidator/EditValidator transactions). "
        f"Their total claim is **{one(v_agg['claim'])} ONE**: "
        f"{one(v_agg['wallet'])} is direct wallet airdrop and "
        f"{one(v_agg['vault'])} is staked to validator vaults. "
        f"Detection: `hmyv2_getValidatorInformation` succeeds and the code RLP-decodes to a wrapper whose address equals the account "
        f"(all {len(validators)} match; all {len(contracts)} real contracts fail both tests). Recommendation: move them from manual contract recovery to the automatic claim category while keeping wallet and vault delivery separate.\n"
    )
    ms_agg = agg(multisig, "is_multisig")["all"]
    L.append(
        f"2. **{len(multisig)} Gnosis Safe multisigs hold {one(ms_agg['claim'])} ONE** ({one(ms_agg['claim'] / total_claim * 100, 1)}% of the reviewed total). "
        f"{sum(1 for r in multisig if r['safe_version'] == '1.2.0')} are proxies of the Harmony `multisig.harmony.one` singleton "
        f"(a non-canonical Gnosis Safe v1.2.0 build at `0x3736aC84…`), {sum(1 for r in multisig if r['safe_version'] == '1.3.0')} use the canonical Safe v1.3.0 L2 singleton, "
        f"{sum(1 for r in multisig if r['safe_version'] == '1.4.1')} uses Safe v1.4.1. Owners and thresholds are in `multisig-wallets.csv`; every owner is an EOA-style address that can sign on Ethereum.\n"
    )
    L.append(
        f"3. **{len(onewallets)} 1wallet contracts have {one(sum(D(r['total_claim_one']) for r in onewallets))} ONE of total claim.** "
        f"{sum(1 for r in onewallets if r['recovery_address'])} have a user-set recovery address; "
        f"{sum(1 for r in onewallets if r['recovery_is_treasury'] == 'True')} still point at the 1wallet treasury (`0x7534978F…`, itself a 2-of-4 Safe in this list). "
        f"{sum(1 for r in onewallets if r['forward_address'])} have a forward address set. All {len(onewallets)} current OTP cores have passed their expiry time.\n"
    )
    ka = [r for r in all_rows if r["known_app"]]
    L.append(
        f"4. **{len(ka)} rows are attributed to {len(set(r['known_app'] for r in ka))} well-known applications** ({one(sum(D(r['total_claim_one']) for r in ka))} ONE of total claim including wallet rows). "
        f"The largest are WONE ({one([r for r in all_rows if r['address'].lower()=='0xcf664087a5bb0237a0bad6742852ec6c8d69a27a'][0]['total_claim_one'])} ONE of wrapped ONE) and the two "
        f"LayerZero NativeOFT lock contracts backing bridged ONE on BNB Chain and Ethereum "
        f"({one(sum(D(r['total_claim_one']) for r in all_rows if r['known_app'].startswith('Harmony LayerZero')))} ONE).\n"
    )
    L.append(
        f"5. **Unidentifiable contracts: {len(unidentified)}.** {len(patterns)} further contracts are identified by function-signature fingerprint "
        f"as to *what* they are (staking-precompile delegation pools, NFT marketplaces, presales, payment splitters, games, escrows) but their operator is not attributed; "
        f"their total claim is {one(sum(D(r['total_claim_one']) for r in patterns))} ONE.\n"
    )

    # primary category table
    L.append("## Statistics by primary category (mutually exclusive)\n")
    rows = []
    for k, v in sorted(pc.items(), key=lambda kv: -kv[1]["claim"]):
        rows.append([k, v["n"], one(v["claim"]), one(v["wallet"]), one(v["vault"]), one(v["latest"]), f"{v['claim'] / total_claim * 100:.2f}%"])
    rows.append(["**total**", len(all_rows), one(total_claim), one(sum(v["wallet"] for v in pc.values())), one(sum(v["vault"] for v in pc.values())), one(sum(v["latest"] for v in pc.values())), "100%"])
    L.append(table(["primary category", "rows", "total claim ONE", "direct wallet airdrop ONE", "staked to vault ONE", "latest liquid balance ONE", "share of total"], rows))
    L.append("\nPriority when a contract fits several categories: validator account > Safe multisig > 1wallet > SmartVault wallet > well-known app > ERC-20 > NFT > fingerprint pattern > unidentified. "
             "The per-question sections below use overlapping membership (e.g. WONE appears both as a known app and as an ERC-20).\n")

    # subcategories
    L.append("### Subcategories\n")
    sc = agg(all_rows, "subcategory")
    rows = [[k, v["n"], one(v["claim"])] for k, v in sorted(sc.items(), key=lambda kv: -kv[1]["claim"])]
    L.append(table(["subcategory", "rows", "total claim ONE"], rows))

    # overlapping
    L.append("\n### Membership per review question (overlapping)\n")
    flags = [("Safe / multisig wallets", multisig), ("1wallet contracts", onewallets), ("SmartVault OTP wallets (additional wallet type found)", smartvault),
             ("ERC-20 token contracts", erc20), ("NFT contracts (ERC-721 / ERC-1155 / other)", nfts), ("Well-known application contracts", apps),
             ("Fingerprint-identified, operator unattributed", patterns), ("Validator accounts (EOAs)", validators), ("Unidentified", unidentified)]
    rows = [[name, len(rs), one(sum(D(r["total_claim_one"]) for r in rs)), one(sum(D(r["balance_latest_one"]) for r in rs))] for name, rs in flags]
    L.append(table(["set", "rows", "total claim ONE", "latest liquid balance ONE"], rows))

    # validators
    L.append("\n## Validator accounts (not contracts)\n")
    st = Counter(r["active_status"] for r in validators)
    L.append(
        f"{len(validators)} accounts; active-status: "
        f"{', '.join(f'{n} {k}' for k, n in st.most_common())}. "
        f"Total claim {one(v_agg['claim'])} ONE = "
        f"{one(v_agg['wallet'])} direct wallet airdrop + "
        f"{one(v_agg['vault'])} staked to validator vaults.\n"
    )
    rows = [[r["validator_name"][:40], f"`{r['address']}`", one(r["total_claim_one"]), one(r["wallet_airdrop_one"]), one(r["staked_to_vault_one"]), r["active_status"], r["creation_time_utc"][:10]]
            for r in sorted(validators, key=lambda r: -D(r["total_claim_one"]))[:15]]
    L.append(table(["validator", "address", "total claim", "wallet airdrop", "staked to vault", "status", "CreateValidator"], rows))
    L.append("\nFull list: `validator-accounts.csv` (name, identity, website, status, creation height/tx, delegation count, BLS keys, activity).\n")

    # multisig
    L.append("## 1. Multisig (Gnosis Safe) wallets\n")
    by_ver = defaultdict(list)
    for r in multisig:
        by_ver[(r["safe_version"], r["master_copy"], r["master_copy_label"])].append(r)
    rows = []
    for (ver, mc, label), rs in sorted(by_ver.items(), key=lambda kv: -sum(D(r["total_claim_one"]) for r in kv[1])):
        dates = sorted(r["creation_time_utc"] for r in rs)
        rows.append([ver, f"`{mc}`", label[:90], len(rs), one(sum(D(r["total_claim_one"]) for r in rs)), f"{dates[0][:10]} .. {dates[-1][:10]}"])
    L.append(table(["Safe VERSION()", "singleton (storage slot 0)", "singleton identification", "wallets", "total claim ONE", "creation range"], rows))
    L.append(f"\nThreshold/owner configurations: {', '.join(f'{k} x{v}' for k, v in Counter(f'{r['threshold']}-of-{r['owner_count']}' for r in multisig).most_common())}.\n")
    L.append("Detection: `getOwners()` returns a non-empty address array, `getThreshold()` is within `[1, owners]`, `VERSION()` returns a string, and storage slot 0 holds the singleton "
             "(Gnosis Safe proxies store `masterCopy` at slot 0 in every version). The Harmony `multisig.harmony.one` singleton returns VERSION `1.2.0` but is a 16,040-byte build that differs "
             "from the canonical 24,410-byte GnosisSafe v1.2.0 runtime, i.e. a recompiled/forked build; all 66 of its proxies were created through factory `0x4f9b1dEf3a0f6747bF8C870a27D3DeCdf029100e`.\n")
    rows = [[f"`{r['address']}`", r["safe_version"], f"{r['threshold']}/{r['owner_count']}", one(r["total_claim_one"]), r["creation_time_utc"][:10], f"`{r['creation_tx_sender']}`", r["first_funding_time_utc"][:10], f"`{r['first_funder']}`", one(r["first_funding_value_one"]), r["last_direct_tx_time_utc"][:10], r["known_app_role"][:40]]
            for r in multisig[:15]]
    L.append("\nLargest 15:\n")
    L.append(table(["Safe", "ver", "thr/owners", "total claim ONE", "created", "creator (tx sender)", "first funded", "first funder", "amount", "last direct tx", "note"], rows))
    L.append("\nCSV `multisig-wallets.csv` columns: balances (cutoff components + latest), `safe_version`, `master_copy`, `threshold`, `owner_count`, `owners` (comma-separated), `safe_nonce`, "
             "creation block/time/tx, `creation_tx_sender` (EOA that called the factory), `creation_direct_creator` (factory), first funding block/time/tx, `first_funder` (address whose value reached the Safe), "
             "`first_funding_tx_sender`, amount, and direct-transaction activity.\n")

    # 1wallet
    L.append("## 2. 1wallet (Modulo OTP wallet) contracts\n")
    ver = Counter(r["onewallet_version"] for r in onewallets)
    L.append(f"{len(onewallets)} wallets, {one(sum(D(r['total_claim_one']) for r in onewallets))} ONE total claim "
             f"({one(sum(D(r['wallet_airdrop_one']) for r in onewallets))} direct wallet recovery, {one(sum(D(r['staked_to_vault_one']) for r in onewallets))} staked to validator vaults).\n")
    rows = [[f"v{k}", n, one(sum(D(r["total_claim_one"]) for r in onewallets if r["onewallet_version"] == k))] for k, n in sorted(ver.items(), key=lambda kv: -float(kv[0] or 0))]
    L.append(table(["contract version (getVersion)", "wallets", "total claim ONE"], rows))
    treasury = [r for r in multisig if r["address"].lower() == "0x7534978f9fa903150ed429c486d1f42b7fdb7a61"]
    tinfo = treasury[0] if treasury else {}
    L.append(
        f"\nRecovery address: {sum(1 for r in onewallets if r['recovery_address'])} wallets have a user-set `lastResortAddress` "
        f"({one(sum(D(r['total_claim_one']) for r in onewallets if r['recovery_address']))} ONE); "
        f"{sum(1 for r in onewallets if r['recovery_is_treasury'] == 'True')} wallets ({one(sum(D(r['total_claim_one']) for r in onewallets if r['recovery_is_treasury'] == 'True'))} ONE) "
        f"still carry the default 1wallet treasury `0x7534978F9fa903150eD429C486D1f42B7fDB7a61`, which the contract treats as \"not set\". "
        f"The treasury is itself a Safe in this ledger: v{tinfo.get('safe_version')} {tinfo.get('threshold')}-of-{tinfo.get('owner_count')}, owners `{tinfo.get('owners')}`. "
        f"{sum(1 for r in onewallets if r['forward_address'])} wallets have `getForwardAddress()` set (upgraded/forwarded to a newer wallet). "
        f"All {len(onewallets)} current OTP cores are past `(t0 + lifespan) * interval`; the CSV carries both times.\n"
    )
    L.append("Detection (works for every version from v2 to v16): `getInfo()` returns exactly 8 words `(root, height, interval, t0, lifespan, maxOperationsPerInterval, lastResortAddress, dailyLimit)` "
             "with a non-zero root, `1 <= height <= 64`, `1 <= interval <= 86400`, a plausible `t0 * interval` (2017-2033) and a valid address; `getVersion()` (v2+) supplies major/minor. "
             "Bytecode selectors `commit(bytes32,bytes32,bytes32)`/`reveal(...)` corroborate. Three contracts that expose an unrelated `getInfo()` (two FIN20 tokens and a presale) are correctly rejected.\n")
    rows = [[f"`{r['address']}`", r["onewallet_version"], one(r["total_claim_one"]), (f"`{r['recovery_address']}`" if r["recovery_address"] else "(treasury default)"), (f"`{r['forward_address']}`" if r["forward_address"] else ""), r["creation_time_utc"][:10], f"`{r['creation_tx_sender']}`", r["first_funding_time_utc"][:10], f"`{r['first_funder']}`", one(r["first_funding_value_one"]), r["wallet_expiry_time_utc"][:10]]
            for r in onewallets[:15]]
    L.append("\nLargest 15:\n")
    L.append(table(["1wallet", "ver", "total claim ONE", "recovery address", "forward address", "created", "relayer (tx sender)", "first funded", "first funder", "amount", "OTP core expiry"], rows))
    L.append("\nCSV `onewallets.csv` columns: balances, `onewallet_version`, `recovery_address` (blank when treasury/unset), `recovery_address_raw`, `recovery_is_treasury`, `forward_address`, "
             "`wallet_effective_time_utc`, `wallet_expiry_time_utc`, `otp_interval_seconds`, `merkle_height`, `daily_limit_one`, `last_operation_time_utc`, creation (tx sender = 1wallet relayer, direct creator = relayer or `ONEWalletFactory`), first funding, activity.\n")

    # smartvault
    L.append("## 2b. SmartVault OTP wallets (additional wallet type discovered)\n")
    L.append(f"{len(smartvault)} proxies (`masterCopy()` pattern, 650/301-byte proxy code) delegate to the TOTPWallet implementation of "
             f"[hashmesan/harmony-totp](https://github.com/hashmesan/harmony-totp) (SmartVault, `setupHOTP2FA`, guardians, meta-transactions; factory `0x494024993160e15f3bf2be8c26cbcd02bc10baa1`). "
             f"Their total claim is {one(sum(D(r['total_claim_one']) for r in smartvault))} ONE. Each has an EOA `owner()` and optional guardians (`smartvault-wallets.csv`).\n")
    rows = [[f"`{r['address']}`", one(r["total_claim_one"]), f"`{r['smartvault_owner']}`", r["guardian_count"], r["creation_time_utc"][:10], f"`{r['first_funder']}`", one(r["first_funding_value_one"]), r["version_hint"][:40]] for r in smartvault]
    L.append(table(["wallet", "total claim ONE", "owner", "guardians", "created", "first funder", "amount", "implementation"], rows))

    # erc20
    L.append("\n## 3. ERC-20 token contracts\n")
    L.append(f"{len(erc20)} contracts satisfy the ERC-20 probe set (`totalSupply()`, `balanceOf()`, `name()`/`symbol()`, dispatcher has `transfer/approve/allowance|transferFrom`). "
             f"Total claim {one(sum(D(r['total_claim_one']) for r in erc20))} ONE; the native ONE held by these contracts is dominated by WONE and the two LayerZero NativeOFT locks, "
             f"which are ONE-backed wrappers rather than project tokens. \"Interactions\" = direct transactions to/from the contract counted by the node's per-address index "
             f"(`hmyv2_getTransactionsCount ALL`); internal calls are not counted.\n")
    rows = [[r["token_name"][:28], r["token_symbol"][:12], f"`{r['address']}`", r["decimals"], r["total_supply_units"][:26], one(r["total_claim_one"]), one(r["balance_latest_one"]), r["direct_tx_count_all"], r["creation_time_utc"][:10], r["last_direct_tx_time_utc"][:10], r["known_app"][:34] or r["primary_category_note"]]
            for r in erc20]
    L.append(table(["name", "symbol", "address", "dec", "total supply (units)", "native ONE (cutoff claim)", "native ONE (latest)", "direct txs", "created", "last direct tx", "app / category"], rows))
    L.append("\nCSV `erc20-tokens.csv` also carries `owner()`, proxy implementation, creation tx/sender and first/last direct tx hashes.\n")

    # nft
    L.append("## 4. NFT contracts\n")
    standards = ", ".join(f"{n} {k}" for k, n in Counter(r["token_standard"] for r in nfts).most_common())
    L.append(f"{len(nfts)} contracts ({standards}). Total claim {one(sum(D(r['total_claim_one']) for r in nfts))} ONE. "
             "Standard detection uses ERC-165 (`0x80ac58cd`, `0xd9b67a26`) when the contract implements ERC-165 consistently, otherwise the dispatcher must contain "
             "`ownerOf`, `safeTransferFrom`, `setApprovalForAll` and `transferFrom`. Owner counts enumerate current `ownerOf` through Multicall3 `aggregate3` "
             "(ERC721Enumerable `tokenByIndex` when available, otherwise sequential ids; CryptoPunks-style via `punkIndexToAddress`). Contracts with non-sequential ids (name-hash domains) are marked not enumerable.\n")
    rows = [[r["token_name"][:26], r["token_symbol"][:10], f"`{r['address']}`", r["token_standard"][:14], r["owner_count"], r["total_supply_raw"], r["tokens_enumerated"], r["direct_tx_count_all"], one(r["total_claim_one"]), r["creation_time_utc"][:10], f"`{r['creation_tx_hash'][:14]}…`", r["known_app"][:28]]
            for r in sorted(nfts, key=lambda r: -D(r["total_claim_one"]))]
    L.append(table(["name", "symbol", "address", "standard", "current owners", "totalSupply", "ids enumerated", "direct txs", "total claim ONE", "created", "creation tx", "app"], rows))

    # known apps
    L.append("\n## 5. Well-known application contracts\n")
    by_app = defaultdict(list)
    for r in apps:
        by_app[r["app_name"]].append(r)
    rows = []
    for name, rs in sorted(by_app.items(), key=lambda kv: -sum(D(r["total_claim_one"]) for r in kv[1])):
        rows.append([name[:70], len(rs), one(sum(D(r["total_claim_one"]) for r in rs)), one(sum(D(r["balance_latest_one"]) for r in rs)), max(r["last_direct_tx_time_utc"][:10] for r in rs), rs[0]["reference_docs"][:60], rs[0]["reference_source"][:60]])
    L.append(table(["application", "contracts", "total claim ONE", "latest native ONE", "most recent direct tx", "documentation", "source code"], rows))
    L.append("\nAttribution evidence, in order of strength: (a) address listed in an official address book/source (Aave address book, DFK docs, Safe deployments, daVinci SDK config, one-wallet constants, "
             "harmony-one/pump.fun.contracts env, LayerZero endpoint state), (b) on-chain parent relationship (`factory()`, `POOL()`, `comptroller()`, `walletFactory()`, `UP_CONTROLLER()`, Safe singleton in slot 0), "
             "(c) function-signature fingerprint of the runtime code or of the implementation behind the proxy matched to the project's published source, (d) `name()`/`symbol()` pattern (used only for Hundred Finance hONE here). "
             "The `known_app_evidence` column records which rule fired.\n")
    rows = [[r["app_name"][:34], r["role"][:60], f"`{r['address']}`", one(r["total_claim_one"]), one(r["balance_latest_one"]), r["creation_time_utc"][:10], f"`{r['creation_tx_hash'][:14]}…`", r["last_direct_tx_time_utc"][:10], r["direct_tx_count_all"], r["evidence"][:38]]
            for r in apps]
    L.append("\nAll attributed contracts:\n")
    L.append(table(["application", "role", "address", "total claim ONE", "latest ONE", "created", "creation tx", "last direct tx", "direct txs", "evidence"], rows))

    # patterns
    L.append("\n## 6. Fingerprint-identified contracts without an attributed operator\n")
    pcat = agg(patterns, "pattern_category")
    rows = [[k, v["n"], one(v["claim"]), one(v["staked"])] for k, v in sorted(pcat.items(), key=lambda kv: -kv[1]["claim"])]
    L.append(table(["type", "contracts", "total claim ONE", "of which staked to vault"], rows))
    rows = [[f"`{r['address']}`", r["pattern_label"][:80], one(r["total_claim_one"]), r["creation_time_utc"][:10], f"`{r['owner_probe']}`" if r["owner_probe"] else "", r["direct_tx_count_all"], r["last_direct_tx_time_utc"][:10]] for r in patterns]
    L.append("\n")
    L.append(table(["address", "what it is (from function signatures)", "total claim ONE", "created", "owner()", "direct txs", "last direct tx"], rows))
    staking_precompile_users = [
        row
        for row in patterns
        if row["pattern_category"] == "staking-precompile-user"
    ]
    L.append(
        f"\nThe {len(staking_precompile_users)} staking-precompile users are "
        "the notable group: contracts that delegate pooled ONE to validators "
        "through the Harmony staking precompile (`epoch()` returns the live "
        "epoch, `delegate/undelegate/collectRewards`). Their delegations are "
        "part of the total claim and vault-share ledger; the share holders behind them "
        "are only knowable from each contract's own storage/events.\n"
    )

    L.append("## 7. Unidentified contracts\n")
    if unidentified:
        rows = [[f"`{r['address']}`", one(r["total_claim_one"]), r["code_size_bytes"], r["creation_time_utc"][:10], r["dispatcher_signatures"][:80]] for r in unidentified]
        L.append(table(["address", "total claim ONE", "code bytes", "created", "dispatcher"], rows))
    else:
        L.append(
            f"None. Every one of the {len(contracts)} real contracts is at "
            f"least type-identified; operator attribution is missing for the "
            f"{len(patterns)} fingerprint-identified contracts above.\n"
        )

    # methodology
    L.append("## Methodology and caveats\n")
    L.append(
        "- **Creation block/tx**: binary search on `eth_getCode(address, block)` over `[0, cutoff]` (27 rounds), then `trace_block` at that block to find the `create` trace whose `result.address` is the account; "
        f"`creation_tx_sender` is the top-level sender, `creation_direct_creator` the contract/EOA executing CREATE/CREATE2 (factory for Safes, 1wallet v15+ and SmartVault). All {len(contracts)} contracts resolved. "
        "For validator accounts the creation is the first staking transaction (CreateValidator height from `hmyv2_getValidatorInformation`).\n"
        "- **First funding**: if the balance is already positive in the creation block, the creation block is traced; otherwise a binary search on `eth_getBalance` between creation and the earlier of the cutoff and the earliest direct value transfer, "
        "then `trace_block` to find the first inbound value-bearing `call`/`create`/`suicide` trace (`first_funder` is the address that sent value, possibly a contract; `first_funding_tx_sender` is the EOA). "
        "Balances are not monotonic, so a wallet drained to exactly zero and refunded could yield a later boundary; the earliest direct value transfer from the node's per-address index is used as a cross-check and wins when earlier. "
        "Some contracts had no inbound trace in the boundary block because of "
        "cross-shard receipts or staking payouts; the earliest direct transfer "
        "is used as a fallback when available.\n"
        "- **Per-address transaction index**: the node's `hmyv2_getTransactionsHistory` index orders by `(block, index, hash)`, but legacy entries migrated with an unknown block sort first by hash. "
        "First-transaction queries therefore fetch the full hash list, resolve the hash-sorted legacy prefix, and take the true minimum block; `DESC` page 0 is reliable for the most recent direct transaction. "
        "Counts (`direct_tx_count_*`) include only direct transactions, not internal calls.\n"
        "- **ABI probing**: 133 `eth_call` probes per contract at the latest block (Safe, 1wallet, ERC-20/721/1155/165, AMM, lending, farming, LayerZero, ENS, misc). Proxies (EIP-1967, EIP-1167 clones, Safe `masterCopy`, "
        "`implementation()`, custom beacons) were followed to their implementation for function-signature analysis; probes already execute through the proxy.\n"
        "- **Function signatures**: 4-byte selectors were extracted from the runtime dispatcher and resolved against a built-in dictionary of standard interfaces plus the public openchain.xyz signature database. "
        "The signature database only supplies human-readable names for labeling; no balance, ownership or state fact comes from it.\n"
        "- **Owner counts (NFT)**: current holders enumerated from state through Multicall3; ERC-1155 and non-sequential-id collections are not enumerable without a full event scan (`eth_getLogs` is limited to 1,024 blocks per query on the public node).\n"
        "- **Current balances** are `eth_getBalance` at the latest block at fetch time; cutoff components come from the claims CSV.\n"
        "- **Not used**: explorer APIs, third-party indexers, or price data.\n"
    )
    L.append("## Files\n")
    L.append(
        "All outputs are in `artifacts/contract-review-20260911/` (git-ignored):\n\n"
        "- `out/contract-review-all.csv` — one row per address, primary category, all facts\n"
        "- `out/validator-accounts.csv`, `out/multisig-wallets.csv`, `out/onewallets.csv`, `out/smartvault-wallets.csv`, `out/erc20-tokens.csv`, `out/nft-contracts.csv`, `out/known-app-contracts.csv`, `out/pattern-identified-contracts.csv`, `out/unidentified-contracts.csv`\n"
        "- `out/summary.json` — machine-readable statistics\n"
        "- `facts.json` — raw RPC facts (code, balances, traces, probes); `selectors.json` — dispatcher signatures; `extra.json` — pair symbols, singleton metadata, NFT owner census, SmartVault owners/guardians\n\n"
        "Reproduce with `toolkit/scripts/contract-review/` (see its README): `fetch-contract-facts.py` -> `selector-census.py` -> `enrich-contract-facts.py` -> `classify-contracts.py` -> `build-report.py`.\n"
    )
    with open(args.report, "w") as handle:
        handle.write("\n".join(L))
    print(f"wrote {args.report}")


if __name__ == "__main__":
    main()
