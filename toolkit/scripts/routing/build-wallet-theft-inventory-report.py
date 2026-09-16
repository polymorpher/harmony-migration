#!/usr/bin/env python3

"""Render the embargoed September 16 wallet-theft inventory update."""

import argparse
import csv
import json
import os


ATTO_PER_ONE = 10**18


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--perpetrator-summary", required=True)
    parser.add_argument("--victim-summary", required=True)
    parser.add_argument("--additions", required=True)
    parser.add_argument("--non-issuance-summary", required=True)
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

    perpetrators = load(args.perpetrator_summary)
    victims = load(args.victim_summary)
    non_issuance = load(args.non_issuance_summary)
    with open(args.additions, newline="") as source:
        additions = list(csv.DictReader(source))
    if any(
        result.get("status") != "passed"
        for result in (perpetrators, victims, non_issuance)
    ):
        raise ValueError("wallet-theft inventory inputs did not pass")
    delta = int(
        perpetrators["addition_delta"]["total_claim_atto"]
    )
    current = int(perpetrators["totals"]["total_claim_atto"])
    baseline = current - delta
    victim_total = int(victims["total_claim_atto"])
    if delta != int(non_issuance["wallet_theft_addition_atto"]):
        raise ValueError("wallet-theft routing delta differs")
    if len(additions) != 4:
        raise ValueError("expected four reviewed additions")

    addition_rows = [
        (
            row["case_id"],
            row["role"],
            row["address_bech32"],
            one(row["cutoff_claim_atto"]),
        )
        for row in additions
    ]
    explicit = perpetrators["roles"]["reported_perpetrator"]
    linked = perpetrators["roles"]["report_linked_theft_recipient"]
    text = f"""# Wallet-theft incident inventory update

Prepared: `2026-09-16`

## Result

The historical 17-address perpetrator inventory is preserved unchanged.
Review of additional case records adds four perpetrator-related addresses:
two addresses explicitly named as alleged perpetrators and two first recipients
linked to reported thefts by successful transactions.

- Historical perpetrator inventory:
  `{perpetrators["addresses"] - len(additions)}` addresses,
  `{one(baseline)} ONE`
- Four-address addition: `{one(delta)} ONE`
- Current perpetrator-related inventory:
  `{perpetrators["addresses"]}` addresses,
  `{one(current)} ONE`
- Explicitly reported perpetrator role:
  `{explicit["addresses"]}` addresses,
  `{one(explicit["total_claim_atto"])} ONE`
- Transaction-linked theft-recipient role:
  `{linked["addresses"]}` addresses,
  `{one(linked["total_claim_atto"])} ONE`

## Four additions

{table(
    ("Case", "Role", "Address", "Cutoff native ONE"),
    addition_rows,
)}

All four amounts already existed in the global cutoff claim ledger and match
its component rows exactly. Adding incident labels and non-issuance routes does
not add ONE to the gross claim calculation.

## Separate victim population

The memo also identifies `{victims["addresses"]}` original victim wallets,
of which `{victims["positive_claim_addresses"]}` have positive native
positions totaling `{one(victim_total)} ONE`.

Victims are not perpetrators. They remain outside `not-issuing` and require
separate beneficiary or recovery review. The combined incident inventory is
`{perpetrators["addresses"] + victims["addresses"]}` unique addresses totaling
`{one(current + victim_total)} ONE`, but only the perpetrator-related set is
added to non-issuance.

## Evidence limits

- `reported_perpetrator` means the source explicitly names the address as the
  alleged thief or hacker wallet.
- `report_linked_theft_recipient` means a successful transaction establishes
  the direct first recipient, but the narrative does not explicitly name that
  address as the perpetrator.
- The CASE-058 token transfer links the reported victim to the explicitly
  named hacker address, but it does not attribute that address's complete
  native ONE balance to the theft.
- Routers, bridges, token contracts, second hops, reimbursement destinations,
  unsupported suspects, and victim wallets remain excluded.
- Native positions exclude HRC20 holdings, protocol positions, and Ethereum
  balances.

## Routing decision

The four existing cutoff claims are routed to terminal `not_issuing`. They
receive no ERC-20 ONE, validator-vault assets, or vault shares. The expanded
non-issuance total is recorded in
`artifacts/supply-reconciliation-20260911/non-issuance-inventory-summary.json`.

## Inputs

- Historical perpetrator summary: `{args.perpetrator_summary}`
- Four-address evidence inventory: `{args.additions}`
- Separate victim summary: `{args.victim_summary}`
- Expanded non-issuance summary: `{args.non_issuance_summary}`
"""
    with open(args.output + ".partial", "x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
