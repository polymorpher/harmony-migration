#!/usr/bin/env python3
"""Prepare the Safe transactions that let each BridgeClaims contract pay its list.

    python3 tools/approve_claims.py --claims-dir runs/bridge-allocations-20261018 --rpc

For every contract built by `tools/build_claims.py` this writes, next to its manifest:

- `safe-approve.json`: a Transaction Builder batch for the funder Safe. It wraps the native
  total (WETH/WBNB `deposit`) when the list has native rows, then approves the contract for
  exactly each asset's total. Tokens that refuse to change a nonzero approval (Ethereum USDT)
  are first approved for 0.
- `safe-revoke.json`: approves 0 for every asset, which stops all claims at once.
- `approvals.json` and `APPROVALS.md`: every call with its decoded arguments, the single Safe
  transaction the batch becomes (MultiSendCallOnly, or a direct call for one), and, when the
  Safe nonce is known, the domain, message, and Safe transaction hashes signers compare.

The spender is the CREATE2 address in the manifest, which is valid before deployment. Tokens that
need the reset come from `chains.json`.

With `--rpc` (chain RPC URLs in the environment, see bridge/README.md) it also reads the chain:
the Safe's owners, threshold, version, and nonce; whether the contract is deployed and its
immutables match the manifest; and the Safe's balances and current approvals. Nothing is sent.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claim_tree as ct  # noqa: E402
import bridge_lib as lib  # noqa: E402  (toolkit/scripts/bridge, on sys.path through claim_tree)
import safe_batch  # noqa: E402  (airdrop/tools, on sys.path through claim_tree)

common = ct.common
SCHEMA_VERSION = 1
APPROVE = common.selector("approve(address,uint256)")
DEPOSIT = common.selector("deposit()")
APPROVE_METHOD = {
    "inputs": [
        {"internalType": "address", "name": "spender", "type": "address"},
        {"internalType": "uint256", "name": "value", "type": "uint256"},
    ],
    "name": "approve",
    "payable": False,
}
DEFAULT_SAFE_VERSION = "1.3.0"
VIEWS = {
    "funder": "funder()",
    "wrappedNative": "wrappedNative()",
    "chainId": "chainId()",
    "root": "root()",
    "claimCount": "claimCount()",
    "listHash": "listHash()",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", default=str(ct.REPO / "bridge" / "routes.json"))
    parser.add_argument("--config", default=str(ct.REPO / "bridge" / "local" / "config.json"))
    parser.add_argument("--chains-file", default=str(ct.CHAINS_FILE))
    parser.add_argument("--claims-dir", required=True, help="output directory of tools/build_claims.py")
    parser.add_argument("--labels", nargs="*", help="only these contracts (default: every ready one)")
    parser.add_argument("--rpc", action="store_true", help="read balances, approvals, and the Safe from the chain")
    parser.add_argument("--nonce", type=int, help="Safe nonce for the hashes (one contract only; --rpc reads it)")
    parser.add_argument("--safe-version", help=f"Safe version without --rpc (default {DEFAULT_SAFE_VERSION})")
    parser.add_argument("--no-wrap", action="store_true", help="the Safe already holds the wrapped coin")
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


# ------------------------------------------------------------------ calls


def approve_call(token, spender, amount, note):
    data = APPROVE + common.unhex(lib.address_word(spender)) + int(amount).to_bytes(32, "big")
    return {
        "to": lib.address(token),
        "value": 0,
        "data": data,
        "method": "approve(address,uint256)",
        "args": {"spender": lib.address(spender), "value": str(amount)},
        "note": note,
    }


def deposit_call(wrapped, amount, note):
    return {
        "to": lib.address(wrapped),
        "value": int(amount),
        "data": DEPOSIT,
        "method": "deposit()",
        "args": {},
        "note": note,
    }


def pack(calls):
    """MultiSend's packed list: operation, to, value, data length, data for each call."""
    packed = b"".join(
        bytes([safe_batch.CALL]) + common.unhex(c["to"]) + c["value"].to_bytes(32, "big")
        + len(c["data"]).to_bytes(32, "big") + c["data"]
        for c in calls
    )
    padding = b"\x00" * (-len(packed) % 32)
    return safe_batch.MULTISEND_SELECTOR + (32).to_bytes(32, "big") + len(packed).to_bytes(32, "big") + packed + padding


def safe_transaction(calls, multisend, nonce):
    """The single transaction the Safe web app creates from the batch."""
    if len(calls) == 1:
        c = calls[0]
        return safe_batch.SafeTx(c["to"], c["value"], c["data"], safe_batch.CALL, nonce)
    return safe_batch.SafeTx(multisend, 0, pack(calls), safe_batch.DELEGATECALL, nonce)


def tx_builder(chain_id, safe, calls, name, description):
    transactions = []
    for c in calls:
        if c["method"] == "approve(address,uint256)":
            transactions.append({
                "to": common.to_checksum(c["to"]),
                "value": "0",
                "data": None,
                "contractMethod": APPROVE_METHOD,
                "contractInputsValues": {
                    "spender": common.to_checksum(c["args"]["spender"]),
                    "value": c["args"]["value"],
                },
            })
        elif c.get("contract_method"):
            transactions.append({
                "to": common.to_checksum(c["to"]),
                "value": str(c["value"]),
                "data": None,
                "contractMethod": c["contract_method"],
                "contractInputsValues": c["contract_inputs"],
            })
        else:
            transactions.append({
                "to": common.to_checksum(c["to"]),
                "value": str(c["value"]),
                "data": common.hex0x(c["data"]),
                "contractMethod": None,
                "contractInputsValues": None,
            })
    batch = {
        "version": "1.0",
        "chainId": str(chain_id),
        "createdAt": int(time.time() * 1000),
        "meta": {
            "name": name,
            "description": description,
            "txBuilderVersion": safe_batch.TX_BUILDER_VERSION,
            "createdFromSafeAddress": common.to_checksum(safe),
            "createdFromOwnerAddress": "",
        },
        "transactions": transactions,
    }
    batch["meta"]["checksum"] = safe_batch.tx_builder_checksum(batch)
    return batch


# ------------------------------------------------------------------ chain reads


class Chain:
    def __init__(self, rpc):
        self.rpc = rpc

    def call(self, to, data):
        result, error = self.rpc.call_soft("eth_call", [{"to": to, "data": data}, "latest"])
        return None if error else result

    def uint(self, to, data):
        return lib.decode_uint(self.call(to, data))

    def code_size(self, address):
        return (len(self.rpc.call("eth_getCode", [address, "latest"])) - 2) // 2

    def native_balance(self, address):
        return int(self.rpc.call("eth_getBalance", [address, "latest"]), 16)

    def erc20(self, token, name, *words):
        return self.uint(token, "0x" + common.selector(name).hex() + "".join(words))


def preflight(chain, manifest, spender, approvals, wrapped, requirements, expected_owners=None):
    checks, state = [], {}

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    funder = manifest["funder"]
    chain_id = int(chain.rpc.call("eth_chainId", []), 16)
    check("chain id", chain_id == manifest["chain_id"], f"{chain_id}")

    size = chain.code_size(funder)
    threshold = chain.uint(funder, lib.call_data("getThreshold"))
    owners_raw = chain.call(funder, lib.call_data("getOwners"))
    owners = lib.crl.decode_address_array(owners_raw) if owners_raw and owners_raw != "0x" else []
    version_raw = chain.call(funder, lib.call_data("VERSION"))
    version = lib.crl.decode_string(version_raw) if version_raw and version_raw != "0x" else None
    nonce = chain.uint(funder, "0x" + common.selector("nonce()").hex())
    state["safe"] = {"code_size": size, "owners": owners, "threshold": threshold, "version": version, "nonce": nonce}
    check("funder is a Safe", size > 0 and threshold is not None and owners, f"version {version}")
    check(
        "funder owners and threshold",
        len(owners) == requirements["owner_count"] and threshold == requirements["threshold"],
        f"{len(owners)} owners, threshold {threshold} (required {requirements['owner_count']} and "
        f"{requirements['threshold']})",
    )
    expected_list = expected_owners or requirements.get("owners")
    if expected_list:
        expected_set = sorted(lib.address(o) for o in expected_list)
        actual_set = sorted(lib.address(o) for o in owners)
        check(
            "funder owner set matches expected",
            actual_set == expected_set,
            f"matches {len(expected_set)} expected owners"
            if actual_set == expected_set
            else f"owners {', '.join(actual_set)} != expected {', '.join(expected_set)}",
        )

    deployed = chain.code_size(spender) > 0
    state["contract_deployed"] = deployed
    if deployed:
        expected = manifest["deployment"]["constructor"]
        for name, signature in VIEWS.items():
            raw = chain.call(spender, "0x" + common.selector(signature).hex())
            if name in ("funder", "wrappedNative"):
                value = lib.decode_address(raw)
                ok = value == lib.address(expected[name])
            elif name in ("root", "listHash"):
                value = raw
                ok = raw is not None and int(raw, 16) == int(expected[name], 16)
            else:
                value = lib.decode_uint(raw)
                ok = value == int(expected[name])
            check(f"contract {name}()", ok, f"{value}")
    else:
        factory = chain.code_size(manifest["deployment"]["create2_deployer"])
        check("CREATE2 deployer present (contract not deployed yet)", factory > 0, f"{factory} bytes")

    state["tokens"] = {}
    for a in approvals:
        token = a["token"]
        balance = chain.erc20(token, "balanceOf(address)", lib.address_word(funder))
        allowance = chain.erc20(token, "allowance(address,address)", lib.address_word(funder), lib.address_word(spender))
        entry = {
            "balance_raw": None if balance is None else str(balance),
            "allowance_raw": None if allowance is None else str(allowance),
        }
        need = int(a["amount_raw"])
        if token == wrapped:
            native = chain.native_balance(funder)
            entry["native_balance_raw"] = str(native)
            have = (balance or 0) + native
        else:
            have = balance or 0
        check(
            f"{a['symbol']} funded",
            balance is not None and have >= need,
            f"Safe holds {lib.format_units(have, a['decimals'])}, needs {a['amount']}"
            if balance is not None else "balanceOf failed",
        )
        state["tokens"][token] = entry
    return checks, state, nonce, version


# ------------------------------------------------------------------ one contract


def build(args, config, chains, manifest, out_dir, chain, expected_owners=None):
    spender = manifest["deployment"]["address"]
    funder = manifest["funder"]
    wrapped = manifest["wrapped_native"]
    approvals = manifest["approvals"]
    reset = {lib.address(t) for t in chains["chains"][manifest["chain"]].get("approve_reset_tokens") or []}
    native_total = sum(int(a["total_raw"]) for a in manifest["assets"] if a["token"] == ct.NATIVE_TOKEN)

    checks, state, nonce, version = [], {}, args.nonce, args.safe_version
    if chain is not None:
        checks, state, rpc_nonce, rpc_version = preflight(
            chain, manifest, spender, approvals, wrapped, config["custody_requirements"], expected_owners
        )
        nonce = rpc_nonce if nonce is None else nonce
        version = version or rpc_version
    version = version or DEFAULT_SAFE_VERSION
    if version not in safe_batch.MULTISEND_CALL_ONLY:
        raise SystemExit(f"{manifest['label']}: no MultiSendCallOnly known for Safe {version}")
    multisend = lib.address(safe_batch.MULTISEND_CALL_ONLY[version][0])

    calls = []
    if native_total and not args.no_wrap:
        wrap = native_total
        if state.get("tokens"):
            held = int(state["tokens"][wrapped]["balance_raw"] or 0)
            wrapped_need = next(int(a["amount_raw"]) for a in approvals if a["token"] == wrapped)
            wrap = min(native_total, max(0, wrapped_need - held))
        if wrap:
            calls.append(deposit_call(wrapped, wrap, "wrap the native coin the native rows pay"))
    for a in approvals:
        token = a["token"]
        current = int((state.get("tokens", {}).get(token) or {}).get("allowance_raw") or 0)
        if token in reset and (chain is None or current != 0):
            calls.append(approve_call(token, spender, 0, f"{a['symbol']} only accepts a new approval from 0"))
        calls.append(approve_call(token, spender, int(a["amount_raw"]), f"{a['symbol']} total of the rows"))
    revoke = [approve_call(a["token"], spender, 0, f"stop {a['symbol']} claims") for a in approvals]

    label = manifest["label"]
    description = (
        f"BridgeClaims {label}: root {manifest['root']}, {manifest['claim_count']} rows, "
        f"spender {spender}"
    )
    approve_batch = tx_builder(manifest["chain_id"], funder, calls, f"Approve BridgeClaims {label}", description)
    revoke_batch = tx_builder(manifest["chain_id"], funder, revoke, f"Revoke BridgeClaims {label}", description)

    def safe_record(call_list):
        tx = safe_transaction(call_list, multisend, nonce)
        return safe_batch.tx_record(manifest["chain_id"], funder, tx)

    record = {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "chain": manifest["chain"],
        "chain_id": manifest["chain_id"],
        "funder": funder,
        "spender": spender,
        "spender_deployed": state.get("contract_deployed"),
        "root": manifest["root"],
        "claim_count": manifest["claim_count"],
        "manifest_sha256": lib.file_sha256(os.path.join(out_dir, "manifest.json")),
        "safe_version": version,
        "multisend_call_only": multisend,
        "approve": {
            "calls": [dict(c, data=common.hex0x(c["data"]), value=str(c["value"])) for c in calls],
            "safe_transaction": safe_record(calls),
            "tx_builder_checksum": approve_batch["meta"]["checksum"],
        },
        "revoke": {
            "calls": [dict(c, data=common.hex0x(c["data"]), value=str(c["value"])) for c in revoke],
            "safe_transaction": safe_record(revoke),
            "tx_builder_checksum": revoke_batch["meta"]["checksum"],
        },
        "preflight": {"checked": chain is not None, "checks": checks, "state": state},
    }
    paths = {name: os.path.join(out_dir, name) for name in
             ("safe-approve.json", "safe-revoke.json", "approvals.json", "APPROVALS.md")}
    lib.prepare_outputs(paths.values(), args.replace)
    for name, value in (("safe-approve.json", approve_batch), ("safe-revoke.json", revoke_batch)):
        lib.write_text(paths[name], json.dumps(value, indent=1) + "\n")
    lib.write_json(paths["approvals.json"], record)
    lib.write_text(paths["APPROVALS.md"], signing_sheet(manifest, record))
    return record


def describe(call, approvals_by_token, wrapped):
    if call["method"] == "deposit()":
        return f"wrap {lib.format_units(call['value'], 18)} native coin into `{call['to']}`"
    a = approvals_by_token[call["to"]]
    amount = int(call["args"]["value"])
    return f"approve {lib.format_units(amount, a['decimals'])} {a['symbol']} (`{call['to']}`)"


def signing_sheet(manifest, record):
    by_token = {a["token"]: a for a in manifest["approvals"]}
    d = manifest["deployment"]
    lines = [
        f"# Approve BridgeClaims {manifest['label']}",
        "",
        f"- Chain: {manifest['chain']} ({manifest['chain_id']})",
        f"- Funder Safe: `{manifest['funder']}` (Safe {record['safe_version']})",
        f"- Spender (BridgeClaims): `{record['spender']}`"
        + ("" if record["spender_deployed"] is None else
           (" — deployed" if record["spender_deployed"] else " — not deployed yet")),
        f"- Root: `{manifest['root']}`; rows: {manifest['claim_count']:,}; list SHA-256 `{manifest['list_sha256']}`",
        "",
        "## Before signing",
        "",
        "1. The spender is the CREATE2 address of the published bytecode and these constructor",
        f"   arguments through the deployer `{d['create2_deployer']}` with salt `{d['salt']}`:",
        "",
        "   ```text",
    ]
    lines += [f"   {k} = {v}" for k, v in d["constructor"].items()]
    lines += [
        "   ```",
        "",
        "   After deployment, read the same six values from the contract (`funder()`, `wrappedNative()`,",
        "   `chainId()`, `root()`, `claimCount()`, `listHash()`).",
        "2. Recompute the root from `claims.csv` (`bridge-claims/tools/build_claims.py`) or check it against",
        "   the published manifest.",
        "3. Compare the Safe transaction hash below with the one the Safe web app shows.",
        "4. Execute the collateral release (and any Safe owner-setup batch) before this approval batch:",
        "   each earlier Safe transaction advances the Safe's nonce, and chains with native ETH/BNB rows",
        "   wrap the released native coin in call 1. Rerun `approve_claims.py --rpc` right before signing",
        "   so the Safe transaction hash below uses the post-release nonce.",
        "",
        "## Approve batch (`safe-approve.json`)",
        "",
        "| # | Call | To | Value | Call data |",
        "|---:|---|---|---:|---|",
    ]
    for i, c in enumerate(record["approve"]["calls"], start=1):
        lines.append(
            f"| {i} | {describe(c | {'value': int(c['value'])}, by_token, manifest['wrapped_native'])} | "
            f"`{c['to']}` | {c['value']} | `{c['data']}` |"
        )
    lines += ["", *sheet_hashes(record["approve"]["safe_transaction"]), ""]
    lines += [
        "## Revoke batch (`safe-revoke.json`)",
        "",
        "Approves 0 for every asset: every claim stops until a new approval. Keep it ready.",
        "",
        "| # | Call | To | Call data |",
        "|---:|---|---|---|",
    ]
    for i, c in enumerate(record["revoke"]["calls"], start=1):
        lines.append(f"| {i} | {c['note']} | `{c['to']}` | `{c['data']}` |")
    lines += ["", *sheet_hashes(record["revoke"]["safe_transaction"]), ""]
    if record["preflight"]["checked"]:
        lines += ["## Chain checks", "", "| Check | Result | Detail |", "|---|---|---|"]
        for c in record["preflight"]["checks"]:
            lines.append(f"| {c['check']} | {'ok' if c['ok'] else '**not yet**'} | {c['detail']} |")
        lines.append("")
    return "\n".join(lines)


def sheet_hashes(tx):
    lines = [
        f"Safe transaction: to `{tx['to']}`, operation {tx['operation']}, value {tx['value']}, "
        f"data keccak `{tx['dataKeccak256']}`.",
    ]
    if tx["nonce"] is None:
        lines.append("Nonce unknown (rerun with `--rpc` or `--nonce`); hashes depend on it.")
    else:
        lines += [
            "",
            f"- Nonce: {tx['nonce']}",
            f"- Domain hash: `{tx['domainHash']}`",
            f"- Message hash: `{tx['messageHash']}`",
            f"- Safe transaction hash: `{tx['safeTxHash']}`",
        ]
    return lines


def main():
    args = parse_args()
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    chains = ct.load_chains(args.chains_file)
    summary = lib.load_json(os.path.join(args.claims_dir, "summary.json"))
    labels = args.labels or [c["label"] for c in summary["contracts"] if c["status"] == "ready"]
    if args.nonce is not None and len(labels) != 1:
        raise SystemExit("--nonce needs exactly one contract (--labels)")
    expected_owners = config["custody_requirements"].get("owners")
    if args.rpc and not expected_owners:
        eth_safe = config["chains"].get("ethereum", {}).get("custody_multisig")
        if eth_safe:
            eth_chain = Chain(lib.chain_rpc(config, "ethereum"))
            raw = eth_chain.call(eth_safe, lib.call_data("getOwners"))
            owners = lib.crl.decode_address_array(raw) if raw and raw != "0x" else []
            if len(owners) == config["custody_requirements"]["owner_count"]:
                expected_owners = owners
    for label in labels:
        out_dir = os.path.join(args.claims_dir, label)
        manifest = lib.load_json(os.path.join(out_dir, "manifest.json"))
        if manifest["deployment"]["status"] != "ready":
            raise SystemExit(f"{label}: deployment is {manifest['deployment']['status']}")
        chain = Chain(lib.chain_rpc(config, manifest["chain"])) if args.rpc else None
        record = build(args, config, chains, manifest, out_dir, chain, expected_owners)
        failed = [c["check"] for c in record["preflight"]["checks"] if not c["ok"]]
        tx = record["approve"]["safe_transaction"]
        print(
            f"{label}: {len(record['approve']['calls'])} calls to approve {record['spender']}"
            + (f", Safe tx hash {tx['safeTxHash']} (nonce {tx['nonce']})" if tx["nonce"] is not None else "")
            + (f"; not yet: {', '.join(failed)}" if failed else "")
        )
    skipped = [c["label"] for c in summary["contracts"] if c["status"] != "ready" and c["label"] not in labels]
    if skipped and not args.labels:
        print("skipped (no funder or bytecode): " + ", ".join(skipped))


if __name__ == "__main__":
    main()
