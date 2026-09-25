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
    "historical_incident_retained_cap": (
        "Retained May 2025 / April 2026 initial-recipient caps"
    ),
    "report_linked_theft_recipient": (
        "Direct recipients linked to reported wallet thefts"
    ),
    "reported_wallet_theft_perpetrator": (
        "Report-identified wallet-theft perpetrator amounts"
    ),
    "revert_leak_credit_recipient": (
        "Rollback-exploit credit at the wallets that received it"
    ),
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory-summary", required=True)
    parser.add_argument("--route-summary", required=True)
    parser.add_argument("--contract-policy-summary", required=True)
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
    contract_policy = load(args.contract_policy_summary)
    routing = load(args.routing_summary)
    inventory_amount = int(inventory["totals_atto"]["not_issued"])
    not_issued = int(routes["not_issued_atto"])
    applied_not_issued = int(routing["not_issued_total_claim_atto"])
    wone_retained = int(routing["wone_retained_not_issued_atto"])
    redistributed = int(routing["redistributed_total_claim_atto"])
    contract_not_issued = sum(
        int(values["not_issued_atto"])
        for values in contract_policy["groups"].values()
    )
    gross = int(routing["source_total_claim_atto"])
    issuable = int(routing["issuable_total_claim_atto"])
    exchange_manual = int(routing["exchange_manual_total_claim_atto"])
    if routes.get("destination_id") != "not-issuing":
        raise ValueError("route summary is not terminal non-issuance")
    if inventory_amount != int(routes["inventories"][0]["not_issued_atto"]):
        raise ValueError("routes changed the existing reviewed inventory")
    if applied_not_issued != not_issued + wone_retained + contract_not_issued:
        raise ValueError(
            "applied non-issuance does not equal inventories, WONE remainder, "
            "and reviewed-contract exclusion"
        )
    if gross != issuable + applied_not_issued + redistributed + exchange_manual:
        raise ValueError(
            "gross claim does not equal issuable plus not-issued plus "
            "redistributed plus exchange manual delivery"
        )

    category_rows = []
    for category in (
        "blacklisted_extra_mint_recipient",
        "burn_or_inaccessible",
        "historical_incident_retained_cap",
        "report_linked_theft_recipient",
        "reported_wallet_theft_perpetrator",
        "revert_leak_credit_recipient",
    ):
        values = routes["categories"].get(category, {"routes": 0, "not_issued_atto": "0"})
        category_rows.append(
            (
                CATEGORY_LABELS[category],
                values["routes"],
                one(values["not_issued_atto"]),
            )
        )
    text = f"""# Migration non-issuance policy

Prepared: `2026-09-17`

## Decision

The reviewed incident amounts, retained historical-incident caps, and excluded
reviewed-contract allocations are **not issued**.
`not-issuing` is a terminal routing outcome, not an address, account, transfer,
or unresolved hold. No ERC-20 ONE, validator-vault deposit asset, or
validator-vault share is created for these amounts.

The historical amounts previously assigned to treasury remain exact.
Burn-aware extra-mint rows keep their existing ordinary-destination remainder.
The September 16 update adds four reviewed perpetrator-related addresses:
two explicitly named alleged perpetrators and two direct theft recipients.
Their balances already existed in the gross cutoff ledger; this is a
classification and routing update, not additional supply.

Twenty reported victim wallets are recorded separately and are not routed to
non-issuance.

The September 23 update removes exploit credit from the wallets that received
it: every wallet credited by a proven revert-leak receipt (May 2025, April
2026, and the June–July 2026 cohort) is not issued the credited amount, capped
at what it still holds after the earlier deductions. A wallet that holds only
exploit credit loses its whole claim; one that also held legitimate funds keeps
the difference. This resolves the `rollback-exploit-proceeds` decision.

The same day, one reviewed address was added to the burn and inaccessible
category: on Ethereum it is the legacy bridged ERC-20 contract named Harmony ONE
(symbol 1ONE), so replacement ONE delivered to it would be stuck, and on Harmony
it never sent a transaction. Its whole cutoff claim is not issued.

## Exact non-issuance

{table(("Category", "Routes", "Not issued ONE"), category_rows)}

- **Compiled total not issued:** `{one(applied_not_issued)} ONE`
- **Existing plus historical incident inventories:** `{one(not_issued)} ONE`
- **Reviewed-contract allocation not issued:**
  `{one(contract_not_issued)} ONE`
- **WONE reserve remainder retained as not issued:**
  `{one(wone_retained)} ONE`
- **WONE source amount redistributed to qualified holders:**
  `{one(redistributed)} ONE`
- **September 16 perpetrator-related addition:**
  `{one(inventory["wallet_theft_addition_atto"])} ONE`
- **Reported victim wallets kept outside non-issuance:**
  `{inventory["victim_addresses_not_routed"]}` addresses,
  `{one(inventory["victim_total_claim_atto_not_routed"])} ONE`
- **Gross cutoff claims represented by routing:**
  `{one(gross)} ONE`
- **Exchange entitlement delivered manually from the 2050 supply reserve
  (excluded from the airdrop):** `{one(exchange_manual)} ONE`
- **Remaining all-stage amount represented by this routing compilation:**
  `{one(issuable)} ONE`

Exact closure:

```text
gross expanded claim = remaining issuable + not issued + redistributed source + exchange manual delivery
{gross} = {issuable} + {applied_not_issued} + {redistributed} + {exchange_manual}
```

## Routing behavior

- `build-non-issuance-routes.py` copies each positive `not_issued_atto` or
  `retained_cap_atto` amount exactly and rejects inventory overlap, except that
  a wallet may carry both a retained cap and the revert-leak amount computed
  from what the cap leaves.
- `apply-routes.py` emits `destination_status = not_issuing` with no
  destination address.
- WONE redistribution is reported separately from `not_issuing`; only the
  below-threshold/excluded reserve remainder is retained in the 2050 premint
  reserve.
- `build-contract-policy-routes.py` marks SmartVault and all other excluded
  reviewed genuine-contract remainders as not issued, including vault shares.
- Not-issued rows do not appear in `unresolved-routing.csv`.
- Partial routes consume direct wallet tokens first and then vault shares
  proportionally, using the existing allocation rule.
- Exchange wallets are routed with `issuance_treatment = manual_from_reserve`
  and `destination_status = exchange_manual`; they are neither not-issued nor
  airdropped. Their delegated principal is released from the validator vaults
  and the whole entitlement is delivered manually from the 2050 supply
  reserve.
- A final deployment builder must omit every not-issued wallet row, subtract
  every not-issued and exchange-manual staked row from its validator's vault
  deposit and share mint, omit every redistributed source row and every
  exchange-manual row, and verify the `issuable_*` totals from
  `routing-summary.json`.

## Evidence

- Current reviewed non-issuance inventory:
  `{args.inventory_summary}`
- Generated non-issuance route summary:
  `{args.route_summary}`
- Reviewed-contract route summary:
  `{args.contract_policy_summary}`
- Applied routing summary:
  `{args.routing_summary}`

The historical treasury-routing analysis remains retained separately as the
evidence used to calculate the original exact amounts. The September 16
wallet-theft inventory provides the evidence for the four additions.
"""
    with open(args.output + ".partial", "x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
