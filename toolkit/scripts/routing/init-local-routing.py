#!/usr/bin/env python3

"""Create ignored local routing files without any distributable destination."""

import argparse
import csv
import os
from pathlib import Path


ROUTE_FIELDS = (
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
GOVERNOR_FIELDS = (
    "validator_address",
    "destination_id",
    "destination_address",
    "status",
    "reason",
    "evidence",
    "notes",
)


def write_csv(path, fields, rows):
    if path.exists():
        raise FileExistsError(path)
    with path.open("x", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="routing/local")
    args = parser.parse_args()
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    targets = [
        directory / name
        for name in (
            "manual.csv",
            "multisigs.csv",
            "lost-wallets.csv",
            "frozen-wallets.csv",
            "bridge-reserves.csv",
            "destinations.csv",
            "validator-governors.csv",
            "policy-decisions.csv",
        )
    ]
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise FileExistsError(
            "refusing partial overwrite; existing files: "
            + ", ".join(existing)
        )
    for name in (
        "manual.csv",
        "multisigs.csv",
        "lost-wallets.csv",
        "frozen-wallets.csv",
        "bridge-reserves.csv",
    ):
        write_csv(directory / name, ROUTE_FIELDS, ())
    write_csv(
        directory / "destinations.csv",
        ("destination_id", "destination_address", "status", "notes"),
        (
            {
                "destination_id": "treasury",
                "destination_address": "",
                "status": "hold",
                "notes": (
                    "fill the approved Ethereum treasury address before "
                    "distribution"
                ),
            },
            {
                "destination_id": "wone-reserve-custody",
                "destination_address": "",
                "status": "hold",
                "notes": (
                    "fill the approved dedicated WONE reserve multisig "
                    "address"
                ),
            },
            {
                "destination_id": "layerzero-nativeoft-custody",
                "destination_address": "",
                "status": "hold",
                "notes": (
                    "fill only after NativeOFT reconciliation and custody "
                    "approval"
                ),
            },
        ),
    )
    write_csv(
        directory / "validator-governors.csv",
        GOVERNOR_FIELDS,
        (),
    )
    write_csv(
        directory / "policy-decisions.csv",
        ("decision_id", "status", "decision", "evidence", "notes"),
        (
            {
                "decision_id": "rollback-exploit-proceeds",
                "status": "pending",
                "decision": "",
                "evidence": "",
                "notes": (
                    "decide whether identified proceeds remain ordinary "
                    "state claims or are frozen/routed"
                ),
            },
            {
                "decision_id": "wone-reserve-custody",
                "status": "resolved",
                "decision": (
                    "migrate the WONE native reserve once to a dedicated "
                    "custody multisig; claimants receive transfers from that "
                    "finite reserve, not additional issuance"
                ),
                "evidence": (
                    "docs/claim-routing.md#wone-reserve-custody"
                ),
                "notes": (
                    "the custody destination remains held until its approved "
                    "multisig address is supplied"
                ),
            },
            {
                "decision_id": "layerzero-nativeoft-reconciliation",
                "status": "pending",
                "decision": "",
                "evidence": "",
                "notes": (
                    "reconcile each direct native reserve against remote "
                    "supply and messages in flight before approving custody"
                ),
            },
        ),
    )
    print(f"initialized held routing files in {directory}")


if __name__ == "__main__":
    main()
