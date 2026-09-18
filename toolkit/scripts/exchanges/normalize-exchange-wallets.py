#!/usr/bin/env python3

"""Normalize private exchange wallet submissions into deterministic CSVs."""

import argparse
import csv
import hashlib
import json
import os
import posixpath
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


XLSX_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
FIELDS = (
    "exchange_id",
    "source_file",
    "source_sha256",
    "source_sheet",
    "source_row",
    "source_row_id",
    "source_address_raw",
    "address_hex",
    "address_one",
    "submitted_balance_raw",
    "submitted_balance_unit",
    "submitted_balance_atto",
    "configured_destination",
    "configured_destination_status",
    "authorization_type",
    "authorization_destination",
    "authorization_message_sha256",
    "authorization_signature_sha256",
    "authorization_status",
)
FORBIDDEN_XLSX_PARTS = (
    "externalLinks/",
    "vbaProject.bin",
    "macrosheets/",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--destinations-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_address(value, context):
    address = lib.any_to_hex(str(value or "").strip())
    if address is None:
        raise ValueError(f"{context}: invalid address {value!r}")
    try:
        raw = bytes.fromhex(address[2:])
    except ValueError as error:
        raise ValueError(f"{context}: invalid address {value!r}") from error
    if len(raw) != 20:
        raise ValueError(f"{context}: invalid address {value!r}")
    return address


def atomic_csv(path, rows, replace):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    with partial.open("x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def atomic_json(path, value, replace):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(path) + ".partial")
    if partial.exists() or (path.exists() and not replace):
        raise FileExistsError(path)
    with partial.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def column_number(reference):
    match = re.fullmatch(r"([A-Z]+)[0-9]+", reference or "")
    if match is None:
        raise ValueError(f"invalid XLSX cell reference: {reference!r}")
    number = 0
    for character in match.group(1):
        number = number * 26 + ord(character) - ord("A") + 1
    return number - 1


def shared_strings(archive):
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ElementTree.parse(archive.open(path)).getroot()
    return [
        "".join(
            node.text or ""
            for node in item.iter(f"{{{XLSX_MAIN}}}t")
        )
        for item in root.findall(f"{{{XLSX_MAIN}}}si")
    ]


def workbook_sheet_path(archive, expected_name):
    workbook = ElementTree.parse(archive.open("xl/workbook.xml")).getroot()
    relationships = ElementTree.parse(
        archive.open("xl/_rels/workbook.xml.rels")
    ).getroot()
    targets = {
        relation.attrib["Id"]: relation.attrib["Target"]
        for relation in relationships.findall("{*}Relationship")
    }
    found = None
    for sheet in workbook.findall(f".//{{{XLSX_MAIN}}}sheet"):
        name = sheet.attrib.get("name", "")
        state = sheet.attrib.get("state", "visible")
        if state != "visible":
            raise ValueError(f"XLSX contains non-visible sheet {name!r}")
        if name != expected_name:
            continue
        relationship_id = sheet.attrib[
            f"{{{XLSX_DOCUMENT_REL}}}id"
        ]
        target = targets[relationship_id]
        found = (
            target.lstrip("/")
            if target.startswith("/xl/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
    if found is None:
        raise ValueError(f"XLSX is missing sheet {expected_name!r}")
    return found


def xlsx_rows(path, sheet_name):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            if any(part in name for part in FORBIDDEN_XLSX_PARTS):
                raise ValueError(f"{path}: forbidden XLSX part {name}")
        strings = shared_strings(archive)
        sheet_path = workbook_sheet_path(archive, sheet_name)
        header = None
        rows = []
        physical_data_rows = 0
        blank_data_rows = 0
        for _event, element in ElementTree.iterparse(
            archive.open(sheet_path), events=("end",)
        ):
            if element.tag != f"{{{XLSX_MAIN}}}row":
                continue
            row_number = int(element.attrib.get("r", len(rows) + 1))
            values = {}
            for cell in element.findall(f"{{{XLSX_MAIN}}}c"):
                if cell.find(f"{{{XLSX_MAIN}}}f") is not None:
                    raise ValueError(
                        f"{path}:{sheet_name}!{cell.attrib.get('r')}: "
                        "formulas are not allowed"
                    )
                index = column_number(cell.attrib.get("r", ""))
                cell_type = cell.attrib.get("t", "")
                value = ""
                if cell_type == "inlineStr":
                    value = "".join(
                        node.text or ""
                        for node in cell.iter(f"{{{XLSX_MAIN}}}t")
                    )
                else:
                    raw = cell.find(f"{{{XLSX_MAIN}}}v")
                    if raw is not None and raw.text is not None:
                        value = raw.text
                        if cell_type == "s":
                            try:
                                value = strings[int(value)]
                            except (IndexError, ValueError) as error:
                                raise ValueError(
                                    f"{path}:{sheet_name}!"
                                    f"{cell.attrib.get('r')}: "
                                    "invalid shared-string reference"
                                ) from error
                values[index] = value
            width = max(values, default=-1) + 1
            populated = [values.get(index, "") for index in range(width)]
            if header is None:
                if not any(str(value).strip() for value in populated):
                    element.clear()
                    continue
                header = [str(value).strip() for value in populated]
                if not all(header) or len(set(header)) != len(header):
                    raise ValueError(f"{path}: invalid XLSX header")
                element.clear()
                continue
            physical_data_rows += 1
            record = {
                field: str(values.get(index, "")).strip()
                for index, field in enumerate(header)
            }
            if any(record.values()):
                rows.append((row_number, record))
            else:
                blank_data_rows += 1
            element.clear()
        if header is None:
            raise ValueError(f"{path}: XLSX has no populated header")
        return header, rows, physical_data_rows, blank_data_rows


def point_add(left, right):
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % SECP256K1_P == 0:
        return None
    if left == right:
        slope = (
            (3 * x1 * x1)
            * pow(2 * y1, -1, SECP256K1_P)
            % SECP256K1_P
        )
    else:
        slope = (
            (y2 - y1)
            * pow((x2 - x1) % SECP256K1_P, -1, SECP256K1_P)
            % SECP256K1_P
        )
    x3 = (slope * slope - x1 - x2) % SECP256K1_P
    y3 = (slope * (x1 - x3) - y1) % SECP256K1_P
    return x3, y3


def jacobian_double(point):
    x, y, z = point
    if not y or not z:
        return 0, 1, 0
    y_squared = y * y % SECP256K1_P
    slope = 3 * x * x % SECP256K1_P
    offset = 4 * x * y_squared % SECP256K1_P
    x3 = (slope * slope - 2 * offset) % SECP256K1_P
    y3 = (
        slope * (offset - x3) - 8 * y_squared * y_squared
    ) % SECP256K1_P
    z3 = 2 * y * z % SECP256K1_P
    return x3, y3, z3


def jacobian_add(left, right):
    x1, y1, z1 = left
    x2, y2, z2 = right
    if not z1:
        return right
    if not z2:
        return left
    z1_squared = z1 * z1 % SECP256K1_P
    z2_squared = z2 * z2 % SECP256K1_P
    u1 = x1 * z2_squared % SECP256K1_P
    u2 = x2 * z1_squared % SECP256K1_P
    s1 = y1 * z2 * z2_squared % SECP256K1_P
    s2 = y2 * z1 * z1_squared % SECP256K1_P
    if u1 == u2:
        return jacobian_double(left) if s1 == s2 else (0, 1, 0)
    difference = (u2 - u1) % SECP256K1_P
    rise = (s2 - s1) % SECP256K1_P
    difference_squared = difference * difference % SECP256K1_P
    difference_cubed = difference_squared * difference % SECP256K1_P
    u1_difference_squared = u1 * difference_squared % SECP256K1_P
    x3 = (
        rise * rise - difference_cubed - 2 * u1_difference_squared
    ) % SECP256K1_P
    y3 = (
        rise * (u1_difference_squared - x3) - s1 * difference_cubed
    ) % SECP256K1_P
    z3 = difference * z1 * z2 % SECP256K1_P
    return x3, y3, z3


def affine_from_jacobian(point):
    x, y, z = point
    if not z:
        return None
    inverse = pow(z, -1, SECP256K1_P)
    inverse_squared = inverse * inverse % SECP256K1_P
    return (
        x * inverse_squared % SECP256K1_P,
        y * inverse_squared * inverse % SECP256K1_P,
    )


def scalar_multiply(scalar, point):
    if scalar < 0:
        scalar %= SECP256K1_N
    result = (0, 1, 0)
    addend = (point[0], point[1], 1)
    while scalar:
        if scalar & 1:
            result = jacobian_add(result, addend)
        addend = jacobian_double(addend)
        scalar >>= 1
    return affine_from_jacobian(result)


def eip191_hash(message):
    encoded = message.encode("utf-8")
    prefix = f"\x19Ethereum Signed Message:\n{len(encoded)}".encode()
    return lib.keccak256(prefix + encoded)


def recover_eip191_address(message, signature, context):
    text = str(signature or "").strip()
    if text.startswith("0x"):
        text = text[2:]
    if len(text) != 130 or any(
        character not in "0123456789abcdefABCDEF" for character in text
    ):
        raise ValueError(f"{context}: signature is not 65-byte hex")
    raw = bytes.fromhex(text)
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:64], "big")
    recovery_id = raw[64]
    if recovery_id in (27, 28):
        recovery_id -= 27
    if recovery_id not in (0, 1):
        raise ValueError(f"{context}: unsupported recovery id")
    if not 0 < r < SECP256K1_N or not 0 < s <= SECP256K1_N // 2:
        raise ValueError(f"{context}: invalid or non-canonical signature")
    x = r
    alpha = (pow(x, 3, SECP256K1_P) + 7) % SECP256K1_P
    beta = pow(alpha, (SECP256K1_P + 1) // 4, SECP256K1_P)
    y = beta if beta % 2 == recovery_id else SECP256K1_P - beta
    recovered_r = (x, y)
    if scalar_multiply(SECP256K1_N, recovered_r) is not None:
        raise ValueError(f"{context}: invalid recovery point")
    digest = int.from_bytes(eip191_hash(message), "big")
    inverse_r = pow(r, -1, SECP256K1_N)
    public_key = scalar_multiply(
        inverse_r,
        point_add(
            scalar_multiply(s, recovered_r),
            scalar_multiply(-digest, SECP256K1_G),
        ),
    )
    if public_key is None:
        raise ValueError(f"{context}: could not recover signer")
    encoded_key = public_key[0].to_bytes(32, "big") + public_key[1].to_bytes(
        32, "big"
    )
    return "0x" + lib.keccak256(encoded_key)[-20:].hex()


def row_identity(source_sha256, sheet, row_number):
    value = f"{source_sha256}:{sheet}:{row_number}".encode()
    return hashlib.sha256(value).hexdigest()


def destination_status(config, destinations_dir):
    filename = config.get("destination_file")
    if not filename:
        if config["destination_required"]:
            return "", "missing_file", None
        return "", "not_required_same_address", None
    path = destinations_dir / filename
    if not path.is_file():
        return "", "missing_file", path
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        return "", "blank", path
    address = normalize_address(value, str(path))
    return lib.to_checksum(address), "configured", path


def base_row(
    exchange_id,
    raw_path,
    raw_sha256,
    sheet,
    row_number,
    source_address,
    destination,
    destination_state,
):
    address = normalize_address(
        source_address,
        f"{raw_path}:{sheet}:{row_number}",
    )
    return {
        "exchange_id": exchange_id,
        "source_file": raw_path.name,
        "source_sha256": raw_sha256,
        "source_sheet": sheet,
        "source_row": str(row_number),
        "source_row_id": row_identity(raw_sha256, sheet, row_number),
        "source_address_raw": source_address,
        "address_hex": lib.to_checksum(address),
        "address_one": lib.hex_to_bech32(address),
        "submitted_balance_raw": "",
        "submitted_balance_unit": "",
        "submitted_balance_atto": "",
        "configured_destination": destination,
        "configured_destination_status": destination_state,
        "authorization_type": "none",
        "authorization_destination": "",
        "authorization_message_sha256": "",
        "authorization_signature_sha256": "",
        "authorization_status": "not_provided",
    }


def parse_xlsx_address(config, path, raw_sha256, destination, state):
    header, source_rows, physical, blank = xlsx_rows(
        path, config["worksheet"]
    )
    if header != ["address"]:
        raise ValueError(f"{path}: expected one address column, got {header}")
    rows = [
        base_row(
            config["id"],
            path,
            raw_sha256,
            config["worksheet"],
            row_number,
            record["address"],
            destination,
            state,
        )
        for row_number, record in source_rows
    ]
    return rows, physical, blank


def parse_okx(config, path, raw_sha256, destination, state):
    rows = []
    physical = 0
    blank = 0
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["address", "balance_raw"]:
            raise ValueError(f"{path}: unexpected fields {reader.fieldnames}")
        for line, record in enumerate(reader, start=2):
            physical += 1
            if not any(str(value or "").strip() for value in record.values()):
                blank += 1
                continue
            amount = str(record["balance_raw"] or "").strip()
            if not amount.isdigit() or int(amount) <= 0:
                raise ValueError(f"{path}:{line}: invalid atto-ONE balance")
            row = base_row(
                config["id"],
                path,
                raw_sha256,
                "",
                line,
                record["address"],
                destination,
                state,
            )
            row.update(
                {
                    "submitted_balance_raw": amount,
                    "submitted_balance_unit": "atto-ONE",
                    "submitted_balance_atto": amount,
                }
            )
            rows.append(row)
    return rows, physical, blank


def message_addresses(message):
    candidates = re.findall(r"0x[0-9a-fA-F]{40}", message)
    candidates.extend(
        re.findall(r"\bone1[023456789acdefghjklmnpqrstuvwxyz]{38}\b", message)
    )
    return {
        normalize_address(candidate, "signed message")
        for candidate in candidates
    }


def parse_mexc(config, path, raw_sha256, destination, state):
    header, source_rows, physical, blank = xlsx_rows(
        path, config["worksheet"]
    )
    expected = [
        "oneAddress",
        "ethAddress",
        "amount",
        "message",
        "signature(EIP-191)",
    ]
    if header != expected:
        raise ValueError(f"{path}: unexpected fields {header}")
    rows = []
    configured = normalize_address(destination, str(path))
    for row_number, record in source_rows:
        context = f"{path}:{config['worksheet']}:{row_number}"
        one_address = normalize_address(record["oneAddress"], context)
        evm_address = normalize_address(record["ethAddress"], context)
        if one_address != evm_address:
            raise ValueError(f"{context}: ONE and EVM addresses differ")
        amount = lib.one_str_to_atto(record["amount"])
        if amount <= 0:
            raise ValueError(f"{context}: non-positive amount")
        message = record["message"]
        authorized = message_addresses(message)
        if authorized != {configured}:
            raise ValueError(
                f"{context}: signed message does not name configured destination"
            )
        signature = record["signature(EIP-191)"]
        signer = recover_eip191_address(message, signature, context)
        if signer != evm_address:
            raise ValueError(f"{context}: signature signer mismatch")
        row = base_row(
            config["id"],
            path,
            raw_sha256,
            config["worksheet"],
            row_number,
            record["oneAddress"],
            destination,
            state,
        )
        row.update(
            {
                "submitted_balance_raw": record["amount"],
                "submitted_balance_unit": "ONE",
                "submitted_balance_atto": str(amount),
                "authorization_type": "EIP-191",
                "authorization_destination": lib.to_checksum(configured),
                "authorization_message_sha256": hashlib.sha256(
                    message.encode("utf-8")
                ).hexdigest(),
                "authorization_signature_sha256": hashlib.sha256(
                    signature.encode("utf-8")
                ).hexdigest(),
                "authorization_status": "verified",
            }
        )
        rows.append(row)
    return rows, physical, blank


def load_policy(path):
    with open(path, encoding="utf-8") as source:
        policy = json.load(source)
    if policy.get("schema_version") != 1:
        raise ValueError("unsupported exchange policy schema")
    if int(policy.get("minimum_atto", 0)) <= 0:
        raise ValueError("exchange policy has invalid threshold")
    seen = set()
    for config in policy.get("exchanges", []):
        exchange_id = config.get("id", "")
        if (
            not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", exchange_id)
            or exchange_id in seen
        ):
            raise ValueError(f"invalid or duplicate exchange id {exchange_id!r}")
        seen.add(exchange_id)
        if config.get("delivery_policy") not in {
            "automatic_threshold",
            "manual_current_claim",
        }:
            raise ValueError(f"invalid delivery policy for {exchange_id}")
    if not seen:
        raise ValueError("exchange policy has no exchanges")
    return policy


def main():
    args = parse_args()
    policy_path = Path(args.policy)
    raw_dir = Path(args.raw_dir)
    destinations_dir = Path(args.destinations_dir)
    output_dir = Path(args.output_dir)
    policy = load_policy(policy_path)
    parsers = {
        "xlsx_address": parse_xlsx_address,
        "xlsx_mexc_eip191": parse_mexc,
        "csv_okx_atto": parse_okx,
    }
    summaries = {}
    all_addresses = {}
    output_paths = []
    for config in policy["exchanges"]:
        exchange_id = config["id"]
        destination, destination_state, destination_path = destination_status(
            config, destinations_dir
        )
        raw_name = config.get("raw_file")
        raw_path = raw_dir / raw_name if raw_name else None
        rows = []
        raw_sha256 = None
        physical_rows = 0
        blank_rows = 0
        inventory_status = config["raw_inventory_status"]
        if raw_path is not None:
            if not raw_path.is_file():
                inventory_status = "missing_file"
            else:
                raw_sha256 = file_sha256(raw_path)
                parser = parsers.get(config.get("input_type"))
                if parser is None:
                    raise ValueError(
                        f"unsupported input type for {exchange_id}"
                    )
                rows, physical_rows, blank_rows = parser(
                    config,
                    raw_path,
                    raw_sha256,
                    destination,
                    destination_state,
                )
                inventory_status = "received"
        seen = set()
        for row in rows:
            address = row["address_hex"].lower()
            if address in seen:
                raise ValueError(
                    f"{exchange_id}: duplicate normalized address {address}"
                )
            seen.add(address)
            if address in all_addresses:
                raise ValueError(
                    "cross-exchange address overlap: "
                    f"{all_addresses[address]} and {exchange_id}"
                )
            all_addresses[address] = exchange_id
        rows.sort(key=lambda row: bytes.fromhex(row["address_hex"][2:]))
        output_path = output_dir / f"{exchange_id}.csv"
        atomic_csv(output_path, rows, args.replace)
        output_paths.append(output_path)
        summaries[exchange_id] = {
            "authorization_verified_rows": sum(
                row["authorization_status"] == "verified" for row in rows
            ),
            "blank_source_rows": blank_rows,
            "configured_destination": destination,
            "configured_destination_status": destination_state,
            "delivery_policy": config["delivery_policy"],
            "destination_file": (
                str(destination_path) if destination_path is not None else None
            ),
            "destination_file_sha256": (
                file_sha256(destination_path)
                if destination_path is not None and destination_path.is_file()
                else None
            ),
            "display_name": config["display_name"],
            "inventory_status": inventory_status,
            "normalized_rows": len(rows),
            "output": str(output_path),
            "output_sha256": file_sha256(output_path),
            "physical_source_rows": physical_rows,
            "raw_file": str(raw_path) if raw_path is not None else None,
            "raw_file_sha256": raw_sha256,
            "submitted_balance_rows": sum(
                bool(row["submitted_balance_atto"]) for row in rows
            ),
        }
    hold_reasons = []
    for exchange_id, summary in summaries.items():
        if summary["inventory_status"] != "received":
            hold_reasons.append(f"{exchange_id}:wallet_inventory")
        if summary["configured_destination_status"] in {
            "blank",
            "missing_file",
        }:
            hold_reasons.append(f"{exchange_id}:destination")
    result = {
        "schema_version": 1,
        "status": "passed" if not hold_reasons else "hold",
        "hold_reasons": hold_reasons,
        "policy": str(policy_path),
        "policy_sha256": file_sha256(policy_path),
        "minimum_atto": str(policy["minimum_atto"]),
        "cutoff_time_utc": policy["cutoff_time_utc"],
        "normalized_addresses": len(all_addresses),
        "cross_exchange_overlaps": 0,
        "exchanges": summaries,
    }
    atomic_json(args.summary, result, args.replace)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
