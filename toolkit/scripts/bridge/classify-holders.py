#!/usr/bin/env python3

"""Classify every holder in the planned snapshots by account type.

Code is read at each snapshot block. Accounts without code, EIP-7702
delegated accounts, and Harmony validator wrappers are key-controlled.
Contracts that answer getThreshold() and getOwners() are Safes; contracts
that answer required() and getOwners() are MultiSigWallets. On Harmony the
cutoff contract review adds 1wallet, SmartVault, multisig, and validator
classifications for the accounts it covers.
"""

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402

FIELDS = (
    "chain",
    "address",
    "block",
    "class",
    "code_size",
    "code_sha256",
    "owners",
    "threshold",
    "evidence",
)
KEY_CONTROLLED = {"eoa", "eoa_7702", "validator_wrapper"}
REVIEW_CLASSES = {
    "onewallet": "onewallet",
    "multisig-wallet": "multisig",
    "smartvault-wallet": "smartvault",
    "validator-account": "validator_wrapper",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--holders-dir", required=True)
    parser.add_argument("--contract-review", help="Harmony contract-review-all.csv")
    parser.add_argument("--previous", help="earlier output to reuse")
    parser.add_argument("--chain", action="append", default=[], help="classify only these chains")
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def classify(chain, account, code, safe, multisig, review):
    code = code or "0x"
    size = (len(code) - 2) // 2
    fingerprint = hashlib.sha256(bytes.fromhex(code[2:])).hexdigest() if size else ""
    row = {
        "address": account,
        "code_size": size,
        "code_sha256": fingerprint,
        "owners": "",
        "threshold": "",
        "evidence": "",
    }
    if size == 0:
        row["class"] = "eoa"
        return row
    if code.lower().startswith("0xef0100") and size == 23:
        row["class"] = "eoa_7702"
        row["evidence"] = "delegated to 0x" + code[8:]
        return row
    if chain == lib.HARMONY and lib.crl.rlp_validator_wrapper_address(code) == account:
        row["class"] = "validator_wrapper"
        row["evidence"] = "ValidatorWrapper RLP for this address"
        return row
    if review:
        row["class"] = review
        row["evidence"] = "cutoff contract review"
        if safe:
            row["owners"], row["threshold"] = ";".join(safe[0]), safe[1]
        return row
    if safe:
        row["class"] = "safe"
        row["owners"], row["threshold"] = ";".join(safe[0]), safe[1]
        return row
    if multisig:
        row["class"] = "multisig"
        row["owners"], row["threshold"] = ";".join(multisig[0]), multisig[1]
        return row
    row["class"] = "contract"
    return row


def main():
    args = parse_args()
    lib.prepare_outputs([args.output, args.summary], args.replace)
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    plan = lib.load_json(args.plan)

    review = {}
    if args.contract_review:
        for row in lib.read_csv(args.contract_review):
            mapped = REVIEW_CLASSES.get(row["primary_category"])
            if mapped:
                review[lib.address(row["address"])] = mapped
    previous = {}
    if args.previous:
        for row in lib.read_csv(args.previous):
            previous[(row["chain"], row["address"], int(row["block"]))] = row

    wanted = {}
    for snapshot in plan["holder_snapshots"]:
        if args.chain and snapshot["chain"] not in args.chain:
            continue
        path = os.path.join(args.holders_dir, snapshot["stem"] + ".csv")
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        for row in lib.read_csv(path):
            if int(row["balance_raw"]) > 0:
                wanted[(snapshot["chain"], lib.address(row["address"]), snapshot["block"])] = True

    rows = []
    by_chain = {}
    for key in sorted(wanted):
        if key in previous:
            rows.append(previous[key])
        else:
            by_chain.setdefault((key[0], key[2]), []).append(key[1])
    for (chain, block), accounts in sorted(by_chain.items()):
        rpc = lib.chain_rpc(config, chain)
        tag = lib.block_tag(block)
        codes = rpc.batch([("eth_getCode", [account, tag]) for account in accounts])
        failed = [account for account, (_, error) in zip(accounts, codes) if error is not None]
        if failed:
            raise lib.RpcError(f"{chain}: eth_getCode failed for {len(failed)} accounts")
        contracts = [
            account for account, (code, _) in zip(accounts, codes)
            if code and code != "0x" and not code.lower().startswith("0xef0100")
        ]
        probes = rpc.batch([
            ("eth_call", [{"to": account, "data": lib.SEL[name]}, tag])
            for account in contracts
            for name in ("getOwners", "getThreshold", "required")
        ])
        safes, multisigs = {}, {}
        for index, account in enumerate(contracts):
            owners_result, threshold_result, required_result = (
                probes[3 * index][0], probes[3 * index + 1][0], probes[3 * index + 2][0]
            )
            owners = lib.crl.decode_address_array(owners_result) if owners_result else None
            threshold = lib.decode_uint(threshold_result) if threshold_result else None
            required = lib.decode_uint(required_result) if required_result else None
            if owners and threshold:
                safes[account] = (owners, threshold)
            elif owners and required:
                multisigs[account] = (owners, required)
        for account, (code, _) in zip(accounts, codes):
            row = classify(
                chain, account, code, safes.get(account), multisigs.get(account),
                review.get(account) if chain == lib.HARMONY else None,
            )
            row.update(chain=chain, block=block)
            rows.append(row)
        print(f"{chain} block {block}: classified {len(accounts)} accounts", flush=True)
    rows.sort(key=lambda row: (row["chain"], int(row["block"]), row["address"]))
    digest = lib.write_csv(args.output, FIELDS, rows)
    counts = {}
    for row in rows:
        counts.setdefault(row["chain"], {}).setdefault(row["class"], 0)
        counts[row["chain"]][row["class"]] += 1
    lib.write_json(args.summary, {
        "schema_version": 1,
        "status": "passed",
        "accounts": len(rows),
        "classes_by_chain": counts,
        "output": os.path.basename(args.output),
        "output_sha256": digest,
    })
    print(f"wrote {args.output} ({len(rows)} accounts)")


if __name__ == "__main__":
    main()
