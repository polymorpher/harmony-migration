#!/usr/bin/env python3

"""Build exact WONE redistribution and retained-reserve source routes."""

import argparse
import csv
import hashlib
import json
import os


WONE_ADDRESS = "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a"
FIELDS = (
    "route_id",
    "priority",
    "source_address",
    "destination_id",
    "destination_address",
    "amount_atto",
    "allocation_method",
    "reason",
    "evidence",
    "notes",
)
WONE_ROUTE_IDS = {
    "wone-reserve-custody",
    "wone-priority-holder-redistribution",
    "wone-reserve-remainder-not-issued",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wone-summary", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    args = parse_args()
    if os.path.realpath(args.input) == os.path.realpath(args.output):
        raise ValueError(
            "--input and --output must differ so the recorded input remains "
            "available for provenance verification"
        )
    for path in (args.output, args.summary):
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path + ".partial")
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    with open(args.wone_summary, encoding="utf-8") as source:
        wone = json.load(source)
    if wone.get("status") != "passed":
        raise ValueError("WONE qualification summary did not pass")
    reserve = int(wone["wone_reserve_atto"])
    redistributed = int(wone["wone_redistributed_to_priority_atto"])
    retained = int(wone["wone_retained_not_issued_atto"])
    if (
        min(reserve, redistributed, retained) < 0
        or redistributed + retained != reserve
    ):
        raise ValueError("WONE reserve split does not close")

    input_sha256 = file_sha256(args.input)
    rows = []
    with open(args.input, newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError(f"unexpected route fields: {reader.fieldnames}")
        for line, row in enumerate(reader, start=2):
            if row["route_id"] in WONE_ROUTE_IDS:
                if row["source_address"].lower() != WONE_ADDRESS:
                    raise ValueError(
                        f"WONE route has wrong source at line {line}"
                    )
                continue
            rows.append(row)

    evidence = (
        "artifacts/cutoff-20260910/claims/"
        "all-address-migration-claims-cutoff-summary.json"
    )
    if redistributed:
        rows.append(
            {
                "route_id": "wone-priority-holder-redistribution",
                "priority": "400",
                "source_address": WONE_ADDRESS,
                "destination_id": "wone-holder-redistribution",
                "destination_address": "",
                "amount_atto": str(redistributed),
                "allocation_method": "wallet_only",
                "reason": "wone_priority_holder_redistribution",
                "evidence": evidence,
                "notes": (
                    "offsets WONE amounts included in current qualified-holder "
                    "wallet airdrops"
                ),
            }
        )
    if retained:
        rows.append(
            {
                "route_id": "wone-reserve-remainder-not-issued",
                "priority": "401",
                "source_address": WONE_ADDRESS,
                "destination_id": "not-issuing",
                "destination_address": "",
                "amount_atto": str(retained),
                "allocation_method": "wallet_only",
                "reason": "wone_reserve_remainder_retained_not_issued",
                "evidence": evidence,
                "notes": (
                    "below-threshold and excluded WONE backing retained in "
                    "the 2050 premint reserve"
                ),
            }
        )
    rows.sort(key=lambda row: (int(row["priority"]), row["route_id"]))
    with open(args.output + ".partial", "x", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)

    result = {
        "schema_version": 1,
        "status": "passed",
        "wone_summary": args.wone_summary,
        "wone_summary_sha256": file_sha256(args.wone_summary),
        "input": args.input,
        "input_sha256": input_sha256,
        "wone_reserve_atto": str(reserve),
        "wone_redistributed_to_holders_atto": str(redistributed),
        "wone_retained_not_issued_atto": str(retained),
        "output": args.output,
        "output_sha256": file_sha256(args.output),
        "route_rows": len(rows),
    }
    with open(args.summary + ".partial", "x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.summary + ".partial", args.summary)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
