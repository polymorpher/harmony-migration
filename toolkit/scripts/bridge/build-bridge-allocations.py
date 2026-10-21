#!/usr/bin/env python3

"""Build the bridge-collateral allocation ledger, claim lists, and custody plan.

For every pool:

  holding     balance of the pool's claimant token(s) at the snapshot block
  adjustment  signed message adjustments from scan-bridge-messages.py
  gross       max(0, holding + adjustment)

Each account is then claimable, held for review, or excluded. Claimable and
held accounts share the collateral: when their gross total exceeds the
collateral every allocation is scaled by collateral / eligible and rounded
down; otherwise allocations equal gross. Everything not allocated (excluded
balances, surplus, rounding dust, amounts below the minimum) stays in the
custody multisig, so for every pool

  collateral = claimable + held + retained
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402

ALLOCATION_FIELDS = (
    "pool_id",
    "claim_chain",
    "claim_token",
    "holder_chain",
    "snapshot_block",
    "account",
    "holding_raw",
    "adjustment_raw",
    "gross_raw",
    "unrecovered_debit_raw",
    "class",
    "status",
    "payee",
    "allocation_raw",
    "reason",
)
POOL_FIELDS = (
    "pool_id",
    "kind",
    "chain",
    "symbol",
    "decimals",
    "claim_asset",
    "claim_chain",
    "claim_token",
    "status",
    "snapshot_chain",
    "snapshot_label",
    "snapshot_block",
    "collateral_source",
    "collateral_raw",
    "holders",
    "holding_raw",
    "adjustment_raw",
    "excluded_raw",
    "eligible_raw",
    "scale_numerator",
    "scale_denominator",
    "claimable_raw",
    "held_raw",
    "below_minimum_raw",
    "retained_raw",
    "surplus_raw",
    "shortfall_raw",
    "unrecovered_debit_raw",
    "claimable_accounts",
    "held_accounts",
)
CLAIM_FIELDS = ("account", "token", "amount_raw", "pools")
ELIGIBLE = {
    "claimable",
    "held_multisig",
    "held_onewallet",
    "held_contract",
    "held_unclassified",
    "held_review",
}
KEY_CONTROLLED = {"eoa", "eoa_7702", "validator_wrapper", "adjustment_target"}
TRANSFER_SELECTOR = lib.SEL["transfer"]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--messages", required=True)
    parser.add_argument("--messages-summary", required=True)
    parser.add_argument("--holders-dir", required=True)
    parser.add_argument("--classes", required=True)
    parser.add_argument("--exclusions", help="CSV: chain,address,pool_id,reason")
    parser.add_argument(
        "--overrides",
        help="CSV: pool_id,address,action,destination,reason "
        "(action is claim, redirect, hold, or exclude)",
    )
    parser.add_argument(
        "--contract-review-policy",
        help="cutoff contract-review-policy.csv; ONE collateral must match it",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def load_holdings(plan_pool, holders_dir):
    holdings = {}
    for token in plan_pool["tokens"]:
        path = os.path.join(holders_dir, token["stem"] + ".csv")
        summary = lib.load_json(os.path.join(holders_dir, token["stem"] + ".summary.json"))
        if summary.get("status") != "passed":
            raise ValueError(f"{token['stem']}: holder snapshot did not pass")
        if summary.get("output_sha256") != lib.file_sha256(path):
            raise ValueError(f"{token['stem']}: holder CSV hash mismatch")
        if int(summary["block"]) != plan_pool["snapshot_block"]["number"]:
            raise ValueError(f"{token['stem']}: holder snapshot block mismatch")
        for row in lib.read_csv(path):
            account = lib.address(row["address"])
            holdings[account] = holdings.get(account, 0) + int(row["balance_raw"])
    return holdings


def collateral_for(pool, state):
    item = state["pools"][pool["pool_id"]]
    if pool["kind"] == "remote_escrow":
        state_values = item["remote"]["by_label"]["state"]
        values = [state_values["escrow_balance"], *state_values["previous_adapter_balances"].values()]
        if any(value is None for value in values):
            raise ValueError(f"{pool['pool_id']}: escrow balance unreadable")
        return sum(int(value) for value in values), (
            f"{pool['chain']} escrow and previous escrows at state block"
        )
    total = 0
    for entry in item["harmony"]:
        cutoff = entry["by_label"]["cutoff"]
        if lib.is_native(entry["token"]):
            total += int(cutoff["native_balance"]) + int(cutoff["wone_balance"] or 0)
        else:
            total += int(cutoff["proxy_token_balance"] or 0)
    return total, "Harmony lock at cutoff (native ONE plus WONE)"


def system_roles(routes, config):
    roles = {}
    for chain in (lib.HARMONY, *routes["chains"]):
        for account, role in lib.system_address_roles(routes, chain).items():
            roles.setdefault(account, role)
    for chain, entry in config["chains"].items():
        if entry.get("custody_multisig"):
            roles.setdefault(entry["custody_multisig"], f"{chain} custody multisig")
    one = config["one_claims"]
    roles.setdefault(one["reserve_safe"], "supply reserve Safe")
    if one.get("custody_multisig"):
        roles.setdefault(one["custody_multisig"], "ONE custody multisig")
    return roles


def status_for(account_class, holder_chain, claim_chain):
    if account_class in KEY_CONTROLLED:
        return "claimable", ""
    if account_class in ("safe", "multisig"):
        if holder_chain == claim_chain:
            return "claimable", "same contract on the claim chain"
        return "held_multisig", "multisig needs an approved claim-chain destination"
    if account_class == "onewallet":
        return "held_onewallet", "1wallet needs an approved claim-chain destination"
    if account_class in ("contract", "smartvault"):
        return "held_contract", "contract holder needs review"
    return "held_unclassified", "account was not classified"


def build_pool(pool, plan_pool, config, state, holders_dir, adjustments, classes,
               roles, exclusions, overrides, minimum):
    pid = pool["pool_id"]
    claim_chain, claim_token = lib.claim_target(pool, config)
    holder_chain = plan_pool["claimant_chain"]
    snapshot_block = plan_pool["snapshot_block"]["number"]
    holdings = load_holdings(plan_pool, holders_dir)
    collateral, collateral_source = collateral_for(pool, state)
    accounts = sorted(set(holdings) | set(adjustments))
    rows = []
    for account in accounts:
        holding = holdings.get(account, 0)
        adjustment = adjustments.get(account, 0)
        gross = holding + adjustment
        unrecovered = -gross if gross < 0 else 0
        gross = max(0, gross)
        account_class = classes.get((holder_chain, account))
        if account_class is None and account not in holdings:
            account_class = "adjustment_target"
        payee = account
        status, reason = status_for(account_class, holder_chain, claim_chain)
        if account in roles:
            status, reason = "excluded_system", roles[account]
        elif lib.reserved_address_reason(account):
            status, reason = "excluded_system", lib.reserved_address_reason(account)
        exclusion = exclusions.get((pid, account)) or exclusions.get(("*", account))
        if exclusion and exclusion["chain"] in ("*", holder_chain):
            status, reason = "excluded_policy", exclusion["reason"]
        override = overrides.get((pid, account)) or overrides.get(("*", account))
        if override:
            action = override["action"]
            reason = override["reason"]
            if action == "claim":
                status = "claimable"
            elif action == "redirect":
                status, payee = "claimable", lib.address(override["destination"], "override")
            elif action == "hold":
                status = "held_review"
            elif action == "exclude":
                status = "excluded_policy"
            else:
                raise ValueError(f"unknown override action {action!r}")
        rows.append({
            "pool_id": pid,
            "claim_chain": claim_chain,
            "claim_token": claim_token,
            "holder_chain": holder_chain,
            "snapshot_block": snapshot_block,
            "account": account,
            "holding_raw": holding,
            "adjustment_raw": adjustment,
            "gross_raw": gross,
            "unrecovered_debit_raw": unrecovered,
            "class": account_class or "",
            "status": status,
            "payee": payee,
            "allocation_raw": 0,
            "reason": reason,
        })
    eligible = sum(row["gross_raw"] for row in rows if row["status"] in ELIGIBLE)
    if eligible > collateral:
        numerator, denominator = collateral, eligible
    else:
        numerator, denominator = 1, 1
    for row in rows:
        if row["status"] not in ELIGIBLE:
            continue
        allocation = row["gross_raw"] * numerator // denominator
        if row["status"] == "claimable" and allocation < minimum:
            row["status"] = "below_minimum"
            row["reason"] = f"allocation below {minimum}"
            continue
        row["allocation_raw"] = allocation
    claimable = sum(row["allocation_raw"] for row in rows if row["status"] == "claimable")
    held = sum(
        row["allocation_raw"] for row in rows
        if row["status"].startswith("held_")
    )
    below = sum(
        row["gross_raw"] * numerator // denominator
        for row in rows if row["status"] == "below_minimum"
    )
    summary = {
        "pool_id": pid,
        "kind": pool["kind"],
        "chain": pool["chain"],
        "symbol": pool["symbol"],
        "decimals": pool["decimals"],
        "claim_asset": pool["claim_asset"],
        "claim_chain": claim_chain,
        "claim_token": claim_token,
        "status": "ready",
        "snapshot_chain": holder_chain,
        "snapshot_label": plan_pool["snapshot_label"],
        "snapshot_block": snapshot_block,
        "collateral_source": collateral_source,
        "collateral_raw": collateral,
        "holders": len(holdings),
        "holding_raw": sum(holdings.values()),
        "adjustment_raw": sum(adjustments.values()),
        "excluded_raw": sum(
            row["gross_raw"] for row in rows if row["status"].startswith("excluded_")
        ),
        "eligible_raw": eligible,
        "scale_numerator": numerator,
        "scale_denominator": denominator,
        "claimable_raw": claimable,
        "held_raw": held,
        "below_minimum_raw": below,
        "retained_raw": collateral - claimable - held,
        "surplus_raw": max(0, collateral - eligible),
        "shortfall_raw": max(0, eligible - collateral),
        "unrecovered_debit_raw": sum(row["unrecovered_debit_raw"] for row in rows),
        "claimable_accounts": sum(1 for row in rows if row["status"] == "claimable"),
        "held_accounts": sum(1 for row in rows if row["status"].startswith("held_")),
    }
    return summary, rows


def load_keyed(path, fields):
    keyed = {}
    if not path:
        return keyed
    for row in lib.read_csv(path):
        missing = [field for field in fields if field not in row]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
        key = (row.get("pool_id") or "*", lib.address(row["address"]))
        if key in keyed:
            raise ValueError(f"{path}: duplicate row for {key}")
        keyed[key] = row
    return keyed


def custody_plan(routes, config, state, pools):
    by_id = {pool["pool_id"]: pool for pool in routes["pools"]}
    chains = {}
    for chain in routes["chains"]:
        custody = config["chains"][chain].get("custody_multisig")
        facts = state["custody"].get(chain, {}).get(custody) if custody else None
        if not custody:
            status = "missing"
        elif not facts or not facts.get("meets_requirements"):
            status = "not_qualified"
        else:
            status = "ready"
        transfers = []
        for summary in pools:
            pool = by_id[summary["pool_id"]]
            if pool["kind"] != "remote_escrow" or pool["chain"] != chain:
                continue
            remote = state["pools"][pool["pool_id"]]["remote"]
            owner_facts = remote.get("owner_facts") or {}
            state_values = remote["by_label"]["state"]
            escrows = [(pool["remote_adapter"], state_values["escrow_balance"], remote.get("owner"))]
            escrows += [
                (previous, balance, remote["previous_adapter_owners"].get(previous))
                for previous, balance in state_values["previous_adapter_balances"].items()
                if int(balance or 0) > 0
            ]
            for escrow, balance, owner in escrows:
                transfers.append({
                    "pool_id": pool["pool_id"],
                    "symbol": pool["symbol"],
                    "asset": pool["remote_token"],
                    "escrow": escrow,
                    "amount_raw": str(int(balance)),
                    "escrow_owner": owner,
                    "escrow_owner_is_safe": bool(owner_facts.get("is_safe")) if owner == remote.get("owner") else None,
                    "escrow_owner_is_custody": owner == custody,
                })
        chains[chain] = {
            "custody_multisig": custody,
            "status": status,
            "custody_facts": facts,
            "transfers": transfers,
            "escrow_owners": sorted({item["escrow_owner"] or "" for item in transfers}),
        }
    one = config["one_claims"]
    one_custody = lib.one_custody(config)
    one_pools = [summary for summary in pools if summary["claim_asset"] == "one"]
    amount = sum(summary["collateral_raw"] for summary in one_pools)
    reserve = state["custody"].get(one["claim_chain"], {}).get(one["reserve_safe"]) or {}
    reserve_balance = int(reserve.get("claim_token_balance") or 0)
    funding = {
        "chain": one["claim_chain"],
        "token": one["claim_token"],
        "from": one["reserve_safe"],
        "to": one_custody,
        "amount_raw": str(amount),
        "pools": {summary["pool_id"]: str(summary["collateral_raw"]) for summary in one_pools},
        "reserve_balance_raw": str(reserve_balance),
        "reserve_sufficient": reserve_balance >= amount,
        "safe_transaction": None,
        "routing_destination": {
            "destination_id": one.get("routing_destination_id"),
            "destination_address": one_custody,
        },
    }
    if one_custody:
        funding["safe_transaction"] = {
            "to": one["claim_token"],
            "value": "0",
            "data": TRANSFER_SELECTOR + lib.address_word(one_custody) + lib.word(amount),
            "operation": 0,
        }
    return {"chains": chains, "one_funding": funding}


def fmt(raw, decimals):
    return lib.format_units(int(raw), int(decimals))


def report(summary, pools, plan, custody, message_summary, unsupported, state):
    lines = [
        "# Bridge collateral allocations",
        "",
        "Generated by `toolkit/scripts/bridge/build-bridge-allocations.py`. Raw",
        "integers are authoritative; formatted amounts use each asset's decimals.",
        "",
        "## Snapshot blocks and hack exposure",
        "",
        "| Chain | Rollback block | Cutoff block | Orphaned ONE deliveries | ONE holder snapshot |",
        "|---|---:|---:|---:|---|",
    ]
    blocks = state["blocks"]
    for chain, item in plan["exposure"].items():
        lines.append(
            f"| {chain} | {blocks[chain]['rollback']['number']} | "
            f"{blocks[chain]['cutoff']['number']} | {item['orphaned_one_deliveries']} | "
            f"{item['snapshot_label'] if item['has_one_pool'] else 'no ONE pool'} |"
        )
    lines += [
        "",
        f"Harmony rollback block {blocks[lib.HARMONY]['rollback']['number']}, cutoff block "
        f"{blocks[lib.HARMONY]['cutoff']['number']}.",
        "",
        "## Pools",
        "",
        "| Pool | Claim chain | Snapshot | Collateral | Eligible | Claimable | Held | Retained | Shortfall |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in pools:
        decimals = item["decimals"]
        lines.append(
            f"| {item['pool_id']} | {item['claim_chain']} | {item['snapshot_chain']} "
            f"{item['snapshot_label']} | {fmt(item['collateral_raw'], decimals)} | "
            f"{fmt(item['eligible_raw'], decimals)} | {fmt(item['claimable_raw'], decimals)} | "
            f"{fmt(item['held_raw'], decimals)} | {fmt(item['retained_raw'], decimals)} | "
            f"{fmt(item['shortfall_raw'], decimals)} |"
        )
    if unsupported:
        lines += ["", "Unsupported pools (no claim asset off Harmony):", ""]
        lines += [f"- `{item['pool_id']}`: {item['reason']}" for item in unsupported]
    funding = custody["one_funding"]
    lines += [
        "",
        "## Custody",
        "",
        "| Chain | Custody multisig | Status | Escrow transfers | Escrow owners |",
        "|---|---|---|---:|---|",
    ]
    for chain, item in custody["chains"].items():
        lines.append(
            f"| {chain} | `{item['custody_multisig'] or 'missing'}` | {item['status']} | "
            f"{len(item['transfers'])} | {', '.join(f'`{owner}`' for owner in item['escrow_owners'])} |"
        )
    lines += [
        "",
        f"ONE funding: `{funding['from']}` sends {fmt(funding['amount_raw'], 18)} of "
        f"`{funding['token']}` to `{funding['to'] or 'missing'}` on {funding['chain']} "
        f"(reserve sufficient: {funding['reserve_sufficient']}).",
        "",
        "## Message adjustments",
        "",
        "| Pool | Category | Messages | Adjustment (raw) |",
        "|---|---|---:|---:|",
    ]
    for pid, item in message_summary["pools"].items():
        for category, values in (item.get("categories") or {}).items():
            lines.append(
                f"| {pid} | {category} | {values['messages']} | {values['adjustment_raw']} |"
            )
    lines += [
        "",
        "## Totals",
        "",
        f"- pools: {summary['pools']}",
        f"- claim lists: {', '.join(summary['claim_lists'])}",
        f"- held accounts: {summary['held_accounts']}",
        "",
    ]
    return "\n".join(lines)


def main():
    args = parse_args()
    out = args.output_dir
    outputs = [
        os.path.join(out, name)
        for name in (
            "pools.csv", "allocations.csv", "custody-plan.json",
            "summary.json", "BRIDGE_ALLOCATIONS.md",
        )
    ]
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    claim_chains = sorted({
        lib.claim_target(pool, config)[0]
        for pool in routes["pools"] if pool["claim_asset"] != "unsupported"
    })
    outputs += [os.path.join(out, "claims", f"{chain}.csv") for chain in claim_chains]
    lib.prepare_outputs(outputs, args.replace)
    state = lib.load_json(args.state)
    plan = lib.load_json(args.plan)
    message_summary = lib.load_json(args.messages_summary)
    if message_summary.get("status") != "passed":
        raise ValueError("message scan did not pass; resolve incomplete pools first")
    if message_summary.get("output_sha256") != lib.file_sha256(args.messages):
        raise ValueError("messages CSV does not match its summary")
    policy = config.get("allocation_policy") or {}
    minimum = int(policy.get("minimum_claim_raw") or 0)
    minimum_by_pool = {
        key: int(value) for key, value in (policy.get("minimum_claim_raw_by_pool") or {}).items()
    }

    adjustments = {}
    for row in lib.read_csv(args.messages):
        if row["adjustment_account"] and int(row["adjustment_raw"]):
            key = row["pool_id"]
            account = lib.address(row["adjustment_account"])
            adjustments.setdefault(key, {})
            adjustments[key][account] = adjustments[key].get(account, 0) + int(row["adjustment_raw"])
    classes = {
        (row["chain"], lib.address(row["address"])): row["class"]
        for row in lib.read_csv(args.classes)
    }
    exclusions = {}
    if args.exclusions:
        for row in lib.read_csv(args.exclusions):
            key = (row.get("pool_id") or "*", lib.address(row["address"]))
            exclusions[key] = {"chain": row.get("chain") or "*", "reason": row["reason"]}
    overrides = load_keyed(args.overrides, ("address", "action", "destination", "reason"))
    roles = system_roles(routes, config)

    pools, allocations, unsupported = [], [], []
    for pool in routes["pools"]:
        pid = pool["pool_id"]
        plan_pool = plan["pools"][pid]
        if plan_pool["status"] == "unsupported":
            collateral, source = collateral_for(pool, state)
            unsupported.append({
                "pool_id": pid,
                "reason": plan_pool["reason"],
                "collateral_raw": str(collateral),
                "collateral_source": source,
            })
            continue
        if plan_pool["status"] != "planned":
            raise ValueError(f"{pid}: snapshot plan status is {plan_pool['status']}")
        if message_summary["pools"].get(pid, {}).get("status") != "complete":
            raise ValueError(f"{pid}: message scan is not complete")
        summary, rows = build_pool(
            pool, plan_pool, config, state, args.holders_dir,
            adjustments.get(pid, {}), classes, roles, exclusions, overrides,
            minimum_by_pool.get(pid, minimum),
        )
        pools.append(summary)
        allocations += rows

    if args.contract_review_policy:
        review = {
            lib.address(row["address"]): row
            for row in lib.read_csv(args.contract_review_policy)
        }
        by_id = {pool["pool_id"]: pool for pool in routes["pools"]}
        for summary in pools:
            pool = by_id[summary["pool_id"]]
            if pool["claim_asset"] != "one":
                continue
            expected = 0
            for entry in pool["harmony"]:
                row = review.get(entry["proxy"])
                if row is None:
                    raise ValueError(f"{entry['proxy']} missing from contract review")
                expected += lib.crl.one_str_to_atto(row["native_total_claim_one"])
            if expected != summary["collateral_raw"]:
                raise ValueError(
                    f"{summary['pool_id']}: collateral {summary['collateral_raw']} does "
                    f"not match the migration ledger {expected}"
                )

    claims = {}
    for row in allocations:
        if row["status"] != "claimable" or not row["allocation_raw"]:
            continue
        key = (row["claim_chain"], row["payee"], row["claim_token"])
        item = claims.setdefault(key, {"amount": 0, "pools": set()})
        item["amount"] += row["allocation_raw"]
        item["pools"].add(row["pool_id"])
    claim_digests = {}
    for chain in claim_chains:
        rows = [
            {
                "account": payee,
                "token": token,
                "amount_raw": item["amount"],
                "pools": ";".join(sorted(item["pools"])),
            }
            for (claim_chain, payee, token), item in sorted(claims.items())
            if claim_chain == chain
        ]
        claim_digests[chain] = lib.write_csv(
            os.path.join(out, "claims", f"{chain}.csv"), CLAIM_FIELDS, rows
        )

    custody = custody_plan(routes, config, state, pools)
    pool_digest = lib.write_csv(os.path.join(out, "pools.csv"), POOL_FIELDS, pools)
    allocation_digest = lib.write_csv(
        os.path.join(out, "allocations.csv"), ALLOCATION_FIELDS, allocations
    )
    custody_digest = lib.write_json(os.path.join(out, "custody-plan.json"), custody)
    summary = {
        "schema_version": 1,
        "status": "built",
        "pools": len(pools),
        "unsupported_pools": unsupported,
        "allocation_rows": len(allocations),
        "held_accounts": sum(item["held_accounts"] for item in pools),
        "claim_lists": claim_chains,
        "inputs": {
            "routes_sha256": lib.file_sha256(args.routes),
            "config_sha256": lib.file_sha256(args.config),
            "state_sha256": lib.file_sha256(args.state),
            "plan_sha256": lib.file_sha256(args.plan),
            "messages_sha256": lib.file_sha256(args.messages),
            "classes_sha256": lib.file_sha256(args.classes),
            "exclusions_sha256": lib.file_sha256(args.exclusions) if args.exclusions else None,
            "overrides_sha256": lib.file_sha256(args.overrides) if args.overrides else None,
        },
        "outputs": {
            "pools.csv": pool_digest,
            "allocations.csv": allocation_digest,
            "custody-plan.json": custody_digest,
            **{f"claims/{chain}.csv": digest for chain, digest in claim_digests.items()},
        },
        "policy": {
            "minimum_claim_raw": str(minimum),
            "minimum_claim_raw_by_pool": {key: str(value) for key, value in minimum_by_pool.items()},
            "orphaned_payouts": message_summary["policy"]["orphaned_payouts"],
        },
    }
    lib.write_json(os.path.join(out, "summary.json"), summary)
    lib.write_text(
        os.path.join(out, "BRIDGE_ALLOCATIONS.md"),
        report(summary, pools, plan, custody, message_summary, unsupported, state),
    )
    print(f"wrote {out}: {len(pools)} pools, {len(allocations)} allocation rows")


if __name__ == "__main__":
    main()
