#!/usr/bin/env python3
"""Plan the release of the bridge collateral from the LayerZero v1 escrows to the custody Safe.

    python3 tools/release_plan.py --allocations-dir ../artifacts/<run>/allocations \\
        --output-dir runs/<run>/release

The escrows (ProxyOFT / NativeOFT) have no owner withdrawal: collateral leaves only when the
escrow's LayerZero endpoint delivers a message from a trusted remote. The escrow's owner chooses
which receive library, verifier, and executor the endpoint accepts for each source, so each owner
releases its escrows on their own chain, in one transaction, with no cross-chain message and no
LayerZero verifier, executor, relayer, or fee:

0a. safe-owners  on any chain where the custody Safe is still 1-of-1, add the remaining three
                 owners and raise the threshold to 2-of-4 (`addOwnerWithThreshold`);
0b. one-reserve  on Ethereum, the Reserve Safe transfers the bridged ONE collateral to the custody
                 Safe (`ERC20.transfer`);
1.  release      for each escrow, its owner:
                   pins the escrow's receive library (`setReceiveVersion`), so a later change of
                     LayerZero's default cannot reach it;
                   makes itself the only verifier and the executor for messages from the custody
                     Safe on the escrow's own chain (`setConfig`), and trusts that path
                     (`setTrustedRemote`);
                   attests and delivers one packet on that path (`verify`, `commitVerification`):
                     the escrow credits its whole locked balance to the custody Safe;
                   writes back the trusted remote, executor, verifier config, and receive version
                     it read before, so only the balance changes.

Safe-owned escrows get one Transaction Builder batch per chain and owner (atomic); EOA-owned escrows
get raw calldata to send in order, and each key holder gets `handoff/<key>/` with their files,
`tools/send_calldata.py`, and step-by-step instructions. Balances, configs, nonces, and Safe nonces
are read live. The Harmony path (chain 116) is never touched. Nothing is sent.

`test/EscrowRelease.t.sol` replays every generated file on a fork of each chain.
"""

import argparse
import json
import os
import shutil
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claim_tree as ct  # noqa: E402
import bridge_lib as lib  # noqa: E402  (toolkit/scripts/bridge, on sys.path through claim_tree)
import approve_claims as ac  # noqa: E402
import safe_batch  # noqa: E402  (airdrop/tools, on sys.path through claim_tree)

common = ct.common
SCHEMA_VERSION = 2
LZ_ID = {"ethereum": 101, "bsc": 102, "arbitrum": 110, "base": 184, "linea": 183}
DST_GAS = 300_000  # gas for the escrow's lzReceive (credit ~50k; FXS checkpoints more)
PT_SEND = 0

# ReceiveUln301 (LayerZero's DVN library for Endpoint v1) and its packet and config formats.
RECEIVE_ULN301_VERSION = (3, 0, 1)
PACKET_VERSION = 1
CONFIG_TYPE_EXECUTOR = 1
CONFIG_TYPE_ULN = 2
NIL_DVN_COUNT = 255  # "no optional verifiers", so no default can add any
CONFIRMATIONS = 1
GAS_PER_ESCROW = 400_000  # bound; forks of all five chains measured 350,000 to 375,000
DEFAULT_MAX_ESCROWS_PER_BATCH = 30

SEL = {s: common.selector(s) for s in (
    "setTrustedRemote(uint16,bytes)",
    "setReceiveVersion(uint16)",
    "setConfig(uint16,uint16,uint256,bytes)",
    "verify(bytes,bytes32,uint64)",
    "commitVerification(bytes,uint256)",
    "addOwnerWithThreshold(address,uint256)",
    "transfer(address,uint256)",
    "circulatingSupply()",
    "lzEndpoint()", "owner()", "token()", "balanceOf(address)", "decimals()",
    "getThreshold()", "getOwners()", "VERSION()", "nonce()", "version()",
    "uaConfigLookup(address)", "getReceiveVersion(address)", "libraryLookup(uint16)",
    "getInboundNonce(uint16,bytes)", "hasStoredPayload(uint16,bytes)",
    "trustedRemoteLookup(uint16)", "getAppUlnConfig(address,uint32)", "executors(address,uint32)",
)}

TRANSFER_METHOD = {
    "inputs": [
        {"internalType": "address", "name": "to", "type": "address"},
        {"internalType": "uint256", "name": "value", "type": "uint256"},
    ],
    "name": "transfer",
    "payable": False,
}

ADD_OWNER_METHOD = {
    "inputs": [
        {"internalType": "address", "name": "owner", "type": "address"},
        {"internalType": "uint256", "name": "_threshold", "type": "uint256"},
    ],
    "name": "addOwnerWithThreshold",
    "payable": False,
}


# ------------------------------------------------------------------ ABI encoding


def _word(value):
    return int(value).to_bytes(32, "big")


def _address_word(address):
    return bytes(12) + bytes.fromhex(address[2:])


def abi_encode(types, values):
    """abi.encode for the static types uint*, address, bool, bytes32 and the dynamic type bytes."""
    head, tail = b"", b""
    base = 32 * len(types)
    for kind, value in zip(types, values):
        if kind == "bytes":
            data = bytes(value)
            head += _word(base + len(tail))
            tail += _word(len(data)) + data + b"\x00" * (-len(data) % 32)
        elif kind == "address":
            head += _word(int(value, 16))
        elif kind == "bool":
            head += _word(1 if value else 0)
        elif kind == "bytes32":
            head += bytes(value).rjust(32, b"\x00")
        else:
            head += _word(value)
    return head + tail


def packed(*addresses):
    return b"".join(bytes.fromhex(a[2:]) for a in addresses)


def release_payload(safe, amount, packet_type=True):
    """The OFT v1 payload the escrows accept:
    - Harmony's OFT v1 fork (49 escrows, with `circulatingSupply()`):
      `abi.encode(uint16 packetType, bytes from, bytes to, uint256 amount)`;
    - Legacy OFT v1 (`0xab89…bcab`, without `circulatingSupply()`):
      `abi.encode(bytes to, uint256 amount)`."""
    to = packed(safe)
    if packet_type:
        return abi_encode(["uint16", "bytes", "bytes", "uint256"], [PT_SEND, to, to, amount])
    return abi_encode(["bytes", "uint256"], [to, amount])


def uln_config(dvn):
    """`abi.encode(UlnConfig)` with `dvn` as the only required verifier, optional verifiers set to
    NIL, and an explicit confirmation count, so no field falls back to LayerZero's default."""
    return b"".join(_word(x) for x in (
        0x20,                            # offset of the struct
        CONFIRMATIONS, 1, NIL_DVN_COUNT, 0,  # confirmations, required, optional, optional threshold
        0xC0, 0x100,                     # offsets of requiredDVNs and optionalDVNs
        1, int(dvn, 16),                 # requiredDVNs = [dvn]
        0,                               # optionalDVNs = []
    ))


def packet_header(nonce, src_eid, sender, dst_eid, receiver):
    """PacketV1Codec header: version, nonce, srcEid, sender, dstEid, receiver (81 bytes)."""
    return (bytes([PACKET_VERSION]) + nonce.to_bytes(8, "big") + src_eid.to_bytes(4, "big")
            + _address_word(sender) + dst_eid.to_bytes(4, "big") + _address_word(receiver))


def packet(header, message):
    """The full packet `header || guid || message` and its payload hash `keccak256(guid || message)`;
    the guid is LayerZero's `keccak256(nonce, srcEid, sender, dstEid, receiver)`."""
    payload = common.keccak256(header[1:]) + message
    return header + payload, common.keccak256(payload)


def call_data(signature, types, values):
    return SEL[signature] + abi_encode(types, values)


# ------------------------------------------------------------------ chain reads


def rpc_for(config, chain):
    return lib.chain_rpc(config, chain)


def eth_call(rpc, to, data):
    result, error = rpc.call_soft("eth_call", [{"to": to, "data": common.hex0x(data)}, "latest"])
    return None if error else result


def read_address(rpc, to, signature, args=b""):
    raw = eth_call(rpc, to, SEL[signature] + args)
    return lib.address("0x" + raw[-40:]) if raw and len(raw) >= 42 else None


def read_uint(rpc, to, data):
    raw = eth_call(rpc, to, data)
    return int(raw, 16) if raw and raw != "0x" else None


def read_raw(rpc, to, data, what):
    raw = eth_call(rpc, to, data)
    if not raw or raw == "0x":
        raise SystemExit(f"{what}: call to {to} failed")
    return ct.unhex(raw)


def read_bytes(rpc, to, data, what):
    raw = read_raw(rpc, to, data, what)
    offset = int.from_bytes(raw[:32], "big")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    return raw[offset + 32:offset + 32 + length]


def read_safe_info(rpc, address):
    code_hex = rpc.call("eth_getCode", [address, "latest"])
    code_size = (len(code_hex) - 2) // 2
    threshold = read_uint(rpc, address, SEL["getThreshold()"]) if code_size > 0 else None
    if not code_size or threshold is None:
        return {"is_safe": False, "code_size": code_size}
    owners_raw = eth_call(rpc, address, SEL["getOwners()"])
    owners = lib.crl.decode_address_array(owners_raw) if owners_raw and owners_raw != "0x" else []
    ver_raw = eth_call(rpc, address, SEL["VERSION()"])
    version = lib.crl.decode_string(ver_raw) if ver_raw and ver_raw != "0x" else "1.3.0"
    nonce = read_uint(rpc, address, SEL["nonce()"])
    native = int(rpc.call("eth_getBalance", [address, "latest"]), 16)
    return {
        "is_safe": True,
        "code_size": code_size,
        "owners": owners,
        "threshold": threshold,
        "version": version,
        "nonce": nonce,
        "native_balance_raw": str(native),
    }


def is_safe(rpc, address):
    return (len(rpc.call("eth_getCode", [address, "latest"])) > 2
            and read_uint(rpc, address, SEL["getThreshold()"]) is not None)


def locked_balance(rpc, escrow):
    """What the escrow can release: the real token a ProxyOFT holds, or the own token a NativeOFT
    holds for itself (its native coin also backs other holders' wrapped tokens, which must stay),
    plus whether the escrow uses Harmony's 4-tuple OFT v1 payload (`circulatingSupply()` present)
    or the legacy 2-tuple OFT v1 payload (`0xab89…bcab`)."""
    token = read_address(rpc, escrow, "token()")
    holder = lib.address_word(escrow)
    has_pt = read_uint(rpc, escrow, SEL["circulatingSupply()"]) is not None
    if token and token != escrow:
        return "proxy", token, read_uint(rpc, token, SEL["balanceOf(address)"] + ct.unhex(holder)), has_pt
    return "native", None, read_uint(rpc, escrow, SEL["balanceOf(address)"] + ct.unhex(holder)), has_pt


def receive_state(rpc, escrow, owner, eid, safe):
    """The escrow's receive side for the local path (source `eid` = its own chain, sender = the
    Safe): endpoint, the receive version it stores and the one in effect, that version's library
    (which must be ReceiveUln301), the path's inbound nonce, and the raw trusted remote, verifier
    config, and executor that the release overwrites and then writes back. Refuses an escrow whose
    settings already name the Safe path or the owner: those are a started EOA release, and planning
    again would write them back instead of the originals."""
    endpoint = read_address(rpc, escrow, "lzEndpoint()")
    ua = read_raw(rpc, endpoint, SEL["uaConfigLookup(address)"] + _address_word(escrow), f"{escrow} uaConfigLookup")
    stored_version = int.from_bytes(ua[32:64], "big")
    version = read_uint(rpc, endpoint, SEL["getReceiveVersion(address)"] + _address_word(escrow))
    library = read_address(rpc, endpoint, "libraryLookup(uint16)", _word(version))
    raw = read_raw(rpc, library, SEL["version()"], f"{library} version()")
    found = tuple(int.from_bytes(raw[i:i + 32], "big") for i in (0, 32, 64))
    if found != RECEIVE_ULN301_VERSION:
        raise SystemExit(f"{escrow}: receive library {library} (version {version}) is {found}, not ReceiveUln301")
    path = packed(safe, escrow)
    path_args = abi_encode(["uint16", "bytes"], [eid, path])
    inbound = read_uint(rpc, endpoint, SEL["getInboundNonce(uint16,bytes)"] + path_args)
    if read_uint(rpc, endpoint, SEL["hasStoredPayload(uint16,bytes)"] + path_args):
        raise SystemExit(f"{escrow}: a payload is stored on the local path; clear it first")
    where = f"{escrow} receive config"
    trusted = read_bytes(rpc, escrow, SEL["trustedRemoteLookup(uint16)"] + _word(eid), where)
    uln = read_raw(rpc, library, SEL["getAppUlnConfig(address,uint32)"] + _address_word(escrow) + _word(eid), where)
    executor = read_raw(rpc, library, SEL["executors(address,uint32)"] + _address_word(escrow) + _word(eid), where)
    if trusted == path or executor == _address_word(owner) or uln == uln_config(owner):
        raise SystemExit(f"{escrow}: a release was started and not finished; finish it from the earlier "
                         "1-release file instead of planning again")
    return {
        "endpoint": endpoint,
        "lz_chain_id": eid,
        "stored_receive_version": stored_version,
        "receive_version": version,
        "receive_library": library,
        "path_nonce": inbound + 1,
        "previous_trusted_remote": common.hex0x(trusted),
        "previous_uln_config": common.hex0x(uln),
        "previous_executor": common.hex0x(executor),
    }


# ------------------------------------------------------------------ the plan


def owner_setup_calls(safe, current_owners, target_owners, target_threshold):
    """Calls on a 1-of-1 Safe to add the remaining owners in reverse linked-list order and set the
    target threshold, so `getOwners()` matches `target_owners` in both membership and order."""
    to_add = [o for o in target_owners if o not in set(current_owners)]
    calls = []
    # `addOwnerWithThreshold` inserts at the head of the Safe's linked list, so adding in reverse
    # order reproduces `target_owners` in exact order.
    ordered = list(reversed(to_add))
    for i, owner in enumerate(ordered):
        th = target_threshold if i == len(ordered) - 1 else 1
        data = call_data("addOwnerWithThreshold(address,uint256)", ["address", "uint256"], [owner, th])
        calls.append({
            "to": safe,
            "value": 0,
            "data": data,
            "method": "addOwnerWithThreshold(address,uint256)",
            "args": {"owner": owner, "_threshold": str(th)},
            "contract_method": ADD_OWNER_METHOD,
            "contract_inputs": {"owner": common.to_checksum(owner), "_threshold": str(th)},
            "note": f"add owner {owner} with threshold {th}",
        })
    return calls


def one_reserve_call(token, to, amount, note):
    data = call_data("transfer(address,uint256)", ["address", "uint256"], [to, amount])
    return {
        "to": lib.address(token),
        "value": 0,
        "data": data,
        "method": "transfer(address,uint256)",
        "args": {"to": lib.address(to), "value": str(amount)},
        "contract_method": TRANSFER_METHOD,
        "contract_inputs": {"to": common.to_checksum(to), "value": str(amount)},
        "note": note,
    }


def release_packet(e, safe):
    """The one packet that releases escrow `e`: from the Safe on the escrow's own chain, at the
    path's next nonce, crediting the whole locked balance to the Safe."""
    eid = e["lz_chain_id"]
    message = release_payload(safe, int(e["locked_raw"]), packet_type=e["payload_format"] == "oft_v1_packet_type")
    header = packet_header(e["path_nonce"], eid, safe, eid, e["escrow"])
    full, payload_hash = packet(header, message)
    return header, full, payload_hash


def release_calls(e, safe):
    """The owner's calls that release escrow `e` and leave every setting as it was read."""
    escrow, owner, eid, version, library = (
        e["escrow"], e["owner"], e["lz_chain_id"], e["receive_version"], e["receive_library"])
    pool = e["pool_id"]
    header, full, payload_hash = release_packet(e, safe)

    def set_config(config_type, config):
        return call_data("setConfig(uint16,uint16,uint256,bytes)", ["uint16", "uint16", "uint256", "bytes"],
                         [version, eid, config_type, config])

    def set_trusted(path):
        return call_data("setTrustedRemote(uint16,bytes)", ["uint16", "bytes"], [eid, path])

    def set_version(v):
        return call_data("setReceiveVersion(uint16)", ["uint16"], [v])

    # The verifier config is set first and written back last, so an EOA sequence that stops
    # partway always leaves it naming the owner, which `receive_state` refuses to plan over.
    pin = e["stored_receive_version"] != version
    calls = [
        _call(escrow, set_config(CONFIG_TYPE_ULN, uln_config(owner)), f"{pool}: owner is the only verifier"),
        _call(escrow, set_config(CONFIG_TYPE_EXECUTOR, _address_word(owner)), f"{pool}: owner is the executor"),
    ]
    if pin:
        calls.append(_call(escrow, set_version(version), f"{pool}: pin receive library version {version}"))
    calls += [
        _call(escrow, set_trusted(packed(safe, escrow)), f"{pool}: trust the Safe on chain {eid}"),
        _call(library, call_data("verify(bytes,bytes32,uint64)", ["bytes", "bytes32", "uint64"],
                                 [header, payload_hash, CONFIRMATIONS]),
              f"{pool}: attest packet nonce {e['path_nonce']}"),
        _call(library, call_data("commitVerification(bytes,uint256)", ["bytes", "uint256"], [full, DST_GAS]),
              f"{pool}: deliver, crediting {e['locked_raw']} to the Safe"),
        _call(escrow, set_trusted(ct.unhex(e["previous_trusted_remote"])), f"{pool}: restore the trusted remote"),
    ]
    if pin:
        calls.append(_call(escrow, set_version(e["stored_receive_version"]),
                           f"{pool}: restore receive version {e['stored_receive_version']}"))
    calls += [
        _call(escrow, set_config(CONFIG_TYPE_EXECUTOR, ct.unhex(e["previous_executor"])),
              f"{pool}: restore the executor"),
        _call(escrow, set_config(CONFIG_TYPE_ULN, ct.unhex(e["previous_uln_config"])),
              f"{pool}: restore the verifier config"),
    ]
    return calls


def plan(args, config, custody_plan, safe):
    rpcs = {}

    def rpc(chain):
        if chain not in rpcs:
            rpcs[chain] = rpc_for(config, chain)
        return rpcs[chain]

    # Inspect the custody Safe across all five chains and build owner-setup batches where still 1-of-1.
    requirements = config.get("custody_requirements") or {"owner_count": 4, "threshold": 2}
    req_count = int(requirements["owner_count"])
    req_threshold = int(requirements["threshold"])
    custody_safes = {}
    for chain in custody_plan["chains"]:
        custody_safes[chain] = read_safe_info(rpc(chain), safe)

    eth_info = custody_safes.get("ethereum") or {}
    target_owners = (
        eth_info.get("owners")
        if len(eth_info.get("owners") or []) == req_count and eth_info.get("threshold") == req_threshold
        else requirements.get("owners") or []
    )
    owner_setup = {}
    for chain, info in custody_safes.items():
        if not info.get("is_safe") or not target_owners:
            continue
        cur = info["owners"]
        if sorted(cur) == sorted(target_owners) and info["threshold"] == req_threshold:
            info["status"] = "ready"
        elif len(cur) == 1 and cur[0] in set(target_owners) and info["threshold"] == 1:
            info["status"] = "needs_owner_setup"
            owner_setup[chain] = owner_setup_calls(safe, cur, target_owners, req_threshold)
        else:
            info["status"] = "unexpected_owners"

    # Inspect the ONE Reserve Safe on Ethereum and build the ONE transfer batch.
    one_funding = None
    funding_cfg = custody_plan.get("one_funding")
    if funding_cfg and int(funding_cfg.get("amount_raw") or 0) > 0:
        one_chain = funding_cfg["chain"]
        reserve = lib.address(funding_cfg["from"])
        token = lib.address(funding_cfg["token"])
        amount = int(funding_cfg["amount_raw"])
        reserve_info = read_safe_info(rpc(one_chain), reserve)
        bal = read_uint(rpc(one_chain), token, SEL["balanceOf(address)"] + ct.unhex(lib.address_word(reserve))) or 0
        one_funding = {
            "chain": one_chain,
            "from": reserve,
            "to": safe,
            "token": token,
            "amount_raw": str(amount),
            "reserve_balance_raw": str(bal),
            "reserve_sufficient": bal >= amount,
            "safe": reserve_info,
            "calls": [one_reserve_call(token, safe, amount, f"transfer {amount} ONE to custody Safe {safe}")],
        }

    escrows, skipped = [], []
    for chain, item in custody_plan["chains"].items():
        for t in item.get("transfers") or []:
            escrow = lib.address(t["escrow"])
            owner = read_address(rpc(chain), escrow, "owner()")
            kind, token, locked, has_pt = locked_balance(rpc(chain), escrow)
            entry = {
                "chain": chain, "pool_id": t["pool_id"], "symbol": t.get("symbol"), "escrow": escrow,
                "owner": owner, "kind": kind, "token": token, "locked_raw": str(locked or 0),
                "decimals": 18 if kind == "native" else read_uint(rpc(chain), token, SEL["decimals()"]),
                "payload_format": "oft_v1_packet_type" if has_pt else "oft_v1_legacy",
            }
            if escrow in args.skip:
                skipped.append(entry | {"reason": "excluded with --skip"})
                continue
            state = receive_state(rpc(chain), escrow, owner, LZ_ID[chain], safe)
            if not locked:
                skipped.append(entry | {"reason": "nothing locked to release"})
                continue
            entry["owner_is_safe"] = bool(owner) and is_safe(rpc(chain), owner)
            entry |= state
            header, _, payload_hash = release_packet(entry, safe)
            entry["packet_header"] = common.hex0x(header)
            entry["payload_hash"] = common.hex0x(payload_hash)
            escrows.append(entry)

    # 1. release, grouped by (chain, owner); Safe batches are split to stay under the per-tx gas cap
    releases = []
    by_owner = {}
    for e in escrows:
        by_owner.setdefault((e["chain"], e["owner"]), []).append(e)
    for (chain, owner), group in sorted(by_owner.items()):
        count = -(-len(group) // args.max_escrows_per_batch) if group[0]["owner_is_safe"] else 1
        step = -(-len(group) // count)
        parts = [group[i:i + step] for i in range(0, len(group), step)]
        for n, part in enumerate(parts, 1):
            releases.append({
                "chain": chain, "owner": owner, "owner_is_safe": part[0]["owner_is_safe"],
                "part": f"{n}of{len(parts)}" if len(parts) > 1 else None,
                "escrows": part,
                "calls": [c for e in part for c in release_calls(e, safe)],
            })

    # Read live Safe info for any additional Safe escrow owners (e.g. 0xE336… on BSC)
    safe_states = {(chain, safe): info for chain, info in custody_safes.items()}
    if one_funding and one_funding["safe"].get("is_safe"):
        safe_states[(one_funding["chain"], one_funding["from"])] = one_funding["safe"]
    for r in releases:
        if r["owner_is_safe"] and (r["chain"], r["owner"]) not in safe_states:
            safe_states[(r["chain"], r["owner"])] = read_safe_info(rpc(r["chain"]), r["owner"])

    return escrows, skipped, releases, custody_safes, owner_setup, one_funding, safe_states


def _call(to, data, note):
    return {"to": lib.address(to), "value": 0, "data": data, "method": "raw", "args": {}, "note": note}


# ------------------------------------------------------------------ outputs


def write_safe_batch(path, chain_id, safe, calls, name, version, nonce):
    batch = ac.tx_builder(chain_id, safe, calls, name, name)
    if version not in safe_batch.MULTISEND_CALL_ONLY:
        version = "1.3.0"
    multisend = lib.address(safe_batch.MULTISEND_CALL_ONLY[version][0])
    record = safe_batch.tx_record(chain_id, safe, ac.safe_transaction(calls, multisend, nonce))
    lib.write_text(path, json.dumps(batch, indent=1) + "\n")
    return record


NATIVE = {"bsc": "BNB"}  # every other chain pays gas in ETH
EOA_TX_GAS = 100_000  # bound; the largest EOA release transaction measured on the forks is 93,000


def balance_check(e):
    """The `cast call` that prints what the escrow still holds (0 once released)."""
    if e["kind"] == "native":
        return f'cast call {e["escrow"]} "balanceOf(address)(uint256)" {e["escrow"]} --rpc-url "$RPC"'
    return f'cast call {e["token"]} "balanceOf(address)(uint256)" {e["escrow"]} --rpc-url "$RPC"'


def handoff_readme(owner, files, by_escrow, config, safe, hashes, gas, built):
    chains = [b["chain"] for b in files]
    total = sum(len(b["escrows"]) for b in files)
    lines = [
        f"# Bridge escrow release: instructions for `{owner}`",
        "",
        f"Built {built} with the release plan. Use only the package you were sent for this build.",
        "",
        f"You hold the key `{owner}`. It owns {total} bridge escrow{'s' if total != 1 else ''} on "
        f"{', '.join(chains)}. These steps release each of them, on its own chain, to the custody Safe",
        f"`{safe}`. Nothing crosses chains and no funds pass through your key: you only pay gas. For",
        "the whole picture, read `bridge-claims/docs/RELEASE.md` in the repository.",
        "",
        "## Your files",
        "",
        "| Chain | Chain id | File | Escrows | Transactions |",
        "|---|---:|---|---|---:|",
    ]
    for b in files:
        released = [by_escrow[(b["chain"], a)] for a in b["escrows"]]
        assets = "; ".join(f"{e['symbol']} {units(e['locked_raw'], e['decimals'])} (`{e['escrow']}`)" for e in released)
        lines.append(f"| {b['chain']} | {config['chains'][b['chain']]['chain_id']} | "
                     f"`{os.path.basename(b['file'])}` | {assets} | {b['calls']} |")
    lines += [
        "",
        "Each escrow takes 10 transactions, always in this order:",
        "",
        "1. Your key becomes the only verifier for messages from the custody Safe on this chain.",
        "2. Your key becomes the only party allowed to deliver them.",
        "3. The escrow is pinned to the receive library LayerZero uses today.",
        "4. The escrow trusts the custody Safe on this chain.",
        "5. Your key attests one message that credits the escrow's whole balance to the custody Safe.",
        "6. Your key delivers it, and the escrow pays the custody Safe. Only this one moves funds.",
        "7. to 10. The escrow's previous settings are put back.",
        "",
        "Each transaction in the files carries a `note` saying which of these it is.",
        "",
        "## Before you start",
        "",
        "1. Install Foundry, which provides `cast` (https://getfoundry.sh), and have Python 3.",
        "2. Check that the files arrived unchanged: `shasum -a 256 *` in this folder must print",
        "",
        "   | File | SHA-256 |",
        "   |---|---|",
    ]
    lines += [f"   | `{name}` | `{digest}` |" for name, digest in hashes.items()]
    lines += [
        "",
        "3. Have an RPC URL for each chain from your own provider, and enough native coin for gas. Each",
        f"   transaction stays under {EOA_TX_GAS:,} gas. At the gas price when this package was built:",
        "",
        "   | Chain | Transactions | Gas cost at most | Your balance then |",
        "   |---|---:|---:|---:|",
    ]
    for b in files:
        price, balance = gas[b["chain"]]
        coin = NATIVE.get(b["chain"], "ETH")
        need = b["calls"] * EOA_TX_GAS * price
        lines.append(f"   | {b['chain']} | {b['calls']} | {units(need, 18, 8)} {coin} | {units(balance, 18, 8)} {coin} |")
    lines += [
        "",
        "   Gas prices move, on Ethereum by 10x or more within a day; keep a wide margin.",
        "4. Start only when the person who sent you this package says it is current.",
        "",
        "## Send",
        "",
        "One file at a time, in any order. In this folder, with `RPC` set to that file's chain:",
        "",
        "```sh",
        f'python3 send_calldata.py {os.path.basename(files[0]["file"])} --rpc-url "$RPC" --dry-run -- --ledger',
        f'python3 send_calldata.py {os.path.basename(files[0]["file"])} --rpc-url "$RPC" -- --ledger',
        "```",
        "",
        "Replace `--ledger` with how you sign: `--trezor`, or `--account <name>` for a Foundry keystore.",
        "A keystore asks for its password at every transaction unless you add `--password-file <file>`.",
        "",
        "The dry run lists the transactions and simulates the first one without sending anything. The",
        "real run first checks that the RPC is on the file's chain and that your key has no pending",
        f"transactions. It refuses a wallet that is not `{owner}`. Then, for each transaction, it",
        "simulates the call, sends it only if the simulation succeeds, and waits until it is mined",
        "successfully before the next. With a hardware wallet, check on the device that each transaction",
        "goes to the address the script prints for it.",
        "",
        "Each confirmed transaction is written to `<file>.sent.json`. Do not delete those logs.",
        "",
        "## Check",
        "",
        "After a file is done, each of its escrows must hold 0:",
        "",
        "```sh",
    ]
    for b in files:
        lines.append(f"# {b['chain']}")
        lines += [balance_check(by_escrow[(b["chain"], a)]) for a in b["escrows"]]
    lines += [
        "```",
        "",
        "## If something goes wrong",
        "",
        "- **A simulation fails.** Nothing was sent for that transaction. Send the output to whoever sent",
        "  you this package.",
        "- **The run stops or is interrupted.** Run the same command again; it resumes after the last",
        "  confirmed transaction. If it says your key sent something after the last logged transaction,",
        "  stop and report it.",
        "- **A transaction reverts on chain.** The script stops. Report it.",
        "- Never edit, skip, or reorder the transactions. Don't send anything else from this key on that",
        "  chain while a file is in progress.",
        "",
        "Once an escrow's first transaction is mined, only your key can complete or undo it, so a pause is",
        "safe. Finish it from the same file, not from a newer package.",
        "",
        "## When you are done",
        "",
        "Send back every `*.sent.json` file. They list each transaction hash for the release record.",
    ]
    return "\n".join(lines) + "\n"


def write_handoff(out, owner, files, by_escrow, config, safe, replace):
    """`handoff/<owner>/`: everything one key holder needs, in one folder they can be sent."""
    folder = os.path.join(out, "handoff", owner[:10])
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_calldata.py")
    sources = [(os.path.join(out, b["file"]), os.path.basename(b["file"])) for b in files]
    sources.append((script, "send_calldata.py"))
    readme = os.path.join(folder, "README.md")
    lib.prepare_outputs([os.path.join(folder, name) for _, name in sources] + [readme], replace)
    for source, name in sources:
        shutil.copyfile(source, os.path.join(folder, name))
    hashes = {name: lib.file_sha256(os.path.join(folder, name)) for _, name in sources}
    gas = {}
    for b in files:
        rpc = rpc_for(config, b["chain"])
        gas[b["chain"]] = (int(rpc.call("eth_gasPrice", []), 16),
                           int(rpc.call("eth_getBalance", [owner, "latest"]), 16))
    built = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lib.write_text(readme, handoff_readme(owner, files, by_escrow, config, safe, hashes, gas, built))
    return [os.path.relpath(p, out) for p in [os.path.join(folder, name) for _, name in sources] + [readme]]


def describe_header(h):
    nonce, src, dst = int.from_bytes(h[1:9], "big"), int.from_bytes(h[9:13], "big"), int.from_bytes(h[45:49], "big")
    return (f"message {nonce} from `0x{h[25:45].hex()}` on chain {src} to `0x{h[61:81].hex()}` on chain {dst}")


def describe_message(m):
    """The OFT payload's recipient and raw amount (4-tuple packet-type form or legacy 2-tuple)."""
    first = int.from_bytes(m[:32], "big")
    if first == PT_SEND:
        to_at, amount = int.from_bytes(m[64:96], "big"), int.from_bytes(m[96:128], "big")
    else:
        to_at, amount = first, int.from_bytes(m[32:64], "big")
    return f"`0x{m[to_at + 32:to_at + 52].hex()}`", amount


def decode_call(data):
    """What a planned call does, read back from its calldata alone."""
    sel, body = data[:4], data[4:]

    def word(i):
        return int.from_bytes(body[32 * i:32 * i + 32], "big")

    def dyn(i):
        at = word(i)
        return body[at + 32:at + 32 + int.from_bytes(body[at:at + 32], "big")]

    if sel == SEL["setConfig(uint16,uint16,uint256,bytes)"]:
        version, eid, kind, config = word(0), word(1), word(2), dyn(3)
        head = f"`setConfig({version}, {eid}, {kind}, …)`"
        if kind == CONFIG_TYPE_EXECUTOR:
            ex = config[12:32].hex()
            return head + (f": executor for chain {eid} = `0x{ex}`" if int(ex, 16) else
                           f": executor for chain {eid} = unset (LayerZero's default)")
        w = [int.from_bytes(config[i:i + 32], "big") for i in range(0, len(config), 32)]
        confirmations, required, optional = w[1], w[2], w[3]
        if (confirmations, required, optional, w[4]) == (0, 0, 0, 0):
            return head + f": verifier config for chain {eid} = unset (LayerZero's default)"
        at = 1 + w[5] // 32
        dvns = ", ".join(f"`0x{x:040x}`" for x in w[at + 1:at + 1 + w[at]])
        extra = "none" if optional == NIL_DVN_COUNT else str(optional)
        return head + (f": verifiers for chain {eid} = required [{dvns}], optional {extra}, "
                       f"{confirmations} confirmation{'s' if confirmations != 1 else ''}")
    if sel == SEL["setReceiveVersion(uint16)"]:
        v = word(0)
        return f"`setReceiveVersion({v})`: " + ("follow LayerZero's default receive library" if v == 0
                                                 else f"pin receive library version {v}")
    if sel == SEL["setTrustedRemote(uint16,bytes)"]:
        eid, path = word(0), dyn(1)
        if not path:
            return f"`setTrustedRemote({eid}, 0x)`: trust no sender on chain {eid}"
        return f"`setTrustedRemote({eid}, …)`: trust sender `0x{path[:20].hex()}` on chain {eid}"
    if sel == SEL["verify(bytes,bytes32,uint64)"]:
        return (f"`verify(header, payloadHash, {word(2)})`: attest {describe_header(dyn(0))}, "
                f"payload hash `0x{body[32:64].hex()}`")
    if sel == SEL["commitVerification(bytes,uint256)"]:
        full = dyn(0)
        to, amount = describe_message(full[113:])
        return (f"`commitVerification(packet, {word(1)})`: deliver {describe_header(full[:81])}; "
                f"the escrow pays {amount} raw units to {to}")
    if sel == SEL["addOwnerWithThreshold(address,uint256)"]:
        return f"`addOwnerWithThreshold(0x{body[12:32].hex()}, {word(1)})`"
    if sel == SEL["transfer(address,uint256)"]:
        return f"`transfer(0x{body[12:32].hex()}, {word(1)})`"
    return f"selector `0x{sel.hex()}`"


def example_release(batches, safe):
    """The release batch to decode for the signers: the custody Safe's own, else the first."""
    releases = [b for b in batches if b["sender_is_safe"] and b["step"] == "1-release"]
    return next((b for b in releases if b["sender"] == safe), releases[0] if releases else None)


STEP_ORDER_NOTE = {
    "0-safe-owners": " (optional): until a chain's batch runs, its initial owner alone controls the custody Safe "
                     "there. Run it before any release pays into the custody Safe on that chain.",
    "0-one-reserve": ": any time.",
    "1-release": ": the batches are independent; any order.",
}


def steps_of(batches):
    return list(dict.fromkeys(b["step"] for b in batches))


def signers_readme(safe, batches, by_escrow, safe_states, hashes, gas, built, descriptions, release, release_txs):
    safe_batches = [b for b in batches if b["sender_is_safe"]]
    steps = steps_of(safe_batches)
    lines = [
        "# Bridge escrow release: Safe signing package",
        "",
        f"Built {built} with the release plan. Sign only from the package you were sent for this build.",
        "",
        "- `README.md`: this guide, for the owners who sign the Safe transactions.",
        "- `PLAN.md`: the full release plan: every file, escrow, amount, and Safe hash.",
        "- `DESIGN.md`: why the release works this way (`bridge-claims/docs/RELEASE.md`).",
        f"- {', '.join(f'`{s}/`' for s in steps)}: the Safe Transaction Builder batches.",
        "",
        "Escrows owned by a single key are released by that key from its own package; they appear in",
        "`PLAN.md` but need no Safe signature.",
        "",
        "## The Safe transactions",
        "",
        "| Step | Chain | Safe | Signers | Calls | Nonce | Gas cost at most | File |",
        "|---|---|---|---|---:|---:|---:|---|",
    ]
    for b in safe_batches:
        info = safe_states.get((b["chain"], b["sender"])) or {}
        owners = ", ".join(f"`{o}`" for o in info.get("owners") or [])
        price = gas[b["chain"]]
        bound = (len(b["escrows"]) * GAS_PER_ESCROW) if b["escrows"] else 200_000
        lines.append(
            f"| {b['step']} | {b['chain']} | `{b['sender']}` | {info.get('threshold')} of {owners} | {b['calls']} | "
            f"{b['planned_nonce']} | {units(bound * price, 18, 8)} {NATIVE.get(b['chain'], 'ETH')} | `{b['file']}` |")
    lines += ["", "What each one does:", ""]
    lines += [f"- **`{b['file']}`**: {descriptions[b['file']]}" for b in safe_batches]
    lines += [
        "",
        "The gas cost is paid by the owner who executes, at the gas price when this package was built.",
        "",
        "## Order",
        "",
    ]
    lines += [f"{i}. `{s}/`{STEP_ORDER_NOTE[s]}" for i, s in enumerate(steps, 1)]
    lines += [
        f"{len(steps) + 1}. Afterwards, the claim contracts are deployed and the approvals signed (`RUNBOOK.md`",
        "   steps 4 and 5). Their Safe hashes depend on the nonce, so they are rebuilt after these batches run.",
        "",
        "## How to sign a batch",
        "",
        "1. Open the Safe in the Safe web app, on the batch's chain.",
        "2. Open Apps, then Transaction Builder, and drag the batch file in. The number of transactions",
        "   must match the table above.",
        "3. Create the batch and send it to the Safe queue. Its nonce must be the planned nonce. If it is",
        "   not, stop: the plan must be rebuilt.",
        "4. Before signing, compare the Safe transaction hash with the table below. A hardware wallet",
        "   shows the domain hash and message hash instead; compare those.",
        "5. When enough owners have signed, one owner executes and pays the gas.",
        "",
        "| File | Nonce | Safe tx hash | Domain hash | Message hash |",
        "|---|---:|---|---|---|",
    ]
    lines += [f"| `{b['file']}` | {b['planned_nonce']} | `{b['safe_tx_hash']}` | `{b['domain_hash']}` | "
              f"`{b['message_hash']}` |" for b in safe_batches]
    if release:
        at, example = 0, None
        for a in release["escrows"]:
            e = by_escrow[(release["chain"], a)]
            n = len(release_calls(e, safe))
            if e["previous_trusted_remote"] == "0x" and example is None:
                example = (e, release_txs[at:at + n])
            at += n
        e, calls = example
        lines += [
            "",
            "## What a release batch contains: one escrow, decoded",
            "",
            f"Every escrow in a release batch gets the same calls. These are the {len(calls)} for {e['pool_id']}",
            f"(`{e['escrow']}`, {units(e['locked_raw'], e['decimals'])} {e['symbol']}) in `{release['file']}`,",
            "decoded from the file's own calldata. The Safe app's decoding of the same transactions should",
            "say the same.",
            "",
        ]
        lines += [f"{i}. To `{c['to']}`: {decode_call(ct.unhex(c['data']))}" for i, c in enumerate(calls, 1)]
        unusual = [x for x in by_escrow.values() if x["previous_trusted_remote"] != "0x"]
        lines += [
            "",
            "Calls 1 to 4 make the escrow's owner its only verifier and executor for messages from the",
            "custody Safe on this chain, pin the library, and trust that sender. Call 5 attests the one",
            "release message, call 6 delivers it (the escrow pays the custody Safe), and calls 7 to 10 put",
            "every setting back. The other escrows differ only in address and amount"
            + (", and in call 7 for " + ", ".join(f"{x['pool_id']} (`{x['escrow']}`), which gets its earlier "
                                                  f"trusted remote `{x['previous_trusted_remote']}` back"
                                                  for x in unusual) if unusual else "") + ".",
        ]
    lines += [
        "",
        "## File hashes",
        "",
        "`shasum -a 256 0-*/* 1-*/*` in this folder must print:",
        "",
        "| File | SHA-256 |",
        "|---|---|",
    ]
    lines += [f"| `{name}` | `{digest}` |" for name, digest in sorted(hashes.items())]
    return "\n".join(lines) + "\n"


def write_signers(out, batches, by_escrow, safe_states, config, safe, sheet_path, descriptions, replace):
    """`handoff/safe-signers/`: every Safe batch, the plan, the design note, and a signing guide."""
    folder = os.path.join(out, "handoff", "safe-signers")
    design = os.path.join(ct.PROJECT, "docs", "RELEASE.md")
    sources = [(os.path.join(out, b["file"]), b["file"]) for b in batches if b["sender_is_safe"]]
    sources += [(sheet_path, "PLAN.md"), (design, "DESIGN.md")]
    readme = os.path.join(folder, "README.md")
    targets = [os.path.join(folder, name) for _, name in sources] + [readme]
    lib.prepare_outputs(targets, replace)
    for source, name in sources:
        shutil.copyfile(source, os.path.join(folder, name))
    hashes = {name: lib.file_sha256(os.path.join(folder, name)) for _, name in sources[:-2]}
    gas = {b["chain"]: int(rpc_for(config, b["chain"]).call("eth_gasPrice", []), 16)
           for b in batches if b["sender_is_safe"]}
    release = example_release(batches, safe)
    release_txs = []
    if release:
        with open(os.path.join(out, release["file"])) as source:
            release_txs = json.load(source)["transactions"]
    built = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lib.write_text(readme, signers_readme(safe, batches, by_escrow, safe_states, hashes, gas, built, descriptions,
                                          release, release_txs))
    return [os.path.relpath(p, out) for p in targets]


def calldata_record(chain, chain_id, owner, calls):
    return {
        "chain": chain, "chain_id": chain_id, "from": owner,
        "transactions": [{"to": c["to"], "value": str(c["value"]), "data": common.hex0x(c["data"]),
                          "note": c["note"]} for c in calls],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", default=str(ct.REPO / "bridge" / "routes.json"))
    parser.add_argument("--config", default=str(ct.REPO / "bridge" / "local" / "config.json"))
    parser.add_argument("--allocations-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--safe", default="0x19565f4771843467aad632d6b56c75396785b06c",
                        help="custody Safe: the recipient on every chain and the sender named in each packet")
    parser.add_argument("--skip", nargs="*", default=[], help="escrow addresses to leave out")
    parser.add_argument("--max-escrows-per-batch", type=int, default=DEFAULT_MAX_ESCROWS_PER_BATCH,
                        help="escrows per Safe release batch (keeps each under the 16,777,216 gas cap)")
    parser.add_argument("--safe-version", default="1.3.0")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    args.skip = {lib.address(a) for a in args.skip}

    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    safe = lib.address(args.safe)
    custody_plan = lib.load_json(os.path.join(args.allocations_dir, "custody-plan.json"))
    escrows, skipped, releases, custody_safes, owner_setup, one_funding, safe_states = plan(
        args, config, custody_plan, safe)

    out = args.output_dir
    written = []
    batches = []
    next_nonce = {
        key: info.get("nonce")
        for key, info in safe_states.items()
        if info.get("nonce") is not None
    }

    def emit(step, chain, sender, calls, is_safe_sender, part=None, released=()):
        chain_id = int(config["chains"][chain]["chain_id"])
        suffix = f"-{part}" if part else ""
        record = {"step": step, "chain": chain, "sender": sender, "sender_is_safe": is_safe_sender,
                  "calls": len(calls), "escrows": [e["escrow"] for e in released]}
        if is_safe_sender:
            path = os.path.join(out, step, f"{chain}-{sender[:10]}{suffix}-safe.json")
            lib.prepare_outputs([path], args.replace)
            info = safe_states.get((chain, sender)) or {}
            version = info.get("version") or args.safe_version
            planned_nonce = next_nonce.get((chain, sender))
            if planned_nonce is not None:
                next_nonce[(chain, sender)] = planned_nonce + 1
            tx = write_safe_batch(path, chain_id, sender, calls, f"{step}: {chain}{suffix}", version, planned_nonce)
            record |= {
                "safe_version": version,
                "current_nonce": info.get("nonce"),
                "planned_nonce": planned_nonce,
                "operation": tx["operation"],
                "to": tx["to"],
                "safe_tx_hash": tx["safeTxHash"],
                "domain_hash": tx["domainHash"],
                "message_hash": tx["messageHash"],
                "data_keccak256": tx["dataKeccak256"],
            }
        else:
            path = os.path.join(out, step, f"{chain}-{sender[:10]}-eoa.json")
            lib.prepare_outputs([path], args.replace)
            lib.write_json(path, calldata_record(chain, chain_id, sender, calls))
        record["file"] = os.path.relpath(path, out)
        batches.append(record)
        written.append(record["file"])

    for chain, calls in sorted(owner_setup.items()):
        emit("0-safe-owners", chain, safe, calls, True)
    if one_funding:
        emit("0-one-reserve", one_funding["chain"], one_funding["from"], one_funding["calls"], True)
    for r in releases:
        emit("1-release", r["chain"], r["owner"], r["calls"], r["owner_is_safe"], r["part"], r["escrows"])
    by_escrow = {(e["chain"], e["escrow"]): e for e in escrows}
    for owner in sorted({b["sender"] for b in batches if not b["sender_is_safe"]}):
        files = [b for b in batches if b["sender"] == owner and not b["sender_is_safe"]]
        written += write_handoff(out, owner, files, by_escrow, config, safe, args.replace)

    summary_path = os.path.join(out, "release-plan.json")
    sheet_path = os.path.join(out, "RELEASE.md")
    lib.prepare_outputs([summary_path, sheet_path], args.replace)
    lib.write_text(sheet_path, sheet(safe, escrows, skipped, custody_safes, owner_setup, one_funding, batches))
    descriptions = {b["file"]: file_description(b, safe, by_escrow, custody_safes, owner_setup, one_funding)
                    for b in batches}
    written += write_signers(out, batches, by_escrow, safe_states, config, safe, sheet_path, descriptions,
                             args.replace)
    one_summary = (
        {k: v for k, v in one_funding.items() if k != "calls"}
        if one_funding else None
    )
    lib.write_json(summary_path, {
        "schema_version": SCHEMA_VERSION,
        "method": "local",
        "safe": safe,
        "built_at": int(time.time()),
        "custody_safes": custody_safes,
        "one_funding": one_summary,
        "escrows": escrows,
        "skipped": skipped,
        "batches": batches,
        "files": sorted(written),
    })
    eoa_txs = sum(b["calls"] for b in batches if b["step"] == "1-release" and not b["sender_is_safe"])
    safe_batches = sum(1 for b in batches if b["step"] == "1-release" and b["sender_is_safe"])
    print(f"{len(escrows)} escrows to release, {len(skipped)} skipped; wrote {len(written)} files in {out}")
    print(f"  release: {safe_batches} Safe batches, {eoa_txs} EOA transactions; no LayerZero fees")


def units(raw, decimals, places=4):
    """A raw token amount for people: thousands separators, at most `places` decimals."""
    if decimals is None:
        return f"{raw} raw"
    text = f"{Decimal(int(raw)).scaleb(-decimals):,.{places}f}"
    return text.rstrip("0").rstrip(".")


def file_description(b, safe, by_escrow, custody_safes, owner_setup, one_funding):
    """One plain-language bullet for a planned file: what it does, who signs, and how big it is."""
    chain, sender = b["chain"], b["sender"]
    if b["step"] == "0-safe-owners":
        added = ", ".join(f"`{c['args']['owner']}`" for c in owner_setup[chain])
        signer = custody_safes[chain]["owners"][0]
        return (f"on {chain}, the custody Safe (1-of-1, signed by `{signer}` alone) adds {added} as owners "
                f"and sets the threshold to 2, matching the Ethereum Safe; {b['calls']} calls at nonce "
                f"{b['planned_nonce']}. Optional for the release itself, but until it runs `{signer}` alone "
                "controls this Safe and everything the release delivers to it.")
    if b["step"] == "0-one-reserve":
        return (f"on {chain}, the Reserve Safe `{sender}` transfers {units(one_funding['amount_raw'], 18)} ONE "
                f"to the custody Safe; 1 call at nonce {b['planned_nonce']}.")
    released = [by_escrow[(chain, a)] for a in b["escrows"]]
    symbols = [e["symbol"] for e in released]
    assets = ", ".join(
        f"{e['symbol']} {units(e['locked_raw'], e['decimals'])}"
        + (f" (`{e['escrow'][:6]}…`)" if symbols.count(e["symbol"]) > 1 else "")
        for e in released
    )
    count = f"{len(released)} escrow{'s' if len(released) != 1 else ''}"
    if b["sender_is_safe"]:
        gas = len(released) * GAS_PER_ESCROW / 1e6
        to = "itself (it is the custody Safe)" if sender == safe else "the custody Safe"
        return (f"on {chain}, Safe `{sender}` releases {count} to {to} in one atomic transaction: "
                f"{b['calls']} calls, nonce {b['planned_nonce']}, under {gas:.1f} million gas. Escrows: {assets}.")
    return (f"on {chain}, key `{sender}` releases {count} to the custody Safe in {b['calls']} transactions, "
            f"sent one by one in file order, each under 100,000 gas. Escrows: {assets}.")


def sheet(safe, escrows, skipped, custody_safes, owner_setup, one_funding, batches):
    by_escrow = {(e["chain"], e["escrow"]): e for e in escrows}
    lines = [
        "# Release the bridge collateral to the custody Safe",
        "",
        f"Custody Safe (recipient on every chain): `{safe}`.",
        "",
        "Every escrow is released on its own chain by its own owner. No message crosses chains, and no",
        "LayerZero verifier, executor, or fee is involved. Why this works, and what each call encodes, is",
        "in `bridge-claims/docs/RELEASE.md`. Run the steps in order, confirming each on chain before the",
        "next: " + ", then ".join(f"`{s}/`" + (" (optional)" if s == "0-safe-owners" else "")
                                  for s in steps_of(batches)) + ".",
        "",
        "## What each file does",
        "",
    ]
    lines += [f"- **`{b['file']}`**: " + file_description(b, safe, by_escrow, custody_safes, owner_setup, one_funding)
              for b in batches]
    lines += [
        "",
        "Each escrow in a `1-release/` file gets the same calls, in this order. Calls 3 and 8 appear only",
        "when the escrow follows LayerZero's default receive library.",
        "",
        "1. The owner becomes the only verifier for messages from the custody Safe on this chain.",
        "2. The owner becomes the only party allowed to deliver them.",
        "3. The escrow is pinned to the receive library in effect, so a LayerZero change cannot reach it.",
        "4. The escrow trusts the custody Safe on this chain.",
        "5. The owner attests one packet that credits the escrow's whole locked balance to the custody Safe.",
        "6. The owner delivers it, and the escrow pays the custody Safe. This is the only call that moves funds.",
        "7. to 10. The trusted remote, receive version, executor, and verifier config are put back to what",
        "   they were when the plan was built.",
        "",
        "How to run the files:",
        "",
        "- A `*-safe.json` file is a Safe Transaction Builder batch: one atomic transaction at the planned",
        "  nonce. Check its Safe transaction hash against the table below before signing. If the Safe",
        "  executes anything else first, the hash no longer applies; rebuild. Send the Safe owners",
        "  `handoff/safe-signers/`: every Safe batch, this plan, the design note, and a signing guide with",
        "  the hashes and one escrow's calls decoded from the batch.",
        "- A `*-eoa.json` file lists transactions for the key in `from`: send each entry's `to`, `value`",
        "  (0), and `data` in file order. If one fails, resend it, and finish a started escrow from the same",
        "  file (the planner refuses to plan over a half-finished release). Send each key holder their",
        "  `handoff/<key>/` folder: their files, `send_calldata.py` (which sends them one by one, simulating",
        "  each first and logging each receipt), and step-by-step instructions.",
        "- Gas is paid by the account that executes; no Safe needs native coin for this step.",
        "- Rebuild right before signing or sending: each packet carries the exact locked balance and path",
        "  nonce read when the plan was built.",
        "",
        "## Custody Safe status",
        "",
        "| Chain | Safe | Version | Owners | Threshold | Nonce | Native balance | Status |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for chain, info in custody_safes.items():
        nat = int(info.get("native_balance_raw") or 0) / 1e18
        lines.append(
            f"| {chain} | `{safe}` | {info.get('version', '?')} | {len(info.get('owners') or [])} | "
            f"{info.get('threshold', '?')} | {info.get('nonce', '?')} | {nat:.6f} | {info.get('status', '?')} |"
        )
    if one_funding:
        amt = lib.format_units(int(one_funding["amount_raw"]), 18)
        lines += [
            "",
            "## ONE reserve transfer (`0-one-reserve/`)",
            "",
            f"- From Reserve Safe `{one_funding['from']}` on {one_funding['chain']} to `{one_funding['to']}`: "
            f"{amt} ONE (`{one_funding['amount_raw']}` raw, token `{one_funding['token']}`)",
        ]
    lines += [
        "",
        "## Safe batches and planned nonces",
        "",
        "| Step | Chain | Safe | Calls | Escrows | Current nonce | Planned nonce | Safe tx hash | File |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for b in batches:
        if b["sender_is_safe"]:
            lines.append(
                f"| {b['step']} | {b['chain']} | `{b['sender']}` | {b['calls']} | {len(b['escrows'])} | "
                f"{b['current_nonce']} | {b['planned_nonce']} | `{b['safe_tx_hash'] or '—'}` | `{b['file']}` |"
            )
    eoa = [b for b in batches if not b["sender_is_safe"]]
    if eoa:
        lines += ["", "## EOA release transactions", "", "| Chain | Owner | Escrows | Transactions | File |",
                  "|---|---|---:|---:|---|"]
        lines += [f"| {b['chain']} | `{b['sender']}` | {len(b['escrows'])} | {b['calls']} | `{b['file']}` |"
                  for b in eoa]
    lines += [
        "",
        "## Escrows",
        "",
        "| Chain | Pool | Escrow | Owner | Kind | Payload | Locked | Locked (raw) | Receive version | Path nonce "
        "| Prior trusted remote |",
        "|---|---|---|---|---|---|---:|---:|---|---:|---|",
    ]
    for e in escrows:
        owner = f"`{e['owner']}` ({'Safe' if e['owner_is_safe'] else 'EOA'})"
        pinned = (f"{e['receive_version']} (pinned)" if e["stored_receive_version"] == e["receive_version"]
                  else f"{e['receive_version']} (default; pinned during the release)")
        prior = f"`{e['previous_trusted_remote']}`" if e["previous_trusted_remote"] != "0x" else "none"
        lines.append(
            f"| {e['chain']} | {e['pool_id']} | `{e['escrow']}` | {owner} | {e['kind']} | {e['payload_format']} | "
            f"{units(e['locked_raw'], e['decimals'])} {e['symbol']} | {e['locked_raw']} | {pinned} | "
            f"{e['path_nonce']} | {prior} |"
        )
    if skipped:
        lines += ["", "## Not released", "", "| Chain | Pool | Escrow | Reason |", "|---|---|---|---|"]
        lines += [f"| {e['chain']} | {e['pool_id']} | `{e['escrow']}` | {e['reason']} |" for e in skipped]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
