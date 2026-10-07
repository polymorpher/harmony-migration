#!/usr/bin/env python3
"""Deploy GovernorDelegator validator vaults and fund delegator principal from the reserve Safe.

One vault per validator. The Safe deploys each vault through the standard CREATE2 deployer and
then funds it with `approve(vault, exact sum)` followed by `depositBatch(amounts, delegators)`.
Everything runs through Safe's official MultiSendCallOnly, like `safe-batch`.

    # prepare the Safe transactions from a deposit list (validator, delegator, amount)
    python3 tools/vault_batch.py build --input deposits.csv --out-dir runs/vaults-1 \
        --safe 0xSafe --token 0xToken --rpc-url URL [--no-nonce] [--min-vault-one 10000]

    # anyone can recompute every file and hash; --rpc-url checks Ethereum, --harmony-rpc checks
    # every deposit against the delegation recorded on Harmony at the cutoff block
    python3 tools/vault_batch.py verify --out-dir runs/vaults-1 [--rpc-url URL] [--harmony-rpc URL]

    # every vault and deposit of the transactions waiting in the Safe's queue, decoded from raw data
    python3 tools/vault_batch.py show --safe 0xSafe --token 0xToken --out queued-vaults.csv \
        [--rpc-url URL] [--harmony-rpc URL] [--compare deposits.csv]

    # hashes and decoded vault calls of one Safe transaction (call data copied from the Safe web app)
    python3 tools/vault_batch.py hash --safe 0xSafe --token 0xToken --chain-id 1 --nonce 40 \
        --to 0x... --operation 1 --data-file data.hex

    # after execution: every vault's code, governor, custody, allowance and every delegator credit
    python3 tools/vault_batch.py reconcile --out-dir runs/vaults-1 --rpc-url URL

The vault address is computed before anything is signed:
keccak256(0xff ++ deployer ++ salt ++ keccak256(creation code ++ abi.encode(token, governor)))[12:],
with the salt equal to the validator address as a 32-byte word. The creation code is the audited
build pinned in vault/GovernorDelegator.json; this tool refuses any other.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import safe_batch as sb  # noqa: E402

FORMAT = "safe-vault-deposits/v1"
ATTO = common.ATTO_PER_ONE
HERE = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = HERE / "vault" / "GovernorDelegator.json"

# The audited build (hiddenstate.xyz, 2026-10-06). A new contract version must change these on purpose.
AUDITED_SOURCE_SHA256 = "df551a62e6b2b967645f51982aad65daedef35eef92d63cf3fbf187bbf9ffbe8"
AUDITED_CREATION_KECCAK = "0x72dad98e0eda48dc4d8aae1760b3a11b664c7005dd13f6719d2209355128d4f2"
AUDITED_RUNTIME_TEMPLATE_KECCAK = "0x914d3202528eb35450bf1d9582ebb8e83363c4af2b4ea5f81621c866fa372bea"

# Arachnid's deterministic deployment proxy: data = 32-byte salt ++ init code; reverts if CREATE2 fails.
FACTORY = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
FACTORY_CODE_HASH = "0x2fa86add0aed31f33a762c9d88e807c475bd51d0f52bd0955754b2608f7e4989"

APPROVE_SELECTOR = common.selector("approve(address,uint256)")
DEPOSIT_BATCH_SELECTOR = common.selector("depositBatch(uint256[],address[])")
assert DEPOSIT_BATCH_SELECTOR.hex() == "c2fff781"

HARMONY_CUTOFF_BLOCK = 93_623_067
HARMONY_CUTOFF_HASH = "0x23572e11f6ef9afe4c27ab3102f15b99fd7277ae5f685ccaef0ae571fe7ee0b6"

# Gas model, measured on a mainnet fork through the reserve Safe (2026-10-06, fresh vaults and
# delegators) and rounded up: a 20-vault deploy transaction used 10,809,009 gas; a funding
# transaction of 3 vaults and 355 deposits used 9,339,252. Planning figures, not guarantees.
GAS_TX_CAP = 16_777_216
DEPLOY_BASE_GAS, DEPLOY_GAS = 50_000, 540_000
FUND_BASE_GAS, FUND_CALL_GAS, FUND_ROW_GAS = 60_000, 40_000, 26_000

DEFAULT_MIN_VAULT_ONE = "10000"
DEFAULT_DEPLOYS_PER_TX = 16  # about 42 KB of call data, the size range the Safe web app already handled
MAX_DEPLOYS_PER_TX = 25
DEFAULT_GAS_TARGET = 10_000_000
DEFAULT_MAX_ROWS_PER_CALL = 350

VALIDATOR_COLUMNS = ("validator_address", "validator")
# `address` and `expected_shares_atto` are the claim portal's confirmed-wallets-*-vault-shares.csv export
DELEGATOR_COLUMNS = ("delegator_address", "beneficiary_address", "delegator", "address")
AMOUNT_COLUMNS = tuple(dict.fromkeys(("amount_atto", "expected_shares_atto", "amount_one", "expected_shares_one")
                                     + common.ATTO_COLUMNS + common.ONE_COLUMNS + common.GENERIC_AMOUNT_COLUMNS))
GOVERNOR_COLUMNS = ("governor_address", "governor", "destination_address")
STATUS_COLUMNS = ("destination_status", "governor_status", "status")

# reviewer columns of `show`
DEPOSIT_COLUMNS = ["validator_one1", "validator_address", "vault_address", "delegator_one1", "delegator_address", "amount"]


def word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def aw(address: str) -> bytes:
    return word(int(address, 16))


def one_text(atto: int) -> str:
    return common.format_one(atto).replace(",", "")


# ---------------------------------------------------------------------------------------------
# the audited contract
# ---------------------------------------------------------------------------------------------


@dataclass
class Artifact:
    creation: bytes
    runtime_template: bytes
    offsets: list[int]
    info: dict

    def initcode(self, token: str, governor: str) -> bytes:
        return self.creation + aw(token) + aw(governor)

    def runtime(self, token: str) -> bytes:
        code = bytearray(self.runtime_template)
        for start in self.offsets:
            code[start:start + 32] = aw(token)
        return bytes(code)


def load_artifact(path: Path = ARTIFACT_PATH) -> Artifact:
    if not path.is_file():
        raise common.InputError(f"contract artifact not found: {path}")
    info = json.loads(path.read_text(encoding="utf-8"))
    creation = common.unhex(info["creation_template"])
    runtime = common.unhex(info["runtime_template"])
    if (info.get("source_sha256") != AUDITED_SOURCE_SHA256
            or common.hex0x(common.keccak256(creation)) != AUDITED_CREATION_KECCAK
            or common.hex0x(common.keccak256(runtime)) != AUDITED_RUNTIME_TEMPLATE_KECCAK):
        raise common.InputError(f"{path} is not the audited GovernorDelegator build; refusing to use it")
    offsets = [int(x) for x in info["asset_immutable_offsets"]]
    if any(runtime[o:o + 32] != bytes(32) for o in offsets):
        raise common.InputError(f"{path}: the asset placeholder offsets are not zero in the runtime template")
    return Artifact(creation, runtime, offsets, info)


def salt_for(validator: str) -> bytes:
    return aw(validator)


def vault_address(artifact: Artifact, token: str, validator: str, governor: str) -> str:
    digest = common.keccak256(b"\xff" + common.unhex(FACTORY) + salt_for(validator)
                              + common.keccak256(artifact.initcode(token, governor)))
    return common.to_checksum("0x" + digest[12:].hex())


# ---------------------------------------------------------------------------------------------
# call data
# ---------------------------------------------------------------------------------------------


def deploy_data(artifact: Artifact, token: str, validator: str, governor: str) -> bytes:
    return salt_for(validator) + artifact.initcode(token, governor)


def approve_data(spender: str, amount: int) -> bytes:
    return APPROVE_SELECTOR + aw(spender) + word(amount)


def deposit_batch_data(amounts: list[int], delegators: list[str]) -> bytes:
    n = len(amounts)
    head = word(0x40) + word(0x40 + 32 + 32 * n)
    return (DEPOSIT_BATCH_SELECTOR + head + word(n) + b"".join(word(a) for a in amounts)
            + word(n) + b"".join(aw(d) for d in delegators))


def multisend_calls(calls: list[tuple[str, bytes]]) -> bytes:
    packed = b"".join(sb.pack_call(to, data) for to, data in calls)
    return sb.MULTISEND_SELECTOR + word(32) + word(len(packed)) + packed + b"\x00" * (-len(packed) % 32)


def safe_tx(calls: list[tuple[str, bytes]], multisend: str, nonce: int | None) -> sb.SafeTx:
    """What the Safe web app creates from a Transaction Builder batch of these calls."""
    if len(calls) == 1:
        return sb.SafeTx(calls[0][0], 0, calls[0][1], sb.CALL, nonce)
    return sb.SafeTx(multisend, 0, multisend_calls(calls), sb.DELEGATECALL, nonce)


def tx_builder_entries(calls: list[tuple[str, bytes]]) -> list[dict]:
    return [{"to": to, "value": "0", "data": common.hex0x(data), "contractMethod": None, "contractInputsValues": None}
            for to, data in calls]


def tx_builder_file(chain_id: int, safe: str, calls: list[tuple[str, bytes]], name: str, description: str,
                    created_ms: int) -> dict:
    batch = {
        "version": "1.0",
        "chainId": str(chain_id),
        "createdAt": created_ms,
        "meta": {"name": name, "description": description, "txBuilderVersion": sb.TX_BUILDER_VERSION,
                 "createdFromSafeAddress": safe, "createdFromOwnerAddress": ""},
        "transactions": tx_builder_entries(calls),
    }
    batch["meta"]["checksum"] = sb.tx_builder_checksum(batch)
    return batch


# ---------------------------------------------------------------------------------------------
# the plan: vaults, deposits, exclusions and how they are packed into Safe transactions
# ---------------------------------------------------------------------------------------------


@dataclass
class Deposit:
    validator: str
    delegator: str
    amount: int
    source: str = ""


@dataclass
class Vault:
    validator: str
    governor: str
    address: str
    deposits: list[Deposit] = field(default_factory=list)
    deploy: bool = True  # False when it already exists on-chain with the expected code

    @property
    def total(self) -> int:
        return sum(d.amount for d in self.deposits)


@dataclass
class PlannedTx:
    kind: str  # "deploy" or "fund"
    calls: list[tuple[str, bytes]]
    vaults: list[Vault]  # deploy: vaults deployed; fund: one entry per depositBatch call
    deposits: list[list[Deposit]]  # fund: the deposits of each depositBatch call
    estimated_gas: int


def deploy_txs(artifact: Artifact, token: str, vaults: list[Vault], per_tx: int) -> list[PlannedTx]:
    todo = [v for v in vaults if v.deploy]
    out = []
    for group in sb.slices(todo, sb.split_sizes(len(todo), per_tx, None)) if todo else []:
        calls = [(FACTORY, deploy_data(artifact, token, v.validator, v.governor)) for v in group]
        out.append(PlannedTx("deploy", calls, group, [], DEPLOY_BASE_GAS + DEPLOY_GAS * len(group)))
    return out


def fund_txs(token: str, vaults: list[Vault], gas_target: int, max_rows: int) -> list[PlannedTx]:
    """First-fit decreasing over (vault, part) items; deterministic for a given plan."""
    items = []
    for v in vaults:
        parts = sb.split_sizes(len(v.deposits), max_rows, None)
        for index, chunk in enumerate(sb.slices(v.deposits, parts)):
            items.append((v, index, chunk, FUND_CALL_GAS + FUND_ROW_GAS * len(chunk)))
    budget = gas_target - FUND_BASE_GAS
    bins: list[list] = []
    for item in sorted(items, key=lambda it: (-it[3], it[0].validator.lower(), it[1])):
        for b in bins:
            if b[0] + item[3] <= budget:
                b[0] += item[3]
                b[1].append(item)
                break
        else:
            bins.append([item[3], [item]])
    out = []
    for used, members in bins:
        members.sort(key=lambda it: (it[0].validator.lower(), it[1]))
        calls = []
        for v, _, chunk, _ in members:
            calls.append((token, approve_data(v.address, sum(d.amount for d in chunk))))
            calls.append((v.address, deposit_batch_data([d.amount for d in chunk], [d.delegator for d in chunk])))
        out.append(PlannedTx("fund", calls, [m[0] for m in members], [m[2] for m in members], FUND_BASE_GAS + used))
    return out


def plan_transactions(artifact: Artifact, token: str, vaults: list[Vault], phase: str, deploys_per_tx: int,
                      gas_target: int, max_rows: int) -> list[PlannedTx]:
    txs = []
    if phase in ("all", "deploy"):
        txs += deploy_txs(artifact, token, vaults, deploys_per_tx)
    if phase in ("all", "fund"):
        txs += fund_txs(token, [v for v in vaults if v.deposits], gas_target, max_rows)
    return txs


# ---------------------------------------------------------------------------------------------
# reading the deposit list and the governors
# ---------------------------------------------------------------------------------------------


def _column(header: list[str], wanted: str | None, candidates: tuple[str, ...], what: str, where: str,
            required: bool = True) -> int | None:
    lowered = [h.strip().lower() for h in header]
    if wanted:
        if wanted.lower() not in lowered:
            raise common.InputError(f"{where}: column {wanted!r} not found; columns are {header}")
        return lowered.index(wanted.lower())
    for name in candidates:
        if name in lowered:
            return lowered.index(name)
    if required:
        raise common.InputError(f"{where}: no {what} column ({' or '.join(candidates)}); columns are {header}. "
                                f"Pass --{what}-column.")
    return None


def _zero_amount(text: str) -> bool:
    try:
        return Decimal(text.strip().replace(",", "") or "x") == 0
    except Exception:
        return False


def read_deposits(paths: list[Path], args, review_portal_exports: bool = False) -> tuple[list[Deposit], dict]:
    """Every row of every input. Each file's columns and unit are detected on their own; zero rows are
    skipped before any other check. With `review_portal_exports` (build), a claim portal export must
    hold only approved wallets' positions that are still to send (common.PortalReview)."""
    deposits, sources = [], []
    files = [f for p in paths for f in common.csv_sources(p)]
    if len({f.resolve() for f in files}) != len(files):
        raise common.InputError("the same file is given twice")
    for file in files:
        with file.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if not header:
                raise common.InputError(f"{file}: empty file")
            vi = _column(header, args.validator_column, VALIDATOR_COLUMNS, "validator", str(file))
            di = _column(header, args.delegator_column, DELEGATOR_COLUMNS, "delegator", str(file))
            if vi == di:
                raise common.InputError(f"{file}: the validator and delegator columns are the same column")
            ai = _column(header, args.amount_column, AMOUNT_COLUMNS, "amount", str(file))
            unit = common.detect_amount_unit(header[ai], args.amount_unit)
            si = _column(header, None, ("destination_status",), "status", str(file), required=False)
            review = (common.PortalReview(header, common.PORTAL_VAULT_STATUS_COLUMN, "--decision approved --vault pending")
                      if review_portal_exports else None)
            count = zeros = 0
            for line, record in enumerate(reader, start=2):
                if not record or all(not c.strip() for c in record):
                    continue
                where = f"{file}:{line}"
                if len(record) <= max(vi, di, ai):
                    raise common.InputError(f"{where}: too few columns")
                if _zero_amount(record[ai]):
                    zeros += 1
                    continue
                if si is not None and record[si].strip().lower() != "ready":
                    raise common.InputError(f"{where}: destination_status is {record[si]!r}, not ready")
                if review is not None:
                    review.check(record, where)
                deposits.append(Deposit(common.normalize_address(record[vi], where),
                                        common.normalize_address(record[di], where),
                                        common.parse_amount(record[ai], unit, where), where))
                count += 1
        source = {"path": str(file), "sha256": common.sha256_file(file), "rows": count, "zero_rows_skipped": zeros,
                  "columns": {"validator": header[vi], "delegator": header[di], "amount": header[ai]},
                  "amount_unit": unit}
        if review is not None and review.columns:
            source["portal_review_columns"] = review.columns
        sources.append(source)
    if not deposits:
        raise common.InputError("the deposit list is empty")
    return deposits, {"sources": sources, "amount_unit": ",".join(sorted({s["amount_unit"] for s in sources}))}


def read_governors(path: Path) -> tuple[dict[str, str], dict[str, int], dict]:
    """validator -> governor; validator -> initial_assets_atto when the file has it (validator-vaults.csv)."""
    governors, assets = {}, {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, None) or []
        vi = _column(header, None, VALIDATOR_COLUMNS, "validator", str(path))
        gi = _column(header, None, GOVERNOR_COLUMNS, "governor", str(path))
        si = _column(header, None, STATUS_COLUMNS, "status", str(path), required=False)
        ti = _column(header, None, ("initial_assets_atto",), "assets", str(path), required=False)
        for line, record in enumerate(reader, start=2):
            if not record or all(not c.strip() for c in record):
                continue
            where = f"{path}:{line}"
            validator = common.normalize_address(record[vi], where).lower()
            if si is not None and record[si].strip().lower() != "ready":
                raise common.InputError(f"{where}: governor status is {record[si]!r}, not ready")
            if validator in governors:
                raise common.InputError(f"{where}: validator {validator} appears twice")
            governors[validator] = common.normalize_address(record[gi], where)
            if ti is not None and record[ti].strip():
                assets[validator] = int(record[ti])
    return governors, assets, {"path": str(path), "sha256": common.sha256_file(path), "rows": len(governors)}


def parse_validators(values: list[str] | None) -> set[str]:
    """Lower-case validator addresses from repeated or comma-separated --validator values (0x or one1)."""
    out = set()
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                address = common.from_one1(part) if part.lower().startswith("one1") else common.normalize_address(part, "--validator")
                out.add(address.lower())
    return out


def governor_policy(args) -> tuple:
    """(validator -> expected governor, description, file overrides, file initial_assets_atto)."""
    if getattr(args, "governor", None) and getattr(args, "governors", None):
        raise common.InputError("pass --governor or --governors, not both")
    if getattr(args, "governor", None):
        single = common.normalize_address(args.governor, "--governor")
        return (lambda validator: single), {"policy": "single", "governor": single}, {}, {}
    overrides, assets, source = {}, {}, {"policy": "validator address"}
    if getattr(args, "governors", None):
        overrides, assets, source = read_governors(args.governors)
        source["policy"] = "file, validator address where absent"
    return (lambda validator: overrides.get(validator.lower(), common.to_checksum(validator))), source, overrides, assets


def assemble_vaults(artifact: Artifact, token: str, deposits: list[Deposit], governor_for, merge: bool) -> list[Vault]:
    by_validator: dict[str, list[Deposit]] = {}
    for d in deposits:
        by_validator.setdefault(d.validator.lower(), []).append(d)
    vaults = []
    for key in sorted(by_validator):
        rows = by_validator[key]
        merged: dict[str, Deposit] = {}
        for d in rows:
            k = d.delegator.lower()
            if k in merged:
                if not merge:
                    raise common.InputError(f"delegator {d.delegator} appears twice for validator {d.validator} "
                                            f"({merged[k].source} and {d.source}); pass --merge-duplicates to add them up")
                merged[k] = Deposit(merged[k].validator, merged[k].delegator, merged[k].amount + d.amount,
                                    f"{merged[k].source}+{d.source}")
            else:
                merged[k] = d
        validator = rows[0].validator
        governor = governor_for(validator)
        vaults.append(Vault(validator, governor, vault_address(artifact, token, validator, governor), list(merged.values())))
    return vaults


def check_recipients(vaults: list[Vault], forbidden: dict[str, str]) -> None:
    """No delegator may be a vault (audit finding L-01), the Safe, the token or the deployer."""
    bad = {v.address.lower(): f"vault of {v.validator}" for v in vaults}
    bad.update({k.lower(): name for k, name in forbidden.items()})
    for v in vaults:
        for d in v.deposits:
            if d.delegator.lower() in bad:
                raise common.InputError(f"{d.source}: delegator {d.delegator} is the {bad[d.delegator.lower()]}")


# ---------------------------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------------------------

VAULT_FIELDS = ["validator_address", "validator_one1", "governor_address", "vault_address", "salt", "deposits",
                "total_atto", "total_one", "deploy"]
DEPOSIT_FIELDS = ["validator_address", "vault_address", "delegator_address", "delegator_one1", "amount_atto", "amount_one"]


def vault_row(v: Vault) -> list[str]:
    return [v.validator, common.to_one1(v.validator), v.governor, v.address, common.hex0x(salt_for(v.validator)),
            str(len(v.deposits)), str(v.total), one_text(v.total), "yes" if v.deploy else "already deployed"]


def deposit_row(v: Vault, d: Deposit) -> list[str]:
    return [v.validator, v.address, d.delegator, common.to_one1(d.delegator), str(d.amount), one_text(d.amount)]


def write_csv(path: Path, fields: list[str], rows: list[list[str]]) -> str:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(fields)
        writer.writerows(rows)
    return common.sha256_file(path)


def read_plan_files(out_dir: Path, artifact: Artifact, token: str, prefix: str = "") -> list[Vault]:
    """Rebuild the vaults from <prefix>vaults.csv and <prefix>deposits.csv, checking every derived value."""
    vaults, index = [], {}
    names = f"{prefix}vaults.csv", f"{prefix}deposits.csv"
    with (out_dir / names[0]).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != VAULT_FIELDS:
            raise common.InputError(f"{out_dir}/{names[0]}: unexpected header {reader.fieldnames}")
        for r in reader:
            v = Vault(common.normalize_address(r["validator_address"], names[0]),
                      common.normalize_address(r["governor_address"], names[0]),
                      common.normalize_address(r["vault_address"], names[0]), [], r["deploy"] == "yes")
            if vault_address(artifact, token, v.validator, v.governor) != v.address:
                raise common.InputError(f"{names[0]}: {v.address} is not the vault of validator {v.validator} "
                                        f"with governor {v.governor}")
            index[v.validator.lower()] = v
            vaults.append(v)
    with (out_dir / names[1]).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != DEPOSIT_FIELDS:
            raise common.InputError(f"{out_dir}/{names[1]}: unexpected header {reader.fieldnames}")
        for line, r in enumerate(reader, start=2):
            where = f"{names[1]}:{line}"
            v = index.get(r["validator_address"].lower())
            if v is None or r["vault_address"].lower() != v.address.lower():
                raise common.InputError(f"{where}: validator or vault not in {names[0]}")
            v.deposits.append(Deposit(v.validator, common.normalize_address(r["delegator_address"], where),
                                      int(r["amount_atto"]), where))
    return vaults


def tx_folder(out_dir: Path, index: int, kind: str, nonce: int | None) -> Path:
    return out_dir / (f"tx-{index:02d}-{kind}" + ("" if nonce is None else f"-nonce-{nonce}"))


def tx_summary(tx: PlannedTx) -> dict:
    deposits = [d for chunk in tx.deposits for d in chunk]
    return {"kind": tx.kind, "vaults": len({v.address for v in tx.vaults}), "deposit_calls": len(tx.deposits),
            "deposits": len(deposits), "amount": str(sum(d.amount for d in deposits)), "estimated_gas": tx.estimated_gas}


def tx_files(folder: Path, tx: PlannedTx) -> str:
    if tx.kind == "deploy":
        return write_csv(folder / "vaults.csv", VAULT_FIELDS, [vault_row(v) for v in tx.vaults])
    return write_csv(folder / "deposits.csv", DEPOSIT_FIELDS,
                     [deposit_row(v, d) for v, chunk in zip(tx.vaults, tx.deposits) for d in chunk])


def tx_label(args_label: str, i: int, count: int, tx: PlannedTx) -> tuple[str, str]:
    s = tx_summary(tx)
    if tx.kind == "deploy":
        what = f"deploy {s['vaults']} GovernorDelegator vault(s)"
    else:
        what = f"fund {s['vaults']} vault(s): {s['deposits']} deposits, {common.format_one(int(s['amount']))} ONE"
    return f"{args_label or 'Validator vaults'} - Safe tx {i} of {count} - {tx.kind}", what


def signing_sheet(manifest: dict) -> str:
    nonce_free = manifest["first_nonce"] is None
    lines = [
        f"# Signing sheet: {manifest['label'] or 'validator vaults'}",
        "",
        "Every value below is recomputed by `vault-batch verify --out-dir <this directory>`. Before signing,",
        "compare the Safe web app and the hardware wallet screen with this sheet, and decode the queue with",
        "`vault-batch show`.",
        "",
        f"- Chain id: `{manifest['chain_id']}`; Safe `{manifest['safe']}` (version {manifest['safe_version']})",
        f"- Token: `{manifest['token']}`; CREATE2 deployer: `{manifest['factory']}`",
        f"- Contract: GovernorDelegator, creation code keccak-256 `{manifest['creation_keccak256']}`; "
        f"deployed code keccak-256 `{manifest['runtime_keccak256']}`",
        f"- MultiSendCallOnly: `{manifest['multisend_call_only']}`",
        f"- Vaults funded: {manifest['funded_vaults']} ({manifest['deployed_vaults']} deployed here); deposits "
        f"{manifest['funded_deposits']}; total {manifest['funded_total_display']} ONE",
        f"- Left out below the {manifest['min_vault_display']} ONE vault minimum: {manifest['excluded_vaults']} vault(s), "
        f"{manifest['excluded_deposits']} deposit(s), {manifest['excluded_total_display']} ONE (excluded-*.csv)",
        f"- Domain hash: `{manifest['domain_hash']}`",
        f"- Estimated gas: {manifest['estimated_gas']:,} in total",
        "",
        "Deploy transactions come first: a funding transaction reverts unless its vaults exist.",
        "",
    ]
    if nonce_free:
        lines += ["Built without a nonce. Get the Safe transaction hashes from the queue with `vault-batch show`, or with",
                  "`vault-batch hash --safe-transaction <folder>/safe-transaction.json --nonce <assigned nonce>`.", ""]
    for tx in manifest["transactions"]:
        title = f"## Safe transaction {tx['index']} of {len(manifest['transactions'])}: {tx['kind']}"
        lines += [title if nonce_free else f"{title}, nonce {tx['nonce']}", "",
                  f"- Folder: `{tx['folder']}` (import `transaction-builder.json` into the Transaction Builder)"]
        if tx["kind"] == "deploy":
            lines.append(f"- Deploys {tx['vaults']} vault(s); governors and addresses in `{tx['folder']}/vaults.csv`")
        else:
            lines.append(f"- Funds {tx['vaults']} vault(s) with {tx['deposit_calls']} approve + depositBatch pair(s): "
                         f"{tx['deposits']} deposits, {tx['amount_display']} ONE (`{tx['folder']}/deposits.csv`)")
        kind = "delegate call to MultiSendCallOnly" if tx["operation"] == sb.DELEGATECALL else "direct call"
        lines += [f"- To: `{tx['to']}` ({kind}); value 0; operation {tx['operation']}; safeTxGas 0, baseGas 0, gasPrice 0",
                  f"- Call data: {tx['data_bytes']} bytes, keccak-256 `{tx['data_keccak256']}`",
                  f"- Estimated gas: {tx['estimated_gas']:,}"]
        if not nonce_free:
            lines += [f"- Message hash: `{tx['message_hash']}`", f"- Safe transaction hash: `{tx['safe_tx_hash']}`"]
        lines.append("")
    return "\n".join(lines)


def gas_costs(gas: int, prices: list[Decimal]) -> str:
    return ", ".join(f"{(Decimal(gas) * p / Decimal(10**9)).normalize():f} ETH at {p.normalize():f} gwei" for p in prices)


def parse_prices(text: str) -> list[Decimal]:
    try:
        return [Decimal(p) for p in text.split(",") if p.strip()]
    except Exception:
        raise common.InputError(f"--gas-price-gwei: not a comma-separated list of numbers: {text!r}") from None


# ---------------------------------------------------------------------------------------------
# chain helpers
# ---------------------------------------------------------------------------------------------


def rpc_batch(rpc: common.Rpc, calls: list[tuple[str, list]], size: int = 20) -> list:
    out = []
    for start in range(0, len(calls), size):
        part = calls[start:start + size]
        for attempt in range(6):
            try:
                out += rpc.batch(part)
                break
            except RuntimeError:
                if attempt == 5:
                    raise
                time.sleep(2 * (attempt + 1))
        time.sleep(0.2)
    return out


def call_item(to: str, data: str) -> tuple[str, list]:
    return ("eth_call", [{"to": to, "data": data}, "latest"])


def vault_states(rpc: common.Rpc, artifact: Artifact, token: str, addresses: list[str]) -> dict[str, dict]:
    """address -> {deployed, code_ok, governor, asset_ok, fee}."""
    expected = artifact.runtime(token)
    codes = rpc_batch(rpc, [("eth_getCode", [a, "latest"]) for a in addresses])
    live = [a for a, c in zip(addresses, codes) if c not in ("0x", "0x0")]
    states = {a.lower(): {"deployed": False} for a in addresses}
    for a, c in zip(addresses, codes):
        if c not in ("0x", "0x0"):
            states[a.lower()] = {"deployed": True, "code_ok": common.unhex(c) == expected}
    views = rpc_batch(rpc, [call_item(a, common.encode_call(sig)) for a in live for sig in ("governor()", "asset()", "feeBps()")])
    for i, a in enumerate(live):
        s = states[a.lower()]
        if not s["code_ok"]:
            continue
        gov, asset, fee = views[3 * i:3 * i + 3]
        s["governor"] = common.decode_address(gov)
        s["asset_ok"] = common.decode_address(asset).lower() == token.lower()
        s["fee"] = common.decode_word(fee)
    return states


def harmony_rpc(url: str):
    rpc = common.Rpc(url, timeout=120)
    block = rpc.call("hmyv2_getBlockByNumber", [HARMONY_CUTOFF_BLOCK, {"fullTx": False}])
    if str(block.get("hash", "")).lower() != HARMONY_CUTOFF_HASH:
        raise common.InputError(f"{url} returns hash {block.get('hash')} for block {HARMONY_CUTOFF_BLOCK}, expected "
                                f"{HARMONY_CUTOFF_HASH}; is it Harmony mainnet shard 0?")
    return rpc


def harmony_delegations(url: str, validators: list[str]) -> dict[str, dict]:
    """validator -> {name, delegations: {delegator: active amount}} at the shard-0 cutoff block."""
    rpc = harmony_rpc(url)

    def fetch(group: list[str]) -> list:
        calls = [("hmyv2_getValidatorInformationByBlockNumber", [v, HARMONY_CUTOFF_BLOCK]) for v in group]
        for attempt in range(6):
            try:
                return rpc.batch(calls)
            except RuntimeError:
                if attempt == 5:
                    raise
                time.sleep(2 * (attempt + 1))

    groups = [validators[i:i + 4] for i in range(0, len(validators), 4)]
    out = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for group, results in zip(groups, pool.map(fetch, groups)):
            for validator, info in zip(group, results):
                inner = info.get("validator") or {}
                if common.from_one1(inner["address"], checksum=False) != validator.lower():
                    raise RuntimeError(f"Harmony RPC answered for {inner.get('address')} instead of {validator}")
                delegations = defaultdict(int)
                for d in inner.get("delegations") or []:
                    delegations[common.from_one1(d["delegator-address"], checksum=False)] += int(d["amount"])
                out[validator.lower()] = {"name": inner.get("name", ""), "delegations": dict(delegations)}
    return out


def harmony_check(vaults: list[tuple[str, list[tuple[str, int]]]], url: str) -> tuple[dict, list[str], list[str], dict]:
    """Compare (validator, [(delegator, amount)]) with Harmony's cutoff delegations.

    Returns per-(validator, delegator) cutoff amounts, failures, warnings and validator names.
    """
    data = harmony_delegations(url, [v for v, _ in vaults])
    cutoff, failures, warnings, names = {}, [], [], {}
    lower = 0
    for validator, rows in vaults:
        info = data[validator.lower()]
        names[validator.lower()] = info["name"]
        for delegator, amount in rows:
            have = info["delegations"].get(delegator.lower(), 0)
            cutoff[(validator.lower(), delegator.lower())] = have
            if amount > have:
                failures.append(f"deposit of {one_text(amount)} ONE for {delegator} in the vault of {validator} exceeds "
                                f"its Harmony delegation at the cutoff block ({one_text(have)} ONE)")
            elif amount < have:
                lower += 1
    if lower:
        warnings.append(f"{lower} deposit(s) are below the delegator's Harmony stake at the cutoff block (a not-issued "
                        "deduction would explain it; see the harmony_cutoff_atto column)")
    return cutoff, failures, warnings, names


# ---------------------------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> None:
    try:
        artifact = load_artifact()
        resolved = sb.resolve_safe(args)
        safe, token = resolved["safe"], resolved["token"]
        args.chain_id, args.safe_version, args.nonce = resolved["chain_id"], resolved["safe_version"], resolved["nonce"]
        multisend = sb.multisend_for(args.safe_version, None)
        prices = parse_prices(args.gas_price_gwei)
        if not 1 <= args.deploys_per_tx <= MAX_DEPLOYS_PER_TX:
            raise common.InputError(f"--deploys-per-tx must be between 1 and {MAX_DEPLOYS_PER_TX}")
        if not 1_000_000 <= args.gas_target <= GAS_TX_CAP - 2_000_000:
            raise common.InputError(f"--gas-target must be between 1,000,000 and {GAS_TX_CAP - 2_000_000:,}")
        if FUND_BASE_GAS + FUND_CALL_GAS + FUND_ROW_GAS * args.max_rows_per_call > args.gas_target or args.max_rows_per_call < 1:
            raise common.InputError("--max-rows-per-call does not fit in --gas-target")
        try:
            zero_minimum = Decimal(args.min_vault_one.replace(",", "")) == 0
        except Exception:
            raise common.InputError(f"--min-vault-one: not a number: {args.min_vault_one!r}") from None
        min_vault = 0 if zero_minimum else common.parse_amount(args.min_vault_one, "one", "--min-vault-one")

        deposits, info = read_deposits(args.input, args, review_portal_exports=True)
        only, skip = parse_validators(args.validator), parse_validators(args.exclude_validator)
        if only & skip:
            raise common.InputError(f"{sorted(only & skip)[0]} is given to both --validator and --exclude-validator")
        for wanted, name in ((only, "--validator"), (skip, "--exclude-validator")):
            missing = wanted - {d.validator.lower() for d in deposits}
            if missing:
                raise common.InputError(f"{name} {sorted(missing)[0]} has no rows in the input")
        if only:
            deposits = [d for d in deposits if d.validator.lower() in only]
        excluded_by_flag = [d for d in deposits if d.validator.lower() in skip]
        deposits = [d for d in deposits if d.validator.lower() not in skip]
        governor_for, governor_source, overrides, assets = governor_policy(args)
        vaults = assemble_vaults(artifact, token, deposits, governor_for, args.merge_duplicates)
        if assets and len(info["sources"]) > 1:
            print(f"note: {args.governors} initial_assets_atto is not compared, because the deposits come from "
                  f"{len(info['sources'])} files", file=sys.stderr)
        elif assets:
            for v in vaults:
                if v.validator.lower() in assets and assets[v.validator.lower()] != v.total:
                    raise common.InputError(f"validator {v.validator}: deposits total {v.total}, but {args.governors} "
                                            f"says initial_assets_atto {assets[v.validator.lower()]}")
        check_recipients(vaults, {safe: "Safe", token: "token", FACTORY: "CREATE2 deployer"})
        input_total = sum(v.total for v in vaults)
        input_rows = sum(len(v.deposits) for v in vaults)
        input_vaults = len(vaults)

        states, already = {}, []
        rpc_url = args.rpc_url or common.load_env(args.env).get("RPC_URL", "")
        if args.skip_funded and not rpc_url:
            raise common.InputError("--skip-funded reads the vaults on-chain; pass --rpc-url")
        if rpc_url:
            rpc = common.Rpc(rpc_url)
            if rpc.call("eth_getCode", [FACTORY, "latest"]) in ("0x", "0x0"):
                raise common.InputError(f"no CREATE2 deployer at {FACTORY} on this chain")
            states = vault_states(rpc, artifact, token, [v.address for v in vaults])
            for v in vaults:
                s = states[v.address.lower()]
                if s["deployed"]:
                    if not s.get("code_ok") or not s.get("asset_ok"):
                        raise common.InputError(f"{v.address} (vault of {v.validator}) exists on-chain but does not "
                                                "hold the expected code or token")
                    v.deploy = False
            # deposits are additive: never credit anyone twice by accident
            pairs = [(v, d) for v in vaults if not v.deploy for d in v.deposits]
            held = rpc_batch(rpc, [call_item(v.address, common.encode_call("balanceOf(address)", d.delegator))
                                   for v, d in pairs])
            credited = [(v, d, common.decode_word(c)) for (v, d), c in zip(pairs, held) if common.decode_word(c)]
            exact = [(v, d) for v, d, c in credited if c == d.amount] if args.skip_funded else []
            other = [(v, d, c) for v, d, c in credited if not (args.skip_funded and c == d.amount)]
            if other and not args.allow_existing_credits:
                v, d, c = other[0]
                hint = ("its credit differs from the list, so it is not a repeat of an earlier build"
                        if args.skip_funded else "a deposit adds to it")
                raise common.InputError(
                    f"{len(other)} delegator(s) already hold a credit in their vault, e.g. {d.delegator} holds "
                    f"{one_text(c)} ONE in {v.address} and the list says {one_text(d.amount)} ({d.source}); {hint}. "
                    "Run `reconcile`; --skip-funded leaves out deposits whose credit already equals the list; "
                    "--allow-existing-credits funds deliberate top-ups")
            done = {(v.address, d.delegator) for v, d in exact}
            for v in vaults:
                already += [(v, d) for d in v.deposits if (v.address, d.delegator) in done]
                v.deposits = [d for d in v.deposits if (v.address, d.delegator) not in done]
            vaults = [v for v in vaults if v.deposits]

        excluded = [v for v in vaults if v.deploy and v.total < min_vault]
        included = [v for v in vaults if v not in excluded]
        if args.phase == "fund":
            missing = [v for v in included if v.deploy]
            if missing and states:
                raise common.InputError(f"--phase fund, but {len(missing)} vault(s) are not deployed yet, e.g. {missing[0].address}")
        funded_total = sum(v.total for v in included)
        funded_rows = sum(len(v.deposits) for v in included)
        if args.expect_total is not None and funded_total != common.parse_amount(args.expect_total, "one", "--expect-total"):
            raise common.InputError(f"funded total {common.format_one(funded_total)} differs from --expect-total {args.expect_total}")
        if args.expect_count is not None and funded_rows != args.expect_count:
            raise common.InputError(f"{funded_rows} deposits are funded, --expect-count says {args.expect_count}")
        if args.expect_vaults is not None and len(included) != args.expect_vaults:
            raise common.InputError(f"{len(included)} vaults are funded, --expect-vaults says {args.expect_vaults}")
        if not included:
            raise common.InputError("nothing to build: every deposit is already funded or below --min-vault-one")
        if args.out_dir.exists():
            if not args.force:
                raise common.InputError(f"{args.out_dir} already exists; pass --force to replace it")
            shutil.rmtree(args.out_dir)
    except (common.InputError, RuntimeError) as exc:
        common.die(str(exc))
        return

    txs = plan_transactions(artifact, token, included, args.phase, args.deploys_per_tx, args.gas_target,
                            args.max_rows_per_call)
    now = _dt.datetime.now(_dt.timezone.utc)
    created_ms = int(now.timestamp() * 1000)
    args.out_dir.mkdir(parents=True)
    hashes = {
        "vaults.csv": write_csv(args.out_dir / "vaults.csv", VAULT_FIELDS, [vault_row(v) for v in included]),
        "deposits.csv": write_csv(args.out_dir / "deposits.csv", DEPOSIT_FIELDS,
                                  [deposit_row(v, d) for v in included for d in v.deposits]),
        "excluded-vaults.csv": write_csv(args.out_dir / "excluded-vaults.csv", VAULT_FIELDS, [vault_row(v) for v in excluded]),
        "excluded-deposits.csv": write_csv(args.out_dir / "excluded-deposits.csv", DEPOSIT_FIELDS,
                                           [deposit_row(v, d) for v in excluded for d in v.deposits]),
        "already-funded.csv": write_csv(args.out_dir / "already-funded.csv", DEPOSIT_FIELDS,
                                        [deposit_row(v, d) for v, d in already]),
    }
    dom = common.hex0x(sb.domain_hash(args.chain_id, safe))
    entries = []
    for i, tx in enumerate(txs, start=1):
        nonce = None if args.nonce is None else args.nonce + i - 1
        stx = safe_tx(tx.calls, multisend, nonce)
        record = sb.tx_record(args.chain_id, safe, stx)
        folder = tx_folder(args.out_dir, i, tx.kind, nonce)
        folder.mkdir()
        name, what = tx_label(args.label, i, len(txs), tx)
        description = what + "; " + (f"call data keccak-256 {record['dataKeccak256']}" if nonce is None
                                     else f"expected safeTxHash {record['safeTxHash']}")
        builder = tx_builder_file(args.chain_id, safe, tx.calls, name + ("" if nonce is None else f" - nonce {nonce}"),
                                  description, created_ms)
        (folder / "transaction-builder.json").write_text(json.dumps(builder, indent=1) + "\n", encoding="utf-8")
        (folder / "safe-transaction.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        list_sha = tx_files(folder, tx)
        s = tx_summary(tx)
        entries.append({"index": i, "nonce": nonce, "folder": folder.name, **s,
                        "amount_display": common.format_one(int(s["amount"])), "list_sha256": list_sha,
                        "to": stx.to, "operation": stx.operation, "data_bytes": len(stx.data),
                        "data_keccak256": record["dataKeccak256"], "message_hash": record["messageHash"],
                        "safe_tx_hash": record["safeTxHash"], "tx_builder_checksum": builder["meta"]["checksum"]})

    total_gas = sum(t.estimated_gas for t in txs)
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
        "factory": FACTORY,
        "salt_rule": "validator address as a 32-byte word",
        "contract_source_sha256": AUDITED_SOURCE_SHA256,
        "creation_keccak256": AUDITED_CREATION_KECCAK,
        "runtime_keccak256": common.hex0x(common.keccak256(artifact.runtime(token))),
        "sources": info["sources"],
        "amount_unit": info["amount_unit"],
        "governors": governor_source,
        "phase": args.phase,
        "first_nonce": args.nonce,
        "parameter_sources": resolved["sources"],
        "min_vault_atto": str(min_vault),
        "min_vault_display": common.format_one(min_vault),
        "deploys_per_tx": args.deploys_per_tx,
        "gas_target": args.gas_target,
        "max_rows_per_call": args.max_rows_per_call,
        "gas_model": {"deploy_base": DEPLOY_BASE_GAS, "deploy": DEPLOY_GAS, "fund_base": FUND_BASE_GAS,
                      "fund_call": FUND_CALL_GAS, "fund_row": FUND_ROW_GAS},
        "input_vaults": input_vaults,
        "input_deposits": input_rows,
        "input_total": str(input_total),
        "funded_vaults": len(included),
        "deployed_vaults": sum(1 for t in txs if t.kind == "deploy" for _ in t.vaults),
        "already_deployed": [v.address for v in included if not v.deploy],
        "funded_deposits": funded_rows,
        "funded_total": str(funded_total),
        "funded_total_display": common.format_one(funded_total),
        "excluded_vaults": len(excluded),
        "excluded_deposits": sum(len(v.deposits) for v in excluded),
        "excluded_total": str(sum(v.total for v in excluded)),
        "excluded_total_display": common.format_one(sum(v.total for v in excluded)),
        "validators_filter": sorted(only),
        "validators_excluded": sorted(skip),
        "skip_funded": bool(args.skip_funded),
        "already_funded_deposits": len(already),
        "already_funded_total": str(sum(d.amount for _, d in already)),
        "file_sha256": hashes,
        "domain_hash": dom,
        "estimated_gas": total_gas,
        "transactions": entries,
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    (args.out_dir / "SIGNING-SHEET.md").write_text(signing_sheet(manifest) + "\n", encoding="utf-8")

    src = resolved["sources"]
    for s in info["sources"]:
        zeros = f", {s['zero_rows_skipped']} zero row(s) skipped" if s["zero_rows_skipped"] else ""
        print(f"input file       : {s['path']}: {s['rows']} rows ({s['columns']['validator']}, {s['columns']['delegator']}, "
              f"{s['columns']['amount']}){zeros}")
    print(f"input            : {input_rows} deposits for {input_vaults} validators, {common.format_one(input_total)} ONE"
          + (f" (only --validator {', '.join(sorted(only))})" if only else ""))
    if skip:
        print(f"--exclude-validator: left out {len(excluded_by_flag)} deposit(s), "
              f"{common.format_one(sum(d.amount for d in excluded_by_flag))} ONE of {len(skip)} validator(s)")
    if args.skip_funded:
        print(f"already funded   : {len(already)} deposit(s), {common.format_one(sum(d.amount for _, d in already))} ONE "
              "left out because the vault already credits exactly that amount (already-funded.csv)")
    print(f"governors        : {governor_source['policy']}" + (f" ({len(overrides)} from {args.governors})" if overrides else ""))
    print(f"vault minimum    : {common.format_one(min_vault)} ONE; left out {len(excluded)} vault(s), "
          f"{manifest['excluded_deposits']} deposit(s), {manifest['excluded_total_display']} ONE (excluded-*.csv)")
    print(f"funded           : {len(included)} vault(s), {funded_rows} deposits, {common.format_one(funded_total)} ONE")
    if states:
        print(f"already deployed : {len(manifest['already_deployed'])} vault(s) (checked on-chain)")
    else:
        print("already deployed : not checked (no RPC); every vault is deployed by these transactions")
    print(f"safe / version   : {safe} / {args.safe_version} ({src['safe_version']}) on chain {args.chain_id} ({src['chain_id']})")
    if args.nonce is None:
        print("first nonce      : none (the Safe web app assigns it; Safe transaction hashes are not computed)")
    else:
        print(f"first nonce      : {args.nonce} ({src['nonce']})")
    if src["nonce"] == "chain":
        print("note: the nonce is the Safe's next executed nonce. If other transactions are already queued in the "
              "Safe web app, rebuild with --nonce set to the next free nonce there, or with --no-nonce.", file=sys.stderr)
    print(f"domain hash      : {dom}")
    print(f"Safe transactions: {len(entries)}")
    for e in entries:
        what = (f"deploy {e['vaults']:>3} vaults" if e["kind"] == "deploy"
                else f"fund {e['vaults']:>3} vaults, {e['deposits']:>4} deposits {e['amount_display']:>30} ONE")
        ref = f"call data keccak-256 {e['data_keccak256']}" if e["nonce"] is None else f"nonce {e['nonce']} safeTxHash {e['safe_tx_hash']}"
        print(f"  #{e['index']:<3} {what:<70} ~{e['estimated_gas']:>10,} gas  {ref}")
    print(f"estimated gas    : {total_gas:,} ({gas_costs(total_gas, prices)})")
    print(f"written to       : {args.out_dir} (see SIGNING-SHEET.md)")


# ---------------------------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------------------------


def verify_offline(out_dir: Path, checks: sb.Checks) -> tuple[dict, Artifact, list[Vault]]:
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise common.InputError(f"{out_dir}/manifest.json: unexpected format {manifest.get('format')!r}")
    artifact = load_artifact()
    chain_id, safe, token = manifest["chain_id"], manifest["safe"], manifest["token"]
    multisend = manifest["multisend_call_only"]
    checks.ok(manifest["factory"] == FACTORY, "the manifest names another CREATE2 deployer")
    checks.ok(manifest["creation_keccak256"] == AUDITED_CREATION_KECCAK, "the manifest names another creation code")
    checks.ok(manifest["runtime_keccak256"] == common.hex0x(common.keccak256(artifact.runtime(token))),
              "the manifest's deployed-code hash differs")
    checks.ok(multisend in sb.MULTISEND_CALL_ONLY.get(manifest["safe_version"], []),
              f"{multisend} is not the official MultiSendCallOnly for Safe {manifest['safe_version']}")
    checks.ok(common.hex0x(sb.domain_hash(chain_id, safe)) == manifest["domain_hash"], "domain hash differs from manifest")
    for name, digest in manifest["file_sha256"].items():
        checks.ok(common.sha256_file(out_dir / name) == digest, f"{name} SHA-256 differs from manifest")

    vaults = read_plan_files(out_dir, artifact, token)
    excluded = read_plan_files(out_dir, artifact, token, "excluded-")
    min_vault = int(manifest["min_vault_atto"])
    for v in vaults:
        checks.ok(bool(v.deposits), f"vault {v.address} has no deposits")
        checks.ok(not v.deploy or v.total >= min_vault, f"vault {v.address} ({v.validator}) is below the vault minimum")
        checks.ok(v.deploy == (v.address not in manifest["already_deployed"]), f"vault {v.address}: deploy flag differs")
    for v in excluded:
        checks.ok(v.total < min_vault, f"excluded vault {v.address} ({v.validator}) is not below the vault minimum")
    all_vaults = vaults + excluded
    checks.ok(len({v.validator.lower() for v in all_vaults}) == len(all_vaults), "a validator appears twice")
    try:
        check_recipients(vaults, {safe: "Safe", token: "token", FACTORY: "CREATE2 deployer"})
    except common.InputError as exc:
        checks.ok(False, str(exc))
    for v in all_vaults:
        seen = set()
        for d in v.deposits:
            checks.ok(d.amount > 0, f"{d.source}: zero amount")
            checks.ok(d.delegator.lower() not in seen, f"{d.source}: delegator repeated in the vault of {v.validator}")
            seen.add(d.delegator.lower())
    checks.ok(len(vaults) == manifest["funded_vaults"] and sum(len(v.deposits) for v in vaults) == manifest["funded_deposits"]
              and str(sum(v.total for v in vaults)) == manifest["funded_total"], "funded counts or total differ from manifest")
    checks.ok(len(excluded) == manifest["excluded_vaults"] and str(sum(v.total for v in excluded)) == manifest["excluded_total"],
              "excluded counts or total differ from manifest")
    already = []
    if (out_dir / "already-funded.csv").is_file():
        with (out_dir / "already-funded.csv").open(newline="", encoding="utf-8") as handle:
            already = [(r["vault_address"].lower(), r["delegator_address"].lower(), int(r["amount_atto"]))
                       for r in csv.DictReader(handle)]
    checks.ok(len(already) == manifest.get("already_funded_deposits", 0)
              and str(sum(a for _, _, a in already)) == manifest.get("already_funded_total", "0"),
              "already-funded counts or total differ from manifest")
    planned = {(v.address.lower(), d.delegator.lower()) for v in vaults for d in v.deposits}
    checks.ok(not planned & {(v, d) for v, d, _ in already}, "a deposit is both planned and listed as already funded")
    checks.ok(manifest["input_total"] == str(sum(v.total for v in all_vaults) + sum(a for _, _, a in already)),
              "funded, excluded and already-funded deposits do not add up to the input total")
    only = set(manifest.get("validators_filter", []))
    checks.ok(not only or all(v.validator.lower() in only for v in all_vaults), "a vault is outside the --validator filter")
    skip = set(manifest.get("validators_excluded", []))
    checks.ok(not any(v.validator.lower() in skip for v in all_vaults), "a vault of an --exclude-validator is in the plan")

    txs = plan_transactions(artifact, token, vaults, manifest["phase"], manifest["deploys_per_tx"], manifest["gas_target"],
                            manifest["max_rows_per_call"])
    checks.ok(len(txs) == len(manifest["transactions"]), "number of Safe transactions differs from the recorded rules")
    for entry, tx in zip(manifest["transactions"], txs):
        label = f"transaction {entry['index']}" + ("" if entry["nonce"] is None else f" (nonce {entry['nonce']})")
        first = manifest["first_nonce"]
        nonce = None if first is None else first + entry["index"] - 1
        stx = safe_tx(tx.calls, multisend, nonce)
        record = sb.tx_record(chain_id, safe, stx)
        folder = out_dir / entry["folder"]
        checks.ok(entry["folder"] == tx_folder(out_dir, entry["index"], tx.kind, nonce).name, f"{label}: folder name differs")
        checks.ok({k: entry[k] for k in tx_summary(tx)} == tx_summary(tx), f"{label}: summary differs")
        checks.ok(entry["nonce"] == nonce and entry["to"] == stx.to and entry["operation"] == stx.operation,
                  f"{label}: nonce/to/operation differ")
        checks.ok(entry["data_keccak256"] == record["dataKeccak256"], f"{label}: call data hash differs")
        checks.ok(entry["message_hash"] == record["messageHash"] and entry["safe_tx_hash"] == record["safeTxHash"],
                  f"{label}: Safe transaction hash differs")
        stored = json.loads((folder / "safe-transaction.json").read_text(encoding="utf-8"))
        checks.ok(stored == record, f"{label}: safe-transaction.json differs from the recomputed transaction")
        name = "vaults.csv" if tx.kind == "deploy" else "deposits.csv"
        expected_rows = ([vault_row(v) for v in tx.vaults] if tx.kind == "deploy"
                         else [deposit_row(v, d) for v, chunk in zip(tx.vaults, tx.deposits) for d in chunk])
        with (folder / name).open(newline="", encoding="utf-8") as handle:
            checks.ok(list(csv.reader(handle))[1:] == expected_rows, f"{label}: {name} differs from the plan")
        checks.ok(common.sha256_file(folder / name) == entry["list_sha256"], f"{label}: {name} SHA-256 differs")
        builder = json.loads((folder / "transaction-builder.json").read_text(encoding="utf-8"))
        checks.ok(builder.get("chainId") == str(chain_id), f"{label}: Transaction Builder file is for another chain")
        checks.ok(builder.get("meta", {}).get("createdFromSafeAddress") == safe, f"{label}: Transaction Builder file names another Safe")
        checks.ok(builder.get("transactions") == tx_builder_entries(tx.calls), f"{label}: Transaction Builder calls differ")
        checks.ok(builder.get("meta", {}).get("checksum") == sb.tx_builder_checksum(builder),
                  f"{label}: Transaction Builder checksum does not match its content")
        checks.ok(entry["estimated_gas"] <= GAS_TX_CAP - 2_000_000, f"{label}: estimated gas too close to the per-transaction cap")
    return manifest, artifact, vaults


def verify_chain(manifest: dict, artifact: Artifact, vaults: list[Vault], rpc: common.Rpc, checks: sb.Checks) -> None:
    safe, token, multisend = manifest["safe"], manifest["token"], manifest["multisend_call_only"]
    chain_id = rpc.chain_id()
    checks.ok(chain_id == manifest["chain_id"], f"RPC chain id {chain_id} differs from manifest {manifest['chain_id']}")
    print(f"block            : {rpc.block_number()}")
    version = sb.decode_string(sb.rpc_call(rpc, safe, "VERSION()"))
    nonce = common.decode_word(sb.rpc_call(rpc, safe, "nonce()"))
    threshold = common.decode_word(sb.rpc_call(rpc, safe, "getThreshold()"))
    owners = sb.decode_address_array(sb.rpc_call(rpc, safe, "getOwners()"))
    print(f"safe             : version {version}, nonce {nonce}, threshold {threshold} of {len(owners)} owners")
    checks.ok(version == manifest["safe_version"], f"Safe reports version {version}, manifest says {manifest['safe_version']}")
    for name, address, expected in (("MultiSendCallOnly", multisend, sb.MULTISEND_CALL_ONLY_CODE_HASH.get(manifest["safe_version"])),
                                    ("CREATE2 deployer", FACTORY, FACTORY_CODE_HASH)):
        code_hash = common.hex0x(common.keccak256(common.unhex(rpc.call("eth_getCode", [address, "latest"]))))
        checks.ok(code_hash == expected, f"code at the {name} {address} has hash {code_hash}, expected {expected}")
    decimals = common.decode_word(sb.rpc_call(rpc, token, "decimals()"))
    balance = common.decode_word(sb.rpc_call(rpc, token, "balanceOf(address)", safe))
    checks.ok(decimals == 18, f"token has {decimals} decimals")
    print(f"token            : {decimals} decimals; Safe balance {common.format_one(balance)} ONE")

    states = vault_states(rpc, artifact, token, [v.address for v in vaults])
    deployed = [v for v in vaults if states[v.address.lower()]["deployed"]]
    for v in deployed:
        s = states[v.address.lower()]
        good = s.get("code_ok") and s.get("asset_ok") and s.get("governor", "").lower() == v.governor.lower()
        checks.ok(bool(good), f"vault {v.address} exists but its code, token or governor is not the planned one")
        if good and s["fee"]:
            checks.warn(f"vault {v.address}: governor has set feeBps {s['fee']}")
    custody = rpc_batch(rpc, [call_item(token, common.encode_call("balanceOf(address)", v.address)) for v in deployed])
    funded = [v for v, c in zip(deployed, custody) if common.decode_word(c) > 0]
    pending = sum(v.total for v in vaults if v not in funded)
    print(f"vaults           : {len(vaults)} planned; {len(deployed)} deployed with the expected code; "
          f"{len(funded)} already hold ONE")
    print(f"still to fund    : {common.format_one(pending)} ONE (vaults that hold no ONE yet)")
    checks.ok(balance >= pending, "the Safe holds less ONE than the vaults still to fund")

    governors = sorted({v.governor for v in vaults})
    codes = rpc_batch(rpc, [("eth_getCode", [g, "latest"]) for g in governors])
    counts = rpc_batch(rpc, [("eth_getTransactionCount", [g, "latest"]) for g in governors])
    contracts = [g for g, c in zip(governors, codes) if c not in ("0x", "0x0") and not c.lower().startswith("0xef0100")]
    unused = sum(1 for n in counts if int(n, 16) == 0)
    print(f"governors        : {len(governors)}: {len(contracts)} contract(s), {unused} never sent an Ethereum transaction")
    for g in contracts:
        checks.warn(f"governor {g} is a contract on this chain; confirm it can call transferGovernor and setFeeBps")
    delegators = sorted({d.delegator for v in vaults for d in v.deposits})
    codes = rpc_batch(rpc, [("eth_getCode", [d, "latest"]) for d in delegators])
    contracts = [d for d, c in zip(delegators, codes) if c not in ("0x", "0x0") and not c.lower().startswith("0xef0100")]
    delegated = sum(1 for c in codes if c.lower().startswith("0xef0100"))
    print(f"delegators       : {len(delegators)}: {len(contracts)} contract(s), {delegated} EIP-7702 delegated account(s)")
    for d in contracts:
        checks.warn(f"delegator {d} is a contract on this chain; confirm it can call withdraw")


def cmd_verify(args: argparse.Namespace) -> None:
    checks = sb.Checks()
    try:
        manifest, artifact, vaults = verify_offline(args.out_dir, checks)
        print(f"run              : {args.out_dir} ({manifest['label'] or 'no label'})")
        print(f"funded           : {manifest['funded_vaults']} vault(s), {manifest['funded_deposits']} deposits, "
              f"{manifest['funded_total_display']} ONE; left out below {manifest['min_vault_display']} ONE: "
              f"{manifest['excluded_vaults']} vault(s), {manifest['excluded_total_display']} ONE")
        nonces = "built without a nonce" if manifest["first_nonce"] is None else \
            f"nonces {manifest['first_nonce']}-{manifest['first_nonce'] + len(manifest['transactions']) - 1}"
        print(f"Safe transactions: {len(manifest['transactions'])}, {nonces}")
        env = common.load_env(args.env)
        rpc_url = args.rpc_url
        if not rpc_url and not args.offline and env.get("RPC_URL") and env.get("CHAIN_ID") == str(manifest["chain_id"]):
            rpc_url = env["RPC_URL"]
        if rpc_url:
            verify_chain(manifest, artifact, vaults, common.Rpc(rpc_url), checks)
        else:
            print("chain checks     : skipped (pass --rpc-url)")
        if args.harmony_rpc:
            cutoff, failures, warnings, _ = harmony_check(
                [(v.validator, [(d.delegator, d.amount) for d in v.deposits]) for v in vaults], args.harmony_rpc)
            for m in failures:
                checks.ok(False, m)
            for m in warnings:
                checks.warn(m)
            rows = [(v.validator.lower(), d.delegator.lower(), d.amount) for v in vaults for d in v.deposits]
            same = sum(1 for v, d, a in rows if cutoff[(v, d)] == a)
            print(f"harmony cutoff   : {same} of {len(rows)} deposits equal the delegation recorded on Harmony at block "
                  f"{HARMONY_CUTOFF_BLOCK:,}; {len(failures)} exceed it")
    except (common.InputError, RuntimeError, KeyError, ValueError) as exc:
        common.die(str(exc))
        return
    for m in checks.warnings:
        print(f"warning: {m}")
    for m in checks.failures:
        print(f"FAIL: {m}")
    if checks.failures:
        common.die(f"{len(checks.failures)} check(s) failed")
    for e in manifest["transactions"]:
        ref = f"call data keccak-256 {e['data_keccak256']}" if e["nonce"] is None else f"nonce {e['nonce']} safeTxHash {e['safe_tx_hash']}"
        print(f"  #{e['index']:<3} {e['kind']:<6} {ref}")
    print("OK: every file and hash matches vaults.csv and deposits.csv")


# ---------------------------------------------------------------------------------------------
# decoding queued transactions (show, hash)
# ---------------------------------------------------------------------------------------------


def decode_deploy(artifact: Artifact, token: str, body: bytes) -> tuple[str, str, str] | str:
    """(validator, governor, vault address), or why the call is not a vault deployment."""
    n = len(artifact.creation)
    if len(body) != 32 + n + 64 or body[32:32 + n] != artifact.creation:
        return "deployer call whose init code is not the audited GovernorDelegator"
    salt, asset, governor = body[:32], body[32 + n:32 + n + 32], body[32 + n + 32:]
    if any(salt[:12]) or any(asset[:12]) or any(governor[:12]):
        return "deployer call with a malformed salt or constructor argument"
    if asset[12:] != common.unhex(token):
        return f"vault for token 0x{asset[12:].hex()}, not {token}"
    validator, gov = common.to_checksum("0x" + salt[12:].hex()), common.to_checksum("0x" + governor[12:].hex())
    if int(gov, 16) == 0:
        return "vault with the zero address as governor"
    return validator, gov, vault_address(artifact, token, validator, gov)


def decode_approve(body: bytes) -> tuple[str, int] | None:
    if len(body) != 68 or body[:4] != APPROVE_SELECTOR or any(body[4:16]):
        return None
    return common.to_checksum("0x" + body[16:36].hex()), int.from_bytes(body[36:68], "big")


def decode_deposit_batch(body: bytes) -> tuple[list[int], list[str]] | None:
    """Only the canonical encoding this tool writes; anything else is rejected."""
    if body[:4] != DEPOSIT_BATCH_SELECTOR:
        return None
    a = body[4:]
    w = lambda i: int.from_bytes(a[i:i + 32], "big")  # noqa: E731
    if len(a) < 128 or len(a) % 32 or w(0) != 64:
        return None
    n = w(64)
    if w(32) != 96 + 32 * n or len(a) != 96 + 32 * n + 32 + 32 * n or w(96 + 32 * n) != n:
        return None
    amounts = [w(96 + 32 * i) for i in range(n)]
    words = [a[128 + 32 * n + 32 * i:160 + 32 * n + 32 * i] for i in range(n)]
    if any(any(x[:12]) for x in words):
        return None
    return amounts, [common.to_checksum("0x" + x[12:].hex()) for x in words]


@dataclass
class Decoded:
    deploys: list[tuple[str, str, str]]  # (validator, governor, vault)
    deposits: list[tuple[int, str, str, int]]  # (call index, vault, delegator, amount)
    problems: list[str]


def decode_vault_tx(tx: sb.SafeTx, artifact: Artifact, token: str) -> Decoded:
    problems = []
    if tx.value:
        problems.append(f"sends {tx.value} wei of ETH")
    if tx.gas_price or int(tx.gas_token, 16) or int(tx.refund_receiver, 16):
        problems.append("pays a gas refund (gasPrice, gasToken or refundReceiver is set)")
    if tx.operation == sb.CALL:
        calls = [(sb.CALL, tx.to, tx.value, tx.data)]
    else:
        if tx.to.lower() not in sb.OFFICIAL_MULTISEND_CALL_ONLY:
            problems.append(f"is a delegate call to {tx.to}, which is not an official MultiSendCallOnly contract")
        try:
            calls = sb.decode_multisend(tx.data)
        except common.InputError as exc:
            return Decoded([], [], problems + [str(exc)])
        if calls is None:
            return Decoded([], [], problems + ["is a delegate call whose data is not multiSend(bytes)"])
    deploys, deposits, pending = [], [], None
    for i, (op, to, value, body) in enumerate(calls, start=1):
        where = f"call {i}"
        if op != sb.CALL or value:
            problems.append(f"{where}: operation {op}, value {value}; only plain calls without ETH are allowed")
            continue
        if to.lower() == FACTORY.lower():
            got = decode_deploy(artifact, token, body)
            if isinstance(got, str):
                problems.append(f"{where}: {got}")
            else:
                deploys.append(got)
        elif to.lower() == token.lower():
            got = decode_approve(body)
            if got is None:
                problems.append(f"{where}: a token call that is not approve(address,uint256)")
            elif pending is not None:
                problems.append(f"{where}: approve while the approval of call {pending[0]} is unused")
            else:
                pending = (i, got[0], got[1])
        else:
            got = decode_deposit_batch(body)
            if got is None:
                problems.append(f"{where}: call to {to} that is not a canonical depositBatch(uint256[],address[])")
                continue
            amounts, delegators = got
            if pending is None or pending[1].lower() != to.lower() or pending[2] != sum(amounts):
                problems.append(f"{where}: depositBatch on {to} is not preceded by approve(that vault, its exact total "
                                f"{sum(amounts)})")
            pending = None
            deposits += [(i, common.to_checksum(to), d, a) for a, d in zip(amounts, delegators)]
    if pending is not None:
        problems.append(f"call {pending[0]}: approve of {pending[2]} to {pending[1]} is not used by a depositBatch")
    return Decoded(deploys, deposits, problems)


def read_compare(paths: list[Path], unit: str | None) -> dict[tuple[str, str], int]:
    class A:  # the column options of build
        validator_column = delegator_column = amount_column = None
        amount_unit = unit
    rows, _ = read_deposits(paths, A)
    out = {}
    for d in rows:
        key = (d.validator.lower(), d.delegator.lower())
        if key in out:
            raise common.InputError(f"{d.source}: delegator repeated for validator {d.validator}")
        out[key] = d.amount
    return out


def cmd_show(args: argparse.Namespace) -> None:
    note = lambda *parts: print(*parts, file=sys.stderr)  # noqa: E731
    try:
        artifact = load_artifact()
        env = common.load_env(args.env)
        safe_text, token_text = args.safe or env.get("RESERVE_ADDRESS", ""), args.token or env.get("TOKEN_ADDRESS", "")
        if not safe_text or not token_text:
            raise common.InputError("need --safe and --token (or RESERVE_ADDRESS and TOKEN_ADDRESS in .env)")
        safe, token = common.normalize_address(safe_text, "--safe"), common.normalize_address(token_text, "--token")
        nonces = sb.parse_nonces(args.nonce)
        if nonces and args.safe_tx_hash:
            raise common.InputError("pass --nonce or --safe-tx-hash, not both")
        expected = read_compare(args.compare, args.amount_unit) if args.compare else None
        governor_for, governor_source, overrides, _ = governor_policy(args)
        records, info, source, notes = sb.fetch_transactions(args, safe, nonces, args.api_key or env.get("SAFE_API_KEY"))
        if not records:
            raise common.InputError("no matching transaction")
        rpc_url = args.rpc_url or (env.get("RPC_URL") if env.get("CHAIN_ID") == str(args.chain_id) else None)
    except (common.InputError, KeyError, ValueError, RuntimeError) as exc:
        common.die(str(exc))
        return

    failures, warnings = [], list(notes)
    domain = common.hex0x(sb.domain_hash(args.chain_id, safe))
    vaults: dict[str, dict] = {}  # vault -> {validator, governor, deployed_nonce, source}
    deposits, transactions = [], []
    for record in sorted(records, key=lambda r: int(r["nonce"])):
        nonce = int(record["nonce"])
        label = f"nonce {nonce}"
        if str(record.get("safe", "")).lower() != safe.lower():
            failures.append(f"{label}: belongs to Safe {record.get('safe')}, not {safe}")
            continue
        try:
            tx = sb.service_safe_tx(record)
        except (KeyError, ValueError, common.InputError) as exc:
            failures.append(f"{label}: cannot read the transaction: {exc}")
            continue
        computed = sb.tx_record(args.chain_id, safe, tx)
        if computed["safeTxHash"].lower() != str(record.get("safeTxHash", "")).lower():
            failures.append(f"{label}: listed as {record.get('safeTxHash')}, but its content hashes to "
                            f"{computed['safeTxHash']}; do not sign")
        decoded = decode_vault_tx(tx, artifact, token)
        failures += [f"{label}: {p}" for p in decoded.problems]
        for validator, governor, vault in decoded.deploys:
            if vault.lower() in vaults:
                failures.append(f"{label}: vault {vault} is deployed twice (also at nonce {vaults[vault.lower()]['nonce']})")
            want = governor_for(validator)
            if governor.lower() != want.lower():
                failures.append(f"{label}: the vault of validator {validator} gets governor {governor}, expected {want} "
                                f"({governor_source['policy']}; see --governors / --governor)")
            vaults[vault.lower()] = {"validator": validator, "governor": governor, "nonce": nonce, "source": "deployed here"}
        for position, (call, vault, delegator, amount) in enumerate(decoded.deposits, start=1):
            deposits.append({"nonce": nonce, "call": call, "position": position, "vault_address": vault.lower(),
                             "delegator_address": delegator.lower(), "_amount": amount})
        transactions.append({"nonce": nonce, "safe_tx_hash": computed["safeTxHash"], "message_hash": computed["messageHash"],
                             "executed": bool(record.get("isExecuted")),
                             "confirmations": [c.get("owner") for c in record.get("confirmations") or []],
                             "required": record.get("confirmationsRequired"), "deploys": len(decoded.deploys),
                             "deposits": len(decoded.deposits), "total": sum(d[3] for d in decoded.deposits)})

    funded_vaults = sorted({d["vault_address"] for d in deposits})
    unknown = [v for v in funded_vaults if v not in vaults]
    if unknown and rpc_url:
        try:
            states = vault_states(common.Rpc(rpc_url), artifact, token, unknown)
        except RuntimeError as exc:
            common.die(str(exc))
            return
        # a vault's validator is not stored on-chain; recover it from the address rule and the governor policy
        candidates = set(overrides) | {k[0] for k in (expected or {})}
        known = {vault_address(artifact, token, c, governor_for(c)).lower(): common.to_checksum(c) for c in candidates}
        for v in unknown:
            s = states[v]
            if s["deployed"] and s.get("code_ok") and s.get("asset_ok"):
                validator = known.get(v)
                if validator is None and vault_address(artifact, token, s["governor"], s["governor"]).lower() == v:
                    validator = s["governor"]
                if validator and vault_address(artifact, token, validator, governor_for(validator)).lower() != v:
                    failures.append(f"vault {v} of validator {validator} was deployed with governor {s['governor']}, "
                                    f"not the expected {governor_for(validator)}")
                vaults[v] = {"validator": validator or "", "governor": s["governor"], "nonce": None, "source": "on-chain"}
                if not validator:
                    failures.append(f"vault {v} is a genuine GovernorDelegator, but it is not the vault of any validator "
                                    "under the governor policy (pass --governors, or --compare with the deposit list)")
    for v in funded_vaults:
        if v not in vaults:
            failures.append(f"{v} receives deposits but is neither deployed by these transactions nor a GovernorDelegator "
                            "on-chain" + ("" if rpc_url else " (pass --rpc-url to check deployed vaults)"))
    vault_addresses = set(vaults)
    seen = {}
    for d in deposits:
        key = (d["vault_address"], d["delegator_address"])
        if key in seen:
            failures.append(f"nonce {d['nonce']}: {d['delegator_address']} is credited twice in vault {d['vault_address']} "
                            f"(also nonce {seen[key]})")
        seen.setdefault(key, d["nonce"])
        if d["_amount"] == 0:
            failures.append(f"nonce {d['nonce']}: zero deposit for {d['delegator_address']}")
        if d["delegator_address"] in vault_addresses or d["delegator_address"] in (safe.lower(), token.lower()):
            failures.append(f"nonce {d['nonce']}: {d['delegator_address']} is a vault, the Safe or the token (audit L-01)")
        v = vaults.get(d["vault_address"], {})
        validator = (v.get("validator") or "").lower()
        d.update({"validator_address": validator, "validator_one1": common.to_one1(validator) if validator else "",
                  "delegator_one1": common.to_one1(d["delegator_address"]), "amount": one_text(d["_amount"]),
                  "amount_atto": str(d["_amount"]), "governor_address": (v.get("governor") or "").lower()})

    names, cutoff = {}, None
    if args.harmony_rpc:
        groups = defaultdict(list)
        for d in deposits:
            if d["validator_address"]:
                groups[d["validator_address"]].append((d["delegator_address"], d["_amount"]))
        try:
            cutoff, fails, warns, names = harmony_check(list(groups.items()), args.harmony_rpc)
        except (common.InputError, RuntimeError) as exc:
            common.die(str(exc))
            return
        failures += fails
        warnings += warns
    for d in deposits:
        reasons = []
        if cutoff is not None:
            have = cutoff.get((d["validator_address"], d["delegator_address"]))
            d["harmony_cutoff_atto"] = "" if have is None else str(have)
            if have is None:
                reasons.append("validator unknown")
            elif have != d["_amount"]:
                reasons.append("differs from Harmony cutoff")
        if expected is not None:
            want = expected.get((d["validator_address"], d["delegator_address"]))
            d["list_amount_atto"] = "" if want is None else str(want)
            if want is None:
                reasons.append("not in list")
            elif want != d["_amount"]:
                reasons.append("differs from list")
        d["check"] = "; ".join(reasons) or "ok"

    vault_rows = []
    for v, meta in sorted(vaults.items(), key=lambda kv: (kv[1]["nonce"] is None, kv[1]["nonce"] or 0, kv[0])):
        rows = [d for d in deposits if d["vault_address"] == v]
        validator = (meta["validator"] or "").lower()
        vault_rows.append({"validator_one1": common.to_one1(validator) if validator else "", "validator_address": validator,
                           "validator_name": names.get(validator, ""), "governor_address": meta["governor"].lower(),
                           "vault_address": v, "deployed": meta["source"] if meta["nonce"] is None else f"nonce {meta['nonce']}",
                           "deposits": len(rows), "total": one_text(sum(d["_amount"] for d in rows))})

    detail_fields = ["nonce", "call", "position", *DEPOSIT_COLUMNS, "amount_atto", "governor_address"]
    if cutoff is not None:
        detail_fields.append("harmony_cutoff_atto")
    if expected is not None:
        detail_fields.append("list_amount_atto")
    checked = cutoff is not None or expected is not None
    if checked:
        detail_fields.append("check")
    fmt = args.format or ("json" if args.out and args.out.suffix.lower() == ".json" else "csv")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
    sb.write_table(args.out, fmt, DEPOSIT_COLUMNS, deposits)
    if args.out:
        sb.write_table(args.out.with_name(f"{args.out.stem}-vaults{args.out.suffix}"), fmt,
                       ["validator_one1", "validator_address", "validator_name", "governor_address", "vault_address",
                        "deployed", "deposits", "total"], vault_rows)
        meta = {"safe": safe, "chain_id": args.chain_id, "token": token, "domain_hash": domain, "source": source,
                "transactions": transactions}
        sb.write_table(args.out.with_name(f"{args.out.stem}-details{args.out.suffix}"), fmt, detail_fields, deposits,
                       meta if fmt == "json" else None)

    total = sum(d["_amount"] for d in deposits)
    if info:
        note(f"safe             : {safe} on chain {args.chain_id}, version {info.get('version')}, "
             f"{info.get('threshold')} of {len(info.get('owners') or [])} owners, next nonce {info.get('nonce')}")
    note(f"read from        : {source}")
    note(f"domain hash      : {domain}")
    for t in transactions:
        status = "executed" if t["executed"] else "queued"
        note(f"nonce {t['nonce']:<10} : {t['deploys']} vault deployment(s), {t['deposits']} deposit(s) "
             f"{common.format_one(t['total'])} ONE, {status}, signed by {len(t['confirmations'])} of {t['required']}")
        note(f"  safeTxHash     : {t['safe_tx_hash']}")
        note(f"  message hash   : {t['message_hash']}")
    note(f"vaults           : {len(vaults)} ({sum(1 for m in vaults.values() if m['nonce'] is not None)} deployed by these "
         f"transactions); {len(funded_vaults)} receive deposits")
    note(f"deposits         : {len(deposits)} to {len({d['delegator_address'] for d in deposits})} delegators, "
         f"total {common.format_one(total)} ONE")
    if cutoff is not None:
        same = sum(1 for d in deposits if cutoff.get((d["validator_address"], d["delegator_address"])) == d["_amount"])
        note(f"--harmony-rpc    : {same} of {len(deposits)} deposits equal the delegation on Harmony at block "
             f"{HARMONY_CUTOFF_BLOCK:,}")
    if expected is not None:
        same = sum(1 for d in deposits if expected.get((d["validator_address"], d["delegator_address"])) == d["_amount"])
        queued = {(d["validator_address"], d["delegator_address"]) for d in deposits}
        missing = [k for k in expected if k not in queued]
        note(f"--compare        : {same} of {len(deposits)} deposits equal {', '.join(p.name for p in args.compare)} exactly; {len(missing)} of "
             f"its {len(expected)} rows ({common.format_one(sum(expected[k] for k in missing))} ONE) are not in these transactions")
    if not checked and deposits:
        note("amounts          : not compared with anything. The checks above cannot see an amount moved between two "
             "delegators of one vault; add --harmony-rpc https://api.s0.t.hmny.io (Harmony's own record) or --compare "
             "<deposit list>")
    bad = sum(1 for d in deposits if d["check"] != "ok") if checked else 0
    if bad:
        failures.append(f"{bad} deposit(s) do not match (see the check column of the details file)")
    if args.out:
        note(f"written to       : {args.out}, {args.out.stem}-vaults{args.out.suffix}, {args.out.stem}-details{args.out.suffix}")
    for m in warnings:
        note(f"warning: {m}")
    for m in failures:
        note(f"FAIL: {m}")
    if failures:
        common.die(f"{len(failures)} check(s) failed; do not sign until they are explained")
    note("OK: each Safe transaction hash above was recomputed from the call data listed; every call deploys the audited "
         "vault, approves a vault's exact total, or deposits into a genuine vault. Before signing, compare the hashes "
         "with the Safe web app and your wallet screen.")


def cmd_hash(args: argparse.Namespace) -> None:
    try:
        artifact = load_artifact()
        if args.safe_transaction:
            record = json.loads(Path(args.safe_transaction).read_text(encoding="utf-8"))
            safe, chain_id = common.normalize_address(record["safe"], "safe"), int(record["chainId"])
            nonce = args.nonce if args.nonce is not None else record["nonce"]
            if nonce is None:
                raise common.InputError(f"{args.safe_transaction} was built without a nonce; pass --nonce")
            tx = sb.SafeTx(common.normalize_address(record["to"], "to"), int(record["value"]), common.unhex(record["data"]),
                           int(record["operation"]), int(nonce), int(record["safeTxGas"]), int(record["baseGas"]),
                           int(record["gasPrice"]), record["gasToken"], record["refundReceiver"])
        else:
            if not (args.safe and args.chain_id and args.nonce is not None and args.to):
                raise common.InputError("need --safe, --chain-id, --nonce and --to (or --safe-transaction FILE)")
            safe, chain_id = common.normalize_address(args.safe, "--safe"), args.chain_id
            text = Path(args.data_file).read_text(encoding="utf-8") if args.data_file else (args.data or "0x")
            tx = sb.SafeTx(common.normalize_address(args.to, "--to"), 0, common.unhex("".join(text.split())), args.operation,
                           args.nonce)
        token = args.token or common.load_env(args.env).get("TOKEN_ADDRESS", "")
        if not token:
            raise common.InputError("need --token (or TOKEN_ADDRESS in .env)")
        token = common.normalize_address(token, "--token")
        governor_for, governor_source, _, _ = governor_policy(args)
        decoded = decode_vault_tx(tx, artifact, token)
        for validator, governor, _ in decoded.deploys:
            if governor.lower() != governor_for(validator).lower():
                decoded.problems.append(f"the vault of validator {validator} gets governor {governor}, expected "
                                        f"{governor_for(validator)} ({governor_source['policy']})")
    except (common.InputError, ValueError, KeyError) as exc:
        common.die(str(exc))
        return
    record = sb.tx_record(chain_id, safe, tx)
    print(f"safe             : {safe} on chain {chain_id}, nonce {tx.nonce}")
    print(f"to / operation   : {tx.to} / {tx.operation}")
    print(f"call data        : {len(tx.data)} bytes, keccak-256 {record['dataKeccak256']}")
    print(f"decoded          : {len(decoded.deploys)} vault deployment(s), {len(decoded.deposits)} deposit(s), "
          f"total {common.format_one(sum(d[3] for d in decoded.deposits))} ONE")
    for validator, governor, vault in decoded.deploys:
        print(f"  deploy         : vault {vault} for validator {validator}, governor {governor}")
    for p in decoded.problems:
        print(f"PROBLEM: {p}")
    print(f"domain hash      : {record['domainHash']}")
    print(f"message hash     : {record['messageHash']}")
    print(f"safeTxHash       : {record['safeTxHash']}")
    if args.list_out:
        rows = [[vault, delegator, str(amount), one_text(amount)] for _, vault, delegator, amount in decoded.deposits]
        write_csv(Path(args.list_out), ["vault_address", "delegator_address", "amount_atto", "amount_one"], rows)
        print(f"decoded list     : {args.list_out}")
    if decoded.problems:
        common.die(f"{len(decoded.problems)} problem(s); do not sign")


# ---------------------------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------------------------


def cmd_reconcile(args: argparse.Namespace) -> None:
    try:
        manifest = json.loads((args.out_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT:
            raise common.InputError(f"{args.out_dir}/manifest.json: unexpected format")
        artifact = load_artifact()
        token, safe = manifest["token"], manifest["safe"]
        vaults = read_plan_files(args.out_dir, artifact, token)
        rpc_url = args.rpc_url or common.load_env(args.env).get("RPC_URL")
        if not rpc_url:
            raise common.InputError("pass --rpc-url")
        rpc = common.Rpc(rpc_url)
        if rpc.chain_id() != manifest["chain_id"]:
            raise common.InputError("the RPC is on another chain than the manifest")
        block = rpc.block_number()
        states = vault_states(rpc, artifact, token, [v.address for v in vaults])
        live = [v for v in vaults if states[v.address.lower()]["deployed"]]
        custody = rpc_batch(rpc, [call_item(token, common.encode_call("balanceOf(address)", v.address)) for v in live])
        allowance = rpc_batch(rpc, [call_item(token, common.encode_call("allowance(address,address)", safe, v.address)) for v in live])
        pairs = [(v, d) for v in live for d in v.deposits]
        credits = rpc_batch(rpc, [call_item(v.address, common.encode_call("balanceOf(address)", d.delegator)) for v, d in pairs])
    except (common.InputError, RuntimeError, KeyError) as exc:
        common.die(str(exc))
        return
    credit = {(v.address, d.delegator): common.decode_word(c) for (v, d), c in zip(pairs, credits)}
    rows, failures, warnings = [], [], []
    counts = defaultdict(int)
    for v in vaults:
        s = states[v.address.lower()]
        row = {"validator_address": v.validator, "vault_address": v.address, "governor_address": v.governor,
               "planned_total_atto": str(v.total), "deposits": len(v.deposits)}
        if not s["deployed"]:
            row["status"] = "not deployed"
            counts["not deployed"] += 1
            rows.append(row)
            continue
        i = live.index(v)
        held, allowed = common.decode_word(custody[i]), common.decode_word(allowance[i])
        matching = sum(1 for d in v.deposits if credit[(v.address, d.delegator)] == d.amount)
        zero = sum(1 for d in v.deposits if credit[(v.address, d.delegator)] == 0)
        row.update({"custody_atto": str(held), "allowance_atto": str(allowed), "credits_matching": matching,
                    "governor_onchain": s.get("governor", ""), "fee_bps": s.get("fee", "")})
        problems = []
        if not (s.get("code_ok") and s.get("asset_ok")):
            problems.append("wrong code or token")
        elif s["governor"].lower() != v.governor.lower():
            problems.append("governor changed")
        if zero == len(v.deposits) and held == 0:
            row["status"] = "deployed, not funded"
            counts["deployed, not funded"] += 1
        elif matching == len(v.deposits) and held == v.total:
            row["status"] = "funded" if not problems else "funded; " + "; ".join(problems)
            counts["funded"] += 1
        else:
            higher = sum(1 for d in v.deposits if credit[(v.address, d.delegator)] > d.amount)
            problems.append(f"{matching} of {len(v.deposits)} credits match, {zero} are zero, {higher} are higher; "
                            f"custody {one_text(held)} of {one_text(v.total)} ONE")
            row["status"] = "; ".join(problems)
            counts["mismatch"] += 1
            (warnings if held >= v.total and zero == 0 else failures).append(f"vault {v.address} ({v.validator}): {row['status']}")
        if allowed:
            warnings.append(f"vault {v.address}: the Safe still allows it {one_text(allowed)} ONE")
        if s.get("fee"):
            warnings.append(f"vault {v.address}: feeBps is {s['fee']}")
        rows.append(row)
    out = args.out or (args.out_dir / f"reconcile-block-{block}.csv")
    fields = ["validator_address", "vault_address", "governor_address", "governor_onchain", "fee_bps", "deposits",
              "credits_matching", "planned_total_atto", "custody_atto", "allowance_atto", "status"]
    sb.write_table(out, "csv", fields, rows)
    print(f"block            : {block}")
    print(f"vaults           : {len(vaults)}: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    print(f"report           : {out}")
    for m in warnings:
        print(f"warning: {m}")
    for m in failures:
        print(f"FAIL: {m}")
    if failures:
        common.die(f"{len(failures)} vault(s) do not match the plan")
    if counts["funded"] == len(vaults):
        print("OK: every vault holds exactly its planned deposits, credits and custody")


# ---------------------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    b = sub.add_parser("build", help="prepare the Safe transactions that deploy and fund validator vaults")
    b.add_argument("--input", required=True, type=Path, action="append",
                   help="deposit list: CSV (or directory of CSVs) with validator_address, delegator_address (or "
                        "beneficiary_address) and amount_atto or amount_one columns. Repeat it to combine lists, e.g. the "
                        "initial-stage vault-shares.csv and the claim portal's confirmed-wallets-*-vault-shares.csv "
                        "(address, expected_shares_atto); each file's columns are detected on their own. A portal "
                        "export must hold only approved, pending rows (export it with --decision approved --vault "
                        "pending)")
    b.add_argument("--validator", action="append",
                   help="build only these validators' vaults (0x or one1; repeatable or comma-separated), e.g. a pilot")
    b.add_argument("--exclude-validator", action="append",
                   help="leave out these validators' vaults (0x or one1; repeatable or comma-separated), e.g. vaults "
                        "already piloted whose credits have since changed through test withdrawals")
    b.add_argument("--skip-funded", action="store_true",
                   help="with --rpc-url, leave out deposits whose vault already credits exactly that amount (they were "
                        "funded by an earlier build, such as a pilot); any other existing credit still stops the build")
    b.add_argument("--out-dir", required=True, type=Path, help="output directory, e.g. runs/vaults-initial")
    b.add_argument("--min-vault-one", default=DEFAULT_MIN_VAULT_ONE,
                   help=f"leave out vaults whose deposits total less than this many ONE (default {DEFAULT_MIN_VAULT_ONE}; "
                        "0 keeps every vault). They are listed in excluded-vaults.csv and excluded-deposits.csv. A vault "
                        "that already exists on-chain (seen with --rpc-url) is funded whatever its size")
    b.add_argument("--governors", type=Path,
                   help="CSV of validator_address and governor_address (or destination_address); validators not in it "
                        "get their own address. validator-vaults.csv works and its initial_assets_atto is checked")
    b.add_argument("--governor", help="one governor for every vault, e.g. the Safe as interim governor")
    b.add_argument("--safe", help="the Safe that holds the tokens (default: RESERVE_ADDRESS from .env)")
    b.add_argument("--token", help="the ONE token (default: TOKEN_ADDRESS from .env)")
    b.add_argument("--rpc-url", help="read the chain id, Safe version and nonce, and skip vaults already deployed")
    b.add_argument("--chain-id", type=int, help="default: from the RPC")
    b.add_argument("--nonce", type=int, help="Safe nonce of the first transaction (default: the Safe's next nonce)")
    b.add_argument("--no-nonce", action="store_true",
                   help="do not tie the files to a nonce (get the hashes after proposing with `show`)")
    b.add_argument("--safe-version", choices=sorted(sb.MULTISEND_CALL_ONLY), help="default: read from the Safe")
    b.add_argument("--phase", choices=["all", "deploy", "fund"], default="all",
                   help="deploy transactions, funding transactions, or both (default; deploy ones come first)")
    b.add_argument("--deploys-per-tx", type=int, default=DEFAULT_DEPLOYS_PER_TX,
                   help=f"vault deployments per Safe transaction (default {DEFAULT_DEPLOYS_PER_TX}, max {MAX_DEPLOYS_PER_TX})")
    b.add_argument("--gas-target", type=int, default=DEFAULT_GAS_TARGET,
                   help=f"estimated gas per funding transaction (default {DEFAULT_GAS_TARGET:,})")
    b.add_argument("--max-rows-per-call", type=int, default=DEFAULT_MAX_ROWS_PER_CALL,
                   help=f"deposits per depositBatch call; larger vaults are split (default {DEFAULT_MAX_ROWS_PER_CALL})")
    b.add_argument("--gas-price-gwei", default="0.1,0.3,1", help="gas prices for the printed cost estimate")
    b.add_argument("--label", default="", help="name shown in the Transaction Builder and the signing sheet")
    b.add_argument("--validator-column", help="name of the validator column (auto-detected by default)")
    b.add_argument("--delegator-column", help="name of the delegator column (auto-detected by default)")
    b.add_argument("--amount-column", help="name of the amount column (auto-detected by default)")
    b.add_argument("--amount-unit", choices=["atto", "one"], help="unit of the amount column, if its name does not say")
    b.add_argument("--merge-duplicates", action="store_true", help="add up a delegator repeated in one vault")
    b.add_argument("--allow-existing-credits", action="store_true",
                   help="with --rpc-url, fund delegators who already hold a credit in their vault (a top-up); "
                        "without it the build stops, so a funded list cannot be credited twice")
    b.add_argument("--expect-total", help="stop unless the funded total equals this many ONE")
    b.add_argument("--expect-count", type=int, help="stop unless exactly this many deposits are funded")
    b.add_argument("--expect-vaults", type=int, help="stop unless exactly this many vaults are funded")
    b.add_argument("--force", action="store_true", help="replace an existing output directory")
    b.add_argument("--env", type=Path, default=common.default_env_path())
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="recompute every file and hash; optionally check Ethereum and Harmony")
    v.add_argument("--out-dir", required=True, type=Path)
    v.add_argument("--rpc-url", help="check the Safe, deployer, token, vaults, governors and delegators on Ethereum")
    v.add_argument("--harmony-rpc", help="check every deposit against Harmony's delegations at the cutoff block, "
                                         "e.g. https://api.s0.t.hmny.io")
    v.add_argument("--offline", action="store_true", help="skip chain checks even if RPC_URL is set")
    v.add_argument("--env", type=Path, default=common.default_env_path())
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("show", help="every vault and deposit of the Safe transactions in the queue (CSV or JSON)")
    s.add_argument("--safe", help="the Safe (default: RESERVE_ADDRESS from .env)")
    s.add_argument("--token", help="the ONE token (default: TOKEN_ADDRESS from .env)")
    s.add_argument("--chain-id", type=int, default=1)
    s.add_argument("--nonce", action="append", help="only these nonces, e.g. 40 or 40-55 (repeatable); default: the queue")
    s.add_argument("--safe-tx-hash", action="append", help="only this Safe transaction (repeatable)")
    s.add_argument("--rpc-url", help="Ethereum RPC, to confirm vaults deployed earlier are genuine GovernorDelegators")
    s.add_argument("--harmony-rpc", help="check every deposit against Harmony's delegations at the cutoff block")
    s.add_argument("--compare", type=Path, action="append",
                   help="deposit list the deposits must match exactly (validator, delegator, amount); repeatable")
    s.add_argument("--amount-unit", choices=["atto", "one"], help="unit of the --compare amount column, if its name does not say")
    s.add_argument("--governors", type=Path,
                   help="expected governors, as for build (default: every vault's governor is its validator's own address)")
    s.add_argument("--governor", help="the one expected governor of every vault, as for build")
    s.add_argument("--out", type=Path,
                   help="write the deposits (validator_one1,validator_address,vault_address,delegator_one1,"
                        "delegator_address,amount) here, plus <name>-vaults.csv and <name>-details.csv")
    s.add_argument("--format", choices=["csv", "json"])
    s.add_argument("--service-url", help="Safe Transaction Service base URL (default: Safe's hosted service)")
    s.add_argument("--api-key", help="Safe API key, if the service asks for one (default: SAFE_API_KEY)")
    s.add_argument("--tx-json", type=Path, help="read the transaction(s) from a saved JSON file instead of the service")
    s.add_argument("--env", type=Path, default=common.default_env_path())
    s.set_defaults(func=cmd_show)

    h = sub.add_parser("hash", help="hashes and decoded vault calls of one Safe transaction")
    h.add_argument("--safe-transaction", help="a safe-transaction.json written by build")
    h.add_argument("--safe")
    h.add_argument("--token", help="the ONE token (default: TOKEN_ADDRESS from .env)")
    h.add_argument("--chain-id", type=int)
    h.add_argument("--nonce", type=int, help="the Safe nonce (required for a file built with --no-nonce)")
    h.add_argument("--to")
    h.add_argument("--data", help="call data as hex")
    h.add_argument("--data-file", help="file containing the call data as hex")
    h.add_argument("--operation", type=int, choices=[sb.CALL, sb.DELEGATECALL], default=sb.DELEGATECALL)
    h.add_argument("--list-out", help="write the decoded deposits to this CSV")
    h.add_argument("--governors", type=Path, help="expected governors, as for build (default: the validator's own address)")
    h.add_argument("--governor", help="the one expected governor of every vault, as for build")
    h.add_argument("--env", type=Path, default=common.default_env_path())
    h.set_defaults(func=cmd_hash)

    r = sub.add_parser("reconcile", help="after execution: compare every vault and credit on-chain with the plan")
    r.add_argument("--out-dir", required=True, type=Path)
    r.add_argument("--rpc-url", help="Ethereum RPC (default: RPC_URL from .env)")
    r.add_argument("--out", type=Path, help="report CSV (default: <out-dir>/reconcile-block-<block>.csv)")
    r.add_argument("--env", type=Path, default=common.default_env_path())
    r.set_defaults(func=cmd_reconcile)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
