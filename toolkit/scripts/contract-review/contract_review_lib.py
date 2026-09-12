#!/usr/bin/env python3
"""Shared helpers for the contract-account review pipeline.

Standard library only. Provides a pure-Python keccak-256, minimal ABI
encoding/decoding, EIP-55 checksums, and a batching JSON-RPC client with
retries. All chain facts come from a Harmony archival node RPC; no explorer
or third-party indexer is used as a data source.
"""

import concurrent.futures
import json
import threading
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# keccak-256 (FIPS-202 permutation, Ethereum padding 0x01)
# --------------------------------------------------------------------------

_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_KECCAK_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_MASK64 = (1 << 64) - 1


def _rotl64(value, shift):
    shift %= 64
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _keccak_f(state):
    for round_constant in _KECCAK_RC:
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        state = [state[i] ^ d[i % 5] for i in range(25)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl64(state[x + 5 * y], _KECCAK_ROT[x][y])
        state = [
            b[i] ^ ((~b[(i % 5 + 1) % 5 + 5 * (i // 5)]) & b[(i % 5 + 2) % 5 + 5 * (i // 5)])
            for i in range(25)
        ]
        state[0] ^= round_constant
    return state


def keccak256(data):
    rate = 136
    state = [0] * 25
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate:
        padded.append(0x00)
    padded[-1] |= 0x80
    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        state = _keccak_f(state)
    out = b"".join(state[i].to_bytes(8, "little") for i in range(4))
    return out[:32]


def selector(signature):
    return "0x" + keccak256(signature.encode()).hex()[:8]


def to_checksum(address):
    raw = address.lower().replace("0x", "")
    digest = keccak256(raw.encode()).hex()
    return "0x" + "".join(
        ch.upper() if int(digest[i], 16) >= 8 else ch for i, ch in enumerate(raw)
    )


_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def bech32_to_hex(text):
    """Decode a Harmony `one1...` bech32 address into a lowercase 0x hex address."""
    if text is None:
        return None
    text = text.strip()
    if text.lower().startswith("0x"):
        return normalize_address(text)
    text = text.lower()
    if "1" not in text:
        return None
    hrp, data = text.rsplit("1", 1)
    try:
        values = [_BECH32_CHARSET.index(ch) for ch in data]
    except ValueError:
        return None
    hrp_expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    if _bech32_polymod(hrp_expanded + values) != 1:
        return None
    payload = values[:-6]
    acc = 0
    bits = 0
    out = bytearray()
    for value in payload:
        acc = (acc << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if len(out) != 20:
        return None
    return "0x" + out.hex()


def hex_to_bech32(address, hrp="one"):
    raw = bytes.fromhex(normalize_address(address)[2:])
    acc = 0
    bits = 0
    values = []
    for byte in raw:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            values.append((acc >> bits) & 31)
    if bits:
        values.append((acc << (5 - bits)) & 31)
    hrp_expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    polymod = _bech32_polymod(hrp_expanded + values + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[v] for v in values + checksum)


def any_to_hex(value):
    """Accept 0x hex or one1 bech32 and return lowercase 0x hex (or None)."""
    if value in (None, ""):
        return None
    if str(value).lower().startswith("0x"):
        return normalize_address(value)
    return bech32_to_hex(value)


def normalize_address(value):
    if value is None:
        return None
    value = value.lower()
    if not value.startswith("0x"):
        value = "0x" + value
    if len(value) != 42:
        return None
    return value


def rlp_validator_wrapper_address(code_hex):
    """If `code` is an RLP list whose first item is a 20-byte address, return it.

    Harmony stores staking ValidatorWrapper structs RLP-encoded in the account
    code field, so this distinguishes validator accounts from EVM bytecode.
    """
    raw = code_hex[2:] if code_hex.startswith("0x") else code_hex
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(data) < 24 or data[0] < 0xC0:
        return None
    # ValidatorWrapper{Validator{Address,...},...}: two nested list headers
    # precede the address item (0x94 + 20 bytes).
    offset = 0
    for _ in range(3):
        if offset >= len(data):
            return None
        prefix = data[offset]
        if prefix == 0x94:
            if len(data) < offset + 21:
                return None
            return "0x" + data[offset + 1:offset + 21].hex()
        if prefix < 0xC0:
            return None
        offset += 1 if prefix <= 0xF7 else 1 + (prefix - 0xF7)
    return None


# --------------------------------------------------------------------------
# ABI helpers (static types + string/bytes/address[] decoding)
# --------------------------------------------------------------------------


def encode_uint(value):
    return format(int(value), "064x")


def encode_address(value):
    return normalize_address(value)[2:].rjust(64, "0")


def encode_bytes4(value):
    return value.replace("0x", "").ljust(64, "0")


def encode_call(signature, *args):
    data = selector(signature)
    for kind, value in args:
        if kind == "address":
            data += encode_address(value)
        elif kind == "uint256":
            data += encode_uint(value)
        elif kind == "bytes4":
            data += encode_bytes4(value)
        elif kind == "bytes32":
            data += value.replace("0x", "").rjust(64, "0")
        else:
            raise ValueError(kind)
    return data


def _words(hexdata):
    raw = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return [raw[i:i + 64] for i in range(0, len(raw) - len(raw) % 64, 64)]


def decode_uint(hexdata, index=0):
    words = _words(hexdata)
    if len(words) <= index:
        return None
    return int(words[index], 16)


def decode_address(hexdata, index=0):
    words = _words(hexdata)
    if len(words) <= index:
        return None
    word = words[index]
    if word[:24] != "0" * 24:
        return None
    return "0x" + word[24:]


def decode_bool(hexdata, index=0):
    value = decode_uint(hexdata, index)
    if value is None:
        return None
    return value == 1


def decode_bytes32(hexdata, index=0):
    words = _words(hexdata)
    if len(words) <= index:
        return None
    return "0x" + words[index]


def decode_string(hexdata):
    """Decode a dynamic string/bytes return, tolerating bytes32-style returns."""
    raw = hexdata[2:] if hexdata.startswith("0x") else hexdata
    if not raw:
        return None
    words = _words(raw)
    if len(words) >= 2:
        offset = int(words[0], 16)
        if offset == 32 and len(words) >= 2:
            length = int(words[1], 16)
            start = 128
            data = raw[start:start + 2 * length]
            if len(data) == 2 * length:
                try:
                    return bytes.fromhex(data).decode("utf-8", errors="replace")
                except ValueError:
                    return None
    if len(words) == 1:
        data = bytes.fromhex(words[0]).rstrip(b"\x00")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def decode_address_array(hexdata):
    words = _words(hexdata)
    if len(words) < 2:
        return None
    offset = int(words[0], 16)
    if offset % 32:
        return None
    start = offset // 32
    if len(words) <= start:
        return None
    length = int(words[start], 16)
    if length > 10000 or len(words) < start + 1 + length:
        return None
    out = []
    for word in words[start + 1:start + 1 + length]:
        if word[:24] != "0" * 24:
            return None
        out.append("0x" + word[24:])
    return out


def word_count(hexdata):
    return len(_words(hexdata))


def has_selector(code, signature):
    """Cheap bytecode heuristic: PUSH4 <selector> appears in the runtime code."""
    return ("63" + selector(signature)[2:]) in code.lower()


# --------------------------------------------------------------------------
# JSON-RPC client
# --------------------------------------------------------------------------


class RpcError(Exception):
    pass


class RpcClient:
    """Batching JSON-RPC client with retries and bounded concurrency."""

    def __init__(self, url, batch_size=50, workers=6, timeout=180, max_attempts=12, min_interval=0.0):
        self.url = url
        self.batch_size = batch_size
        self.workers = workers
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.requests_sent = 0
        self.items_sent = 0

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
        for attempt in range(self.max_attempts):
            self._throttle()
            try:
                request = urllib.request.Request(
                    self.url, data=encoded, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                with self._lock:
                    self.requests_sent += 1
                    self.items_sent += len(payload) if isinstance(payload, list) else 1
                return json.loads(body)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as error:
                last_error = error
                time.sleep(min(60, 2.0 * (attempt + 1)))
        raise RpcError(f"rpc transport failed after {self.max_attempts} attempts: {last_error}")

    def call(self, method, params):
        result = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if isinstance(result, dict) and result.get("error"):
            raise RpcError(json.dumps(result["error"]))
        return result["result"]

    def call_soft(self, method, params):
        """Like call() but returns (result, error) instead of raising on RPC-level errors."""
        result = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if isinstance(result, dict) and result.get("error"):
            return None, result["error"]
        return result["result"], None

    def _batch_chunk(self, chunk):
        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(chunk)
        ]
        result = self._post(payload)
        if isinstance(result, dict):
            # whole-batch error; surface it for every item
            error = result.get("error", {"message": "unexpected non-list batch response"})
            return [(None, error) for _ in chunk]
        by_id = {item.get("id"): item for item in result}
        out = []
        for index in range(len(chunk)):
            item = by_id.get(index)
            if item is None:
                out.append((None, {"message": "missing batch item"}))
            elif item.get("error"):
                out.append((None, item["error"]))
            else:
                out.append((item.get("result"), None))
        return out

    def batch(self, calls):
        """Execute many (method, params) calls. Returns list of (result, error) in order."""
        if not calls:
            return []
        chunks = [calls[i:i + self.batch_size] for i in range(0, len(calls), self.batch_size)]
        results = [None] * len(chunks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._batch_chunk, chunk): idx for idx, chunk in enumerate(chunks)}
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()
        flat = []
        for part in results:
            flat.extend(part)
        return flat


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def atto_to_one_str(value):
    value = int(value)
    negative = value < 0
    value = abs(value)
    whole, frac = divmod(value, 10 ** 18)
    text = f"{whole}.{frac:018d}"
    return "-" + text if negative else text


def one_str_to_atto(value):
    text = str(value or "0").strip()
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    whole, separator, fraction = text.partition(".")
    if not whole or not whole.isdigit():
        raise ValueError(f"invalid ONE amount: {value!r}")
    if separator and (not fraction or not fraction.isdigit()):
        raise ValueError(f"invalid ONE amount: {value!r}")
    if len(fraction) > 18:
        raise ValueError(f"ONE amount exceeds 18 decimals: {value!r}")
    atto = int(whole) * 10 ** 18
    atto += int(fraction.ljust(18, "0") or "0")
    return -atto if negative else atto


def hex_to_int(value, default=0):
    if value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return default


def load_json(path, default=None):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def dump_json(path, data):
    tmp = path + ".partial"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    import os

    os.replace(tmp, path)
