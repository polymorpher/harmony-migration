#!/usr/bin/env python3

"""Independently verify a built bridge allocation directory.

Holdings are recomputed from the holder CSVs named in the snapshot plan and
adjustments from the message CSV. Every allocation is re-derived from the
pool scale, every pool must conserve its collateral, claim lists must equal
the claimable allocations exactly, and the custody plan must move exactly
the pool collateral. Output hashes recorded in summary.json must match.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_lib as lib  # noqa: E402

ELIGIBLE_PREFIXES = ("claimable", "held_", "below_minimum")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--messages", required=True)
    parser.add_argument("--holders-dir", required=True)
    parser.add_argument("--allocations-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


class Checks:
    def __init__(self):
        self.failures = []
        self.count = 0

    def require(self, condition, message):
        self.count += 1
        if not condition:
            self.failures.append(message)


def main():
    args = parse_args()
    lib.prepare_outputs([args.output], args.replace)
    routes = lib.load_routes(args.routes)
    config = lib.load_config(args.config, routes)
    state = lib.load_json(args.state)
    plan = lib.load_json(args.plan)
    out = args.allocations_dir
    summary = lib.load_json(os.path.join(out, "summary.json"))
    checks = Checks()

    for name, digest in summary["outputs"].items():
        path = os.path.join(out, name)
        checks.require(
            os.path.exists(path) and lib.file_sha256(path) == digest,
            f"{name}: hash does not match summary.json",
        )

    pools = {row["pool_id"]: row for row in lib.read_csv(os.path.join(out, "pools.csv"))}
    allocations = lib.read_csv(os.path.join(out, "allocations.csv"))
    by_pool = {}
    for row in allocations:
        by_pool.setdefault(row["pool_id"], []).append(row)

    adjustments = {}
    for row in lib.read_csv(args.messages):
        if row["adjustment_account"] and int(row["adjustment_raw"]):
            pool = adjustments.setdefault(row["pool_id"], {})
            account = lib.address(row["adjustment_account"])
            pool[account] = pool.get(account, 0) + int(row["adjustment_raw"])

    registry = {pool["pool_id"]: pool for pool in routes["pools"]}
    expected_pools = {
        pid for pid, item in plan["pools"].items() if item["status"] != "unsupported"
    }
    checks.require(set(pools) == expected_pools, "pools.csv does not cover the planned pools")

    for pid, pool_row in pools.items():
        pool = registry[pid]
        plan_pool = plan["pools"][pid]
        holdings = {}
        for token in plan_pool["tokens"]:
            path = os.path.join(args.holders_dir, token["stem"] + ".csv")
            holder_summary = lib.load_json(
                os.path.join(args.holders_dir, token["stem"] + ".summary.json")
            )
            checks.require(holder_summary.get("status") == "passed", f"{token['stem']}: snapshot not passed")
            checks.require(
                holder_summary.get("output_sha256") == lib.file_sha256(path),
                f"{token['stem']}: snapshot hash mismatch",
            )
            checks.require(
                int(holder_summary["block"]) == plan_pool["snapshot_block"]["number"]
                and holder_summary["block_hash"].lower()
                == plan_pool["snapshot_block"]["hash"].lower(),
                f"{token['stem']}: snapshot block differs from the plan",
            )
            total = 0
            for row in lib.read_csv(path):
                account = lib.address(row["address"])
                amount = int(row["balance_raw"])
                holdings[account] = holdings.get(account, 0) + amount
                total += amount
            checks.require(
                str(total) == str(holder_summary["total_supply_raw"]),
                f"{token['stem']}: holders do not sum to totalSupply",
            )
        pool_adjustments = adjustments.get(pid, {})
        rows = by_pool.get(pid, [])
        accounts = [lib.address(row["account"]) for row in rows]
        checks.require(len(accounts) == len(set(accounts)), f"{pid}: duplicate accounts")
        checks.require(
            set(accounts) == set(holdings) | set(pool_adjustments),
            f"{pid}: allocation accounts differ from holders plus adjustments",
        )
        numerator = int(pool_row["scale_numerator"])
        denominator = int(pool_row["scale_denominator"])
        collateral = int(pool_row["collateral_raw"])
        sums = {"claimable": 0, "held": 0, "eligible": 0, "excluded": 0}
        for row in rows:
            account = lib.address(row["account"])
            holding = holdings.get(account, 0)
            adjustment = pool_adjustments.get(account, 0)
            gross = max(0, holding + adjustment)
            checks.require(int(row["holding_raw"]) == holding, f"{pid} {account}: holding")
            checks.require(int(row["adjustment_raw"]) == adjustment, f"{pid} {account}: adjustment")
            checks.require(int(row["gross_raw"]) == gross, f"{pid} {account}: gross")
            allocation = int(row["allocation_raw"])
            status = row["status"]
            if status.startswith(ELIGIBLE_PREFIXES):
                sums["eligible"] += gross
                expected = gross * numerator // denominator
                if status == "below_minimum":
                    checks.require(allocation == 0, f"{pid} {account}: below-minimum allocation")
                else:
                    checks.require(allocation == expected, f"{pid} {account}: scaled allocation")
                if status == "claimable":
                    sums["claimable"] += allocation
                elif status.startswith("held_"):
                    sums["held"] += allocation
            else:
                checks.require(status.startswith("excluded_"), f"{pid} {account}: unknown status")
                checks.require(allocation == 0, f"{pid} {account}: excluded allocation")
                sums["excluded"] += gross
        checks.require(sums["eligible"] == int(pool_row["eligible_raw"]), f"{pid}: eligible total")
        checks.require(sums["claimable"] == int(pool_row["claimable_raw"]), f"{pid}: claimable total")
        checks.require(sums["held"] == int(pool_row["held_raw"]), f"{pid}: held total")
        checks.require(sums["excluded"] == int(pool_row["excluded_raw"]), f"{pid}: excluded total")
        if sums["eligible"] > collateral:
            checks.require(
                (numerator, denominator) == (collateral, sums["eligible"]),
                f"{pid}: scale is not collateral over eligible",
            )
        else:
            checks.require((numerator, denominator) == (1, 1), f"{pid}: scale should be 1")
        checks.require(
            collateral == sums["claimable"] + sums["held"] + int(pool_row["retained_raw"])
            and int(pool_row["retained_raw"]) >= 0,
            f"{pid}: collateral is not conserved",
        )
        item = state["pools"][pid]
        if pool["kind"] == "remote_escrow":
            state_values = item["remote"]["by_label"]["state"]
            expected_collateral = int(state_values["escrow_balance"]) + sum(
                int(value) for value in state_values["previous_adapter_balances"].values()
            )
        else:
            expected_collateral = sum(
                int(entry["by_label"]["cutoff"]["native_balance"])
                + int(entry["by_label"]["cutoff"]["wone_balance"] or 0)
                for entry in item["harmony"]
            )
        checks.require(collateral == expected_collateral, f"{pid}: collateral differs from state")
        claim_chain, claim_token = lib.claim_target(pool, config)
        checks.require(
            (pool_row["claim_chain"], pool_row["claim_token"]) == (claim_chain, claim_token),
            f"{pid}: claim target",
        )

    expected_claims = {}
    for row in allocations:
        if row["status"] == "claimable" and int(row["allocation_raw"]):
            key = (row["claim_chain"], lib.address(row["payee"]), row["claim_token"])
            expected_claims[key] = expected_claims.get(key, 0) + int(row["allocation_raw"])
    found_claims = {}
    for chain in summary["claim_lists"]:
        for row in lib.read_csv(os.path.join(out, "claims", f"{chain}.csv")):
            key = (chain, lib.address(row["account"]), row["token"])
            checks.require(key not in found_claims, f"claims/{chain}.csv: duplicate {key}")
            checks.require(int(row["amount_raw"]) > 0, f"claims/{chain}.csv: non-positive amount")
            found_claims[key] = int(row["amount_raw"])
    checks.require(found_claims == expected_claims, "claim lists differ from claimable allocations")

    custody = lib.load_json(os.path.join(out, "custody-plan.json"))
    moved = {}
    for chain, item in custody["chains"].items():
        for transfer in item["transfers"]:
            moved[transfer["pool_id"]] = moved.get(transfer["pool_id"], 0) + int(transfer["amount_raw"])
    for pid, amount in moved.items():
        checks.require(
            amount == int(pools[pid]["collateral_raw"]),
            f"{pid}: custody transfers differ from collateral",
        )
    escrow_pools = {
        pid for pid in pools if registry[pid]["kind"] == "remote_escrow"
    }
    planned_transfers = {
        transfer["pool_id"]
        for item in custody["chains"].values() for transfer in item["transfers"]
    }
    checks.require(escrow_pools == planned_transfers, "custody plan does not cover every escrow pool")
    funding = custody["one_funding"]
    one_total = sum(
        int(row["collateral_raw"]) for pid, row in pools.items()
        if registry[pid]["claim_asset"] == "one"
    )
    checks.require(int(funding["amount_raw"]) == one_total, "ONE funding differs from ONE collateral")
    if funding.get("safe_transaction"):
        data = funding["safe_transaction"]["data"]
        checks.require(
            data == lib.SEL["transfer"] + lib.address_word(funding["to"]) + lib.word(one_total),
            "ONE funding calldata does not transfer the ONE collateral to custody",
        )

    result = {
        "schema_version": 1,
        "status": "passed" if not checks.failures else "failed",
        "checks": checks.count,
        "failures": checks.failures[:200],
        "failure_count": len(checks.failures),
        "allocations_summary_sha256": lib.file_sha256(os.path.join(out, "summary.json")),
    }
    lib.write_json(args.output, result)
    print(f"{result['status']}: {checks.count} checks, {len(checks.failures)} failures")
    if checks.failures:
        for failure in checks.failures[:20]:
            print("  " + failure)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
