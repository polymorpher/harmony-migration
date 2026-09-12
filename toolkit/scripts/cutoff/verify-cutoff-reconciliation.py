#!/usr/bin/env python3

import argparse
import hashlib
import json
import os


COMPONENT_TO_VERIFY_FIELD = {
    "liquid_shard0": "liquid_shard0_atto",
    "liquid_shard1": "liquid_shard1_atto",
    "liquid_total": "liquid_total_atto",
    "active_staked_or_delegated": "active_staked_or_delegated_atto",
    "pending_undelegation": "pending_undelegation_atto",
    "unclaimed_staking_reward": "unclaimed_staking_reward_atto",
    "pending_cross_shard": "pending_cross_shard_atto",
    "total_claim": "total_claim_atto",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff-root", required=True)
    parser.add_argument("--old-root", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def load(path):
    with open(path) as source:
        return json.load(source)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def equal(left, right, label, checks):
    if str(left) != str(right):
        raise ValueError(f"{label}: {left} != {right}")
    checks.append(label)


def main():
    args = parse_args()
    if os.path.exists(args.output) or os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)

    paths = {
        "shard0_snapshot": f"{args.cutoff_root}/state/shard0-positive-balances-cutoff-summary.json",
        "shard1_snapshot": f"{args.cutoff_root}/state/shard1-positive-balances-cutoff-summary.json",
        "staking": f"{args.cutoff_root}/state/staking-claims-cutoff-summary.json",
        "actual_supply": f"{args.cutoff_root}/state/actual-supply-cutoff.json",
        "shard0_state_diff": f"{args.cutoff_root}/state/shard0-liquid-state-diff-summary.json",
        "shard0_state_diff_rpc": f"{args.cutoff_root}/state/shard0-liquid-state-diff.rpc-verify.json",
        "shard0_created_origins": f"{args.cutoff_root}/state/shard0-created-account-origins-summary.json",
        "shard1_state_diff": f"{args.cutoff_root}/state/shard1-liquid-state-diff-summary.json",
        "ledger": f"{args.cutoff_root}/claims/actual-supply-ledger-cutoff-summary.json",
        "claims": f"{args.cutoff_root}/claims/all-address-claims-cutoff.verify.json",
        "claim_diff": f"{args.cutoff_root}/claims/migration-claims-original-to-cutoff-diff-summary.json",
        "shard0_interval": f"{args.cutoff_root}/interval-audit/shard0-summary.json",
        "shard1_interval": f"{args.cutoff_root}/interval-audit/shard1-summary.json",
        "cross_shard": f"{args.cutoff_root}/cross-shard-supply-cutoff.json",
        "old_claims": f"{args.old_root}/all-address-claims-with-usd-v2-fully-resolved.verify.json",
    }
    data = {name: load(path) for name, path in paths.items()}
    checks = []

    equal(
        data["shard0_snapshot"]["total_balance_atto"],
        data["actual_supply"]["liquid_balance_atto"],
        "shard0 full snapshot liquid equals independent actual-supply scan",
        checks,
    )
    equal(
        data["shard0_snapshot"]["positive_accounts"],
        data["actual_supply"]["positive_account_count"],
        "shard0 positive-account counts agree",
        checks,
    )
    for staking_field, actual_field in (
        ("active_atto", "active_delegation_atto"),
        ("pending_undelegation_atto", "pending_undelegation_atto"),
        ("reward_atto", "unclaimed_delegation_reward_atto"),
        ("validator_count", "validator_count"),
        ("delegation_count", "delegation_count"),
        ("undelegation_entry_count", "undelegation_entry_count"),
    ):
        equal(
            data["staking"][staking_field],
            data["actual_supply"][actual_field],
            f"staking {staking_field} equals independent actual-supply scan",
            checks,
        )

    ledger = data["ledger"]
    claims = data["claims"]
    for ledger_field, verify_field in (
        ("liquid_shard0_atto", "liquid_shard0_atto"),
        ("liquid_shard1_atto", "liquid_shard1_atto"),
        ("active_delegation_atto", "active_staked_or_delegated_atto"),
        ("pending_undelegation_atto", "pending_undelegation_atto"),
        ("unclaimed_reward_atto", "unclaimed_staking_reward_atto"),
        ("pending_cross_shard_atto", "pending_cross_shard_atto"),
        ("total_claim_atto", "total_claim_atto"),
        ("rows", "rows"),
    ):
        equal(
            ledger[ledger_field],
            claims[verify_field],
            f"component ledger {ledger_field} equals strict claim verifier",
            checks,
        )
    equal(claims["rows_without_address"], 0, "all cutoff addresses resolved", checks)
    equal(
        claims["rows_with_address"],
        claims["rows"],
        "every cutoff row has a verified address",
        checks,
    )
    equal(
        data["cross_shard"]["pending_active_atto"],
        ledger["pending_cross_shard_atto"],
        "pending receipt report equals component ledger",
        checks,
    )

    old_claims = data["old_claims"]
    diff = data["claim_diff"]
    for component, verify_field in COMPONENT_TO_VERIFY_FIELD.items():
        totals = diff["component_totals"][component]
        equal(
            totals["old_atto"],
            old_claims[verify_field],
            f"{component} old diff total equals old strict verifier",
            checks,
        )
        equal(
            totals["final_atto"],
            claims[verify_field],
            f"{component} final diff total equals cutoff strict verifier",
            checks,
        )
        expected_delta = int(totals["final_atto"]) - int(totals["old_atto"])
        equal(
            totals["delta_atto"],
            expected_delta,
            f"{component} aggregate delta arithmetic",
            checks,
        )

    equal(
        data["shard0_state_diff"]["net_liquid_delta_atto"],
        diff["component_totals"]["liquid_shard0"]["delta_atto"],
        "shard0 trie-difference net equals full-ledger liquid delta",
        checks,
    )
    equal(
        data["shard0_state_diff_rpc"]["status"],
        "passed",
        "every changed shard0 balance and nonce passed RPC verification",
        checks,
    )
    equal(
        data["shard0_state_diff_rpc"]["net_delta_atto"],
        data["shard0_state_diff"]["net_liquid_delta_atto"],
        "RPC-verified per-account liquid delta equals trie difference",
        checks,
    )
    equal(
        data["shard0_created_origins"]["created_accounts"],
        data["shard0_state_diff"]["created_accounts"],
        "every newly created shard0 account was checked",
        checks,
    )
    equal(
        data["shard0_created_origins"]["trace_matches"],
        data["shard0_state_diff"]["created_accounts"],
        "every newly created shard0 account appears in a canonical execution trace",
        checks,
    )
    equal(
        data["shard0_created_origins"]["protocol_state_transitions"],
        0,
        "no new shard0 account lacks transaction-level trace evidence",
        checks,
    )
    equal(
        data["shard1_state_diff"]["net_liquid_delta_atto"],
        0,
        "shard1 trie difference is zero",
        checks,
    )
    equal(
        data["shard1_interval"]["state_root_changes"],
        0,
        "shard1 interval has no state-root changes",
        checks,
    )
    equal(
        data["shard1_interval"]["normal_transactions"],
        0,
        "shard1 interval has no regular transactions",
        checks,
    )
    equal(
        data["shard1_interval"]["staking_transactions"],
        0,
        "shard1 interval has no staking transactions",
        checks,
    )
    equal(
        data["shard1_interval"]["gas_used"],
        0,
        "shard1 interval has zero gas use",
        checks,
    )
    equal(
        data["shard0_interval"]["cross_shard_transactions"],
        0,
        "shard0 interval has no new cross-shard source transactions",
        checks,
    )
    equal(
        data["shard0_interval"]["last_state_root"],
        data["shard0_snapshot"]["state_root"],
        "shard0 RPC interval ends at enumerated state root",
        checks,
    )
    equal(
        data["shard1_interval"]["last_state_root"],
        data["shard1_snapshot"]["state_root"],
        "shard1 RPC interval ends at enumerated state root",
        checks,
    )

    result = {
        "status": "passed",
        "checks": checks,
        "check_count": len(checks),
        "input_sha256": {name: sha256(path) for name, path in paths.items()},
        "cutoff_blocks": {"shard0": 93623067, "shard1": 95882100},
        "cutoff_roots": {
            "shard0": data["shard0_snapshot"]["state_root"],
            "shard1": data["shard1_snapshot"]["state_root"],
        },
        "total_claim_atto": claims["total_claim_atto"],
        "total_claim_delta_atto": diff["component_totals"]["total_claim"][
            "delta_atto"
        ],
    }
    with open(args.output + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
