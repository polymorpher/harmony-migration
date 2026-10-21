#!/usr/bin/env python3
"""Shared helpers for the transaction-archive export that replaces the Harmony explorer.

Standard library only. Provides a Harmony JSON-RPC client, the CSV schemas of
every pipeline stage, transaction/trace classification, cross-shard receipt
attribution, and atomic output helpers.

A "run" is one shard and one branch (canonical or rolled_back) over one block
range, exported from one RPC endpoint into its own directory.
"""

import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contract-review"))
import contract_review_lib as crl  # noqa: E402

# Calldata columns can exceed the csv module's default 128 KiB field limit.
csv.field_size_limit(2 ** 31 - 1)

ONE = 10 ** 18
EMPTY_ROOT = "0x56e81f171bcc55a6ff8345e692c0f86e5b48e01b996cadc001622fb5e363b421"
XSHARD_PRECOMPILE = "0x00000000000000000000000000000000000000f9"
BRANCHES = ("canonical", "rolled_back")
SCHEMA_VERSION = 1
# Mainnet cx-receipt-state-rollback epoch: from this epoch a reverted ancestor frame
# also discards the cross-shard receipt created by a precompile call below it.
RECEIPT_ROLLBACK_FIX_EPOCH = 2964
COLLECT_REWARDS_TOPIC = "0x" + crl.keccak256(b"Harmony/CollectRewards").hex()

TX_FIELDS = [
    "kind", "block_number", "block_hash", "timestamp", "tx_index", "tx_hash", "eth_hash",
    "from", "to", "value_atto", "shard_id", "to_shard_id", "status", "gas_used", "gas_price",
    "input_selector", "input_bytes",
]
STAKING_FIELDS = [
    "type", "block_number", "block_hash", "timestamp", "tx_index", "tx_hash", "from",
    "validator", "amount_atto", "status", "gas_used", "gas_price",
]
TRACE_BLOCK_FIELDS = ["block_number", "tx_count", "call_tx_count", "reason", "block_hash"]
XSHARD_BLOCK_FIELDS = ["block_number", "block_hash", "timestamp", "epoch", "incoming", "outgoing"]
SEGMENT_FIELDS = ["start", "end", "first_parent_hash", "first_hash", "last_hash"]
INTERNAL_FIELDS = [
    "block_number", "block_hash", "timestamp", "tx_index", "tx_hash", "eth_hash",
    "trace_address", "type", "from", "to", "value_atto", "effective",
]
BLOCK_STATS_FIELDS = ["block_number", "frames", "internal_value_gt0", "internal_ge_min",
                      "internal_ge_min_effective"]
RECEIPT_FIELDS = [
    "from_shard", "to_shard", "mechanism", "block_number", "block_hash", "timestamp",
    "tx_index", "tx_hash", "depth", "ancestor_reverted", "from", "to", "value_atto", "eth_hash",
]
DESTINATION_FIELDS = RECEIPT_FIELDS + [
    "dest_status", "dest_block_number", "dest_block_hash", "dest_timestamp",
]


# --------------------------------------------------------------------------
# Values and addresses
# --------------------------------------------------------------------------


def integer(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"invalid integer {value!r}")


def address(value):
    """Return a lowercase 0x address for a one1/0x input, or "" for an empty input."""
    if value in (None, ""):
        return ""
    normalized = crl.any_to_hex(value)
    if normalized is None:
        raise ValueError(f"invalid address {value!r}")
    return normalized


def utc(timestamp):
    return (
        datetime.fromtimestamp(int(timestamp), timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_block_pin(text):
    """Parse NUMBER:HASH."""
    number, _, block_hash = text.partition(":")
    if not block_hash.startswith("0x") or len(block_hash) != 66:
        raise ValueError(f"expected NUMBER:0xHASH, got {text!r}")
    return int(number), block_hash.lower()


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def classify_tx(tx, to_hex):
    """Kind of a plain (EVM) transaction as returned by hmyv2 block RPCs."""
    if integer(tx["toShardID"]) != integer(tx["shardID"]):
        return "xshard_native"
    if to_hex == XSHARD_PRECOMPILE:
        return "xshard_precompile"
    if not to_hex:
        return "create"
    if (tx.get("input") or "0x") == "0x":
        return "transfer"
    return "call"


def decode_xshard_input(data):
    """Decode crossShardTransfer(uint256 value, address to, uint32 toShardID) calldata."""
    raw = (data or "0x")[2:]
    if len(raw) < 8 + 64 * 3:
        return None
    body = raw[8:]
    return {
        "value": int(body[0:64], 16),
        "to": "0x" + body[64 + 24:128].lower(),
        "to_shard": int(body[128:192], 16),
    }


def staking_amount(tx, receipt):
    """Amount moved by a staking transaction: the message amount, or the CollectRewards log."""
    msg = tx.get("msg") or {}
    if msg.get("amount") is not None:
        return integer(msg["amount"])
    if tx.get("type") == "CollectRewards" and receipt:
        for log in receipt.get("logs") or []:
            topics = log.get("topics") or []
            if topics and topics[0].lower() == COLLECT_REWARDS_TOPIC:
                data = log.get("data") or "0x"
                return int(data, 16) if data != "0x" else 0
    return None


def flatten_block_trace(result, block_hash=""):
    """Parity frames from debug_traceBlockByHash with the ParityBlockTracer.

    Items are per-transaction results ({"result": [frames]} or {"error": ...}) or
    bare frame lists, depending on the node version.
    """
    frames = []
    for item in result or []:
        if isinstance(item, list):
            frames += item
        elif isinstance(item, dict) and item.get("error"):
            raise RpcError(f"trace of a transaction in {block_hash} failed: {item['error']}")
        elif isinstance(item, dict) and "traceAddress" in item:
            frames.append(item)
        elif isinstance(item, dict):
            frames += item.get("result") or []
    return frames


def rlp_encode(item):
    if isinstance(item, int):
        item = item.to_bytes((item.bit_length() + 7) // 8, "big") if item else b""
    if isinstance(item, bytes):
        if len(item) == 1 and item[0] < 0x80:
            return item
        return _rlp_length(len(item), 0x80) + item
    body = b"".join(rlp_encode(part) for part in item)
    return _rlp_length(len(body), 0xC0) + body


def _rlp_length(size, offset):
    if size < 56:
        return bytes([offset + size])
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(raw)]) + raw


def signed_value_matches(tx, value):
    """Whether value, with the transaction's other signed fields, reproduces its hash.

    A plain transaction's hash is keccak256 of the RLP list (nonce, gasPrice, gas,
    shardID, toShardID, to, value, input, v, r, s); its Ethereum-style hash omits the
    two shard fields. A match proves the value is the one that was signed.
    """
    to = bytes.fromhex(address(tx.get("to"))[2:]) if tx.get("to") else b""
    data = bytes.fromhex((tx.get("input") or "0x")[2:])
    v, r, s = (integer(tx[key]) for key in ("v", "r", "s"))
    head = [integer(tx["nonce"]), integer(tx["gasPrice"]), integer(tx["gas"])]
    harmony = head + [integer(tx["shardID"]), integer(tx["toShardID"]), to, value, data, v, r, s]
    if "0x" + crl.keccak256(rlp_encode(harmony)).hex() == tx["hash"].lower():
        return True
    eth_hash = (tx.get("ethHash") or "").lower()
    ethereum = head + [to, value, data, v, r, s]
    return bool(eth_hash) and "0x" + crl.keccak256(rlp_encode(ethereum)).hex() == eth_hash


def frame_transfer(frame):
    """(type, from, to, value) for a Parity-style trace frame, or None if it cannot move value."""
    action = frame.get("action") or {}
    kind = frame.get("type")
    if kind == "call":
        call_type = action.get("callType", "call")
        if call_type in ("delegatecall", "staticcall"):
            return call_type, action.get("from"), action.get("to"), 0
        return call_type, action.get("from"), action.get("to"), int(action.get("value", "0x0"), 16)
    if kind == "create":
        return ("create", action.get("from"), (frame.get("result") or {}).get("address"),
                int(action.get("value", "0x0"), 16))
    if kind == "suicide":
        return ("selfdestruct", action.get("address"), action.get("refundAddress"),
                int(action.get("balance", "0x0"), 16))
    return None


def reverted_paths(frames):
    """Map transaction position -> set of trace addresses whose frame reverted."""
    errored = {}
    for frame in frames:
        if frame.get("error"):
            errored.setdefault(frame["transactionPosition"], set()).add(tuple(frame["traceAddress"]))
    return errored


def has_reverted_ancestor(path, errored, include_self=True):
    limit = len(path) + 1 if include_self else len(path)
    return any(path[:k] in errored for k in range(limit))


def internal_transfers(frames):
    """Value-moving non-root frames of a trace_block result.

    Returns (rows, counters). Each row has tx_index, eth_hash, trace_address, type,
    from, to, value_atto and effective (no reverted ancestor, including itself).
    """
    errored = reverted_paths(frames)
    rows = []
    counters = {"frames": len(frames), "internal_value_gt0": 0}
    for frame in frames:
        path = tuple(frame["traceAddress"])
        if not path:
            continue
        moved = frame_transfer(frame)
        if moved is None or moved[3] == 0:
            continue
        counters["internal_value_gt0"] += 1
        kind, sender, recipient, value = moved
        position = frame["transactionPosition"]
        rows.append({
            "tx_index": position,
            "eth_hash": frame.get("transactionHash", ""),
            "trace_address": ".".join(str(item) for item in path),
            "type": kind,
            "from": (sender or "").lower(),
            "to": (recipient or "").lower(),
            "value_atto": value,
            "effective": 0 if has_reverted_ancestor(path, errored.get(position, ())) else 1,
        })
    return rows, counters


def outgoing_receipts(block, frames, shard, rollback_fix_epoch=RECEIPT_ROLLBACK_FIX_EPOCH):
    """Attribute the outgoing cross-shard receipts of one block; see _attribute_receipts.

    Each row also carries the source transaction's Ethereum-style hash: a
    precompile receipt is stored under tx.HashByType(), which is that hash for a
    transaction sent from an Ethereum wallet.
    """
    rows, fallback = _attribute_receipts(block, frames, shard, rollback_fix_epoch)
    txs = block.get("transactions") or []
    for row in rows:
        index = int(row["tx_index"])
        row["eth_hash"] = txs[index].get("ethHash", "") if index < len(txs) else ""
    return rows, fallback


def _attribute_receipts(block, frames, shard, rollback_fix_epoch):
    """Attribute the outgoing cross-shard receipts of one block.

    block is an hmyv2 block with full transactions and an "epoch" field; frames is
    its trace_block result, or None when no trace is available. Returns
    (rows, used_fallback). A native cross-shard transaction is read from the block;
    a precompile receipt comes from a completed call to 0x..f9. Before the rollback
    fix epoch a reverted ancestor does not cancel the receipt. When the header
    proves a receipt but no completed call is found, attempted calls are returned
    as mechanism precompile_replay_incompatible (the current binary replays some
    historical DELEGATECALL precompile calls differently).
    """
    txs = block.get("transactions") or []
    epoch = integer(block.get("epoch", 0))
    base = {
        "from_shard": shard,
        "block_number": integer(block["number"]),
        "block_hash": block["hash"],
        "timestamp": integer(block["timestamp"]),
    }
    rows = []
    for tx in txs:
        if integer(tx["toShardID"]) != integer(tx["shardID"]):
            rows.append({**base, "to_shard": integer(tx["toShardID"]), "mechanism": "native",
                         "tx_index": integer(tx["transactionIndex"]), "tx_hash": tx["hash"],
                         "depth": 0, "ancestor_reverted": 0, "from": address(tx["from"]),
                         "to": address(tx["to"]), "value_atto": integer(tx["value"])})
    if frames is None:
        for tx in txs:
            if address(tx.get("to")) == XSHARD_PRECOMPILE:
                decoded = decode_xshard_input(tx.get("input"))
                if decoded:
                    rows.append({**base, "to_shard": decoded["to_shard"], "mechanism": "precompile",
                                 "tx_index": integer(tx["transactionIndex"]), "tx_hash": tx["hash"],
                                 "depth": 0, "ancestor_reverted": 0, "from": address(tx["from"]),
                                 "to": decoded["to"], "value_atto": decoded["value"]})
        return rows, False

    errored = reverted_paths(frames)

    def precompile_rows(include_failed, mechanism):
        out = []
        for frame in frames:
            action = frame.get("action") or {}
            if frame.get("type") != "call" or (action.get("to") or "").lower() != XSHARD_PRECOMPILE:
                continue
            if frame.get("error") and not include_failed:
                continue
            decoded = decode_xshard_input(action.get("input"))
            if decoded is None:
                continue
            path = tuple(frame["traceAddress"])
            position = frame["transactionPosition"]
            ancestor = has_reverted_ancestor(path, errored.get(position, ()), include_self=False)
            if ancestor and epoch >= rollback_fix_epoch and not include_failed:
                continue
            call_type = action.get("callType", "call")
            label = mechanism if call_type == "call" else f"{mechanism}_{call_type}"
            out.append({**base, "to_shard": decoded["to_shard"], "mechanism": label,
                        "tx_index": position,
                        "tx_hash": txs[position]["hash"] if position < len(txs) else "",
                        "depth": len(path), "ancestor_reverted": int(ancestor),
                        "from": (action.get("from") or "").lower(), "to": decoded["to"],
                        "value_atto": decoded["value"]})
        return out

    rows += precompile_rows(False, "precompile")
    if rows:
        return rows, False
    fallback = precompile_rows(True, "precompile_replay_incompatible")
    return fallback, bool(fallback)


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------


class RpcError(Exception):
    pass


def redact_url(url):
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname or ''}"


class Rpc:
    """Harmony JSON-RPC client with retries and JSON-RPC batching."""

    def __init__(self, url, timeout=600, attempts=6):
        self.url = url
        self.timeout = timeout
        self.attempts = attempts

    @property
    def host(self):
        return redact_url(self.url)

    def _post(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        last = None
        for attempt in range(self.attempts):
            try:
                request = urllib.request.Request(
                    self.url, data=body,
                    headers={"Content-Type": "application/json",
                             "User-Agent": "harmony-migration-tx-archive/1"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
                last = error
                time.sleep(min(30, 2 ** attempt))
        raise RpcError(f"{self.host}: transport failed after {self.attempts} attempts: {last}")

    def call(self, method, params):
        result = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(result, dict) or result.get("error") or "result" not in result:
            raise RpcError(f"{self.host} {method}: {json.dumps(result)[:300]}")
        return result["result"]

    def batch(self, calls, size=500):
        """Run (method, params) calls in JSON-RPC batches; any error raises."""
        out = []
        for start in range(0, len(calls), size):
            chunk = calls[start:start + size]
            payload = [{"jsonrpc": "2.0", "id": index, "method": method, "params": params}
                       for index, (method, params) in enumerate(chunk)]
            for attempt in range(self.attempts):
                result = self._post(payload)
                if isinstance(result, list) and len(result) == len(chunk):
                    break
                time.sleep(min(30, 2 ** attempt))
            else:
                raise RpcError(f"{self.host}: malformed batch response")
            by_id = {item.get("id"): item for item in result}
            for index, (method, _) in enumerate(chunk):
                item = by_id.get(index) or {}
                if item.get("error") or "result" not in item:
                    raise RpcError(f"{self.host} {method}: {json.dumps(item)[:300]}")
                out.append(item["result"])
        return out

    def head(self):
        return integer(self.call("hmyv2_blockNumber", []))

    def block(self, number, full=False):
        value = self.call("hmyv2_getBlockByNumber",
                          [number, {"fullTx": full, "inclTx": True, "inclStaking": full}])
        if value is None:
            raise RpcError(f"{self.host}: block {number} not found")
        return value

    def shard(self):
        return integer(self.call("hmyv2_getNodeMetadata", [])["shard-id"])


def first_block_at_or_after(rpc, timestamp, low, high):
    """Lowest block in [low, high] whose timestamp is >= timestamp."""
    while low < high:
        middle = (low + high) // 2
        if integer(rpc.block(middle)["timestamp"]) >= timestamp:
            high = middle
        else:
            low = middle + 1
    return low


# --------------------------------------------------------------------------
# Runs and files
# --------------------------------------------------------------------------


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def write_json(path, value):
    path = str(path)
    partial = path + ".partial"
    with open(partial, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)
    return file_sha256(path)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def iter_csv(path):
    with open(path, newline="", encoding="utf-8") as source:
        yield from csv.DictReader(source)


class AppendCsv:
    """CSV that a resumable stage appends to; the header is written once."""

    def __init__(self, path, fields):
        self.fields = fields
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.handle = open(path, "a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.handle, fieldnames=fields, lineterminator="\n",
                                     extrasaction="ignore")
        if new:
            self.writer.writeheader()

    def write(self, rows):
        self.writer.writerows(rows)

    def flush(self):
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def close(self):
        self.flush()
        self.handle.close()


def write_csv(path, fields, rows):
    path = str(path)
    partial = path + ".partial"
    count = 0
    with open(partial, "w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)
    return count, file_sha256(path)


def open_stage(directory, identity, resume):
    """Create or resume a stage directory whose stage.json must match identity."""
    directory = Path(directory)
    marker = directory / "stage.json"
    if marker.exists():
        existing = load_json(marker)
        if not resume:
            raise FileExistsError(f"{directory} exists; pass --resume to continue it")
        if existing.get("identity") != identity:
            raise ValueError(f"{directory} was started with different arguments:\n"
                             f"{json.dumps(existing.get('identity'), sort_keys=True)}")
        if existing.get("complete"):
            raise ValueError(f"{directory} is already complete")
        return existing
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError(f"{directory} is not empty")
    directory.mkdir(parents=True, exist_ok=True)
    state = {"identity": identity, "complete": False, "started_utc": utc(time.time())}
    write_json(marker, state)
    return state


def finish_stage(directory, state, extra):
    state = {**state, **extra, "complete": True, "finished_utc": utc(time.time())}
    write_json(Path(directory) / "stage.json", state)
    return state


CALLDATA_FIELDS = ["tx_hash", "input"]


def write_calldata_stage(out, identity, transactions_sha256, rows, counters):
    """<run>/calldata: calldata.csv (tx_hash, input) and a stage.json tied to the run's transactions.csv."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    count, digest = write_csv(out / "calldata.csv", CALLDATA_FIELDS, rows)
    write_json(out / "stage.json", {
        "identity": {"stage": "fetch-calldata", "scan": identity, "transactions_sha256": transactions_sha256},
        "complete": True, "finished_utc": utc(time.time()),
        "counters": dict(sorted(counters.items())),
        "files": {"calldata.csv": {"rows": count, "sha256": digest, "bytes": (out / "calldata.csv").stat().st_size}},
    })
    return count


def read_done(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as source:
        return {int(line) for line in source if line.strip()}


def output_files(directory, names):
    """sha256 and row counts of a stage's CSV outputs."""
    files = {}
    for name in names:
        path = Path(directory) / name
        if not path.exists():
            continue
        with open(path, newline="", encoding="utf-8") as source:
            rows = sum(1 for _ in source) - 1
        files[name] = {"sha256": file_sha256(path), "rows": max(rows, 0), "bytes": path.stat().st_size}
    return files
