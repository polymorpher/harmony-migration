#!/usr/bin/env python3

"""Render the WONE qualification, airdrop, and source-routing report."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path


ATTO_PER_ONE = 10**18


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--holder-scan", required=True)
    parser.add_argument("--overlay-summary", required=True)
    parser.add_argument("--threshold-summary", required=True)
    parser.add_argument("--policy-summary", required=True)
    parser.add_argument("--prior-policy-summary", required=True)
    parser.add_argument("--routing-summary", required=True)
    parser.add_argument("--verification-summary", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def one(value):
    whole, fraction = divmod(int(value), ATTO_PER_ONE)
    return f"{whole:,}.{fraction:018d}"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_policy_addresses(summary, label, summary_path=None):
    categories = {}
    union = set()
    for category in ("automatic", "contract_review", "excluded_address"):
        record = summary["categories"][category]
        recorded_path = Path(record["output"])
        candidates = [recorded_path]
        if summary_path is not None:
            candidates.append(
                Path(summary_path).resolve().parent / recorded_path.name
            )
        path = next(
            (
                candidate
                for candidate in candidates
                if candidate.is_file()
                and (
                    not record.get("output_sha256")
                    or sha256(candidate) == record["output_sha256"]
                )
            ),
            None,
        )
        if path is None:
            raise ValueError(
                f"{label} {category} output does not match its recorded hash"
            )
        addresses = set()
        with open(path, newline="") as source:
            reader = csv.DictReader(source)
            if "address" not in set(reader.fieldnames or ()):
                raise ValueError(f"{label} {category} output has no address")
            for line, row in enumerate(reader, start=2):
                address = row["address"].lower()
                if address in addresses:
                    raise ValueError(
                        f"{label} {category} output line {line}: duplicate"
                    )
                addresses.add(address)
        if len(addresses) != int(summary["categories"][category]["rows"]):
            raise ValueError(f"{label} {category} row count mismatch")
        if union & addresses:
            raise ValueError(f"{label} policy categories overlap")
        union.update(addresses)
        categories[category] = addresses
    return categories, union


def main():
    args = parse_args()
    if os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output + ".partial")
    if os.path.exists(args.output) and not args.replace:
        raise FileExistsError(args.output)
    parent = os.path.dirname(args.output)
    if parent:
        os.makedirs(parent, exist_ok=True)

    scan = load(args.holder_scan)
    overlay = load(args.overlay_summary)
    threshold = load(args.threshold_summary)
    policy = load(args.policy_summary)
    prior_policy = load(args.prior_policy_summary)
    routing = load(args.routing_summary)
    verification = load(args.verification_summary)
    for name, value in (
        ("holder scan", scan),
        ("overlay", overlay),
        ("routing", routing),
    ):
        if value.get("status") != "passed" and name != "routing":
            raise ValueError(f"{name} did not pass")
    if routing.get("status") not in {"ready", "hold"}:
        raise ValueError("routing has invalid status")
    verification_checks = verification.get("checks")
    if (
        verification.get("status") != "passed"
        or not isinstance(verification_checks, dict)
        or not verification_checks
        or any(value is not True for value in verification_checks.values())
    ):
        raise ValueError("post-WONE verification did not pass")

    reserve = int(overlay["wone_reserve_atto"])
    redistributed = int(
        overlay.get(
            "wone_redistributed_to_recipients_atto",
            overlay["wone_redistributed_to_priority_atto"],
        )
    )
    ordinary_threshold_wone = int(
        overlay.get(
            "ordinary_threshold_wone_atto",
            threshold["wone_airdrop_atto"],
        )
    )
    aggregate_delivery_wone = int(
        overlay.get("aggregate_delivery_wone_atto", 0)
    )
    retained = int(overlay["wone_retained_not_issued_atto"])
    excluded_wone = int(overlay["excluded_wone_atto"])
    if reserve != redistributed + retained:
        raise ValueError("WONE reserve split does not close")
    if int(scan["total_holder_balance_atto"]) != reserve:
        raise ValueError("holder scan does not equal WONE reserve")
    if int(threshold["wone_airdrop_atto"]) != ordinary_threshold_wone:
        raise ValueError(
            "threshold output does not equal ordinary-threshold WONE"
        )
    if ordinary_threshold_wone + aggregate_delivery_wone != redistributed:
        raise ValueError("WONE delivery categories do not close")
    if int(routing["wone_reserve_source_atto"]) != reserve:
        raise ValueError("routing used a different WONE reserve")
    if int(routing["wone_redistributed_to_holders_atto"]) != redistributed:
        raise ValueError("routing used a different WONE redistribution")
    if int(routing["wone_retained_not_issued_atto"]) != retained:
        raise ValueError("routing used a different retained remainder")
    verified_routing = verification["routing"]
    if (
        int(verified_routing["wone_reserve_atto"]) != reserve
        or int(verified_routing["wone_redistributed_atto"])
        != redistributed
        or int(verified_routing["wone_retained_not_issued_atto"])
        != retained
    ):
        raise ValueError("verification used a different WONE reserve split")

    current_categories, current_addresses = load_policy_addresses(
        policy, "current", args.policy_summary
    )
    prior_categories, prior_addresses = load_policy_addresses(
        prior_policy, "prior", args.prior_policy_summary
    )
    removed = prior_addresses - current_addresses
    if removed:
        raise ValueError("current threshold set removed prior qualifiers")
    newcomers = current_addresses - prior_addresses
    if len(newcomers) != int(overlay["newly_qualified_rows"]):
        raise ValueError("new qualification address set does not close")
    newcomer_counts = {
        category: len(newcomers & addresses)
        for category, addresses in current_categories.items()
    }
    reclassified = {
        address
        for address in prior_addresses & current_addresses
        if any(
            (address in prior_categories[category])
            != (address in current_categories[category])
            for category in current_categories
        )
    }

    text = f"""# WONE holder qualification and migration routing

Prepared from cutoff-pinned archival-node data.

## Result

- Cutoff: `2026-09-10T14:00:00Z`, shard-0 block
  `{scan["cutoff_block"]}`.
- Positive WONE holders: **{int(scan["holder_count"]):,}**.
- WONE supply/native reserve:
  **{one(reserve)} WONE/ONE**.
- Existing native-only `>= 1,000 ONE` rows:
  **{int(overlay["baseline_qualified_rows"]):,}**.
- Additional rows qualified by WONE: **{int(overlay["newly_qualified_rows"]):,}**.
- Current combined-threshold rows: **{int(overlay["qualified_rows"]):,}**.
- Ordinary-threshold WONE recipient rows after excluding system custody:
  **{int(overlay.get("ordinary_threshold_wone_holder_rows", overlay["priority_wone_holder_rows"])):,}**.
- Below-threshold exchange manual-delivery WONE recipient rows:
  **{int(overlay.get("aggregate_delivery_wone_holder_rows", 0)):,}**.
- WONE-backed ONE added to current wallet delivery:
  **{one(redistributed)} ONE**.
  This consists of **{one(ordinary_threshold_wone)} ONE** selected by the
  ordinary threshold and **{one(aggregate_delivery_wone)} ONE** selected by
  confirmed exchange inventories delivered manually from the 2050 supply
  reserve.
- Reserve remainder retained as not issued:
  **{one(retained)} ONE**.

An address-set join, rather than a category-total delta, shows that the newly
qualified rows split into:

- `{newcomer_counts["automatic"]:,}` automatic-policy rows;
- `{newcomer_counts["contract_review"]:,}` contract-review rows;
- `{newcomer_counts["excluded_address"]:,}` excluded-address rows.

Separately, `{len(reclassified):,}` existing qualifiers changed policy category.
They are not WONE-created eligibility.

## Classification

The entire WONE source reserve is **not** classified as `not_issued`.
That would conflict with the current definition because replacement ONE is
created for ordinary-threshold and exchange manual-delivery WONE holders.

Instead:

```text
WONE native reserve
= redistributed source offset
+ retained not-issued remainder

{reserve}
= {redistributed}
+ {retained}
```

`redistributed` is a terminal source offset, not a destination and not another
issuance. It exactly cancels the WONE amount added to ordinary-threshold and
exchange manual-delivery wallet rows. The retained remainder receives no replacement
asset in the current migration and remains in the 2050 premint reserve.

The WONE contract's own balance is excluded from recipient delivery as a
circular, code-controlled system balance. Together with selected inaccessible
addresses, excluded WONE totals `{one(excluded_wone)} WONE`. The two Harmony
LayerZero NativeOFT contracts hold native ONE directly and had no WONE balance
to exclude. Same-address shard-1 ONE at the WONE address is not backing and
is included in reviewed-contract non-issuance; it is not subtracted as backing
a second time.

## Claim fields

The canonical migration claim CSV now preserves native accounting and adds:

- `native_wallet_airdrop_atto`;
- `wone_balance_atto`;
- `wone_airdrop_atto`;
- `qualification_total_atto`;
- `native_total_claim_atto`;
- fixed-decimal `_one` companions.

The inclusive threshold uses:

```text
qualification_total_atto
= native_total_claim_atto + wone_balance_atto
>= 1,000 * 10^18
```

Only WONE belonging to the current ordinary qualifying batch or a normalized
confirmed exchange inventory enters `wone_airdrop_atto`. Other and
excluded backing remains in the retained reserve.

## Routing

- WONE redistributed source:
  `{one(routing["redistributed_total_claim_atto"])} ONE`.
- Compiled not issued, including reviewed incidents, reviewed contracts, and
  the retained WONE
  remainder: `{one(routing["not_issued_total_claim_atto"])} ONE`.
- Routing status: `{routing["status"]}`.
- Exchange manual reserve-delivery stage:
  `{routing["stage_readiness"]["exchange_manual"]["status"]}`.

The routing remains held for unrelated unresolved destinations and policy
decisions, plus other non-exchange manual claims whose stage remains under
review. The exchange manual reserve-delivery stage is independently ready,
and the WONE arithmetic itself is fully reconciled. WONE held by a reviewed excluded
contract is part of that contract's non-issuance and is not subtracted from
source backing a second time.

## Evidence

- Holder scan: `{args.holder_scan}`
- WONE claim overlay: `{args.overlay_summary}`
- Inclusive threshold summary: `{args.threshold_summary}`
- Final eligibility summary: `{args.policy_summary}`
- Applied routing summary: `{args.routing_summary}`
- Independent post-WONE verification: `{args.verification_summary}`
"""
    with open(args.output + ".partial", "x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
