#!/usr/bin/env python3
"""Look up one payee in a BridgeClaims list and print every row of it with its proof.

    python3 tools/proof.py runs/<run>/<label> 0x1234...        # or a one1... address
    python3 tools/proof.py runs/<run>/<label> 0x1234... --cast # plus a claimMany command

Prints JSON: the contract (label, chain, address, root) and, for each of the payee's rows, the
index, token, symbol, amount, and proof. Every proof is checked against the manifest's root
before it is printed. `--cast` adds a `cast send` command that pays all of the payee's rows in one
`claimMany` transaction; anyone may send it, the tokens always go to the payee.
"""

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claim_tree as ct  # noqa: E402

CLAIM_MANY = "claimMany((uint256,address,address,uint256,bytes32[])[])"


def payee_address(text):
    text = text.strip()
    if text.lower().startswith("one1"):
        return ct.common.from_one1(text, checksum=False).lower()
    return ct.common.normalize_address(text, "payee").lower()


def lookup(contract_dir, payee):
    with open(os.path.join(contract_dir, "manifest.json"), encoding="utf-8") as source:
        manifest = json.load(source)
    with open(os.path.join(contract_dir, manifest["list_file"]), newline="", encoding="utf-8") as source:
        rows = [r for r in csv.DictReader(source) if r["account"] == payee]
    out = {
        "label": manifest["label"],
        "chain": manifest["chain"],
        "chain_id": manifest["chain_id"],
        "address": manifest["deployment"].get("address"),
        "root": manifest["root"],
        "account": ct.common.to_checksum(payee),
        "rows": [],
    }
    if not rows:
        return out
    tree = ct.read_tree(os.path.join(contract_dir, manifest["tree_file"]), manifest["level_sizes"])
    root = ct.unhex(manifest["root"])
    if ct.root(tree) != root:
        raise SystemExit(f"{contract_dir}: tree root differs from the manifest")
    for row in rows:
        index, amount = int(row["index"]), int(row["amount_raw"])
        leaf = ct.leaf(index, row["account"], row["token"], amount)
        if tree[0][index] != leaf:
            raise SystemExit(f"row {index}: leaf in the tree differs from the list")
        proof = ct.proof(tree, index)
        if not ct.verify(proof, root, leaf):
            raise SystemExit(f"row {index}: proof does not verify")
        out["rows"].append({
            "index": index,
            "token": ct.common.to_checksum(row["token"]),
            "symbol": row["symbol"],
            "amount_raw": str(amount),
            "amount": row["amount"],
            "proof": [ct.hex0x(node) for node in proof],
        })
    return out


def cast_command(result):
    claims = ",".join(
        f"({r['index']},{result['account']},{r['token']},{r['amount_raw']},[{','.join(r['proof'])}])"
        for r in result["rows"]
    )
    # --chain makes cast sign for this chain id, so an RPC of another chain rejects the transaction.
    return (f'cast send {result["address"]} "{CLAIM_MANY}" "[{claims}]" --chain {result["chain_id"]} '
            '--rpc-url "$RPC" --account <keystore>')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("contract_dir", help="runs/<run>/<label>, written by tools/build_claims.py")
    parser.add_argument("payee", help="0x or one1 address")
    parser.add_argument("--cast", action="store_true", help="also print a claimMany cast command")
    args = parser.parse_args()
    try:
        payee = payee_address(args.payee)
    except ct.common.InputError as error:
        raise SystemExit(str(error))
    result = lookup(args.contract_dir, payee)
    print(json.dumps(result, indent=1))
    if not result["rows"]:
        print(f"{result['account']} has no rows in {result['label']}", file=sys.stderr)
    elif args.cast:
        if not result["address"]:
            raise SystemExit("the manifest has no deployment address (no funder or no bytecode)")
        print(cast_command(result))


if __name__ == "__main__":
    main()
