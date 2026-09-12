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
    / "all-address-migration-claims-cutoff.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "claims"
    / "migration-claims-at-least-1000-one.csv",
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
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "contract-review-all.csv",
    ROOT
    / "artifacts"
    / "cutoff-20260910"
    / "state"
    / "staked-to-vault-by-delegation-rpc.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "validator-vault-deposits.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "priority-vault-shares.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "deferred-vault-shares.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "automatic-wallet-airdrop.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "contract-wallet-recovery.csv",
    ROOT
    / "artifacts"
    / "contract-review-20260911"
    / "out"
    / "excluded-wallet-routing.csv",
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
    if name in {
        "all-address-migration-claims-cutoff.csv",
        "migration-claims-at-least-1000-one.csv",
    }:
        domain = "claim-accounting"
        classification = (
            "database-derived"
            if name.startswith("all-address-")
            else "policy-scenario"
        )
    elif name == "staked-to-vault-by-delegation-rpc.csv":
        domain = "claim-accounting"
        classification = "RPC-derived"
    elif name == "contract-review-all.csv":
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
