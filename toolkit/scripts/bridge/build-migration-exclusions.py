#!/usr/bin/env python3

"""Build bridge exclusions from the migration's non-issuance decisions.

An address is excluded from every bridge pool when the migration issues none
of its claim: non-issuance inventory rows with no remaining original-address
allocation, reported wallet-theft perpetrators and report-linked theft
recipients, and burn or inaccessible addresses. Addresses the migration only
partly deducts (capped incident and revert-leak credits, partial extra-mint
reclaims) keep their bridge allocations and are listed for review in the
summary, with the part of their intended deduction that their cutoff claim
could not cover.
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402

FIELDS = ("chain", "address", "pool_id", "reason")
THEFT_ROLES = {"reported_perpetrator", "report_linked_theft_recipient"}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--non-issuance", required=True, help="non-issuance-inventory.csv")
    parser.add_argument("--theft", action="append", default=[],
                        help="wallet-theft inventory CSVs (role column optional)")
    parser.add_argument("--burn-audit", required=True, help="burn-address-audit.json")
    parser.add_argument("--inaccessible", action="append", default=[],
                        help="inaccessible-address inventory CSVs")
    parser.add_argument("--retained-initial", help="not-issued-retained-initial-addresses.csv")
    parser.add_argument("--revert-leak", help="not-issued-revert-leak-recipients.csv")
    parser.add_argument("--treasury-reclaim", help="treasury-reclaim-inventory.csv")
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    lib.prepare_outputs([args.output, args.summary], args.replace)
    excluded = {}

    def exclude(account, reason):
        excluded.setdefault(lib.address(account), reason)

    partial = {}
    for row in lib.read_csv(args.non_issuance):
        if int(row["remaining_original_address_allocation_atto"] or 0) == 0:
            exclude(row["address_hex"], f"migration non-issuance: {row['category']}")
        else:
            partial[lib.address(row["address_hex"])] = {
                "source": "non-issuance inventory (partial)",
                "category": row["category"],
            }
    for path in args.theft:
        for row in lib.read_csv(path):
            role = row.get("role", "reported_perpetrator")
            if role in THEFT_ROLES and row.get("address_hex"):
                exclude(row["address_hex"], f"migration wallet theft: {role}")
    audit = lib.load_json(args.burn_audit)
    for item in audit["positive_inaccessible_or_precompile_addresses"]:
        exclude(item["address"], f"migration burn or inaccessible: {item['reason']}")
    for path in args.inaccessible:
        for row in lib.read_csv(path):
            exclude(row["address_hex"], f"migration burn or inaccessible: {row.get('category', 'inaccessible')}")

    uncovered = {}

    def short(account, intended, kept, source):
        gap = int(intended) - int(kept)
        if gap > 0:
            account = lib.address(account)
            item = uncovered.setdefault(account, {"uncovered_atto": 0, "sources": set()})
            item["uncovered_atto"] += gap
            item["sources"].add(source)

    if args.retained_initial:
        for row in lib.read_csv(args.retained_initial):
            short(row["address_hex"], row["initial_distribution_atto"], row["retained_cap_atto"],
                  f"incident wallet {row['incident']}")
    if args.revert_leak:
        for row in lib.read_csv(args.revert_leak):
            short(row["address_hex"], row["unbacked_credit_atto"], row["retained_cap_atto"],
                  f"revert-leak {row['incident']}")
    if args.treasury_reclaim:
        for row in lib.read_csv(args.treasury_reclaim):
            unreturned = int(row["extra_mint_atto"] or 0) - int(row["verified_burn_atto"] or 0)
            short(row["address_hex"], unreturned, row["treasury_reclaim_atto"] or 0,
                  "extra-mint reclaim")

    rows = [
        {"chain": "*", "address": account, "pool_id": "", "reason": reason}
        for account, reason in sorted(excluded.items())
    ]
    digest = lib.write_csv(args.output, FIELDS, rows)
    lib.write_json(args.summary, {
        "schema_version": 1,
        "excluded_addresses": len(rows),
        "output": os.path.basename(args.output),
        "output_sha256": digest,
        "partial_non_issuance": {
            account: item for account, item in sorted(partial.items())
            if account not in excluded
        },
        "uncovered_deductions": {
            account: {
                "uncovered_atto": str(item["uncovered_atto"]),
                "sources": sorted(item["sources"]),
                "excluded": account in excluded,
            }
            for account, item in sorted(uncovered.items())
        },
        "inputs": {
            "non_issuance_sha256": lib.file_sha256(args.non_issuance),
            "burn_audit_sha256": lib.file_sha256(args.burn_audit),
        },
    })
    print(f"wrote {args.output}: {len(rows)} excluded addresses; "
          f"{len(uncovered)} addresses with uncovered deductions")


if __name__ == "__main__":
    main()
