#!/usr/bin/env python3
"""Shared helpers for the airdrop tools. Standard library only.

Everything here is deliberately small enough to read in one sitting:

- keccak-256 (pure Python; uses pycryptodome automatically when it is installed, for speed);
- EIP-55 address checksums and address validation;
- the exact ABI encoding the contract hashes, `abi.encode(uint256, address[], uint256[])`;
- the batch tree (leaf, root, proof, verify) that must match `script/lib/BatchTree.sol` and
  `src/CommittedBatchAirdrop.sol`;
- reading recipient lists from a CSV file or a directory of CSV files;
- reading/writing a run directory (manifest.json, distribution.csv, batches/*.json);
- a minimal JSON-RPC client for read-only calls;
- reading `.env`.
"""

from __future__ import annotations

import csv
import datetime as _dt
import functools
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------------------------
# keccak-256
# ---------------------------------------------------------------------------------------------

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


def _keccak256_pure(data: bytes) -> bytes:
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
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))[:32]


try:  # optional speed-up; the pure-Python version above is the reference
    from Crypto.Hash import keccak as _pycryptodome_keccak  # type: ignore

    def keccak256(data: bytes) -> bytes:
        h = _pycryptodome_keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()

    KECCAK_BACKEND = "pycryptodome"
except Exception:  # pragma: no cover - depends on the environment
    keccak256 = _keccak256_pure
    KECCAK_BACKEND = "pure-python"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "0x" + digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# addresses and amounts
# ---------------------------------------------------------------------------------------------

ATTO_PER_ONE = 10 ** 18
UINT96_MAX = (1 << 96) - 1


class InputError(ValueError):
    """A problem with the recipient list that a human has to fix."""


def to_checksum(address: str) -> str:
    raw = address.lower().removeprefix("0x")
    digest = keccak256(raw.encode()).hex()
    return "0x" + "".join(ch.upper() if int(digest[i], 16) >= 8 else ch for i, ch in enumerate(raw))


def normalize_address(value: str, where: str) -> str:
    """Validate an address and return its EIP-55 form. Mixed-case input must have a valid checksum."""
    text = value.strip()
    if text.startswith(("0x", "0X")):
        text = "0x" + text[2:]
    else:
        raise InputError(f"{where}: address must start with 0x: {value!r}")
    body = text[2:]
    if len(body) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in body):
        raise InputError(f"{where}: not a 20-byte hex address: {value!r}")
    if int(body, 16) == 0:
        raise InputError(f"{where}: the zero address is not allowed")
    checksummed = to_checksum(text)
    if body != body.lower() and body != body.upper() and text != checksummed:
        raise InputError(f"{where}: address checksum is wrong (typo?): {value!r}")
    return checksummed


def parse_amount(value: str, unit: str, where: str) -> int:
    """Return the amount in atto (smallest unit). `unit` is 'atto' or 'one'."""
    text = value.strip().replace(",", "").replace("_", "")
    if not text:
        raise InputError(f"{where}: empty amount")
    if unit == "atto":
        if not text.isdigit():
            raise InputError(f"{where}: atto amounts must be whole numbers: {value!r}")
        amount = int(text)
    elif unit == "one":
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise InputError(f"{where}: not a decimal number: {value!r}") from exc
        if dec.is_nan() or dec.is_infinite():
            raise InputError(f"{where}: not a finite number: {value!r}")
        scaled = dec * ATTO_PER_ONE
        if scaled != scaled.to_integral_value():
            raise InputError(f"{where}: more than 18 decimal places: {value!r}")
        amount = int(scaled)
    else:
        raise InputError(f"unknown amount unit {unit!r} (use 'atto' or 'one')")
    if amount <= 0:
        raise InputError(f"{where}: amount must be positive: {value!r}")
    return amount


def format_one(atto: int) -> str:
    """Human-readable whole-unit string with up to 18 decimals, exact."""
    whole, frac = divmod(atto, ATTO_PER_ONE)
    if frac == 0:
        return f"{whole:,}"
    return f"{whole:,}.{str(frac).rjust(18, '0').rstrip('0')}"


# ---------------------------------------------------------------------------------------------
# ABI encoding of (uint256 batchIndex, address[] recipients, uint256[] amounts)
# ---------------------------------------------------------------------------------------------


def _word(value: int) -> bytes:
    if value < 0 or value >= 1 << 256:
        raise ValueError("value does not fit in 256 bits")
    return value.to_bytes(32, "big")


def abi_encode_batch(batch_index: int, recipients: list[str], amounts: list[int]) -> bytes:
    """Byte-for-byte the same as Solidity `abi.encode(batchIndex, recipients, amounts)`."""
    if len(recipients) != len(amounts):
        raise ValueError("recipients and amounts differ in length")
    n = len(recipients)
    head = _word(batch_index) + _word(3 * 32) + _word(3 * 32 + 32 + 32 * n)
    tail = _word(n) + b"".join(_word(int(addr, 16)) for addr in recipients)
    tail += _word(n) + b"".join(_word(a) for a in amounts)
    return head + tail


def batch_leaf(batch_index: int, recipients: list[str], amounts: list[int]) -> bytes:
    """keccak256(keccak256(abi.encode(...))): identical to CommittedBatchAirdrop.batchLeaf."""
    return keccak256(keccak256(abi_encode_batch(batch_index, recipients, amounts)))


# ---------------------------------------------------------------------------------------------
# batch tree
# ---------------------------------------------------------------------------------------------


def hash_pair(a: bytes, b: bytes) -> bytes:
    return keccak256(a + b) if a < b else keccak256(b + a)


def tree_levels(leaves: list[bytes]) -> list[list[bytes]]:
    """All levels, leaves first, root last. An unpaired last node is carried up unchanged."""
    if not leaves:
        raise ValueError("no leaves")
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        prev = levels[-1]
        nxt = []
        for i in range(0, len(prev), 2):
            nxt.append(hash_pair(prev[i], prev[i + 1]) if i + 1 < len(prev) else prev[i])
        levels.append(nxt)
    return levels


def tree_root(levels: list[list[bytes]]) -> bytes:
    return levels[-1][0]


def tree_proof(levels: list[list[bytes]], index: int) -> list[bytes]:
    proof = []
    idx = index
    for level in levels[:-1]:
        sibling = idx ^ 1
        if sibling < len(level):
            proof.append(level[sibling])
        idx >>= 1
    return proof


def tree_verify(proof: list[bytes], root: bytes, leaf: bytes) -> bool:
    h = leaf
    for p in proof:
        h = hash_pair(h, p)
    return h == root


# ---------------------------------------------------------------------------------------------
# recipient lists
# ---------------------------------------------------------------------------------------------

ADDRESS_COLUMNS = ("address", "destination_address", "recipient", "recipient_address", "to", "wallet")
ATTO_COLUMNS = ("amount_atto", "amount_wei", "atto", "wei", "amount_smallest_unit")
ONE_COLUMNS = ("amount_one", "amount_ether", "one", "ether")
GENERIC_AMOUNT_COLUMNS = ("amount", "value")


@dataclass
class Row:
    address: str  # EIP-55
    amount: int  # atto
    source: str  # "file:line" for error messages


def csv_sources(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
        if not files:
            raise InputError(f"no .csv files in directory {path}")
        return files
    if not path.is_file():
        raise InputError(f"input not found: {path}")
    return [path]


def _pick_column(header: list[str], wanted: str | None, candidates: tuple[str, ...], what: str, where: str) -> str:
    lowered = {h.strip().lower(): h for h in header}
    if wanted:
        if wanted.lower() not in lowered:
            raise InputError(f"{where}: column {wanted!r} not found; columns are {header}")
        return lowered[wanted.lower()]
    hits = [lowered[c] for c in candidates if c in lowered]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise InputError(f"{where}: could not find the {what} column; columns are {header}. Pass --{what}-column.")
    raise InputError(f"{where}: several possible {what} columns {hits}; pass --{what}-column.")


def detect_amount_unit(column: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    name = column.strip().lower()
    if name in ATTO_COLUMNS or name.endswith("_atto") or name.endswith("_wei"):
        return "atto"
    if name in ONE_COLUMNS or name.endswith("_one"):
        return "one"
    raise InputError(
        f"cannot tell whether column {column!r} is in atto (smallest unit) or ONE; pass --amount-unit atto|one"
    )


def read_rows(
    input_path: Path,
    address_column: str | None = None,
    amount_column: str | None = None,
    amount_unit: str | None = None,
) -> tuple[list[Row], dict]:
    """Read every CSV under `input_path`. Returns the rows in file order and a description of what was read."""
    rows: list[Row] = []
    sources = []
    resolved_unit = None
    resolved_columns = None
    for path in csv_sources(input_path):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                raise InputError(f"{path}: empty file") from None
            addr_col = _pick_column(header, address_column, ADDRESS_COLUMNS, "address", str(path))
            amt_col = _pick_column(header, amount_column, ATTO_COLUMNS + ONE_COLUMNS + GENERIC_AMOUNT_COLUMNS, "amount", str(path))
            unit = detect_amount_unit(amt_col, amount_unit)
            if resolved_unit and unit != resolved_unit:
                raise InputError(f"{path}: amount unit {unit!r} differs from earlier files ({resolved_unit!r})")
            resolved_unit = unit
            resolved_columns = (addr_col, amt_col)
            ai, mi = header.index(addr_col), header.index(amt_col)
            count = 0
            for line_no, record in enumerate(reader, start=2):
                if not record or all(not cell.strip() for cell in record):
                    continue
                where = f"{path}:{line_no}"
                if len(record) <= max(ai, mi):
                    raise InputError(f"{where}: too few columns")
                rows.append(Row(normalize_address(record[ai], where), parse_amount(record[mi], unit, where), where))
                count += 1
        sources.append({"path": str(path), "sha256": sha256_file(path), "rows": count})
    if not rows:
        raise InputError("the input contains no recipients")
    info = {
        "sources": sources,
        "address_column": resolved_columns[0],
        "amount_column": resolved_columns[1],
        "amount_unit": resolved_unit,
    }
    return rows, info


def consolidate(rows: list[Row], order: str, merge_duplicates: bool) -> list[Row]:
    """Reject or merge duplicate addresses, then order the list."""
    by_address: dict[str, Row] = {}
    duplicates: list[str] = []
    for row in rows:
        key = row.address.lower()
        if key in by_address:
            if merge_duplicates:
                prev = by_address[key]
                by_address[key] = Row(prev.address, prev.amount + row.amount, f"{prev.source}+{row.source}")
            else:
                duplicates.append(f"{row.address} ({by_address[key].source} and {row.source})")
        else:
            by_address[key] = row
    if duplicates:
        shown = "\n  ".join(duplicates[:20])
        more = "" if len(duplicates) <= 20 else f"\n  ... and {len(duplicates) - 20} more"
        raise InputError(
            f"{len(duplicates)} duplicate address(es). Fix the input or pass --merge-duplicates to add them up:\n  {shown}{more}"
        )
    out = list(by_address.values())
    if order == "address":
        out.sort(key=lambda r: r.address.lower())
    elif order != "input":
        raise InputError(f"unknown order {order!r} (use 'address' or 'input')")
    return out


# ---------------------------------------------------------------------------------------------
# run directories
# ---------------------------------------------------------------------------------------------

FORMAT = "committed-batch-airdrop/v1"
LEAF_SCHEME = "keccak256(keccak256(abi.encode(uint256 batchIndex, address[] recipients, uint256[] amounts)))"
TREE_SCHEME = "binary tree over batch leaves in index order; pairs hashed keccak256(min || max); unpaired node carried up"
# One recipient costs about 27,700 gas. 500 recipients is about 13.9M gas, which leaves room for the
# executor's gas-estimate margin under the 16,777,216 per-transaction cap (EIP-7825).
MAX_BATCH_SIZE = 500
DEFAULT_BATCH_SIZE = 400


def hex0x(data: bytes) -> str:
    return "0x" + data.hex()


def unhex(text: str) -> bytes:
    return bytes.fromhex(text.removeprefix("0x"))


def batch_file(run_dir: Path, index: int) -> Path:
    return run_dir / "batches" / f"batch-{index:05d}.json"


def chunk(rows: list[Row], size: int) -> list[list[Row]]:
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def write_run(run_dir: Path, rows: list[Row], batch_size: int, info: dict, label: str, order: str) -> dict:
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise InputError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "batches").mkdir()

    distribution = run_dir / "distribution.csv"
    with distribution.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["address", "amount"])
        for row in rows:
            writer.writerow([row.address, str(row.amount)])
    list_sha256 = sha256_file(distribution)

    batches = chunk(rows, batch_size)
    leaves = [batch_leaf(i, [r.address for r in b], [r.amount for r in b]) for i, b in enumerate(batches)]
    levels = tree_levels(leaves)
    root = tree_root(levels)

    summaries = []
    for i, b in enumerate(batches):
        proof = tree_proof(levels, i)
        payload = {
            "batch_index": i,
            "leaf": hex0x(leaves[i]),
            "proof": [hex0x(p) for p in proof],
            "recipients": [r.address for r in b],
            "amounts": [str(r.amount) for r in b],
        }
        batch_file(run_dir, i).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        summaries.append({"index": i, "leaf": hex0x(leaves[i]), "recipients": len(b), "amount": str(sum(r.amount for r in b))})

    total = sum(r.amount for r in rows)
    manifest = {
        "format": FORMAT,
        "created_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": label,
        "keccak_backend": KECCAK_BACKEND,
        "sources": info["sources"],
        "address_column": info["address_column"],
        "amount_column": info["amount_column"],
        "amount_unit": info["amount_unit"],
        "order": order,
        "batch_size": batch_size,
        "batch_count": len(batches),
        "recipient_count": len(rows),
        "total_amount": str(total),
        "total_amount_display": format_one(total),
        "root": hex0x(root),
        "list_sha256": list_sha256,
        "leaf_scheme": LEAF_SCHEME,
        "tree_scheme": TREE_SCHEME,
        "batches": summaries,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def read_manifest(run_dir: Path) -> dict:
    path = run_dir / "manifest.json"
    if not path.is_file():
        raise InputError(f"{path} not found; is {run_dir} a run directory?")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise InputError(f"{path}: unexpected format {manifest.get('format')!r}")
    return manifest


def read_distribution(run_dir: Path) -> list[Row]:
    path = run_dir / "distribution.csv"
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        if header != ["address", "amount"]:
            raise InputError(f"{path}: unexpected header {header}")
        for line_no, record in enumerate(reader, start=2):
            rows.append(Row(record[0], int(record[1]), f"{path}:{line_no}"))
    return rows


def read_batch(run_dir: Path, index: int) -> dict:
    path = batch_file(run_dir, index)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("batch_index") != index:
        raise InputError(f"{path}: batch_index is {payload.get('batch_index')}, expected {index}")
    return payload


# ---------------------------------------------------------------------------------------------
# .env and JSON-RPC
# ---------------------------------------------------------------------------------------------


def load_env(env_path: Path) -> dict:
    """Read KEY=VALUE lines. Real environment variables win over the file."""
    values: dict[str, str] = {}
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    for key, value in os.environ.items():
        values.setdefault(key, value)
    return values


class Rpc:
    """Minimal JSON-RPC client for read-only calls, with batching and retries."""

    def __init__(self, url: str, timeout: int = 60, retries: int = 3):
        if not url:
            raise InputError("RPC_URL is not set")
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self._id = 0

    def _post(self, payload):
        data = json.dumps(payload).encode()
        # Some public endpoints reject urllib's default User-Agent (Cloudflare error 1010).
        headers = {"Content-Type": "application/json", "User-Agent": "harmony-airdrop-tools/1"}
        request = urllib.request.Request(self.url, data=data, headers=headers)
        last_error = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode())
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"RPC request failed after {self.retries} attempts: {last_error}")

    def call(self, method: str, params: list):
        self._id += 1
        result = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if "error" in result:
            raise RuntimeError(f"RPC error for {method}: {result['error']}")
        return result["result"]

    def batch(self, calls: list[tuple[str, list]]) -> list:
        if not calls:
            return []
        payload = []
        for method, params in calls:
            self._id += 1
            payload.append({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        results = self._post(payload)
        if isinstance(results, dict):
            raise RuntimeError(f"RPC batch error: {results.get('error')}")
        by_id = {item["id"]: item for item in results}
        out = []
        for item in payload:
            entry = by_id.get(item["id"])
            if entry is None or "error" in entry:
                raise RuntimeError(f"RPC batch item failed: {entry}")
            out.append(entry["result"])
        return out

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        return self.call("eth_call", [{"to": to, "data": data}, block])


@functools.lru_cache(maxsize=None)
def selector(signature: str) -> bytes:
    return keccak256(signature.encode())[:4]


def encode_call(signature: str, *args: int | str) -> str:
    """ABI-encode a call whose arguments are all single 32-byte words (uint256 or address)."""
    data = selector(signature)
    for arg in args:
        if isinstance(arg, str):
            data += _word(int(arg, 16))
        else:
            data += _word(arg)
    return hex0x(data)


def decode_word(result: str) -> int:
    body = result.removeprefix("0x")
    if len(body) < 64:
        raise RuntimeError(f"unexpected eth_call result: {result!r}")
    return int(body[:64], 16)


def decode_address(result: str) -> str:
    return to_checksum("0x" + f"{decode_word(result):040x}")


def decode_bytes32(result: str) -> str:
    return "0x" + f"{decode_word(result):064x}"


# ---------------------------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------------------------


def die(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def default_env_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".env"
