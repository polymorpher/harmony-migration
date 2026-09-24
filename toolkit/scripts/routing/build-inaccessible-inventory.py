#!/usr/bin/env python3

"""Turn reviewed inaccessible addresses into exact non-issuance inventory rows.

An address is inaccessible when nobody can ever move funds at it, on Harmony
or at the same address on Ethereum, where the replacement token would be
delivered. Its whole cutoff claim is not issued. The output uses the same
schema and `burn_or_inaccessible` category as the existing reviewed inventory,
so every consumer of that inventory can read it as an additional file.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


ATTO_PER_ONE = 10**18
CATEGORY = "burn_or_inaccessible"
FIELDS = (
    "address_bech32",
    "address_hex",
    "category",
    "cutoff_claim_atto",
    "cutoff_claim_one",
    "not_issued_atto",
    "not_issued_one",
    "remaining_original_address_allocation_atto",
    "remaining_original_address_allocation_one",
    "evidence",
    "decision",
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True, help="reviewed CSV: address_hex, reason, evidence")
    parser.add_argument("--claims", required=True, help="all-address migration claims ledger at cutoff")
    parser.add_argument("--other-inventory", action="append", default=[],
                        help="existing non-issuance inventory; an overlap is an error (repeatable)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def one(value):
    whole, fraction = divmod(int(value), ATTO_PER_ONE)
    return f"{whole}.{fraction:018d}"


def write_atomic(path, write, replace):
    partial = path + ".partial"
    if os.path.exists(partial):
        raise FileExistsError(partial)
    if os.path.exists(path) and not replace:
        raise FileExistsError(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(partial, "x", newline="", encoding="utf-8") as output:
        write(output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def main():
    args = parse_args()
    review = []
    with open(args.review, newline="", encoding="utf-8") as source:
        for line, row in enumerate(csv.DictReader(source), start=2):
            address = row["address_hex"].strip().lower()
            if len(address) != 42 or not address.startswith("0x"):
                raise ValueError(f"{args.review}:{line}: invalid address")
            if not row["reason"].strip() or not row["evidence"].strip():
                raise ValueError(f"{args.review}:{line}: reason and evidence are required")
            if address in {r["address"] for r in review}:
                raise ValueError(f"{args.review}:{line}: duplicate address")
            review.append({"address": address, "reason": row["reason"].strip(), "evidence": row["evidence"].strip()})

    listed = {}
    for path in args.other_inventory:
        with open(path, newline="") as source:
            for row in csv.DictReader(source):
                listed[row["address_hex"].lower()] = path
    for entry in review:
        if entry["address"] in listed:
            raise ValueError(f"{entry['address']} is already in {listed[entry['address']]}")

    wanted = {entry["address"] for entry in review}
    claims = {}
    with open(args.claims, newline="") as source:
        for row in csv.DictReader(source):
            address = row["address"].lower()
            if address in wanted:
                claims[address] = int(row["total_claim_atto"])
    missing = wanted - set(claims)
    if missing:
        raise ValueError(f"no cutoff claim for: {sorted(missing)}")

    rows = []
    for entry in sorted(review, key=lambda e: e["address"]):
        claim = claims[entry["address"]]
        rows.append({
            "address_bech32": lib.hex_to_bech32(entry["address"]),
            "address_hex": entry["address"],
            "category": CATEGORY,
            "cutoff_claim_atto": str(claim),
            "cutoff_claim_one": one(claim),
            "not_issued_atto": str(claim),
            "not_issued_one": one(claim),
            "remaining_original_address_allocation_atto": "0",
            "remaining_original_address_allocation_one": one(0),
            "evidence": entry["evidence"],
            "decision": f"inaccessible: {entry['reason']}; whole cutoff claim not issued",
        })

    def write_rows(output):
        writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    write_atomic(args.output, write_rows, args.replace)
    total = sum(int(row["not_issued_atto"]) for row in rows)
    summary = {
        "status": "passed",
        "rule": "reviewed inaccessible addresses: the whole cutoff claim is not issued",
        "category": CATEGORY,
        "rows": len(rows),
        "not_issued_atto": str(total),
        "not_issued_one": one(total),
        "inputs": {
            "review": {"path": args.review, "sha256": sha256(args.review)},
            "claims": {"path": args.claims, "sha256": sha256(args.claims)},
            "other_inventories": [{"path": p, "sha256": sha256(p)} for p in args.other_inventory],
        },
        "output": args.output,
        "output_sha256": sha256(args.output),
    }

    def write_summary(output):
        json.dump(summary, output, indent=2, sort_keys=True)
        output.write("\n")

    write_atomic(args.summary, write_summary, args.replace)
    print(json.dumps({k: summary[k] for k in ("rows", "not_issued_one", "output_sha256")}))


if __name__ == "__main__":
    main()
