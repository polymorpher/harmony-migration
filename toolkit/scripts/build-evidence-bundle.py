#!/usr/bin/env python3

"""Freeze pipeline outputs into a versioned, hash-bound evidence bundle.

The bundle is a directory holding copies of the claim, overlay, eligibility,
stage, routing, and allocation files, optional airdrop and archived chain
evidence, and bundle-manifest.json. The manifest pins the public cutoff,
records every file's SHA-256, size, and row count, and declares every total the
random-sample verifier recomputes.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "verifier"))
import sample_core as core  # noqa: E402


LAYOUT = {
    "native_claims": "claims/native-claims.csv",
    "wone_overlay": "claims/wone-overlay-claims.csv",
    "stage_policy": "policy/migration-stage-policy.csv",
    "routing_exceptions": "routing/routing-exceptions.csv",
    "wallet_allocations": "allocation/wallet-allocations.csv",
    "vault_shares": "allocation/vault-shares.csv",
    "vault_delegations": "ledgers/vault-delegations.csv",
    "wone_holders": "ledgers/wone-holders.csv",
    "exchange_wallets": "ledgers/exchange-wallets.csv",
    "wone_overlay_summary": "summaries/wone-overlay-summary.json",
    "eligibility_summary": "summaries/eligibility-summary.json",
    "stage_summary": "summaries/migration-stage-summary.json",
    "routing_summary": "summaries/routing-summary.json",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="new bundle directory")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument(
        "--created-utc",
        help="YYYY-MM-DDTHH:MM:SSZ (default: now); pass it for byte-identical rebuilds",
    )
    parser.add_argument(
        "--sources",
        required=True,
        help="JSON list of source objects: id, kind, description, authoritative, retrieved_utc",
    )
    parser.add_argument("--default-source", required=True, help="source id for pipeline files")
    parser.add_argument(
        "--source-for",
        action="append",
        default=[],
        metavar="ROLE=SOURCE_ID",
        help="override the source of one role (repeatable)",
    )
    for role in LAYOUT:
        parser.add_argument("--" + role.replace("_", "-"))
    parser.add_argument(
        "--eligibility",
        action="append",
        default=[],
        metavar="CATEGORY=PATH",
        help="eligibility category output: automatic, contract_review, excluded_address",
    )
    parser.add_argument("--airdrop-run", help="airdrop run directory (manifest.json, distribution.csv, batches/)")
    parser.add_argument("--airdrop-scope", choices=("complete", "partial"), default="partial")
    parser.add_argument(
        "--historical-evidence",
        action="append",
        default=[],
        help="harmony-historical-evidence/v1 JSON file (repeatable)",
    )
    return parser.parse_args(argv)


def pairs(values, label):
    result = {}
    for value in values:
        name, sep, item = value.partition("=")
        if not sep or not name or not item:
            raise ValueError(f"{label}: expected NAME=VALUE, got {value!r}")
        if name in result:
            raise ValueError(f"{label}: {name} given twice")
        result[name] = item
    return result


def build(args):
    rules = core.load_rules()
    snapshot = core.load_snapshot()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"{output} already exists; bundles are immutable")
    sources = core.parse_json_strict(Path(args.sources).read_bytes(), "sources")
    if not isinstance(sources, list):
        raise ValueError("sources must be a JSON list")
    source_ids = {item.get("id") for item in sources if isinstance(item, dict)}
    if args.default_source not in source_ids:
        raise ValueError(f"--default-source {args.default_source!r} is not in --sources")
    role_sources = pairs(args.source_for, "--source-for")
    for source in role_sources.values():
        if source not in source_ids:
            raise ValueError(f"--source-for names unknown source {source!r}")

    staged = []  # (role, category, source path, bundle path)
    for role, relative in LAYOUT.items():
        path = getattr(args, role)
        if path:
            staged.append((role, "", Path(path), relative))
    for category, path in pairs(args.eligibility, "--eligibility").items():
        if category not in rules["roles"]["eligibility"]["categories"]:
            raise ValueError(f"unknown eligibility category {category!r}")
        staged.append(("eligibility", category, Path(path), f"eligibility/{category}.csv"))
    if args.airdrop_run:
        run = Path(args.airdrop_run)
        staged.append(("airdrop_manifest", "", run / "manifest.json", "airdrop/manifest.json"))
        staged.append(("airdrop_distribution", "", run / "distribution.csv", "airdrop/distribution.csv"))
        for batch in sorted((run / "batches").glob("batch-*.json")):
            staged.append(("airdrop_batch", "", batch, f"airdrop/batches/{batch.name}"))
    evidence_sources = {}
    for index, path in enumerate(args.historical_evidence):
        doc = core.parse_json_strict(Path(path).read_bytes(), path)
        source = doc.get("source_id") if isinstance(doc, dict) else None
        if source not in source_ids:
            raise ValueError(f"{path}: source_id {source!r} is not in --sources")
        relative = f"evidence/{index:03d}-{Path(path).name}"
        evidence_sources[relative] = source
        staged.append(("historical_evidence", "", Path(path), relative))

    partial = Path(str(output) + ".partial")
    if partial.exists():
        raise FileExistsError(partial)
    partial.mkdir(parents=True)
    try:
        files = []
        for role, category, source_path, relative in staged:
            target = partial / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target)
            entry = {
                "role": role,
                "path": relative,
                "sha256": core.file_sha256(target),
                "bytes": target.stat().st_size,
                "source": evidence_sources.get(relative)
                or role_sources.get(role)
                or args.default_source,
            }
            if category:
                entry["category"] = category
            if role in rules["roles"]:
                entry["rows"] = core.count_csv_rows(target)
            files.append(entry)
        manifest = {
            "format": rules["bundle_format"],
            "bundle_id": args.bundle_id,
            "created_utc": args.created_utc
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "network": rules["constants"]["NETWORK"]["str"],
            "chain_ids": {
                "shard0": int(rules["constants"]["CHAIN_ID_SHARD0"]["int"]),
                "shard1": int(rules["constants"]["CHAIN_ID_SHARD1"]["int"]),
            },
            "cutoff": snapshot["cutoff"],
            "threshold_atto": rules["constants"]["THRESHOLD_ATTO"]["int"],
            "initial_window_since_utc": rules["constants"]["INITIAL_SINCE_UTC"]["str"],
            "airdrop_scope": args.airdrop_scope,
            "sources": sources,
            "files": files,
        }
        manifest["declared_totals"] = core.compute_declared_totals(
            partial, manifest, rules=rules, snapshot=snapshot
        )
        with (partial / core.MANIFEST_NAME).open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, output)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return manifest


def main(argv=None):
    args = parse_args(argv)
    manifest = build(args)
    digest = core.file_sha256(Path(args.output) / core.MANIFEST_NAME)
    print(f"wrote {args.output} with {len(manifest['files'])} files")
    print(f"bundle-manifest.json sha256 {digest}")


if __name__ == "__main__":
    main()
