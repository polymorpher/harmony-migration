#!/usr/bin/env python3
"""Pay recipients directly from the reserve Safe, one Safe transaction per batch.

No airdrop contract is involved. Each Safe transaction is a batch of plain ERC-20
`transfer(recipient, amount)` calls sent by the Safe itself (through Safe's official
MultiSendCallOnly contract), and the Safe's signers approve every batch.

    # prepare the Safe transactions for a list (version, chain id and next nonce read from the Safe)
    python3 tools/safe_batch.py build --input selected.csv --out-dir runs/safe-batch-1 \
        --safe 0xSafe --token 0xToken --rpc-url URL [--nonce N]
    # or fully offline
    python3 tools/safe_batch.py build ... --chain-id 1 --nonce 42 --safe-version 1.4.1

    # anyone can recompute every file and hash from the list; add --rpc-url to check the chain too
    python3 tools/safe_batch.py verify --out-dir runs/safe-batch-1 [--rpc-url URL] [--balances]

    # hashes (and a decoded summary) of any Safe transaction, for example one already in the queue
    python3 tools/safe_batch.py hash --safe 0xSafe --chain-id 1 --nonce 42 --to 0x... --data-file data.hex --operation 1

For every Safe transaction `build` writes a file to import into the Safe web app's Transaction
Builder, the recipients of that transaction, and the exact transaction the Safe will be asked to
sign (target, call data, operation, nonce) with the three hashes a signer checks: the domain hash
and message hash that a hardware wallet displays while signing, and the Safe transaction hash that
the Safe web app displays. SIGNING-SHEET.md lists them all.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

FORMAT = "safe-direct-transfer/v1"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
CALL, DELEGATECALL = 0, 1

# Official MultiSendCallOnly deployments (github.com/safe-global/safe-deployments). The Safe web app
# uses the first listed address for the Safe's version when it bundles several calls into one.
MULTISEND_CALL_ONLY = {
    "1.3.0": ["0x40A2aCCbd92BCA938b02010E17A5b8929b49130D", "0xA1dabEF33b3B82c7814B6D82A79e50F4AC44102B"],
    "1.4.1": ["0x9641d764fc13c8B624c04430C7356C1C7C8102e2"],
    "1.5.0": ["0xA83c336B20401Af773B6219BA5027174338D1836"],
}
MULTISEND_CALL_ONLY_CODE_HASH = {
    "1.3.0": "0xa9865ac2d9c7a1591619b188c4d88167b50df6cc0c5327fcbd1c8c75f7c066ad",
    "1.4.1": "0xecd5bd14a08c5d2122379900b2f272bdf107a7e92423c10dd5fe3254386c9939",
    "1.5.0": "0xcdbdcec38d2f1c7d961b0029ff8416b7e86e9974d6f0e9c9580c7d17fcfb6663",
}

# EIP-712 type hashes used by Safe 1.3.0 and later.
DOMAIN_TYPEHASH = common.keccak256(b"EIP712Domain(uint256 chainId,address verifyingContract)")
SAFE_TX_TYPEHASH = common.keccak256(
    b"SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,"
    b"uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)"
)
assert DOMAIN_TYPEHASH.hex() == "47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218"
assert SAFE_TX_TYPEHASH.hex() == "bb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8"

TRANSFER_SELECTOR = common.selector("transfer(address,uint256)")
MULTISEND_SELECTOR = common.selector("multiSend(bytes)")

# One transfer to a fresh address costs roughly 30,000 gas inside a Safe batch. 300 transfers is
# about 9 million gas; 450 stays well below the 16,777,216 per-transaction cap (EIP-7825) even after
# the wallet adds its estimate margin.
DEFAULT_MAX_TRANSFERS = 300
MAX_TRANSFERS = 450

TX_BUILDER_VERSION = "1.19.0"
TRANSFER_METHOD = {
    "inputs": [
        {"internalType": "address", "name": "to", "type": "address"},
        {"internalType": "uint256", "name": "value", "type": "uint256"},
    ],
    "name": "transfer",
    "payable": False,
}


def word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def address_word(address: str) -> bytes:
    return word(int(address, 16))


# ---------------------------------------------------------------------------------------------
# call data
# ---------------------------------------------------------------------------------------------


def transfer_data(recipient: str, amount: int) -> bytes:
    return TRANSFER_SELECTOR + address_word(recipient) + word(amount)


def pack_call(to: str, data: bytes) -> bytes:
    """One entry of MultiSend's packed list: operation (1 byte), to (20), value (32), length (32), data."""
    return bytes([CALL]) + common.unhex(to) + word(0) + word(len(data)) + data


def multisend_data(token: str, rows: list[common.Row]) -> bytes:
    packed = b"".join(pack_call(token, transfer_data(r.address, r.amount)) for r in rows)
    padding = b"\x00" * (-len(packed) % 32)
    return MULTISEND_SELECTOR + word(32) + word(len(packed)) + packed + padding


@dataclass
class SafeTx:
    to: str
    value: int
    data: bytes
    operation: int
    nonce: int
    safe_tx_gas: int = 0
    base_gas: int = 0
    gas_price: int = 0
    gas_token: str = ZERO_ADDRESS
    refund_receiver: str = ZERO_ADDRESS


def transfers_tx(token: str, multisend: str, rows: list[common.Row], nonce: int) -> SafeTx:
    """The transaction the Safe web app creates from a Transaction Builder batch of these transfers.

    A batch of one is sent as a direct call to the token; two or more are bundled with
    MultiSendCallOnly and executed as a delegate call from the Safe.
    """
    if len(rows) == 1:
        return SafeTx(token, 0, transfer_data(rows[0].address, rows[0].amount), CALL, nonce)
    return SafeTx(multisend, 0, multisend_data(token, rows), DELEGATECALL, nonce)


# ---------------------------------------------------------------------------------------------
# Safe transaction hashes (EIP-712)
# ---------------------------------------------------------------------------------------------


def domain_hash(chain_id: int, safe: str) -> bytes:
    return common.keccak256(DOMAIN_TYPEHASH + word(chain_id) + address_word(safe))


def message_hash(tx: SafeTx) -> bytes:
    return common.keccak256(
        SAFE_TX_TYPEHASH + address_word(tx.to) + word(tx.value) + common.keccak256(tx.data) + word(tx.operation)
        + word(tx.safe_tx_gas) + word(tx.base_gas) + word(tx.gas_price) + address_word(tx.gas_token)
        + address_word(tx.refund_receiver) + word(tx.nonce)
    )


def safe_tx_hash(chain_id: int, safe: str, tx: SafeTx) -> bytes:
    return common.keccak256(b"\x19\x01" + domain_hash(chain_id, safe) + message_hash(tx))


def tx_record(chain_id: int, safe: str, tx: SafeTx) -> dict:
    return {
        "safe": safe,
        "chainId": chain_id,
        "to": tx.to,
        "value": str(tx.value),
        "data": common.hex0x(tx.data),
        "operation": tx.operation,
        "safeTxGas": str(tx.safe_tx_gas),
        "baseGas": str(tx.base_gas),
        "gasPrice": str(tx.gas_price),
        "gasToken": tx.gas_token,
        "refundReceiver": tx.refund_receiver,
        "nonce": tx.nonce,
        "dataKeccak256": common.hex0x(common.keccak256(tx.data)),
        "domainHash": common.hex0x(domain_hash(chain_id, safe)),
        "messageHash": common.hex0x(message_hash(tx)),
        "safeTxHash": common.hex0x(safe_tx_hash(chain_id, safe, tx)),
    }


# ---------------------------------------------------------------------------------------------
# Transaction Builder file (the Safe web app's batch format)
# ---------------------------------------------------------------------------------------------


def _tx_builder_serialize(obj) -> str:
    """Same serialization the Transaction Builder uses for its checksum (keys sorted at every level)."""
    if isinstance(obj, list):
        return "[" + ",".join(_tx_builder_serialize(x) for x in obj) + "]"
    if isinstance(obj, dict):
        keys = sorted(obj.keys())
        out = "{" + json.dumps(keys, separators=(",", ":"), ensure_ascii=False)
        for key in keys:
            out += _tx_builder_serialize(obj[key]) + ","
        return out + "}"
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def tx_builder_checksum(batch: dict) -> str:
    """keccak256 of the serialized batch with meta.name blanked and meta.checksum removed."""
    copy = json.loads(json.dumps(batch))
    copy["meta"].pop("checksum", None)
    copy["meta"]["name"] = None
    return common.hex0x(common.keccak256(_tx_builder_serialize(copy).encode("utf-8")))


def tx_builder_transactions(token: str, rows: list[common.Row]) -> list[dict]:
    return [
        {"to": token, "value": "0", "data": None, "contractMethod": TRANSFER_METHOD,
         "contractInputsValues": {"to": r.address, "value": str(r.amount)}}
        for r in rows
    ]


def tx_builder_file(chain_id: int, safe: str, token: str, rows: list[common.Row], name: str, description: str,
                    created_at_ms: int) -> dict:
    batch = {
        "version": "1.0",
        "chainId": str(chain_id),
        "createdAt": created_at_ms,
        "meta": {
            "name": name,
            "description": description,
            "txBuilderVersion": TX_BUILDER_VERSION,
            "createdFromSafeAddress": safe,
            "createdFromOwnerAddress": "",
        },
        "transactions": tx_builder_transactions(token, rows),
    }
    batch["meta"]["checksum"] = tx_builder_checksum(batch)
    return batch


# ---------------------------------------------------------------------------------------------
# decoding (for `hash`, so a signer can check call data copied from the Safe web app)
# ---------------------------------------------------------------------------------------------


def decode_multisend(data: bytes) -> list[tuple[int, str, int, bytes]] | None:
    if data[:4] != MULTISEND_SELECTOR or len(data) < 68:
        return None
    offset = int.from_bytes(data[4:36], "big")
    start = 4 + offset
    length = int.from_bytes(data[start:start + 32], "big")
    packed = data[start + 32:start + 32 + length]
    if len(packed) != length:
        raise common.InputError("multiSend data is shorter than its declared length")
    calls = []
    i = 0
    while i < len(packed):
        if i + 85 > len(packed):
            raise common.InputError("multiSend data ends in the middle of an entry")
        operation = packed[i]
        to = common.to_checksum("0x" + packed[i + 1:i + 21].hex())
        value = int.from_bytes(packed[i + 21:i + 53], "big")
        size = int.from_bytes(packed[i + 53:i + 85], "big")
        body = packed[i + 85:i + 85 + size]
        if len(body) != size:
            raise common.InputError("multiSend entry is shorter than its declared length")
        calls.append((operation, to, value, body))
        i += 85 + size
    return calls


def decode_transfer(body: bytes) -> tuple[str, int] | None:
    if len(body) != 68 or body[:4] != TRANSFER_SELECTOR or any(body[4:16]):
        return None
    return common.to_checksum("0x" + body[16:36].hex()), int.from_bytes(body[36:68], "big")


def describe_calls(to: str, data: bytes, operation: int) -> tuple[list[str], list[tuple[str, str, int]]]:
    """Human summary lines plus (token, recipient, amount) for every ERC-20 transfer found."""
    lines: list[str] = []
    transfers: list[tuple[str, str, int]] = []
    calls = decode_multisend(data) if operation == DELEGATECALL else None
    if calls is None:
        single = decode_transfer(data)
        if single and operation == CALL:
            transfers.append((common.to_checksum(to), single[0], single[1]))
            lines.append(f"one ERC-20 transfer on token {common.to_checksum(to)}")
        else:
            lines.append("call data is not a MultiSend batch or a single ERC-20 transfer; not decoded")
        return lines, transfers
    other = 0
    for op, target, value, body in calls:
        decoded = decode_transfer(body)
        if op == CALL and value == 0 and decoded:
            transfers.append((target, decoded[0], decoded[1]))
        else:
            other += 1
    lines.append(f"MultiSend batch with {len(calls)} call(s): {len(transfers)} ERC-20 transfer(s), {other} other call(s)")
    tokens = sorted({t for t, _, _ in transfers})
    for token in tokens:
        amounts = [a for t, _, a in transfers if t == token]
        lines.append(f"  token {token}: {len(amounts)} transfer(s), total {sum(amounts)} ({common.format_one(sum(amounts))})")
    recipients = [r.lower() for _, r, _ in transfers]
    if len(set(recipients)) != len(recipients):
        lines.append("  WARNING: some recipient appears more than once")
    if other:
        lines.append("  WARNING: the batch contains calls that are not plain ERC-20 transfers")
    return lines, transfers


# ---------------------------------------------------------------------------------------------
# splitting a list into Safe transactions
# ---------------------------------------------------------------------------------------------


def split_sizes(count: int, max_transfers: int, first: int | None) -> list[int]:
    """Sizes of the Safe transactions: an optional small first one, then near-equal ones <= max."""
    sizes: list[int] = []
    left = count
    if first:
        sizes.append(min(first, left))
        left -= sizes[0]
    if left:
        parts = -(-left // max_transfers)
        base, extra = divmod(left, parts)
        sizes += [base + 1] * extra + [base] * (parts - extra)
    return sizes


def slices(rows: list[common.Row], sizes: list[int]) -> list[list[common.Row]]:
    out, start = [], 0
    for size in sizes:
        out.append(rows[start:start + size])
        start += size
    return out


def tx_dir(out_dir: Path, index: int, nonce: int) -> Path:
    return out_dir / f"tx-{index:02d}-nonce-{nonce}"


def write_recipients(path: Path, rows: list[common.Row]) -> str:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["address", "amount_atto", "amount_tokens"])
        for row in rows:
            writer.writerow([row.address, str(row.amount), common.format_one(row.amount).replace(",", "")])
    return common.sha256_file(path)


def read_recipients(path: Path) -> list[common.Row]:
    rows, _ = common.read_rows(path, "address", "amount_atto", "atto")
    return rows


def pairs(rows: list[common.Row]) -> list[tuple[str, int]]:
    return [(r.address, r.amount) for r in rows]


def multisend_for(version: str, override: str | None) -> str:
    if override:
        return common.normalize_address(override, "--multisend")
    return MULTISEND_CALL_ONLY[version][0]


def build_transactions(chain_id: int, safe: str, token: str, multisend: str, rows: list[common.Row],
                       sizes: list[int], first_nonce: int) -> list[tuple[list[common.Row], SafeTx]]:
    return [(chunk, transfers_tx(token, multisend, chunk, first_nonce + i))
            for i, chunk in enumerate(slices(rows, sizes))]


def signing_sheet(manifest: dict) -> str:
    lines = [
        f"# Signing sheet: {manifest['label'] or 'direct Safe payment'}",
        "",
        "Every value below is recomputed by `python3 tools/safe_batch.py verify --out-dir <this directory>`.",
        "Before signing, compare the Safe web app and the hardware wallet screen with this sheet.",
        "",
        f"- Chain id: `{manifest['chain_id']}`",
        f"- Safe: `{manifest['safe']}` (version {manifest['safe_version']})",
        f"- Token: `{manifest['token']}`",
        f"- MultiSendCallOnly: `{manifest['multisend_call_only']}`",
        f"- Recipients: {manifest['recipient_count']}; total {manifest['total_amount']} "
        f"(smallest unit) = {manifest['total_amount_display']} tokens",
        f"- recipients.csv SHA-256: `{manifest['recipients_sha256']}`",
        f"- Domain hash (same for every transaction of this Safe on this chain): `{manifest['domain_hash']}`",
        "",
    ]
    for tx in manifest["transactions"]:
        kind = "delegate call to MultiSendCallOnly" if tx["operation"] == DELEGATECALL else "direct call to the token"
        lines += [
            f"## Safe transaction {tx['index']} of {len(manifest['transactions'])}: nonce {tx['nonce']}",
            "",
            f"- Folder: `{tx['folder']}` (import `transaction-builder.json` into the Transaction Builder)",
            f"- Recipients: {tx['recipients']} (list positions {tx['first_position']} to "
            f"{tx['first_position'] + tx['recipients'] - 1}); total {tx['amount']} = {tx['amount_display']} tokens",
            f"- To: `{tx['to']}` ({kind}); value 0; operation {tx['operation']}",
            "- safeTxGas 0, baseGas 0, gasPrice 0, gasToken and refundReceiver the zero address",
            f"- Call data: {tx['data_bytes']} bytes, keccak-256 `{tx['data_keccak256']}`",
            f"- Domain hash: `{manifest['domain_hash']}`",
            f"- Message hash: `{tx['message_hash']}`",
            f"- Safe transaction hash: `{tx['safe_tx_hash']}`",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------------------------


def resolve_safe(args: argparse.Namespace) -> dict:
    """Safe, token, chain id, Safe version and nonce: from flags, else .env, else the Safe contract."""
    env = common.load_env(args.env)
    safe_text = args.safe or env.get("RESERVE_ADDRESS", "")
    token_text = args.token or env.get("TOKEN_ADDRESS", "")
    if not safe_text or not token_text:
        raise common.InputError("need --safe and --token (or RESERVE_ADDRESS and TOKEN_ADDRESS in .env)")
    safe = common.normalize_address(safe_text, "--safe")
    token = common.normalize_address(token_text, "--token")
    sources = {"safe_version": "argument", "nonce": "argument", "chain_id": "argument"}
    chain_id, version, nonce = args.chain_id, args.safe_version, args.nonce
    rpc_url = args.rpc_url or env.get("RPC_URL", "")
    if version is None or nonce is None or chain_id is None:
        if not rpc_url:
            raise common.InputError(
                "need --rpc-url (or RPC_URL in .env) to read the Safe's version, nonce and chain id, "
                "or pass --safe-version, --nonce and --chain-id to build offline"
            )
        rpc = common.Rpc(rpc_url)
        rpc_chain = rpc.chain_id()
        if chain_id is not None and chain_id != rpc_chain:
            raise common.InputError(f"--chain-id {chain_id} but the RPC is on chain {rpc_chain}")
        if rpc.call("eth_getCode", [safe, "latest"]) in ("0x", "0x0"):
            raise common.InputError(f"no contract at {safe} on chain {rpc_chain}; is this the Safe address?")
        chain_version = decode_string(rpc_call(rpc, safe, "VERSION()"))
        chain_nonce = common.decode_word(rpc_call(rpc, safe, "nonce()"))
        if version is not None and version != chain_version:
            raise common.InputError(f"--safe-version {version} but the Safe reports {chain_version}")
        if nonce is not None and nonce < chain_nonce:
            raise common.InputError(f"--nonce {nonce} is already used; the Safe's next nonce is {chain_nonce}")
        if chain_id is None:
            chain_id, sources["chain_id"] = rpc_chain, "chain"
        if version is None:
            version, sources["safe_version"] = chain_version, "chain"
        if nonce is None:
            nonce, sources["nonce"] = chain_nonce, "chain"
    if version not in MULTISEND_CALL_ONLY:
        raise common.InputError(
            f"Safe version {version} is not one this tool knows ({', '.join(sorted(MULTISEND_CALL_ONLY))})"
        )
    if nonce < 0 or chain_id <= 0:
        raise common.InputError("--nonce must be >= 0 and --chain-id > 0")
    return {"safe": safe, "token": token, "chain_id": chain_id, "safe_version": version, "nonce": nonce,
            "sources": sources}


def cmd_build(args: argparse.Namespace) -> None:
    try:
        resolved = resolve_safe(args)
        safe, token = resolved["safe"], resolved["token"]
        args.chain_id, args.safe_version, args.nonce = resolved["chain_id"], resolved["safe_version"], resolved["nonce"]
        multisend = multisend_for(args.safe_version, args.multisend)
        if not 1 <= args.max_transfers <= MAX_TRANSFERS:
            raise common.InputError(f"--max-transfers must be between 1 and {MAX_TRANSFERS}")
        if args.first_transfers is not None and not 1 <= args.first_transfers <= args.max_transfers:
            raise common.InputError("--first-transfers must be between 1 and --max-transfers")
        rows, info = common.read_rows(args.input, args.address_column, args.amount_column, args.amount_unit)
        raw_count = len(rows)
        rows = common.consolidate(rows, args.order, args.merge_duplicates)
        for row in rows:
            if row.address.lower() in (safe.lower(), token.lower()):
                raise common.InputError(f"{row.source}: the Safe or the token itself cannot be a recipient")
        total = sum(r.amount for r in rows)
        if args.expect_total is not None and total != common.parse_amount(args.expect_total, "one", "--expect-total"):
            raise common.InputError(f"list total {common.format_one(total)} differs from --expect-total {args.expect_total}")
        if args.expect_count is not None and len(rows) != args.expect_count:
            raise common.InputError(f"list has {len(rows)} recipients, --expect-count says {args.expect_count}")
        if args.out_dir.exists():
            if not args.force:
                raise common.InputError(f"{args.out_dir} already exists; pass --force to replace it")
            shutil.rmtree(args.out_dir)
    except common.InputError as exc:
        common.die(str(exc))
        return

    now = _dt.datetime.now(_dt.timezone.utc)
    created_ms = int(now.timestamp() * 1000)
    sizes = split_sizes(len(rows), args.max_transfers, args.first_transfers)
    txs = build_transactions(args.chain_id, safe, token, multisend, rows, sizes, args.nonce)

    args.out_dir.mkdir(parents=True)
    recipients_sha = write_recipients(args.out_dir / "recipients.csv", rows)
    dom = common.hex0x(domain_hash(args.chain_id, safe))
    entries = []
    position = 1
    for i, (chunk, tx) in enumerate(txs, start=1):
        folder = tx_dir(args.out_dir, i, tx.nonce)
        folder.mkdir()
        record = tx_record(args.chain_id, safe, tx)
        amount = sum(r.amount for r in chunk)
        name = f"{args.label or 'Direct payment'} - Safe tx {i} of {len(txs)} - nonce {tx.nonce}"
        description = (f"{len(chunk)} ERC-20 transfers from {safe} on token {token}; "
                       f"total {amount}; expected safeTxHash {record['safeTxHash']}")
        builder = tx_builder_file(args.chain_id, safe, token, chunk, name, description, created_ms)
        (folder / "transaction-builder.json").write_text(json.dumps(builder, indent=1) + "\n", encoding="utf-8")
        (folder / "safe-transaction.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        chunk_sha = write_recipients(folder / "recipients.csv", chunk)
        entries.append({
            "index": i,
            "nonce": tx.nonce,
            "folder": folder.name,
            "first_position": position,
            "recipients": len(chunk),
            "amount": str(amount),
            "amount_display": common.format_one(amount),
            "recipients_sha256": chunk_sha,
            "to": tx.to,
            "operation": tx.operation,
            "data_bytes": len(tx.data),
            "data_keccak256": record["dataKeccak256"],
            "message_hash": record["messageHash"],
            "safe_tx_hash": record["safeTxHash"],
            "tx_builder_checksum": builder["meta"]["checksum"],
        })
        position += len(chunk)

    manifest = {
        "format": FORMAT,
        "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": args.label,
        "keccak_backend": common.KECCAK_BACKEND,
        "chain_id": args.chain_id,
        "safe": safe,
        "safe_version": args.safe_version,
        "token": token,
        "multisend_call_only": multisend,
        "multisend_is_official": multisend in MULTISEND_CALL_ONLY[args.safe_version],
        "sources": info["sources"],
        "address_column": info["address_column"],
        "amount_column": info["amount_column"],
        "amount_unit": info["amount_unit"],
        "order": args.order,
        "max_transfers": args.max_transfers,
        "first_transfers": args.first_transfers,
        "first_nonce": args.nonce,
        "parameter_sources": resolved["sources"],
        "recipient_count": len(rows),
        "total_amount": str(total),
        "total_amount_display": common.format_one(total),
        "recipients_sha256": recipients_sha,
        "domain_hash": dom,
        "transactions": entries,
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    (args.out_dir / "SIGNING-SHEET.md").write_text(signing_sheet(manifest) + "\n", encoding="utf-8")

    print(f"input            : {len(info['sources'])} file(s), {raw_count} rows, {len(rows)} recipients")
    print(f"columns          : address={info['address_column']!r} amount={info['amount_column']!r} unit={info['amount_unit']}")
    print(f"total            : {total} ({common.format_one(total)} tokens)")
    src = resolved["sources"]
    print(f"safe / version   : {safe} / {args.safe_version} ({src['safe_version']}) on chain {args.chain_id} ({src['chain_id']})")
    print(f"first nonce      : {args.nonce} ({src['nonce']})")
    if src["nonce"] == "chain":
        print("note: the nonce is the Safe's next executed nonce. If other transactions are already queued in the "
              "Safe web app, rebuild with --nonce set to the next free nonce there.", file=sys.stderr)
    print(f"token            : {token}")
    print(f"multisend        : {multisend}{'' if manifest['multisend_is_official'] else '  (NOT an official address for this version)'}")
    print(f"domain hash      : {dom}")
    print(f"recipients.csv   : sha256 {recipients_sha}")
    print(f"Safe transactions: {len(entries)}")
    for e in entries:
        print(f"  #{e['index']:<3} nonce {e['nonce']:<6} {e['recipients']:>4} transfers  {e['amount_display']:>32}  "
              f"safeTxHash {e['safe_tx_hash']}")
    print(f"written to       : {args.out_dir} (see SIGNING-SHEET.md)")


# ---------------------------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------------------------


class Checks:
    def __init__(self):
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def verify_offline(out_dir: Path, checks: Checks) -> tuple[dict, list[common.Row]]:
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise common.InputError(f"{out_dir}/manifest.json: unexpected format {manifest.get('format')!r}")
    chain_id, safe, token = manifest["chain_id"], manifest["safe"], manifest["token"]
    multisend = manifest["multisend_call_only"]
    rows = read_recipients(out_dir / "recipients.csv")
    checks.ok(common.sha256_file(out_dir / "recipients.csv") == manifest["recipients_sha256"], "recipients.csv SHA-256 differs from manifest")
    checks.ok(len({r.address.lower() for r in rows}) == len(rows), "recipients.csv has a duplicate address")
    checks.ok(len(rows) == manifest["recipient_count"], "recipient count differs from manifest")
    checks.ok(str(sum(r.amount for r in rows)) == manifest["total_amount"], "total differs from manifest")
    checks.ok(common.hex0x(domain_hash(chain_id, safe)) == manifest["domain_hash"], "domain hash differs from manifest")
    if multisend not in MULTISEND_CALL_ONLY.get(manifest["safe_version"], []):
        checks.warn(f"MultiSendCallOnly {multisend} is not an official address for Safe {manifest['safe_version']}")

    sizes = [t["recipients"] for t in manifest["transactions"]]
    checks.ok(sizes == split_sizes(len(rows), manifest["max_transfers"], manifest["first_transfers"]),
              "transaction sizes differ from the recorded split rule")
    checks.ok(sum(sizes) == len(rows), "transactions do not cover the list exactly")
    txs = build_transactions(chain_id, safe, token, multisend, rows, sizes, manifest["first_nonce"])
    for entry, (chunk, tx) in zip(manifest["transactions"], txs):
        label = f"transaction {entry['index']} (nonce {entry['nonce']})"
        folder = out_dir / entry["folder"]
        record = tx_record(chain_id, safe, tx)
        checks.ok(entry["nonce"] == tx.nonce and entry["to"] == tx.to and entry["operation"] == tx.operation,
                  f"{label}: nonce/to/operation differ")
        checks.ok(entry["amount"] == str(sum(r.amount for r in chunk)), f"{label}: amount differs")
        checks.ok(entry["data_keccak256"] == record["dataKeccak256"], f"{label}: call data hash differs")
        checks.ok(entry["message_hash"] == record["messageHash"], f"{label}: message hash differs")
        checks.ok(entry["safe_tx_hash"] == record["safeTxHash"], f"{label}: Safe transaction hash differs")
        stored = json.loads((folder / "safe-transaction.json").read_text(encoding="utf-8"))
        checks.ok(stored == record, f"{label}: safe-transaction.json differs from the recomputed transaction")
        checks.ok(pairs(read_recipients(folder / "recipients.csv")) == pairs(chunk),
                  f"{label}: recipients.csv differs from its slice of the list")
        checks.ok(common.sha256_file(folder / "recipients.csv") == entry["recipients_sha256"], f"{label}: recipients.csv SHA-256 differs")
        builder = json.loads((folder / "transaction-builder.json").read_text(encoding="utf-8"))
        checks.ok(builder.get("chainId") == str(chain_id), f"{label}: Transaction Builder file is for another chain")
        checks.ok(builder.get("meta", {}).get("createdFromSafeAddress") == safe, f"{label}: Transaction Builder file names another Safe")
        checks.ok(builder.get("transactions") == tx_builder_transactions(token, chunk),
                  f"{label}: Transaction Builder calls differ from the list")
        checks.ok(builder.get("meta", {}).get("checksum") == tx_builder_checksum(builder),
                  f"{label}: Transaction Builder checksum does not match its content")
    return manifest, rows


def rpc_call(rpc: common.Rpc, to: str, signature: str, *args) -> str:
    return rpc.eth_call(to, common.encode_call(signature, *args))


def decode_string(result: str) -> str:
    raw = common.unhex(result)
    offset = int.from_bytes(raw[:32], "big")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    return raw[offset + 32:offset + 32 + length].decode("utf-8", "replace")


def decode_address_array(result: str) -> list[str]:
    raw = common.unhex(result)
    offset = int.from_bytes(raw[:32], "big")
    count = int.from_bytes(raw[offset:offset + 32], "big")
    return [common.to_checksum("0x" + raw[offset + 32 + 32 * i + 12:offset + 64 + 32 * i].hex()) for i in range(count)]


def verify_chain(manifest: dict, rows: list[common.Row], rpc: common.Rpc, checks: Checks, balances: bool) -> None:
    chain_id = rpc.chain_id()
    checks.ok(chain_id == manifest["chain_id"], f"RPC chain id {chain_id} differs from manifest {manifest['chain_id']}")
    safe, token, multisend = manifest["safe"], manifest["token"], manifest["multisend_call_only"]
    print(f"block            : {rpc.block_number()}")

    if rpc.call("eth_getCode", [safe, "latest"]) in ("0x", "0x0"):
        checks.ok(False, f"no contract at the Safe address {safe}")
        return
    version = decode_string(rpc_call(rpc, safe, "VERSION()"))
    nonce = common.decode_word(rpc_call(rpc, safe, "nonce()"))
    threshold = common.decode_word(rpc_call(rpc, safe, "getThreshold()"))
    owners = decode_address_array(rpc_call(rpc, safe, "getOwners()"))
    print(f"safe             : version {version}, nonce {nonce}, threshold {threshold} of {len(owners)} owners")
    for owner in owners:
        print(f"  owner          : {owner}")
    checks.ok(version == manifest["safe_version"], f"Safe reports version {version}, manifest says {manifest['safe_version']}")
    first = manifest["first_nonce"]
    last = first + len(manifest["transactions"]) - 1
    if nonce > last:
        checks.warn(f"Safe nonce {nonce} is past every transaction here (nonces {first}-{last}): all were executed or replaced")
    elif nonce > first:
        checks.warn(f"Safe nonce {nonce}: nonces {first}-{nonce - 1} were already used (executed or replaced)")
    elif nonce < first:
        checks.warn(f"Safe nonce is {nonce}; nonces {nonce}-{first - 1} must be executed before this batch can run")

    code = rpc.call("eth_getCode", [multisend, "latest"])
    code_hash = common.hex0x(common.keccak256(common.unhex(code)))
    expected = MULTISEND_CALL_ONLY_CODE_HASH.get(manifest["safe_version"])
    checks.ok(code_hash == expected, f"code at MultiSendCallOnly {multisend} has hash {code_hash}, official is {expected}")

    decimals = common.decode_word(rpc_call(rpc, token, "decimals()"))
    symbol = decode_string(rpc_call(rpc, token, "symbol()"))
    balance = common.decode_word(rpc_call(rpc, token, "balanceOf(address)", safe))
    pending = sum(int(t["amount"]) for t in manifest["transactions"] if t["nonce"] >= nonce)
    print(f"token            : {symbol}, {decimals} decimals; Safe balance {common.format_one(balance)}")
    print(f"still to send    : {common.format_one(pending)} (transactions with nonce >= {nonce})")
    checks.ok(decimals == 18, f"token has {decimals} decimals; amounts in this tool assume 18")
    checks.ok(balance >= pending, "the Safe holds less than the transactions still to send")

    with_code, delegated = [], []
    for start in range(0, len(rows), 100):
        part = rows[start:start + 100]
        codes = rpc.batch([("eth_getCode", [r.address, "latest"]) for r in part])
        for row, code in zip(part, codes):
            if code not in ("0x", "0x0"):
                (delegated if code.lower().startswith("0xef0100") else with_code).append(row.address)
    print(f"recipients       : {len(rows)} checked for code: {len(with_code)} contract(s), "
          f"{len(delegated)} EIP-7702 delegated account(s)")
    for address in with_code:
        checks.warn(f"recipient {address} is a contract on this chain; confirm its owners can use the tokens")

    if balances:
        holding = 0
        for start in range(0, len(rows), 100):
            part = rows[start:start + 100]
            results = rpc.batch([("eth_call", [{"to": token, "data": common.encode_call("balanceOf(address)", r.address)}, "latest"])
                                 for r in part])
            holding += sum(1 for row, res in zip(part, results) if common.decode_word(res) >= row.amount)
        print(f"balances         : {holding} of {len(rows)} recipients hold at least their amount")


def cmd_verify(args: argparse.Namespace) -> None:
    checks = Checks()
    try:
        manifest, rows = verify_offline(args.out_dir, checks)
        print(f"run              : {args.out_dir} ({manifest['label'] or 'no label'})")
        print(f"recipients       : {len(rows)}, total {common.format_one(sum(r.amount for r in rows))}")
        print(f"Safe transactions: {len(manifest['transactions'])}, nonces {manifest['first_nonce']}-"
              f"{manifest['first_nonce'] + len(manifest['transactions']) - 1}")
        env = common.load_env(args.env)
        rpc_url = args.rpc_url
        if not rpc_url and not args.offline and env.get("RPC_URL") and env.get("CHAIN_ID") == str(manifest["chain_id"]):
            rpc_url = env["RPC_URL"]
        if rpc_url:
            verify_chain(manifest, rows, common.Rpc(rpc_url), checks, args.balances)
        else:
            print("chain checks     : skipped (pass --rpc-url, or set RPC_URL and a matching CHAIN_ID in .env)")
    except common.InputError as exc:
        common.die(str(exc))
        return
    for message in checks.warnings:
        print(f"warning: {message}")
    for message in checks.failures:
        print(f"FAIL: {message}")
    if checks.failures:
        common.die(f"{len(checks.failures)} check(s) failed")
    for entry in manifest["transactions"]:
        print(f"  #{entry['index']:<3} nonce {entry['nonce']:<6} safeTxHash {entry['safe_tx_hash']}")
    print("OK: every file and hash matches the recipient list")


# ---------------------------------------------------------------------------------------------
# hash
# ---------------------------------------------------------------------------------------------


def cmd_hash(args: argparse.Namespace) -> None:
    try:
        if args.safe_transaction:
            record = json.loads(Path(args.safe_transaction).read_text(encoding="utf-8"))
            safe = common.normalize_address(record["safe"], "safe")
            chain_id = int(record["chainId"])
            tx = SafeTx(common.normalize_address(record["to"], "to"), int(record["value"]), common.unhex(record["data"]),
                        int(record["operation"]), int(record["nonce"]), int(record["safeTxGas"]), int(record["baseGas"]),
                        int(record["gasPrice"]), record["gasToken"], record["refundReceiver"])
        else:
            if not (args.safe and args.chain_id and args.nonce is not None and args.to):
                raise common.InputError("need --safe, --chain-id, --nonce and --to (or --safe-transaction FILE)")
            safe = common.normalize_address(args.safe, "--safe")
            chain_id = args.chain_id
            if args.data_file:
                text = Path(args.data_file).read_text(encoding="utf-8").strip()
            else:
                text = args.data or "0x"
            data = common.unhex("".join(text.split()))
            to = common.normalize_address(args.to, "--to")
            gas_token = ZERO_ADDRESS if int(args.gas_token, 16) == 0 else common.normalize_address(args.gas_token, "--gas-token")
            refund = ZERO_ADDRESS if int(args.refund_receiver, 16) == 0 else common.normalize_address(args.refund_receiver, "--refund-receiver")
            tx = SafeTx(to, args.value, data, args.operation, args.nonce, args.safe_tx_gas, args.base_gas,
                        args.gas_price, gas_token, refund)
        lines, transfers = describe_calls(tx.to, tx.data, tx.operation)
    except (common.InputError, ValueError, KeyError) as exc:
        common.die(str(exc))
        return
    record = tx_record(chain_id, safe, tx)
    print(f"safe             : {safe} on chain {chain_id}, nonce {tx.nonce}")
    print(f"to / operation   : {tx.to} / {tx.operation} ({'delegate call' if tx.operation == DELEGATECALL else 'call'})")
    print(f"call data        : {len(tx.data)} bytes, keccak-256 {record['dataKeccak256']}")
    for line in lines:
        print(f"decoded          : {line}")
    print(f"domain hash      : {record['domainHash']}")
    print(f"message hash     : {record['messageHash']}")
    print(f"safeTxHash       : {record['safeTxHash']}")
    if args.list_out and transfers:
        with Path(args.list_out).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["token", "address", "amount_atto"])
            for token, recipient, amount in transfers:
                writer.writerow([token, recipient, str(amount)])
        print(f"decoded list     : {args.list_out}")


# ---------------------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    b = sub.add_parser("build", help="prepare the Safe transactions for a recipient list")
    b.add_argument("--input", required=True, type=Path, help="CSV file or directory of CSV files")
    b.add_argument("--out-dir", required=True, type=Path, help="output directory, e.g. runs/safe-batch-1")
    b.add_argument("--safe", help="address of the Safe that holds the tokens (default: RESERVE_ADDRESS from .env)")
    b.add_argument("--token", help="address of the ERC-20 token (default: TOKEN_ADDRESS from .env)")
    b.add_argument("--rpc-url", help="read the chain id, Safe version and nonce from the chain (default: RPC_URL from .env)")
    b.add_argument("--chain-id", type=int, help="1 for Ethereum mainnet, 11155111 for Sepolia (default: from the RPC)")
    b.add_argument("--nonce", type=int,
                   help="Safe nonce of the first transaction, then +1 for each (default: the Safe's next nonce "
                        "on-chain; set it if other transactions are already queued in the Safe web app)")
    b.add_argument("--safe-version", choices=sorted(MULTISEND_CALL_ONLY), help="default: read from the Safe")
    b.add_argument("--env", type=Path, default=common.default_env_path())
    b.add_argument("--multisend", help="MultiSendCallOnly address, if not the official one for --safe-version")
    b.add_argument("--max-transfers", type=int, default=DEFAULT_MAX_TRANSFERS,
                   help=f"transfers per Safe transaction (default {DEFAULT_MAX_TRANSFERS}, max {MAX_TRANSFERS})")
    b.add_argument("--first-transfers", type=int, help="make the first Safe transaction this small (a pilot)")
    b.add_argument("--label", default="", help="optional name shown in the Transaction Builder and the signing sheet")
    b.add_argument("--order", choices=["input", "address"], default="input",
                   help="'input' keeps the file order (default); 'address' sorts by address")
    b.add_argument("--address-column", help="name of the address column (auto-detected by default)")
    b.add_argument("--amount-column", help="name of the amount column (auto-detected by default)")
    b.add_argument("--amount-unit", choices=["atto", "one"], help="unit of the amount column")
    b.add_argument("--merge-duplicates", action="store_true", help="add up repeated addresses instead of failing")
    b.add_argument("--expect-total", help="stop unless the list total equals this many whole tokens")
    b.add_argument("--expect-count", type=int, help="stop unless the list has exactly this many recipients")
    b.add_argument("--force", action="store_true", help="replace an existing output directory")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="recompute every file and hash from recipients.csv; optionally check the chain")
    v.add_argument("--out-dir", required=True, type=Path)
    v.add_argument("--rpc-url", help="check the Safe, token, MultiSendCallOnly and recipients on-chain (default: RPC_URL from .env)")
    v.add_argument("--offline", action="store_true", help="skip chain checks even if RPC_URL is set")
    v.add_argument("--balances", action="store_true", help="also count recipients whose balance covers their amount")
    v.add_argument("--env", type=Path, default=common.default_env_path())
    v.set_defaults(func=cmd_verify)

    h = sub.add_parser("hash", help="hashes and a decoded summary of any Safe transaction")
    h.add_argument("--safe-transaction", help="a safe-transaction.json written by build")
    h.add_argument("--safe")
    h.add_argument("--chain-id", type=int)
    h.add_argument("--nonce", type=int)
    h.add_argument("--to")
    h.add_argument("--value", type=int, default=0)
    h.add_argument("--data", help="call data as hex")
    h.add_argument("--data-file", help="file containing the call data as hex (for long data copied from the Safe web app)")
    h.add_argument("--operation", type=int, choices=[CALL, DELEGATECALL], default=CALL, help="0 = call, 1 = delegate call")
    h.add_argument("--safe-tx-gas", type=int, default=0)
    h.add_argument("--base-gas", type=int, default=0)
    h.add_argument("--gas-price", type=int, default=0)
    h.add_argument("--gas-token", default=ZERO_ADDRESS)
    h.add_argument("--refund-receiver", default=ZERO_ADDRESS)
    h.add_argument("--list-out", help="write the decoded ERC-20 transfers to this CSV")
    h.set_defaults(func=cmd_hash)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
