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
get raw calldata to send in order. Balances, configs, nonces, and Safe nonces are read live. The
Harmony path (chain 116) is never touched. Nothing is sent.

`test/EscrowRelease.t.sol` replays every generated file on a fork of each chain.
"""

import argparse
import json
import os
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

    summary_path = os.path.join(out, "release-plan.json")
    sheet_path = os.path.join(out, "RELEASE.md")
    lib.prepare_outputs([summary_path, sheet_path], args.replace)
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
    lib.write_text(sheet_path, sheet(safe, escrows, skipped, custody_safes, owner_setup, one_funding, batches))
    eoa_txs = sum(b["calls"] for b in batches if b["step"] == "1-release" and not b["sender_is_safe"])
    safe_batches = sum(1 for b in batches if b["step"] == "1-release" and b["sender_is_safe"])
    print(f"{len(escrows)} escrows to release, {len(skipped)} skipped; wrote {len(written)} files in {out}")
    print(f"  release: {safe_batches} Safe batches, {eoa_txs} EOA transactions; no LayerZero fees")


def units(raw, decimals):
    """A raw token amount for people: thousands separators, at most four decimals."""
    if decimals is None:
        return f"{raw} raw"
    text = f"{Decimal(int(raw)).scaleb(-decimals):,.4f}"
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
        "next: `0-safe-owners/` (optional) and `0-one-reserve/`, then `1-release/`.",
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
        "  executes anything else first, the hash no longer applies; rebuild.",
        "- A `*-eoa.json` file lists transactions for the key in `from`: send each entry's `to`, `value`",
        "  (0), and `data` in file order. If one fails, resend it, and finish a started escrow from the same",
        "  file (the planner refuses to plan over a half-finished release).",
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
