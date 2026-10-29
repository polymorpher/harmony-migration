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
  transaction the batch becomes (MultiSendCallOnly, or a direct call for one), and the domain,
  message, and Safe transaction hashes signers compare.

The spender is not read from the manifest: the tool recomputes it as the CREATE2 address of the
compiled creation code (`forge build`) and the manifest's constructor arguments through the
canonical deployer and salt in `chains.json`, and stops when the manifest disagrees. Tokens that
need the reset come from `chains.json`.

With `--rpc` (chain RPC URLs in the environment, see bridge/README.md) it reads the chain at one
pinned block: the funder is an unmodified Safe 1.3.0 (proxy code, singleton, fallback handler, no
module, no guard) whose owners are the roster in the config (`custody_requirements.owners`) and
whose threshold is the required one; its nonce; the code of the CREATE2 deployer and of
MultiSendCallOnly; the contract's code and immutables once deployed; and the Safe's balances and
current approvals. Signable files are written only when every check passes. `--draft` writes the
same files as `*.draft.*`, marked not to be signed, without `--rpc` or despite failed checks.
Nothing is sent.
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
SCHEMA_VERSION = 2
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
VIEWS = {
    "funder": "funder()",
    "wrappedNative": "wrappedNative()",
    "chainId": "chainId()",
    "root": "root()",
    "claimCount": "claimCount()",
    "listHash": "listHash()",
}
OUTPUTS = ("safe-approve.json", "safe-revoke.json", "approvals.json", "APPROVALS.md")

# Safe 1.3.0 as deployed (github.com/safe-global/safe-deployments) for the custody Safe on all five
# chains: the proxy's runtime code, the singleton and its code, and the fallback handlers allowed
# (none, or CompatibilityFallbackHandler).
SAFE_VERSION = "1.3.0"
SAFE_PROXY_CODEHASH = "0xb89c1b3bdf2cf8827818646bce9a8f6e372885f8c55e5c07acbd307cb133b000"
SAFE_SINGLETONS = {
    "0xd9db270c1b5e3bd161e8c8503c55ceabee709552": "0xbba688fbdb21ad2bb58bc320638b43d94e7d100f6f3ebaab0a4e4de6304b1c2e",
}
SAFE_FALLBACK_HANDLERS = {lib.ZERO_ADDRESS, "0xf48f2b2d2a534e402487b3ee7c18c33aec0fe5e4"}
GUARD_SLOT = common.hex0x(common.keccak256(b"guard_manager.guard.address"))
FALLBACK_HANDLER_SLOT = common.hex0x(common.keccak256(b"fallback_manager.handler.address"))
SENTINEL = "0x" + "00" * 19 + "01"
EMPTY_CODEHASH = common.hex0x(common.keccak256(b""))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", default=str(ct.REPO / "bridge" / "routes.json"))
    parser.add_argument("--config", default=str(ct.REPO / "bridge" / "local" / "config.json"))
    parser.add_argument("--chains-file", default=str(ct.CHAINS_FILE))
    parser.add_argument("--bytecode", default=str(ct.ARTIFACT), help="forge artifact of BridgeClaims")
    parser.add_argument("--claims-dir", required=True, help="output directory of tools/build_claims.py")
    parser.add_argument("--labels", nargs="*", help="only these contracts (default: every ready one)")
    parser.add_argument("--rpc", action="store_true", help="check the chain at one block (needed for signable files)")
    parser.add_argument("--nonce", type=int, help="Safe nonce for the hashes (one contract only); with --rpc it must "
                        "equal the Safe's nonce")
    parser.add_argument("--draft", action="store_true", help="write *.draft.* files marked not to be signed, without "
                        "--rpc or despite failed checks")
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


# ------------------------------------------------------------------ the spender


def bound_deployment(manifest, chains, artifact, out_dir):
    """The spender, recomputed rather than read: the CREATE2 address of the compiled creation code and
    the manifest's constructor arguments through the canonical deployer and salt, and the hash of the
    runtime code that deployment leaves there. Stops when the manifest disagrees with any of it."""
    label, d = manifest["label"], manifest["deployment"]
    c = d.get("constructor") or {}
    constants = chains["chains"][manifest["chain"]]
    wrapped = lib.address(constants["wrapped_native"]) if constants.get("wrapped_native") else ct.NATIVE_TOKEN
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    def same(a, b):
        return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()

    list_sha = lib.file_sha256(os.path.join(out_dir, manifest["list_file"]))
    need(d.get("status") == "ready", f"deployment status is {d.get('status')}")
    need(same(chains["create2_deployer"], ct.CREATE2_DEPLOYER), "chains.json names another CREATE2 deployer")
    need(same(d.get("create2_deployer"), ct.CREATE2_DEPLOYER), f"deployer {d.get('create2_deployer')} is not "
         f"the canonical {ct.CREATE2_DEPLOYER}")
    need(same(d.get("salt"), chains["salt"]), f"salt {d.get('salt')} is not chains.json's {chains['salt']}")
    need(int(constants["chain_id"]) == manifest["chain_id"] == c.get("chainId"), "chain id differs between "
         "chains.json, the manifest, and the constructor")
    need(same(c.get("funder"), manifest["funder"]), "constructor funder is not the manifest funder")
    need(same(c.get("wrappedNative"), wrapped) and same(manifest["wrapped_native"] or ct.NATIVE_TOKEN, wrapped),
         "wrapped native coin differs from chains.json")
    need(same(c.get("root"), manifest["root"]), "constructor root is not the manifest root")
    need(c.get("claimCount") == manifest["claim_count"], "constructor claimCount is not the manifest row count")
    need(same(c.get("listHash"), "0x" + manifest["list_sha256"]) and list_sha == manifest["list_sha256"],
         f"listHash, the manifest, and SHA-256 of {manifest['list_file']} ({list_sha}) differ")
    creation = artifact["creation_code"]
    need(same(common.hex0x(common.keccak256(creation)), d.get("bytecode_keccak256")),
         "compiled creation code differs from the manifest; rebuild with the pinned settings")
    if problems:
        raise SystemExit(f"{label}: spender not bound to the compiled contract: " + "; ".join(problems))

    values = (c["funder"], c["wrappedNative"], c["chainId"], ct.unhex(c["root"]), c["claimCount"], ct.unhex(c["listHash"]))
    args_blob = ct.constructor_args(*values)
    init_code = creation + args_blob
    address = ct.create2_address(ct.CREATE2_DEPLOYER, ct.unhex(chains["salt"]), init_code)
    need(same(common.hex0x(args_blob), d.get("constructor_args")), "constructor_args do not encode the constructor")
    need(same(common.hex0x(common.keccak256(init_code)), d.get("init_code_keccak256")), "init code hash differs")
    need(same(d.get("address"), address), f"manifest address {d.get('address')} is not the CREATE2 address {address}")
    if problems:
        raise SystemExit(f"{label}: spender not bound to the compiled contract: " + "; ".join(problems))
    return {
        "address": address,
        "create2_deployer": ct.CREATE2_DEPLOYER,
        "salt": chains["salt"].lower(),
        "bytecode_keccak256": common.hex0x(common.keccak256(creation)),
        "init_code_keccak256": common.hex0x(common.keccak256(init_code)),
        "runtime_keccak256": common.hex0x(common.keccak256(ct.runtime_code(artifact, *values))),
    }


# ------------------------------------------------------------------ chain reads


class Chain:
    """Chain reads, all at the block `pin()` chose."""

    def __init__(self, rpc):
        self.rpc = rpc
        self.block = "latest"

    def pin(self):
        head = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        self.block = head["number"]
        return {"number": int(head["number"], 16), "hash": head["hash"]}

    def block_hash(self, number):
        return self.rpc.call("eth_getBlockByNumber", [hex(number), False])["hash"]

    def chain_id(self):
        return int(self.rpc.call("eth_chainId", []), 16)

    def call(self, to, data):
        result, error = self.rpc.call_soft("eth_call", [{"to": to, "data": data}, self.block])
        return None if error else result

    def code(self, address):
        return ct.unhex(self.rpc.call("eth_getCode", [address, self.block]))

    def storage(self, address, slot):
        return self.rpc.call("eth_getStorageAt", [address, slot, self.block])

    def native_balance(self, address):
        return int(self.rpc.call("eth_getBalance", [address, self.block]), 16)

    def code_hash(self, address):
        return common.hex0x(common.keccak256(self.code(address)))

    def uint(self, to, data):
        return lib.decode_uint(self.call(to, data))

    def erc20(self, token, name, *words):
        return self.uint(token, "0x" + common.selector(name).hex() + "".join(words))


def slot_address(value):
    return lib.address("0x" + value[-40:])


def decode_modules(raw):
    """The module list from `getModulesPaginated`'s `(address[] array, address next)`."""
    if not raw or raw == "0x":
        return None
    data = ct.unhex(raw)
    at = int.from_bytes(data[:32], "big")
    count = int.from_bytes(data[at:at + 32], "big")
    return ["0x" + data[at + 44 + 32 * i:at + 64 + 32 * i].hex() for i in range(count)]


def read_safe(chain, address):
    """What identifies the Safe at `address`: its code, singleton, version, modules, guard, fallback
    handler, owners, threshold, and nonce."""
    singleton = slot_address(chain.storage(address, "0x0"))
    owners_raw = chain.call(address, lib.call_data("getOwners"))
    version_raw = chain.call(address, lib.call_data("VERSION"))
    modules_raw = chain.call(address, "0x" + common.selector("getModulesPaginated(address,uint256)").hex()
                             + lib.address_word(SENTINEL) + lib.word(10))
    return {
        "code_hash": chain.code_hash(address),
        "singleton": singleton,
        "singleton_code_hash": chain.code_hash(singleton),
        "version": lib.crl.decode_string(version_raw) if version_raw and version_raw != "0x" else None,
        "modules": decode_modules(modules_raw),
        "guard": slot_address(chain.storage(address, GUARD_SLOT)),
        "fallback_handler": slot_address(chain.storage(address, FALLBACK_HANDLER_SLOT)),
        "owners": [lib.address(o) for o in lib.crl.decode_address_array(owners_raw)]
        if owners_raw and owners_raw != "0x" else [],
        "threshold": chain.uint(address, lib.call_data("getThreshold")),
        "nonce": chain.uint(address, "0x" + common.selector("nonce()").hex()),
    }


def safe_identity_checks(info, role):
    """[(check, ok, detail)]: `info` is an unmodified Safe 1.3.0."""
    return [
        (f"{role} is a Safe {SAFE_VERSION} proxy", info["code_hash"] == SAFE_PROXY_CODEHASH, f"code {info['code_hash']}"),
        (f"{role} singleton is the official Safe {SAFE_VERSION}",
         SAFE_SINGLETONS.get(info["singleton"]) == info["singleton_code_hash"], f"`{info['singleton']}`"),
        (f"{role} version", info["version"] == SAFE_VERSION, f"VERSION() {info['version']}"),
        (f"{role} has no module", info["modules"] == [], f"modules {info['modules']}"),
        (f"{role} has no guard", info["guard"] == lib.ZERO_ADDRESS, f"`{info['guard']}`"),
        (f"{role} fallback handler is none or the official one", info["fallback_handler"] in SAFE_FALLBACK_HANDLERS,
         f"`{info['fallback_handler']}`"),
    ]


def safe_owner_checks(info, requirements, role):
    """[(check, ok, detail)]: the Safe's owners are the configured roster and its threshold is the
    required one."""
    roster = sorted(lib.address(o) for o in requirements.get("owners") or [])
    owners = sorted(lib.address(o) for o in info["owners"])
    if not roster:
        roster_detail = ("no `custody_requirements.owners` in the config; confirm the owners out of band and list "
                         "them there. The Safe has " + ", ".join(f"`{o}`" for o in owners))
    elif owners == roster:
        roster_detail = f"the {len(roster)} configured owners"
    else:
        roster_detail = f"owners {', '.join(owners)} != roster {', '.join(roster)}"
    return [
        (f"{role} owners and threshold",
         len(owners) == requirements["owner_count"] and info["threshold"] == requirements["threshold"],
         f"{len(owners)} owners, threshold {info['threshold']} (required {requirements['owner_count']} and "
         f"{requirements['threshold']})"),
        (f"{role} owners are the configured roster", bool(roster) and owners == roster, roster_detail),
    ]


def preflight(chain, manifest, bound, approvals, wrapped, requirements, nonce=None):
    """Every check the approval needs, read at one block; each one must pass for signable files.
    Returns the checks, the state read, and the Safe's nonce."""
    checks, state = [], {}

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    funder, spender = manifest["funder"], bound["address"]
    block = chain.pin()
    state["block"] = block
    chain_id = chain.chain_id()
    check("chain id", chain_id == manifest["chain_id"], f"{chain_id}")

    safe = read_safe(chain, funder)
    state["safe"] = safe
    for name, ok, detail in safe_identity_checks(safe, "funder") + safe_owner_checks(safe, requirements, "funder"):
        check(name, ok, detail)
    if nonce is not None:
        check("nonce is the Safe's", nonce == safe["nonce"], f"--nonce {nonce}, Safe nonce {safe['nonce']}")

    multisend = lib.address(safe_batch.MULTISEND_CALL_ONLY[SAFE_VERSION][0])
    code_hash = chain.code_hash(multisend)
    check("MultiSendCallOnly is the official contract",
          code_hash == safe_batch.MULTISEND_CALL_ONLY_CODE_HASH[SAFE_VERSION], f"`{multisend}` code {code_hash}")
    code_hash = chain.code_hash(ct.CREATE2_DEPLOYER)
    check("CREATE2 deployer is the canonical proxy", code_hash == ct.CREATE2_DEPLOYER_CODEHASH,
          f"`{ct.CREATE2_DEPLOYER}` code {code_hash}")

    code_hash = chain.code_hash(spender)
    deployed = code_hash != EMPTY_CODEHASH
    state["contract_deployed"] = deployed
    if deployed:
        check("contract code is this BridgeClaims", code_hash == bound["runtime_keccak256"],
              f"code {code_hash}, expected {bound['runtime_keccak256']}")
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
    final = chain.block_hash(block["number"])
    check("every read is from one block", final == block["hash"], f"block {block['number']} `{block['hash']}`")
    return checks, state, safe["nonce"]


# ------------------------------------------------------------------ one contract


def draft_name(name):
    base, ext = os.path.splitext(name)
    return f"{base}.draft{ext}"


def build(args, config, chains, manifest, out_dir, chain, artifact):
    label = manifest["label"]
    bound = bound_deployment(manifest, chains, artifact, out_dir)
    spender = bound["address"]
    funder = manifest["funder"]
    wrapped = manifest["wrapped_native"]
    approvals = manifest["approvals"]
    reset = {lib.address(t) for t in chains["chains"][manifest["chain"]].get("approve_reset_tokens") or []}
    native_total = sum(int(a["total_raw"]) for a in manifest["assets"] if a["token"] == ct.NATIVE_TOKEN)

    checks, state, nonce = [], {}, args.nonce
    if chain is not None:
        checks, state, safe_nonce = preflight(
            chain, manifest, bound, approvals, wrapped, config["custody_requirements"], args.nonce
        )
        nonce = safe_nonce if args.nonce is None else args.nonce
    failed = [c for c in checks if not c["ok"]]
    if not args.draft:
        if chain is None:
            raise SystemExit(f"{label}: signable files need the chain checks (--rpc); --draft writes unsignable ones")
        if failed:
            raise SystemExit(f"{label}: no signable files, these checks failed: "
                             + "; ".join(f"{c['check']} ({c['detail']})" for c in failed))
    multisend = lib.address(safe_batch.MULTISEND_CALL_ONLY[SAFE_VERSION][0])

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

    manifest_sha = lib.file_sha256(os.path.join(out_dir, "manifest.json"))
    prefix = "DRAFT, do not sign: " if args.draft else ""
    description = (
        f"BridgeClaims {label}: root {manifest['root']}, {manifest['claim_count']} rows, "
        f"spender {spender}, manifest SHA-256 {manifest_sha}"
    )
    approve_batch = tx_builder(manifest["chain_id"], funder, calls, f"{prefix}Approve BridgeClaims {label}", description)
    revoke_batch = tx_builder(manifest["chain_id"], funder, revoke, f"{prefix}Revoke BridgeClaims {label}", description)

    def safe_record(call_list):
        tx = safe_transaction(call_list, multisend, nonce)
        return safe_batch.tx_record(manifest["chain_id"], funder, tx)

    record = {
        "schema_version": SCHEMA_VERSION,
        "draft": bool(args.draft),
        "label": label,
        "chain": manifest["chain"],
        "chain_id": manifest["chain_id"],
        "funder": funder,
        "spender": spender,
        "spender_binding": bound,
        "spender_deployed": state.get("contract_deployed"),
        "root": manifest["root"],
        "claim_count": manifest["claim_count"],
        "manifest_sha256": manifest_sha,
        "safe_version": SAFE_VERSION,
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
        "preflight": {"checked": chain is not None, "block": state.get("block"), "checks": checks, "state": state},
    }
    paths = {name: os.path.join(out_dir, draft_name(name) if args.draft else name) for name in OUTPUTS}
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
    d, bound, pre = manifest["deployment"], record["spender_binding"], record["preflight"]
    lines = [f"# {'DRAFT, do not sign: ' if record['draft'] else ''}Approve BridgeClaims {manifest['label']}", ""]
    if record["draft"]:
        why = ("without chain checks" if not pre["checked"] else
               f"with {sum(1 for c in pre['checks'] if not c['ok'])} failed chain check(s)")
        lines += [
            f"**Draft, built {why}.** Nothing here may be signed. Rebuild with `--rpc` and without `--draft` right",
            "before signing; that run refuses to write files unless every chain check passes.",
            "",
        ]
    lines += [
        f"- Chain: {manifest['chain']} ({manifest['chain_id']})",
        f"- Funder Safe: `{manifest['funder']}` (Safe {record['safe_version']})",
        f"- Spender (BridgeClaims): `{record['spender']}`"
        + ("" if record["spender_deployed"] is None else
           (" — deployed" if record["spender_deployed"] else " — not deployed yet")),
        f"- Root: `{manifest['root']}`; rows: {manifest['claim_count']:,}; list SHA-256 `{manifest['list_sha256']}`",
        f"- Manifest SHA-256: `{record['manifest_sha256']}`",
    ]
    if pre["checked"]:
        lines.append(f"- Chain state read at block {pre['block']['number']:,} (`{pre['block']['hash']}`)")
    lines += [
        "",
        "## Before signing",
        "",
        "1. The spender is the CREATE2 address of the compiled creation code (keccak256",
        f"   `{bound['bytecode_keccak256']}`) with these constructor arguments, through the deployer",
        f"   `{d['create2_deployer']}` with salt `{d['salt']}`. This tool recomputed it from the bytecode and",
        "   stops when the manifest names any other address:",
        "",
        "   ```text",
    ]
    lines += [f"   {k} = {v}" for k, v in d["constructor"].items()]
    lines += [
        "   ```",
        "",
        f"   Init code keccak256 `{bound['init_code_keccak256']}`. After deployment, the code at the spender",
        f"   hashes to `{bound['runtime_keccak256']}` and the contract returns the same six values",
        "   (`funder()`, `wrappedNative()`, `chainId()`, `root()`, `claimCount()`, `listHash()`).",
        "2. Check the manifest SHA-256 above against the published manifest, and recompute the root from",
        "   `claims.csv` (`bridge-claims/tools/build_claims.py`) or check it against that manifest.",
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
        "Approves 0 for every asset: every claim stops until a new approval. Keep it ready, and rebuild it",
        "at the Safe's nonce when it is needed.",
        "",
        "| # | Call | To | Call data |",
        "|---:|---|---|---|",
    ]
    for i, c in enumerate(record["revoke"]["calls"], start=1):
        lines.append(f"| {i} | {c['note']} | `{c['to']}` | `{c['data']}` |")
    lines += ["", *sheet_hashes(record["revoke"]["safe_transaction"]), ""]
    if pre["checked"]:
        lines += ["## Chain checks", "", "| Check | Result | Detail |", "|---|---|---|"]
        for c in pre["checks"]:
            lines.append(f"| {c['check']} | {'ok' if c['ok'] else '**FAILED**'} | {c['detail']} |")
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
    if not args.rpc and not args.draft:
        raise SystemExit("signable files need the chain checks: add --rpc (or --draft for files not to be signed)")
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    chains = ct.load_chains(args.chains_file)
    artifact = ct.load_artifact(args.bytecode)
    summary = lib.load_json(os.path.join(args.claims_dir, "summary.json"))
    labels = args.labels or [c["label"] for c in summary["contracts"] if c["status"] == "ready"]
    if args.nonce is not None and len(labels) != 1:
        raise SystemExit("--nonce needs exactly one contract (--labels)")
    for label in labels:
        out_dir = os.path.join(args.claims_dir, label)
        manifest = lib.load_json(os.path.join(out_dir, "manifest.json"))
        chain = Chain(lib.chain_rpc(config, manifest["chain"])) if args.rpc else None
        record = build(args, config, chains, manifest, out_dir, chain, artifact)
        failed = [c["check"] for c in record["preflight"]["checks"] if not c["ok"]]
        tx = record["approve"]["safe_transaction"]
        print(
            f"{label}: {'DRAFT, ' if record['draft'] else ''}{len(record['approve']['calls'])} calls to approve "
            f"{record['spender']}"
            + (f", Safe tx hash {tx['safeTxHash']} (nonce {tx['nonce']})" if tx["nonce"] is not None else "")
            + (f"; failed: {', '.join(failed)}" if failed else "")
        )
    skipped = [c["label"] for c in summary["contracts"] if c["status"] != "ready" and c["label"] not in labels]
    if skipped and not args.labels:
        print("skipped (no funder or bytecode): " + ", ".join(skipped))


if __name__ == "__main__":
    main()
