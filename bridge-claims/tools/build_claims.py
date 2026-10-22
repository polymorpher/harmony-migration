#!/usr/bin/env python3
"""Build the BridgeClaims list, Merkle tree, and deployment manifest for every claim chain.

    python3 tools/build_claims.py --allocations-dir ../artifacts/bridge-allocations-20261018/allocations \\
        --output-dir runs/bridge-allocations-20261018

Reads the verified claim lists (`allocations/claims/<chain>.csv`) and writes, per claim contract:

- `claims.csv`: the published list, one row per payee and asset, numbered from 0 in
  (account, token) order; native coin rows use token 0x0;
- `tree.txt`: every tree node, leaves first, root last;
- `samples.json`: the first row of every asset with its proof, for spot checks and the fork test;
- `manifest.json`: root, row count, list hash, per-asset totals, the approvals the funder must
  give, and the CREATE2 deployment (constructor arguments and the resulting address).

There is one contract per chain and funding multisig: normally one per chain. The funder is the
chain's `custody_multisig` in the bridge config, or `--funder <label>=<address>`. When
`one_claims.custody_multisig` names a different multisig than the ONE claim chain's custody,
the ONE rows get their own contract funded by that multisig. Chain constants (wrapped native
coin, CREATE2 deployer and salt) come from `chains.json`.

Every asset total must equal the claimable total of its pools in `pools.csv`, and every input
claim list must match the hash recorded in the allocation summary.
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claim_tree as ct  # noqa: E402
import bridge_lib as lib  # noqa: E402  (toolkit/scripts/bridge, on sys.path through claim_tree)

SCHEMA_VERSION = 1
LIST_FIELDS = ["index", "account", "token", "symbol", "amount_raw", "amount", "pools"]
SAMPLE_PROOFS = 2000


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", default=str(ct.REPO / "bridge" / "routes.json"))
    parser.add_argument("--config", default=str(ct.REPO / "bridge" / "local" / "config.json"))
    parser.add_argument("--chains-file", default=str(ct.CHAINS_FILE))
    parser.add_argument("--allocations-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--bytecode",
        default=str(ct.ARTIFACT),
        help="forge artifact of BridgeClaims (default: out/BridgeClaims.sol/BridgeClaims.json after "
        "`forge build`); without it no deployment address is computed",
    )
    parser.add_argument(
        "--funder", action="append", default=[], metavar="LABEL=ADDRESS",
        help="funding multisig of one contract, overriding the bridge config (repeatable)",
    )
    parser.add_argument("--chains", nargs="*", help="only these claim chains")
    parser.add_argument("--workers", type=int, default=ct.default_workers())
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def funder_overrides(values):
    out = {}
    for value in values:
        label, sep, address = value.partition("=")
        if not sep:
            raise SystemExit(f"--funder {value!r}: expected LABEL=ADDRESS")
        out[label] = lib.address(address, f"--funder {label}")
    return out


def load_bytecode(path):
    if not path or not os.path.exists(path):
        print(f"no bytecode at {path}: run `forge build` for deployment addresses", file=sys.stderr)
        return None
    artifact = lib.load_json(path)
    code = artifact["bytecode"]["object"]
    meta = artifact.get("metadata") or {}
    settings = meta.get("settings") or {}
    return {
        "init_code": ct.unhex(code),
        "artifact": os.path.relpath(path),
        "compiler": (meta.get("compiler") or {}).get("version"),
        "evm_version": settings.get("evmVersion"),
        "optimizer": settings.get("optimizer"),
    }


def asset_table(pools, chain):
    """token -> {symbol, decimals, pools, claimable_raw} for the pools paid on `chain`."""
    table = {}
    for pool in pools:
        if pool["claim_chain"] != chain or pool["status"] == "unsupported":
            continue
        token = ct.NATIVE_TOKEN if lib.is_native(pool["claim_token"]) else lib.address(pool["claim_token"])
        entry = table.setdefault(
            token, {"symbol": pool["symbol"], "decimals": int(pool["decimals"]), "pools": [], "claimable_raw": 0}
        )
        if entry["symbol"] != pool["symbol"] or entry["decimals"] != int(pool["decimals"]):
            raise ValueError(f"{chain} {token}: pools disagree on symbol or decimals")
        entry["pools"].append(pool["pool_id"])
        entry["claimable_raw"] += int(pool["claimable_raw"] or 0)
    return table


def read_claims(path, chain, assets):
    rows, seen = [], set()
    for line, row in enumerate(lib.read_csv(path), start=2):
        where = f"{path}:{line}"
        account = lib.address(row["account"], where)
        if int(account, 16) == 0:
            raise ValueError(f"{where}: zero account")
        token = ct.NATIVE_TOKEN if lib.is_native(row["token"]) else lib.address(row["token"], where)
        if token not in assets:
            raise ValueError(f"{where}: {chain} has no pool paying {token}")
        amount = int(row["amount_raw"])
        if amount <= 0 or amount >= 1 << 256:
            raise ValueError(f"{where}: amount out of range")
        if (account, token) in seen:
            raise ValueError(f"{where}: second row for {account} {token}")
        seen.add((account, token))
        rows.append({"account": account, "token": token, "amount": amount, "pools": row["pools"]})
    return rows


def format_amount(raw, decimals):
    text = str(Decimal(raw).scaleb(-decimals).quantize(Decimal(1).scaleb(-decimals)))
    return text.rstrip("0").rstrip(".") if "." in text else text


def contract_groups(config, chain, rows):
    """[(label, funder, funder_source, rows)]: one per funding multisig on this chain."""
    custody = config["chains"][chain].get("custody_multisig")
    custody = lib.address(custody, f"{chain} custody_multisig") if custody else None
    one = config["one_claims"]
    one_funder = lib.one_custody(config)
    one_funder = lib.address(one_funder, "ONE custody multisig") if one_funder else None
    if chain == one["claim_chain"] and one_funder != custody:
        one_rows = [r for r in rows if r["token"] == one["claim_token"]]
        rest = [r for r in rows if r["token"] != one["claim_token"]]
        source = "one_claims.custody_multisig" if one.get("custody_multisig") else "custody_multisig"
        groups = [(f"{chain}-one", one_funder, source, one_rows)]
        if rest:
            groups.insert(0, (chain, custody, "custody_multisig", rest))
        return [g for g in groups if g[3]]
    return [(chain, custody, "custody_multisig", rows)]


def funding_sources(custody_plan):
    """pool id -> where its collateral reaches the funder from: the escrows in the custody plan, or,
    for bridged ONE, the reserve Safe's ONE funding."""
    sources = defaultdict(list)
    for chain in custody_plan["chains"].values():
        for t in chain.get("transfers") or []:
            sources[t["pool_id"]].append({"from": lib.address(t["escrow"]), "amount_raw": str(t["amount_raw"]),
                                          "kind": "escrow"})
    funding = custody_plan.get("one_funding") or {}
    for pool, amount in (funding.get("pools") or {}).items():
        sources[pool].append({"from": lib.address(funding["from"]), "amount_raw": str(amount), "kind": "reserve"})
    return sources


def build_group(args, chains, chain, label, funder, funder_source, rows, assets, bytecode, sources):
    out_dir = os.path.join(args.output_dir, label)
    list_path = os.path.join(out_dir, "claims.csv")
    tree_path = os.path.join(out_dir, "tree.txt")
    samples_path = os.path.join(out_dir, "samples.json")
    manifest_path = os.path.join(out_dir, "manifest.json")
    lib.prepare_outputs([list_path, tree_path, samples_path, manifest_path], args.replace)

    rows = sorted(rows, key=lambda r: (r["account"], r["token"]))
    constants = chains["chains"][chain]
    wrapped = constants.get("wrapped_native")
    wrapped = lib.address(wrapped, f"{chain} wrapped_native") if wrapped else None
    salt = chains["salt"]
    deployer = lib.address(chains["create2_deployer"])
    if any(r["token"] == ct.NATIVE_TOKEN for r in rows) and not wrapped:
        raise ValueError(f"{label}: native rows but no wrapped_native configured for {chain}")

    list_rows = []
    for index, row in enumerate(rows):
        info = assets[row["token"]]
        list_rows.append({
            "index": index,
            "account": row["account"],
            "token": row["token"],
            "symbol": info["symbol"],
            "amount_raw": row["amount"],
            "amount": format_amount(row["amount"], info["decimals"]),
            "pools": row["pools"],
        })
    list_sha = lib.write_csv(list_path, LIST_FIELDS, list_rows)
    list_hash = bytes.fromhex(list_sha)

    leaf_rows = [(i, r["account"], r["token"], r["amount"]) for i, r in enumerate(rows)]
    tree = ct.levels(ct.leaves(leaf_rows, args.workers), args.workers)
    root = ct.root(tree)
    step = max(1, len(rows) // SAMPLE_PROOFS)
    sample = sorted({*range(0, len(rows), step), len(rows) - 1})
    for index in sample:
        if not ct.verify(ct.proof(tree, index), root, tree[0][index]):
            raise RuntimeError(f"{label}: proof for row {index} does not verify")
        if tree[0][index] != ct.leaf(*leaf_rows[index]):
            raise RuntimeError(f"{label}: leaf {index} differs on recomputation")
    ct.write_tree(tree_path, tree)

    first = {}
    for index, row in enumerate(rows):
        first.setdefault(row["token"], index)
    lib.write_json(samples_path, {
        "label": label,
        "root": ct.hex0x(root),
        "rows": [
            {
                "index": index,
                "account": rows[index]["account"],
                "token": rows[index]["token"],
                "symbol": assets[rows[index]["token"]]["symbol"],
                "amount_raw": str(rows[index]["amount"]),
                "proof": [ct.hex0x(node) for node in ct.proof(tree, index)],
            }
            for index in sorted(first.values())
        ],
    })

    per_asset = defaultdict(lambda: {"rows": 0, "total_raw": 0})
    for row in rows:
        entry = per_asset[row["token"]]
        entry["rows"] += 1
        entry["total_raw"] += row["amount"]
    asset_list, approvals = [], {}
    for token in sorted(per_asset, key=lambda t: (t != ct.NATIVE_TOKEN, assets[t]["symbol"].lower(), t)):
        info, entry = assets[token], per_asset[token]
        native = token == ct.NATIVE_TOKEN
        approve_token = wrapped if native else token
        asset_list.append({
            "token": token,
            "symbol": info["symbol"],
            "decimals": info["decimals"],
            "pools": sorted(info["pools"]),
            "rows": entry["rows"],
            "total_raw": str(entry["total_raw"]),
            "total": format_amount(entry["total_raw"], info["decimals"]),
            "approve_token": approve_token,
        })
        approval = approvals.setdefault(approve_token, {
            "symbol": "W" + info["symbol"] if native else info["symbol"],
            "decimals": info["decimals"],
            "amount_raw": 0,
            "covers": [],
            "sources": [],
        })
        approval["amount_raw"] += entry["total_raw"]
        approval["covers"].append(f"native {info['symbol']}" if native else info["symbol"])
        approval["sources"] += [s for pool in sorted(info["pools"]) for s in sources.get(pool, [])]
    approval_list = [
        {"token": token, "symbol": a["symbol"], "decimals": a["decimals"], "amount_raw": str(a["amount_raw"]),
         "amount": format_amount(a["amount_raw"], a["decimals"]), "covers": a["covers"], "sources": a["sources"]}
        for token, a in approvals.items()
    ]

    chain_id = int(constants["chain_id"])
    deployment = {"status": "ready", "create2_deployer": deployer, "salt": salt}
    if not funder:
        deployment["status"] = "no_funder"
    elif bytecode is None:
        deployment["status"] = "no_bytecode"
    if funder:
        args_blob = ct.constructor_args(funder, wrapped, chain_id, root, len(rows), list_hash)
        deployment["constructor"] = {
            "funder": funder,
            "wrappedNative": wrapped or ct.NATIVE_TOKEN,
            "chainId": chain_id,
            "root": ct.hex0x(root),
            "claimCount": len(rows),
            "listHash": "0x" + list_sha,
        }
        deployment["constructor_args"] = ct.hex0x(args_blob)
        if bytecode is not None:
            init_code = bytecode["init_code"] + args_blob
            deployment["address"] = ct.create2_address(deployer, ct.unhex(salt), init_code)
            deployment["bytecode_keccak256"] = ct.hex0x(ct.common.keccak256(bytecode["init_code"]))
            deployment["init_code_keccak256"] = ct.hex0x(ct.common.keccak256(init_code))
            deployment["compiler"] = {k: bytecode[k] for k in ("artifact", "compiler", "evm_version", "optimizer")}

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "chain": chain,
        "chain_id": chain_id,
        "funder": funder,
        "funder_source": funder_source,
        "wrapped_native": wrapped,
        "leaf_scheme": ct.LEAF_SCHEME,
        "tree_scheme": ct.TREE_SCHEME,
        "root": ct.hex0x(root),
        "claim_count": len(rows),
        "payees": len({r["account"] for r in rows}),
        "level_sizes": [len(level) for level in tree],
        "list_file": "claims.csv",
        "list_sha256": list_sha,
        "tree_file": "tree.txt",
        "tree_sha256": lib.file_sha256(tree_path),
        "samples_file": "samples.json",
        "proofs_checked": len(sample),
        "assets": asset_list,
        "approvals": approval_list,
        "deployment": deployment,
    }
    lib.write_json(manifest_path, manifest)
    return manifest


def report(manifests, inputs):
    lines = [
        "# Bridge claim contracts",
        "",
        "One BridgeClaims contract per chain and funding multisig. Addresses are CREATE2 addresses",
        "of the compiled bytecode with the constructor arguments in each `manifest.json`; they are",
        "valid before deployment. The funder approves each contract for the totals below.",
        "",
        "| Contract | Chain | Funder | Rows | Payees | Assets | Root | Address | Status |",
        "|---|---|---|---:|---:|---:|---|---|---|",
    ]
    for m in manifests:
        d = m["deployment"]
        lines.append(
            f"| {m['label']} | {m['chain']} ({m['chain_id']}) | `{m['funder'] or 'none'}` | "
            f"{m['claim_count']:,} | {m['payees']:,} | {len(m['assets'])} | `{m['root']}` | "
            f"`{d.get('address', '—')}` | {d['status']} |"
        )
    lines += [
        "",
        "## Approvals",
        "",
        "| Contract | Approve token | Covers | Amount | Raw |",
        "|---|---|---|---:|---:|",
    ]
    for m in manifests:
        for a in m["approvals"]:
            lines.append(
                f"| {m['label']} | {a['symbol']} `{a['token']}` | {', '.join(a['covers'])} | "
                f"{a['amount']} | {a['amount_raw']} |"
            )
    lines += [
        "",
        "## Assets",
        "",
        "| Contract | Asset | Token | Rows | Total | Pools |",
        "|---|---|---|---:|---:|---|",
    ]
    for m in manifests:
        for a in m["assets"]:
            lines.append(
                f"| {m['label']} | {a['symbol']} | `{a['token']}` | {a['rows']:,} | {a['total']} | "
                f"{', '.join(a['pools'])} |"
            )
    lines += ["", "## Inputs", ""]
    lines += [f"- `{name}`: `{digest}`" for name, digest in sorted(inputs.items())]
    return "\n".join(lines) + "\n"


def main():
    args = parse_args()
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    summary = lib.load_json(os.path.join(args.allocations_dir, "summary.json"))
    if summary.get("status") != "built":
        raise SystemExit("allocation summary is not built")
    pools = lib.read_csv(os.path.join(args.allocations_dir, "pools.csv"))
    bytecode = load_bytecode(args.bytecode)
    chain_constants = ct.load_chains(args.chains_file)
    overrides = funder_overrides(args.funder)
    custody_path = os.path.join(args.allocations_dir, "custody-plan.json")
    for name in ("pools.csv", "custody-plan.json"):
        if summary["outputs"].get(name) != lib.file_sha256(os.path.join(args.allocations_dir, name)):
            raise SystemExit(f"{name} does not match the allocation summary")
    sources = funding_sources(lib.load_json(custody_path))

    chains = args.chains or summary["claim_lists"]
    inputs = {
        "summary.json": lib.file_sha256(os.path.join(args.allocations_dir, "summary.json")),
        "custody-plan.json": lib.file_sha256(custody_path),
        "chains.json": lib.file_sha256(args.chains_file),
    }
    manifests, labels = [], set()
    for chain in chains:
        if chain not in summary["claim_lists"]:
            raise SystemExit(f"{chain} has no claim list")
        constants = chain_constants["chains"].get(chain)
        if constants is None:
            raise SystemExit(f"{args.chains_file} has no entry for {chain}")
        if int(constants["chain_id"]) != int(config["chains"][chain]["chain_id"]):
            raise SystemExit(f"{chain}: chain id differs between {args.chains_file} and the bridge config")
        name = f"claims/{chain}.csv"
        path = os.path.join(args.allocations_dir, name)
        digest = lib.file_sha256(path)
        if summary["outputs"].get(name) != digest:
            raise SystemExit(f"{name} does not match the allocation summary")
        inputs[name] = digest
        assets = asset_table(pools, chain)
        rows = read_claims(path, chain, assets)
        totals = defaultdict(int)
        for row in rows:
            totals[row["token"]] += row["amount"]
        for token, info in assets.items():
            if totals.get(token, 0) != info["claimable_raw"]:
                raise SystemExit(
                    f"{chain} {info['symbol']}: claim rows total {totals.get(token, 0)}, "
                    f"pools claimable {info['claimable_raw']}"
                )
        for label, funder, source, group_rows in contract_groups(config, chain, rows):
            if label in overrides:
                funder, source = overrides[label], "--funder"
            labels.add(label)
            manifest = build_group(
                args, chain_constants, chain, label, funder, source, group_rows, assets, bytecode, sources
            )
            manifests.append(manifest)
            print(
                f"{label}: {manifest['claim_count']:,} rows, root {manifest['root']}, "
                f"{manifest['deployment'].get('address', manifest['deployment']['status'])}"
            )

    unknown = sorted(set(overrides) - labels)
    if unknown:
        raise SystemExit(f"--funder for unknown contracts: {', '.join(unknown)}")

    summary_path = os.path.join(args.output_dir, "summary.json")
    report_path = os.path.join(args.output_dir, "CLAIM_CONTRACTS.md")
    lib.prepare_outputs([summary_path, report_path], args.replace)
    lib.write_json(summary_path, {
        "schema_version": SCHEMA_VERSION,
        "inputs": inputs,
        "contracts": [
            {k: m[k] for k in ("label", "chain", "chain_id", "funder", "root", "claim_count", "list_sha256")}
            | {"address": m["deployment"].get("address"), "status": m["deployment"]["status"]}
            for m in manifests
        ],
    })
    lib.write_text(report_path, report(manifests, inputs))
    print(f"wrote {summary_path} and {report_path}")


if __name__ == "__main__":
    main()
