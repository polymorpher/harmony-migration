#!/usr/bin/env python3

"""Render the WONE qualification, airdrop, and source-routing report."""

import argparse
import json
import os


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
    redistributed = int(overlay["wone_redistributed_to_priority_atto"])
    retained = int(overlay["wone_retained_not_issued_atto"])
    excluded_wone = int(overlay["excluded_wone_atto"])
    if reserve != redistributed + retained:
        raise ValueError("WONE reserve split does not close")
    if int(scan["total_holder_balance_atto"]) != reserve:
        raise ValueError("holder scan does not equal WONE reserve")
    if int(threshold["wone_airdrop_atto"]) != redistributed:
        raise ValueError("threshold output does not equal WONE redistribution")
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

    current_categories = policy["categories"]
    prior_categories = prior_policy["categories"]
    automatic_delta = (
        current_categories["automatic"]["rows"]
        - prior_categories["automatic"]["rows"]
    )
    contract_delta = (
        current_categories["contract_review"]["rows"]
        - prior_categories["contract_review"]["rows"]
    )
    excluded_delta = (
        current_categories["excluded_address"]["rows"]
        - prior_categories["excluded_address"]["rows"]
    )
    if (
        automatic_delta + contract_delta + excluded_delta
        != int(overlay["newly_qualified_rows"])
    ):
        raise ValueError("new qualification category deltas do not close")

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
- Qualified WONE recipient rows after excluding system custody:
  **{int(overlay["priority_wone_holder_rows"]):,}**.
- WONE-backed ONE added to current wallet delivery:
  **{one(redistributed)} ONE**.
- Reserve remainder retained as not issued:
  **{one(retained)} ONE**.

The newly qualified rows split into:

- `{automatic_delta:,}` ordinary automatic same-address rows;
- `{contract_delta:,}` code-bearing contract-recovery rows;
- `{excluded_delta:,}` excluded-address rows.

## Classification

The entire WONE source reserve is **not** classified as `not_issued`.
That would conflict with the current definition because replacement ONE is
created for qualified WONE holders.

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
issuance. It exactly cancels the WONE amount added to qualified-holder wallet
rows. The retained remainder receives no replacement asset in the current
migration and remains in the Year 2025 Supply Reserve.

The WONE contract's own balance is excluded from recipient delivery as a
circular, code-controlled system balance. Together with selected inaccessible
addresses, excluded WONE totals `{one(excluded_wone)} WONE`. The two Harmony
LayerZero NativeOFT contracts hold native ONE directly and had no WONE balance
to exclude. Same-address shard-1 ONE at the WONE address is not backing and
continues through ordinary contract-recovery routing.

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

Only WONE belonging to the current qualifying batch enters
`wone_airdrop_atto`. Below-threshold and excluded backing remains in the
retained reserve.

## Routing

- WONE redistributed source:
  `{one(routing["redistributed_total_claim_atto"])} ONE`.
- Total not issued, including reviewed incidents and the retained WONE
  remainder: `{one(routing["not_issued_total_claim_atto"])} ONE`.
- Remaining issuable amount after routing:
  `{one(routing["issuable_total_claim_atto"])} ONE`.
- Routing status: `{routing["status"]}`.

The routing remains held for unrelated unresolved destinations and policy
decisions; the WONE arithmetic itself is fully reconciled.

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
