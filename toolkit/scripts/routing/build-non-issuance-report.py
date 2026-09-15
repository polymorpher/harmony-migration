#!/usr/bin/env python3

"""Render the embargoed incident non-issuance policy report."""

import argparse
import json
import os


ATTO_PER_ONE = 10**18
CATEGORY_LABELS = {
    "blacklisted_extra_mint_recipient": (
        "Burn-aware extra-mint portion for blacklisted recipients"
    ),
    "burn_or_inaccessible": "Burn and inaccessible-address amounts",
    "reported_wallet_theft_perpetrator": (
        "Report-identified wallet-theft perpetrator amounts"
    ),
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory-summary", required=True)
    parser.add_argument("--route-summary", required=True)
    parser.add_argument("--routing-summary", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def one(value):
    whole, fraction = divmod(int(value), ATTO_PER_ONE)
    return f"{whole:,}.{fraction:018d}"


def table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend(
        "| " + " | ".join(str(value) for value in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def main():
    args = parse_args()
    if os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output + ".partial")
    if os.path.exists(args.output) and not args.replace:
        raise FileExistsError(args.output)
    parent = os.path.dirname(args.output)
    if parent:
        os.makedirs(parent, exist_ok=True)

    inventory = load(args.inventory_summary)
    routes = load(args.route_summary)
    routing = load(args.routing_summary)
    historical_amount = int(inventory["totals_atto"]["treasury_reclaim"])
    not_issued = int(routes["not_issued_atto"])
    applied_not_issued = int(routing["not_issued_total_claim_atto"])
    gross = int(routing["source_total_claim_atto"])
    issuable = int(routing["issuable_total_claim_atto"])
    if routes.get("destination_id") != "not-issuing":
        raise ValueError("route summary is not terminal non-issuance")
    if historical_amount != not_issued:
        raise ValueError("non-issuance changed the audited historical amount")
    if applied_not_issued != not_issued:
        raise ValueError("applied routing did not consume every exact route")
    if gross != issuable + applied_not_issued:
        raise ValueError("gross claim does not equal issuable plus not-issued")

    category_rows = []
    for category in (
        "blacklisted_extra_mint_recipient",
        "burn_or_inaccessible",
        "reported_wallet_theft_perpetrator",
    ):
        values = routes["categories"][category]
        category_rows.append(
            (
                CATEGORY_LABELS[category],
                values["routes"],
                one(values["not_issued_atto"]),
            )
        )
    text = f"""# Incident-address non-issuance policy

Prepared: `2026-09-15`

## Decision

The exact amounts previously assigned to treasury are now **not issued**.
`not-issuing` is a terminal routing outcome, not an address, account, transfer,
or unresolved hold. No ERC-20 ONE, validator-vault deposit asset, or
validator-vault share is created for these amounts.

This update changes only the destination outcome. It does not expand the
address set or the amount selected by the prior audit. In particular,
burn-aware extra-mint rows keep their existing ordinary-destination remainder.
The source inventory retains the legacy field name
`treasury_reclaim_atto` because that file is historical evidence.

## Exact non-issuance

{table(("Category", "Routes", "Not issued ONE"), category_rows)}

- **Total not issued:** `{one(applied_not_issued)} ONE`
- **Gross cutoff claims represented by routing:**
  `{one(gross)} ONE`
- **Remaining issuable amount:** `{one(issuable)} ONE`

Exact closure:

```text
gross cutoff claim = remaining issuable + not issued
{gross} = {issuable} + {applied_not_issued}
```

## Routing behavior

- `build-non-issuance-routes.py` copies each positive historical
  `treasury_reclaim_atto` amount exactly.
- `apply-routes.py` emits `destination_status = not_issuing` with no
  destination address.
- Not-issued rows do not appear in `unresolved-routing.csv`.
- Partial routes consume direct wallet tokens first and then vault shares
  proportionally, using the existing allocation rule.
- A final deployment builder must omit every not-issued wallet row, subtract
  every not-issued staked row from its validator's vault deposit and share
  mint, and verify the `issuable_*` totals from `routing-summary.json`.

## Evidence

- Historical audited inventory:
  `{args.inventory_summary}`
- Generated non-issuance route summary:
  `{args.route_summary}`
- Applied routing summary:
  `{args.routing_summary}`

The historical treasury-routing analysis remains retained separately as the
evidence used to calculate the exact amounts. This policy report supersedes
only its destination conclusion.
"""
    with open(args.output + ".partial", "x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
