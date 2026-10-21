#!/usr/bin/env python3
"""Shared helpers for the bridge-collateral allocation pipeline.

Standard library only. Keccak, ABI decoding, and validator-wrapper detection
come from the contract-review library; this module adds a multi-chain
JSON-RPC client, the route registry and configuration loaders, LayerZero V1
call encoders, and deterministic CSV/JSON output helpers.
"""

import concurrent.futures
import csv
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contract-review"))
import contract_review_lib as crl  # noqa: E402

HARMONY = "harmony"
NATIVE = "native"
ZERO_ADDRESS = "0x" + "0" * 40
KINDS = {"remote_escrow", "harmony_origin"}
CLAIM_ASSETS = {"escrow", "one", "unsupported"}
TIME_LABELS = ("rollback", "cutoff")

SEL = {
    name: crl.selector(signature)
    for name, signature in {
        "balanceOf": "balanceOf(address)",
        "totalSupply": "totalSupply()",
        "decimals": "decimals()",
        "symbol": "symbol()",
        "owner": "owner()",
        "token": "token()",
        "lzEndpoint": "lzEndpoint()",
        "getOwners": "getOwners()",
        "getThreshold": "getThreshold()",
        "required": "required()",
        "VERSION": "VERSION()",
        "getInboundNonce": "getInboundNonce(uint16,bytes)",
        "getOutboundNonce": "getOutboundNonce(uint16,address)",
        "trustedRemoteLookup": "trustedRemoteLookup(uint16)",
        "getSendVersion": "getSendVersion(address)",
        "getReceiveVersion": "getReceiveVersion(address)",
        "transfer": "transfer(address,uint256)",
    }.items()
}
TOPIC = {
    "Transfer": "0x" + crl.keccak256(b"Transfer(address,address,uint256)").hex(),
    "SendToChain": "0x"
    + crl.keccak256(b"SendToChain(uint16,address,bytes,uint256)").hex(),
    "ReceiveFromChain": "0x"
    + crl.keccak256(b"ReceiveFromChain(uint16,bytes,address,uint256)").hex(),
    "MessageFailed": "0x"
    + crl.keccak256(b"MessageFailed(uint16,bytes,uint64,bytes,bytes)").hex(),
    "RetryMessageSuccess": "0x"
    + crl.keccak256(b"RetryMessageSuccess(uint16,bytes,uint64,bytes32)").hex(),
    "SetTrustedRemote": "0x" + crl.keccak256(b"SetTrustedRemote(uint16,bytes)").hex(),
    "SetTrustedRemoteAddress": "0x"
    + crl.keccak256(b"SetTrustedRemoteAddress(uint16,bytes)").hex(),
}


# --------------------------------------------------------------------------
# Addresses and amounts
# --------------------------------------------------------------------------


def address(value, context="address"):
    normalized = crl.any_to_hex(value)
    if normalized is None:
        raise ValueError(f"{context}: invalid address {value!r}")
    return normalized


def is_native(value):
    return value == NATIVE


RESERVED_LOW_ADDRESS_LIMIT = 0xFFFF


def reserved_address_reason(value):
    """Return why no key can control this address, or None.

    Addresses at or below 0xffff are precompiles and system contracts
    (Ethereum precompiles, BNB Chain system contracts at 0x1000 and up);
    an address that repeats one hex digit (0xeeee…, 0xffff…) is a placeholder
    with no known key.
    """
    raw = value[2:].lower()
    if int(raw, 16) <= RESERVED_LOW_ADDRESS_LIMIT:
        return "reserved low address (precompile or system contract)"
    if len(set(raw)) == 1:
        return "placeholder address with one repeated digit"
    return None


def format_units(raw, decimals):
    raw = int(raw)
    sign = "-" if raw < 0 else ""
    whole, fraction = divmod(abs(raw), 10 ** int(decimals))
    if int(decimals) == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:0{int(decimals)}d}"


def utc_text(timestamp):
    return (
        datetime.fromtimestamp(int(timestamp), timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def parse_utc(text):
    value = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    return int(value.replace(tzinfo=timezone.utc).timestamp())


# --------------------------------------------------------------------------
# ABI helpers
# --------------------------------------------------------------------------


def word(value):
    return format(int(value), "064x")


def address_word(value):
    return address(value)[2:].rjust(64, "0")


def encode_bytes_argument(data_hex):
    raw = data_hex[2:] if data_hex.startswith("0x") else data_hex
    length = len(raw) // 2
    padded = raw.ljust(((len(raw) + 63) // 64) * 64, "0")
    return word(length) + padded


def call_data(name, *words):
    return SEL[name] + "".join(words)


def inbound_nonce_data(source_lz_chain_id, source_app, destination_app):
    path = address(source_app)[2:] + address(destination_app)[2:]
    return (
        SEL["getInboundNonce"]
        + word(source_lz_chain_id)
        + word(64)
        + encode_bytes_argument(path)
    )


def outbound_nonce_data(destination_lz_chain_id, source_app):
    return (
        SEL["getOutboundNonce"]
        + word(destination_lz_chain_id)
        + address_word(source_app)
    )


def decode_uint(data):
    if not data or data == "0x":
        return None
    return crl.decode_uint(data)


def decode_address(data):
    if not data or data == "0x":
        return None
    return crl.decode_address(data)


def decode_dynamic_bytes(data, offset_word=0):
    raw = data[2:] if data.startswith("0x") else data
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    if len(words) <= offset_word:
        return None
    offset = int(words[offset_word], 16)
    if offset % 32:
        return None
    start = offset // 32
    if len(words) <= start:
        return None
    length = int(words[start], 16)
    body = raw[(start + 1) * 64:(start + 1) * 64 + 2 * length]
    if len(body) != 2 * length:
        return None
    return "0x" + body


def topic_address(topic):
    raw = topic[2:] if topic.startswith("0x") else topic
    if len(raw) != 64 or raw[:24] != "0" * 24:
        raise ValueError(f"topic is not an address: {topic}")
    return "0x" + raw[24:].lower()


def packed_address(data_hex):
    """Return the address encoded as 20 packed bytes, or None."""
    if data_hex is None:
        return None
    raw = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(raw) != 40:
        return None
    return "0x" + raw.lower()


def decode_oft_payload(payload_hex):
    """Decode an OFT V1 send payload into (from, to, amount).

    Harmony's OFT fork encodes (uint16 packetType, bytes from, bytes to,
    uint amount); upstream V1 encodes (uint16 packetType, bytes to, uint
    amount). Both forms are accepted; `from` is None for the upstream form.
    """
    raw = payload_hex[2:] if payload_hex.startswith("0x") else payload_hex
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    if len(words) >= 4 and int(words[1], 16) == 128 and int(words[2], 16) >= 160:
        source = packed_address(decode_dynamic_bytes(payload_hex, 1))
        recipient = packed_address(decode_dynamic_bytes(payload_hex, 2))
        return source, recipient, int(words[3], 16)
    if len(words) >= 3 and int(words[1], 16) == 96:
        recipient = packed_address(decode_dynamic_bytes(payload_hex, 1))
        return None, recipient, int(words[2], 16)
    raise ValueError("unrecognized OFT payload layout")


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------


class RpcError(Exception):
    pass


def redact_url(url):
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname or ''}"


class Rpc:
    """Small JSON-RPC client with retries, batching, and bounded concurrency."""

    def __init__(
        self,
        url,
        label,
        batch_size=40,
        workers=6,
        timeout=120,
        attempts=8,
        min_interval=0.0,
    ):
        self.url = url
        self.label = label
        self.batch_size = batch_size
        self.workers = workers
        self.timeout = timeout
        self.attempts = attempts
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.requests = 0

    @property
    def host(self):
        return redact_url(self.url)

    def _throttle(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def _post(self, payload):
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        last_error = None
        for attempt in range(self.attempts):
            self._throttle()
            try:
                request = urllib.request.Request(
                    self.url,
                    data=encoded,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "harmony-migration-bridge/1",
                    },
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                with self._lock:
                    self.requests += 1
                return json.loads(body)
            except urllib.error.HTTPError as error:
                last_error = error
                if 400 <= error.code < 500 and error.code != 429:
                    try:
                        decoded = json.loads(error.read())
                    except ValueError:
                        decoded = None
                    if isinstance(decoded, (dict, list)):
                        return decoded
                time.sleep(min(30, 1.5 * (attempt + 1)))
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                ValueError,
                OSError,
            ) as error:
                last_error = error
                time.sleep(min(30, 1.5 * (attempt + 1)))
        raise RpcError(
            f"{self.label} {self.host}: transport failed after "
            f"{self.attempts} attempts: {last_error}"
        )

    def call(self, method, params):
        result = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if not isinstance(result, dict):
            raise RpcError(f"{self.label}: unexpected {method} response")
        if result.get("error") or "result" not in result:
            raise RpcError(f"{self.label} {method}: {json.dumps(result)[:300]}")
        return result["result"]

    def call_soft(self, method, params):
        result = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if not isinstance(result, dict):
            return None, {"message": "unexpected response"}
        if result.get("error"):
            return None, result["error"]
        if "result" not in result:
            return None, {"message": f"response without result: {json.dumps(result)[:200]}"}
        return result["result"], None

    def _batch_chunk(self, chunk):
        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(chunk)
        ]
        result = self._post(payload)
        if not isinstance(result, list):
            if len(chunk) == 1:
                if isinstance(result, dict) and result.get("error"):
                    return [(None, result["error"])]
                if isinstance(result, dict):
                    return [(result.get("result"), None)]
            return [self.call_soft(method, params) for method, params in chunk]
        by_id = {item.get("id"): item for item in result if isinstance(item, dict)}
        out = []
        for index, (method, params) in enumerate(chunk):
            item = by_id.get(index)
            if item is None:
                out.append(self.call_soft(method, params))
            elif item.get("error"):
                out.append((None, item["error"]))
            elif "result" not in item:
                out.append((None, {"message": "batch item without result"}))
            else:
                out.append((item["result"], None))
        return out

    def batch(self, calls, retries=4):
        """Run (method, params) calls; return (result, error) pairs in order.

        Errors that are not contract reverts (rate limits, timeouts, missing
        state on a load-balanced backend) are retried one call at a time.
        """
        if not calls:
            return []
        chunks = [
            calls[i:i + self.batch_size]
            for i in range(0, len(calls), self.batch_size)
        ]
        results = [None] * len(chunks)
        with concurrent.futures.ThreadPoolExecutor(self.workers) as pool:
            futures = {
                pool.submit(self._batch_chunk, chunk): index
                for index, chunk in enumerate(chunks)
            }
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()
        flat = [item for part in results for item in part]
        for attempt in range(retries):
            pending = [
                index for index, (_, error) in enumerate(flat)
                if error is not None and not is_revert(error)
            ]
            if not pending:
                break
            time.sleep(1.5 * (attempt + 1))
            with concurrent.futures.ThreadPoolExecutor(min(self.workers, 4)) as pool:
                retried = list(pool.map(lambda index: self.call_soft(*calls[index]), pending))
            for index, outcome in zip(pending, retried):
                flat[index] = outcome
        return flat

    def eth_call(self, to, data, block):
        result, error = self.call_soft(
            "eth_call", [{"to": to, "data": data}, block_tag(block)]
        )
        if error is not None:
            return None
        return result

    def block(self, number):
        value = self.call("eth_getBlockByNumber", [block_tag(number), False])
        if value is None:
            raise RpcError(f"{self.label}: block {number} not found")
        return value

    def head(self):
        return int(self.call("eth_blockNumber", []), 16)

    def chain_id(self):
        return int(self.call("eth_chainId", []), 16)

    def logs(self, address_value, topics, from_block, to_block, range_size=5000):
        """Fetch logs in [from_block, to_block], splitting ranges on errors."""
        ranges = []
        start = from_block
        while start <= to_block:
            end = min(to_block, start + range_size - 1)
            ranges.append((start, end))
            start = end + 1
        if len(ranges) == 1:
            return self._logs_range(address_value, topics, *ranges[0])
        with concurrent.futures.ThreadPoolExecutor(self.workers) as pool:
            parts = pool.map(
                lambda item: self._logs_range(address_value, topics, *item), ranges
            )
            return [log for part in parts for log in part]

    def _logs_range(self, address_value, topics, start, end):
        params = [{
            "address": address_value,
            "topics": topics,
            "fromBlock": block_tag(start),
            "toBlock": block_tag(end),
        }]
        result, error = self.call_soft("eth_getLogs", params)
        for attempt in range(6):
            if error is None or not is_rate_limited(error):
                break
            time.sleep(5 * (attempt + 1))
            result, error = self.call_soft("eth_getLogs", params)
        if error is None and isinstance(result, list):
            for item in result:
                if item.get("removed"):
                    raise RpcError(f"{self.label}: RPC returned a removed log")
            return result
        if start == end:
            raise RpcError(
                f"{self.label}: eth_getLogs failed at block {start}: {error}"
            )
        middle = start + (end - start) // 2
        return self._logs_range(address_value, topics, start, middle) + (
            self._logs_range(address_value, topics, middle + 1, end)
        )


def is_revert(error):
    message = json.dumps(error).lower()
    return (
        "revert" in message
        or "invalid opcode" in message
        or (isinstance(error, dict) and error.get("code") == 3)
    )


def is_rate_limited(error):
    message = json.dumps(error).lower()
    return any(
        marker in message
        for marker in ("rate limit", "rate-limit", "too many requests", "limit exceeded", "usage limit")
    ) and "block range" not in message and "blocks range" not in message


def deployment_block(rpc, contract, high):
    """First block at which contract has code, by binary search."""
    def has_code(block):
        code = rpc.call("eth_getCode", [contract, block_tag(block)])
        return bool(code) and code != "0x"
    if not has_code(high):
        raise ValueError(f"{rpc.label}: {contract} has no code at {high}")
    low = 0
    while low < high:
        middle = (low + high) // 2
        if has_code(middle):
            high = middle
        else:
            low = middle + 1
    return low


def block_tag(number):
    if isinstance(number, str):
        return number
    return hex(int(number))


def chain_rpc(config, chain, **kwargs):
    entry = config["chains"][chain]
    variable = entry.get("rpc_env")
    url = os.environ.get(variable or "")
    if not url:
        raise SystemExit(
            f"set {variable} to an archive JSON-RPC URL for {chain}"
        )
    client_keys = ("batch_size", "workers", "timeout", "attempts", "min_interval")
    settings = {
        key: value
        for key, value in (entry.get("rpc_settings") or {}).items()
        if key in client_keys
    }
    settings.update(kwargs)
    return Rpc(url, chain, **settings)


# --------------------------------------------------------------------------
# Registry and configuration
# --------------------------------------------------------------------------


def load_json(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def load_routes(path):
    routes = load_json(path)
    if routes.get("schema_version") != 1:
        raise ValueError("unsupported route registry schema")
    harmony = routes["harmony"]
    harmony["endpoint"] = address(harmony["endpoint"], "harmony endpoint")
    harmony["wone"] = address(harmony["wone"], "harmony WONE")
    chains = routes["chains"]
    if HARMONY in chains:
        raise ValueError("harmony belongs in the harmony section, not chains")
    seen = set()
    for pool in routes["pools"]:
        pool_id = pool["pool_id"]
        if pool_id in seen:
            raise ValueError(f"duplicate pool_id {pool_id}")
        seen.add(pool_id)
        if pool["kind"] not in KINDS:
            raise ValueError(f"{pool_id}: unknown kind {pool['kind']}")
        if pool["claim_asset"] not in CLAIM_ASSETS:
            raise ValueError(f"{pool_id}: unknown claim_asset")
        if pool["chain"] not in chains:
            raise ValueError(f"{pool_id}: unknown chain {pool['chain']}")
        if pool["kind"] == "remote_escrow" and pool["claim_asset"] != "escrow":
            raise ValueError(f"{pool_id}: remote_escrow pools pay the escrow asset")
        if pool["claim_asset"] == "one" and pool["kind"] != "harmony_origin":
            raise ValueError(f"{pool_id}: ONE claims need a harmony_origin pool")
        pool["remote_adapter"] = address(pool["remote_adapter"], pool_id)
        if not is_native(pool["remote_token"]):
            pool["remote_token"] = address(pool["remote_token"], pool_id)
        if not pool.get("harmony"):
            raise ValueError(f"{pool_id}: no Harmony contracts")
        for entry in pool["harmony"]:
            entry["proxy"] = address(entry["proxy"], pool_id)
            if not is_native(entry["token"]):
                entry["token"] = address(entry["token"], pool_id)
        pool["previous_remote_adapters"] = [
            address(item, pool_id)
            for item in pool.get("previous_remote_adapters", ())
        ]
        for item in pool.setdefault("ignored_peers", []):
            item["address"] = address(item["address"], pool_id)
        for item in pool.setdefault("ignored_remote_peers", []):
            item["address"] = address(item["address"], pool_id)
        pool["decimals"] = int(pool["decimals"])
    for entry in routes.get("system_addresses", ()):
        entry["address"] = address(entry["address"], "system address")
        if entry["chain"] != "*" and entry["chain"] != HARMONY:
            if entry["chain"] not in chains:
                raise ValueError(f"system address on unknown chain {entry}")
    return routes


def load_config(path, routes):
    config = load_json(path)
    if config.get("schema_version") != 1:
        raise ValueError("unsupported bridge config schema")
    for label in TIME_LABELS:
        parse_utc(config["times"][label])
    chains = config["chains"]
    required = {HARMONY, *routes["chains"]}
    missing = sorted(required - set(chains))
    if missing:
        raise ValueError("config chains missing: " + ", ".join(missing))
    for name, entry in chains.items():
        if entry.get("custody_multisig"):
            entry["custody_multisig"] = address(entry["custody_multisig"], name)
        for label, pin in (entry.get("pinned_blocks") or {}).items():
            if label not in TIME_LABELS:
                raise ValueError(f"{name}: unknown pinned block label {label}")
            pin["number"] = int(pin["number"])
    one = config["one_claims"]
    if one["claim_chain"] not in routes["chains"]:
        raise ValueError("one_claims.claim_chain must be a remote chain")
    one["claim_token"] = address(one["claim_token"], "one_claims.claim_token")
    one["reserve_safe"] = address(one["reserve_safe"], "one_claims.reserve_safe")
    if one.get("custody_multisig"):
        one["custody_multisig"] = address(
            one["custody_multisig"], "one_claims.custody_multisig"
        )
    snapshot = config.setdefault("snapshot_policy", {})
    for chain, label in (snapshot.get("remote_holder_overrides") or {}).items():
        if label not in TIME_LABELS:
            raise ValueError(f"snapshot override for {chain}: {label}")
    return config


def one_custody(config):
    one = config["one_claims"]
    return one.get("custody_multisig") or config["chains"][one["claim_chain"]].get(
        "custody_multisig"
    )


def system_address_roles(routes, chain):
    roles = {}
    for entry in routes.get("system_addresses", ()):
        if entry["chain"] in ("*", chain):
            roles.setdefault(entry["address"], entry["role"])
    for pool in routes["pools"]:
        if chain == pool["chain"]:
            roles.setdefault(pool["remote_adapter"], f"{pool['pool_id']} remote adapter")
            for item in pool["previous_remote_adapters"]:
                roles.setdefault(item, f"{pool['pool_id']} previous adapter")
            for item in pool["ignored_remote_peers"]:
                roles.setdefault(item["address"], f"{pool['pool_id']} ignored remote peer")
            if not is_native(pool["remote_token"]):
                roles.setdefault(pool["remote_token"], f"{pool['pool_id']} token")
        if chain == HARMONY:
            for item in pool["ignored_peers"]:
                roles.setdefault(item["address"], f"{pool['pool_id']} ignored Harmony peer")
            for entry in pool["harmony"]:
                roles.setdefault(entry["proxy"], f"{pool['pool_id']} Harmony proxy")
                if not is_native(entry["token"]):
                    roles.setdefault(entry["token"], f"{pool['pool_id']} Harmony token")
    if chain == HARMONY:
        roles.setdefault(routes["harmony"]["endpoint"], "LayerZero endpoint")
        roles.setdefault(routes["harmony"]["wone"], "WONE contract")
    return roles


def claimant_chain(pool):
    return HARMONY if pool["kind"] == "remote_escrow" else pool["chain"]


def claimant_tokens(pool):
    """Distinct (chain, token) pairs whose holders claim from the pool."""
    if pool["kind"] == "remote_escrow":
        pairs = [(HARMONY, entry["token"]) for entry in pool["harmony"]]
    else:
        pairs = [(pool["chain"], pool["remote_token"])]
    return list(dict.fromkeys(pairs))


def claim_target(pool, config):
    """Return (claim_chain, claim_token) where a pool's claims are paid."""
    if pool["claim_asset"] == "escrow":
        return pool["chain"], pool["remote_token"]
    if pool["claim_asset"] == "one":
        one = config["one_claims"]
        return one["claim_chain"], one["claim_token"]
    return None, None


def holder_file_stem(chain, token, block):
    return f"{chain}-{address(token)}-{int(block)}"


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_outputs(paths, replace):
    for path in paths:
        path = str(path)
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path + ".partial")
        if os.path.exists(path) and not replace:
            raise FileExistsError(f"{path} exists; pass --replace")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)


def write_csv(path, fields, rows):
    path = str(path)
    partial = path + ".partial"
    with open(partial, "x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)
    return file_sha256(path)


def write_json(path, value):
    path = str(path)
    partial = path + ".partial"
    with open(partial, "x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)
    return file_sha256(path)


def write_text(path, text):
    path = str(path)
    partial = path + ".partial"
    with open(partial, "x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)
    return file_sha256(path)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))
