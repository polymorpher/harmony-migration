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
            "bridge-reserves.base.csv",
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
    ):
        write_csv(directory / name, ROUTE_FIELDS, ())
    write_csv(
        directory / "bridge-reserves.base.csv",
        ROUTE_FIELDS,
        (
            {
                "route_id": "layerzero-nativeoft-bsc-custody",
                "priority": "400",
                "source_address": (
                    "0x5b18a4e73f9a4fe337a072516b317863ad3046aa"
                ),
                "destination_id": "layerzero-nativeoft-custody",
                "destination_address": "",
                "amount_atto": "SHARD0_LIQUID",
                "allocation_method": "wallet_first_pro_rata_vault",
                "reason": "layerzero_nativeoft_reconciliation_hold",
                "evidence": (
                    "docs/claim-routing.md#layerzero-nativeoft-reconciliation"
                ),
                "notes": "remote supply and in-flight reconciliation pending",
            },
            {
                "route_id": "layerzero-nativeoft-ethereum-custody",
                "priority": "400",
                "source_address": (
                    "0x905582f21fb9855c809d5b8933272a292dfbb138"
                ),
                "destination_id": "layerzero-nativeoft-custody",
                "destination_address": "",
                "amount_atto": "SHARD0_LIQUID",
                "allocation_method": "wallet_first_pro_rata_vault",
                "reason": "layerzero_nativeoft_reconciliation_hold",
                "evidence": (
                    "docs/claim-routing.md#layerzero-nativeoft-reconciliation"
                ),
                "notes": "remote supply and in-flight reconciliation pending",
            },
        ),
    )
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
                "destination_id": "not-issuing",
                "destination_address": "",
                "status": "not_issuing",
                "notes": (
                    "terminal outcome: do not create or distribute tokens "
                    "for the routed amount"
                ),
            },
            {
                "destination_id": "onewallet-recovery-multisig",
                "destination_address": "",
                "status": "hold",
                "notes": (
                    "next-stage 1wallet recovery multisig; preserve cutoff "
                    "ownership and recovery evidence before filling"
                ),
            },
            {
                "destination_id": "wone-holder-redistribution",
                "destination_address": "",
                "status": "redistributed",
                "notes": (
                    "terminal source offset paired exactly with WONE added "
                    "to current qualified-holder airdrops"
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
                "decision_id": "initial-wallet-activity-stage",
                "status": "resolved",
                "decision": (
                    "place positive eligible wallets with activity in the "
                    "six calendar months before cutoff in the initial stage"
                ),
                "evidence": (
                    "docs/eligibility-policy.md#initial-and-later-stages"
                ),
                "notes": (
                    "account classification and destination readiness remain "
                    "separate from migration stage"
                ),
            },
            {
                "decision_id": "reviewed-contract-migration-policy",
                "status": "resolved",
                "decision": (
                    "hold reviewed multisig, LayerZero, and 1wallet "
                    "allocations for the next stage; do not issue SmartVault "
                    "or other reviewed genuine-contract allocations"
                ),
                "evidence": "docs/claim-routing.md#reviewed-contract-policy",
                "notes": (
                    "next-stage eligibility does not imply a verified "
                    "destination"
                ),
            },
            {
                "decision_id": "wone-holder-redistribution",
                "status": "resolved",
                "decision": (
                    "redistribute the qualified-holder portion of the WONE "
                    "native reserve to holder airdrops and retain the "
                    "remainder as not issued"
                ),
                "evidence": (
                    "docs/claim-routing.md#wone-holder-redistribution"
                ),
                "notes": (
                    "build-wone-routes.py writes exact source offsets from "
                    "the verified WONE qualification summary"
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
