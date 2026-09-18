#!/usr/bin/env python3

"""Render the private wallet-airdrop and vault-share correction report."""

import argparse
import json
import os


ATTO_PER_ONE = 10**18


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--claims-summary", required=True)
    parser.add_argument("--all-metadata-summary", required=True)
    parser.add_argument("--all-metadata-ledger")
    parser.add_argument("--vault-rpc-summary", required=True)
    parser.add_argument("--vault-allocation-summary", required=True)
    parser.add_argument("--policy-summary", required=True)
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


def display_path(value):
    return os.path.relpath(value) if os.path.isabs(value) else value


def table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend(
        "| " + " | ".join(str(cell) for cell in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def main():
    args = parse_args()
    if os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)
    if os.path.exists(args.output) and not args.replace:
        raise FileExistsError(args.output)
    parent = os.path.dirname(args.output)
    if parent:
        os.makedirs(parent, exist_ok=True)
    claims = load(args.claims_summary)
    all_metadata = load(args.all_metadata_summary)
    vault_rpc = load(args.vault_rpc_summary)
    vault_allocation = load(args.vault_allocation_summary)
    policy = load(args.policy_summary)
    routing = load(args.routing_summary)
    components = claims["component_totals_atto"]

    rows = [
        (
            "Native direct wallet claim",
            one(components["native_wallet_airdrop"]),
        ),
        (
            "Qualified-holder WONE airdrop",
            one(components["wone_airdrop"]),
        ),
        ("Direct wallet airdrop", one(components["wallet_airdrop"])),
        (
            "Staked amount moved to validator vaults",
            one(components["staked_to_vault"]),
        ),
        ("Total claim", one(components["total_claim"])),
    ]
    category_rows = []
    for category in ("automatic", "contract_review", "excluded"):
        values = vault_allocation["category_totals"][category]
        category_rows.append(
            (
                category,
                values["rows"],
                one(values["wallet"]),
                one(values.get("wone", 0)),
                one(values["staked"]),
                one(values["total_claim"]),
            )
        )

    text = f"""# Migration claim delivery split

Generated from exact integer summaries at the final cutoff.

## Corrected semantics

The `total_claim` calculation was correct, but it was incorrectly presented as
if the full amount were delivered directly to an Ethereum wallet. Delivery
must be split without changing the total.

The claim is delivered in two places:

```text
native_wallet_airdrop
= liquid_shard0 + liquid_shard1
+ pending_undelegation + unclaimed_staking_reward
+ pending_cross_shard

wallet_airdrop
= native_wallet_airdrop + qualified_wone_airdrop

staked_to_vault = active_staked_or_delegated

total_claim = wallet_airdrop + staked_to_vault
```

`native_total_claim + WONE balance` determines whether the account is in the
prioritized batch.
`wallet_airdrop` is the direct ERC-20 ONE delivery. `staked_to_vault` is
deposited into the corresponding validator's ERC-4626 vault and represented by
shares.

## Exact cutoff split

{table(("Component", "ONE"), rows)}

## Snapshot-threshold base categories

{table(("Category", "Rows", "Wallet airdrop ONE", "WONE airdrop ONE", "Staked to vault ONE", "Total claim ONE"), category_rows)}

## Validator-vault evidence

- canonical block: `{vault_rpc["block"]}`
- block hash: `{vault_rpc["block_hash"]}`
- state root: `{vault_rpc["state_root"]}`
- validators scanned: `{vault_rpc["validators"]}`
- positive active-delegation rows: `{vault_rpc["active_delegation_rows"]}`
- validator vaults with positive principal: `{vault_allocation["vaults"]}`
- full vault assets: `{one(vault_allocation["vault_assets_atto"])} ONE`
- prioritized share principal:
  `{one(vault_allocation["priority_staked_to_vault_atto"])} ONE`
- deferred share principal:
  `{one(vault_allocation["deferred_staked_to_vault_atto"])} ONE`

The archival-RPC detail total equals the independently database-derived active
stake/delegation total exactly.

## Claim-account metadata evidence

- source: `{all_metadata["source_kind"]}`
- block: `{all_metadata["block"]}`
- block hash: `{all_metadata["block_hash"]}`
- state root: `{all_metadata["state_root"]}`
- claim rows checked: `{all_metadata["rows"]}`
- previously blank shard-0 code fields resolved:
  `{all_metadata["updated_rows"]}`
- code-bearing accounts among those rows:
  `{all_metadata["code_bearing_among_queried"]}`

This metadata pass verifies every row in the current prioritized batch. The
separate metadata-complete all-address ledger retains cutoff code state for
native claims below the threshold.

## Explicit routing and stage status

- conservative all-stage status: `{routing["status"]}`
- initial-stage status: `{routing["initial_stage_status"]}`
- initial-stage blockers:
  `{", ".join(routing["stage_readiness"]["initial"]["blockers"]) or "none"}`
- prioritized claims checked: `{routing["priority_claims"]}`
- explicitly routed deferred claims:
  `{routing["explicitly_routed_deferred_claims"]}`
- active explicit routes: `{routing["active_routes"]}`
- sparse wallet/vault exception rows:
  `{routing["routing_exception_rows"]}`
- verified validator-account exceptions:
  `{routing["validator_account_exceptions"]}`
- validator-governor exception rows:
  `{routing["governor_exception_rows"]}`
- unresolved wallet amount:
  `{one(routing["unresolved_wallet_airdrop_atto"])} ONE`
- unresolved staked-to-vault amount:
  `{one(routing["unresolved_staked_to_vault_atto"])} ONE`
- not-issued wallet amount:
  `{one(routing["not_issued_wallet_airdrop_atto"])} ONE`
- not-issued staked-to-vault amount:
  `{one(routing["not_issued_staked_to_vault_atto"])} ONE`
- WONE reserve redistributed to current qualified-holder airdrops:
  `{one(routing["wone_redistributed_to_holders_atto"])} ONE`
- WONE reserve remainder retained as not issued:
  `{one(routing["wone_retained_not_issued_atto"])} ONE`
- total source amount classified as redistributed:
  `{one(routing["redistributed_total_claim_atto"])} ONE`
- all-stage nonterminal amount represented by this routing compilation:
  `{one(routing["issuable_total_claim_atto"])} ONE`
- initial-stage allocation:
  `{one(routing["stage_totals"]["initial"]["total_claim_atto"])} ONE`
- next-stage allocation:
  `{one(routing["stage_totals"]["next_stage"]["total_claim_atto"])} ONE`
- qualified deferred-wallet allocation:
  `{one(routing["stage_totals"]["deferred"]["total_claim_atto"])} ONE`
- explicitly routed below-threshold claims pending stage review:
  `{routing["pending_stage_review_claims"]}` rows,
  `{one(routing["stage_totals"]["manual_review"]["total_claim_atto"])} ONE`
- unresolved validator governors: `{routing["unresolved_governors"]}`
- pending policy decisions:
  `{", ".join(routing["pending_policy_decisions"]) or "none"}`

Ordinary code-less EOAs use the implicit same-address rule and are deliberately
absent from the sparse exception output. The global result remains a
conservative all-stage hold. Each stage has separate held destination,
validator-governor, and policy-gate totals; a later-stage hold does not by
itself change initial-stage readiness.

`redistributed` is a terminal source offset paired exactly with WONE already
added to qualified-holder wallet rows. It is not itself another destination or
issuance. `not_issuing` is also terminal, but unlike redistribution those exact
amounts receive no token and remain in the 2050 premint reserve. A
not-issued staked row also removes the corresponding vault deposit assets and
shares. Existing partial-row remainders continue to their ordinary destinations.

Migration stage is separate from destination status. Reviewed multisig,
LayerZero, and 1wallet allocations are held for the next stage; a verified
destination is still required. SmartVault and all other reviewed genuine
contracts are terminal `not_issuing`. WONE uses exact redistribution and
retained non-issuance source routes, and its non-backing shard-1 remainder is
included in reviewed-contract non-issuance.

## Outputs

- all-address claim ledger:
  `{display_path(claims["output"])}`
- metadata-complete all-address companion:
  `{display_path(args.all_metadata_ledger or all_metadata["output"])}`
- per-validator/delegator source ledger:
  `{vault_rpc["output"]}`
- intermediate validator vault deposits:
  `{vault_allocation["outputs"]["vault_deposits"]["path"]}`
- intermediate prioritized vault shares:
  `{vault_allocation["outputs"]["priority_shares"]["path"]}`
- intermediate deferred vault shares:
  `{vault_allocation["outputs"]["deferred_shares"]["path"]}`
- intermediate automatic wallet amount:
  `{vault_allocation["outputs"]["automatic_wallet_airdrop"]["path"]}`
- intermediate contract wallet base amount:
  `{vault_allocation["outputs"]["contract_wallet_base"]["path"]}`
- intermediate excluded wallet amount:
  `{vault_allocation["outputs"]["excluded_wallet_routing"]["path"]}`
- base destination categories:
  `{policy["categories"]["automatic"]["output"]}`,
  `{policy["categories"]["contract_review"]["output"]}`, and
  `{policy["categories"]["excluded_address"]["output"]}`
- migration-stage policy:
  `{routing["migration_stages"]}`
- sparse wallet and vault-share exceptions:
  `{routing["outputs"]["routing_exceptions"]["path"]}`
- sparse validator-governor exceptions:
  `{routing["outputs"]["governor_exceptions"]["path"]}`
- complete validator-vault stage partition:
  `{routing["outputs"]["vault_stages"]["path"]}`
- unresolved route list:
  `{routing["outputs"]["unresolved"]["path"]}`

All values use integer atto-ONE arithmetic. `total_claim` is the complete
economic claim. Only `wallet_airdrop` is transferred directly to the
individual wallet; `staked_to_vault` is delivered through vault shares.
"""

    with open(args.output + ".partial", "x", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
