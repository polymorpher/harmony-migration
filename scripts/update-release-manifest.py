#!/usr/bin/env python3

import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "manifests" / "releases" / "2026-09-11.json"
RELEASE_FILES = (
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "all-address-native-claims-cutoff.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "all-address-migration-claims-cutoff.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "all-address-migration-claims-cutoff-metadata.csv",
    ROOT
    / "artifacts"
    / "wone-holder-accounting-20260917"
    / "wone-holders-cutoff-excluding-layerzero.csv",
    ROOT
    / "artifacts"
    / "wone-holder-accounting-20260917"
    / "wone-only-qualified-metadata.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "migration-claims-at-least-1000-one.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "migration-claims-at-least-1000-one-metadata.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "account-activity-shard0.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "account-activity-shard1.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "migration-claims-at-least-1000-one-metadata-activity.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "policy-automatic.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "policy-genuine-contract-review.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "policy-excluded.csv",
    *(
        ROOT / "exchanges" / "wallets-standardized" / filename
        for filename in (
            "binance.csv",
            "binance-us.csv",
            "gate.csv",
            "mexc.csv",
            "okx.csv",
            "kucoin.csv",
            "summary.json",
        )
    ),
    *(
        ROOT
        / "artifacts"
        / "exchange-accounting-20260917"
        / filename
        for filename in (
            "summary.json",
            "routing-verification.json",
            "EXCHANGE_MIGRATION_ACCOUNTING_2026-09-17.md",
            "GATE_AUTOMATIC_AIRDROP_AUDIT_2026-09-17.md",
            "gate-airdropped.csv",
            "gate-not-airdropped.csv",
            "qualified-non-gate-exclusions.csv",
            "exchange-routes.csv",
            "exchange-destinations.csv",
        )
    ),
    *(
        ROOT
        / "artifacts"
        / "exchange-accounting-20260917"
        / "audits"
        / f"{exchange_id}.csv"
        for exchange_id in (
            "binance",
            "binance-us",
            "gate",
            "mexc",
            "okx",
            "kucoin",
        )
    ),
    *(
        ROOT
        / "artifacts"
        / "exchange-accounting-20260917"
        / "memos"
        / f"{exchange_id}.md"
        for exchange_id in (
            "binance",
            "binance-us",
            "gate",
            "mexc",
            "okx",
            "kucoin",
        )
    ),
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "contract-review-policy.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "state"
    / "staked-to-vault-by-delegation-rpc.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "base-validator-vault-deposits.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "base-priority-vault-shares.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "base-deferred-vault-shares.csv",
    ROOT
    / "artifacts"
    / "migration-policy-20260917"
    / "migration-stage-policy.csv",
    ROOT
    / "artifacts"
    / "migration-policy-20260917"
    / "migration-stage-summary.json",
    ROOT
    / "artifacts"
    / "migration-policy-20260917"
    / "migration-stage.verify.json",
    ROOT
    / "artifacts"
    / "migration-policy-20260917"
    / "INITIAL_STAGE_MATERIALIZATION_2026-09-17.md",
    ROOT
    / "routing"
    / "local"
    / "not-issuing.csv",
    ROOT
    / "routing"
    / "local"
    / "not-issuing-summary.json",
    ROOT
    / "routing"
    / "local"
    / "bridge-reserves.csv",
    ROOT
    / "routing"
    / "local"
    / "bridge-reserves-summary.json",
    ROOT
    / "routing"
    / "local"
    / "contract-policy.csv",
    ROOT
    / "routing"
    / "local"
    / "contract-policy-summary.json",
    ROOT
    / "routing"
    / "local"
    / "exchanges.csv",
    ROOT
    / "routing"
    / "local"
    / "exchange-destinations.csv",
    ROOT
    / "routing"
    / "local"
    / "generated"
    / "routing-exceptions.csv",
    ROOT
    / "routing"
    / "local"
    / "generated"
    / "validator-governor-exceptions.csv",
    ROOT
    / "routing"
    / "local"
    / "generated"
    / "validator-vault-stages.csv",
    *(
        ROOT
        / "routing"
        / "local"
        / "generated"
        / "initial-stage"
        / filename
        for filename in (
            "wallet-allocations.csv",
            "vault-shares.csv",
            "validator-vaults.csv",
            "unresolved.csv",
            "summary.json",
        )
    ),
    ROOT
    / "routing"
    / "local"
    / "generated"
    / "unresolved-routing.csv",
    ROOT
    / "routing"
    / "local"
    / "generated"
    / "routing-summary.json",
    ROOT
    / "artifacts"
    / "supply-reconciliation-20260911"
    / "reported-wallet-theft-perpetrator-related-cutoff.csv",
    ROOT
    / "artifacts"
    / "supply-reconciliation-20260911"
    / "wallet-theft-inventory-additions-20260916.csv",
    ROOT
    / "artifacts"
    / "supply-reconciliation-20260911"
    / "wallet-theft-victim-inventory-20260916.csv",
    ROOT
    / "artifacts"
    / "supply-reconciliation-20260911"
    / "non-issuance-inventory.csv",
    ROOT
    / "artifacts"
    / "supply-reconciliation-20260911"
    / "treasury-reclaim-inventory.csv",
)


def inspect(path):
    digest = hashlib.sha256()
    lines = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            lines += chunk.count(b"\n")
    relative = path.relative_to(ROOT).as_posix()
    name = path.name
    if name == "all-address-native-claims-cutoff.csv":
        domain = "claim-accounting"
        classification = "database-derived"
    elif name in {
        "all-address-migration-claims-cutoff.csv",
        "migration-claims-at-least-1000-one.csv",
        "migration-stage-policy.csv",
        "migration-stage-summary.json",
        "migration-stage.verify.json",
        "INITIAL_STAGE_MATERIALIZATION_2026-09-17.md",
    }:
        domain = "claim-accounting"
        classification = "policy-scenario"
    elif name in {
        "wone-holders-cutoff-excluding-layerzero.csv",
        "wone-only-qualified-metadata.csv",
    }:
        domain = "claim-accounting"
        classification = "RPC-derived"
    elif name == "staked-to-vault-by-delegation-rpc.csv":
        domain = "claim-accounting"
        classification = "RPC-derived"
    elif name == "account-activity-shard0.csv":
        domain = "claim-accounting"
        classification = "hybrid"
    elif name == "account-activity-shard1.csv":
        domain = "claim-accounting"
        classification = "RPC-derived"
    elif name == "migration-claims-at-least-1000-one-metadata-activity.csv":
        domain = "claim-accounting"
        classification = "hybrid"
    elif name in {
        "all-address-migration-claims-cutoff-metadata.csv",
        "migration-claims-at-least-1000-one-metadata.csv",
    }:
        domain = "claim-accounting"
        classification = "policy-scenario"
    elif name == "contract-review-policy.csv":
        domain = "destination-mapping"
        classification = "RPC-derived"
    else:
        domain = "destination-mapping"
        classification = "policy-scenario"
    return {
        "path": relative,
        "domain": domain,
        "classification": classification,
        "bytes": path.stat().st_size,
        "rows": max(lines - 1, 0),
        "sha256": digest.hexdigest(),
    }


def main():
    for path in RELEASE_FILES:
        if not path.is_file():
            raise FileNotFoundError(path)
    result = {
        "schema_version": 1,
        "release": "2026-09-11",
        "status": "private-during-numerical-embargo",
        "entries": [inspect(path) for path in RELEASE_FILES],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(OUTPUT) + ".partial")
    if partial.exists():
        raise FileExistsError(partial)
    with partial.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, OUTPUT)
    print(f"wrote {len(RELEASE_FILES)} entries to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
