#!/usr/bin/env python3

"""Verify a random, reproducible sample of claim rows in a frozen evidence bundle.

    python3 toolkit/scripts/random-sample-verify.py \\
      --bundle artifacts/released-migration-bundle \\
      --sample-size 25 --seed 20260925 --output sample-report.json

Every sampled row is checked for exact atto-ONE arithmetic, WONE overlay and
threshold policy, and agreement across the native claims, WONE overlay,
eligibility output, stage policy, routing exceptions, wallet and vault-share
allocation, and airdrop list. Bundle-level metadata, hashes, row counts, and
totals are always verified. Archived chain evidence, when bundled, is compared
separately; the tool never claims chain truth without it.

Exit status: 0 all requested checks passed; 1 a check failed; 2 the bundle or
arguments could not be read; 3 verification was incomplete (a required check
was NOT VERIFIED).
"""

import argparse
import json
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "verifier"))
import sample_core as core  # noqa: E402


TIER_LABELS = (
    ("sampled_rows", "Sampled rows passed"),
    ("bundle_metadata", "Bundle metadata passed"),
    ("chain_truth", "Historical chain truth independently verified"),
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bundle", required=True, type=Path, help="evidence bundle directory")
    parser.add_argument("--sample-size", type=int, help="number of rows to sample (never reduced)")
    parser.add_argument("--seed", help="sampling seed; generated and printed when omitted")
    parser.add_argument(
        "--sample-by",
        choices=("secure_key", "address"),
        default="secure_key",
        help="sampling identifier (default: secure_key)",
    )
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing --output")
    parser.add_argument("--list-files", action="store_true", help="list bundle files with hash status and exit")
    parser.add_argument("--show-row", metavar="ID", help="show and verify one row by secure key or address")
    parser.add_argument(
        "--verify-all-metadata",
        action="store_true",
        help="also check keccak identity for every row; allows a run without a sample",
    )
    parser.add_argument("--expected-manifest-sha256", help="published SHA-256 of bundle-manifest.json")
    parser.add_argument(
        "--require-chain-truth",
        action="store_true",
        help="fail unless archived evidence independently corroborates the cutoff and sampled rows",
    )
    parser.add_argument("--quiet", action="store_true", help="print only the final result lines")
    args = parser.parse_args(argv)
    if args.sample_size is not None and args.sample_size < 0:
        parser.error("--sample-size must be non-negative")
    if (
        not args.list_files
        and not args.show_row
        and not args.verify_all_metadata
        and not args.sample_size
    ):
        parser.error("--sample-size (positive) is required unless --verify-all-metadata, --show-row, or --list-files")
    if args.output and args.output.exists() and not args.replace:
        parser.error(f"{args.output} exists; pass --replace to overwrite it")
    return args


def list_files(bundle):
    verifier = core.Verifier(bundle, sample_size=0, seed="list-files")
    verifier.read_manifest()
    verifier.check_files()
    status = {}
    for check in verifier.report.checks:
        if check["id"] in ("file.present", "file.sha256", "file.entry"):
            if check["status"] != core.PASS or check["subject"] not in status:
                status[check["subject"]] = (check["status"], check["message"])
    print(f"bundle   : {bundle}")
    print(f"manifest : {core.MANIFEST_NAME} sha256 {verifier.manifest_sha256}")
    print()
    print(f"{'status':<13} {'role':<22} {'rows':>10}  path / sha256")
    failed = False
    for entry in verifier.manifest["files"]:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        state, message = status.get(path, (core.FAIL, "not checked"))
        failed |= state != core.PASS
        role = entry.get("role", "") + (f":{entry['category']}" if entry.get("category") else "")
        rows = "" if entry.get("rows") is None else str(entry.get("rows"))
        print(f"{state:<13} {role:<22} {rows:>10}  {path}")
        print(f"{'':<47}{entry.get('sha256')}")
        if state != core.PASS:
            print(f"{'':<47}{message}")
    for check in verifier.report.checks:
        if check["id"] in ("files.required_roles", "files.undeclared") and check["status"] != core.PASS:
            failed = True
            print(f"\n{check['status']}: {check['title']}: {check['message']}")
    return 1 if failed else 0


def one(atto_text):
    return f"{atto_text} atto ({core.fixed18(int(atto_text))} ONE)" if atto_text.isdigit() else atto_text


def print_row_detail(result):
    print(f"\nRow {result['identifier']}")
    print(f"  secure key          : {result['secure_key']}")
    print(f"  address             : {result['address']}")
    print(f"  qualifies (>= 1,000 ONE): {result['qualifies']}")
    print(f"  eligibility category: {result['eligibility_category'] or '-'}")
    print(f"  migration stage     : {result['migration_stage'] or '-'} ({result['issuance_treatment'] or '-'})")
    print(f"  routing category    : {result['routing_category'] or '-'}")
    for field, value in result["values"].items():
        print(f"  {field:<32}: {one(value['atto'])}")
    print("  records:")
    for role, lines in result["records"].items():
        print(f"    {role:<22} {', '.join(lines)}")
    print("  checks:")
    for check in result["checks"]:
        print(f"    {check['status']:<13} {check['id']:<34} {check['title']}")
        if check["status"] != core.PASS and check["message"]:
            print(f"    {'':<13} {check['message']}")


def print_summary(report, quiet):
    bundle = report["bundle"]
    sample = report["sample"]
    if not quiet:
        print("Harmony migration random-sample verification")
        print(f"bundle          : {bundle['path']} ({bundle['bundle_id']})")
        print(f"manifest sha256 : {bundle['manifest_sha256']}")
        cutoff = bundle.get("cutoff") or {}
        for shard in ("shard0", "shard1"):
            item = cutoff.get(shard) or {}
            print(f"{shard} cutoff   : block {item.get('block')} at {item.get('timestamp_utc')} hash {item.get('hash')}")
        print(f"threshold       : {bundle['threshold_atto']} atto (>= 1,000 ONE, inclusive)")
        print(f"seed            : {sample['seed']}" + ("  (generated; record it to reproduce)" if sample["seed_generated"] else ""))
        print(f"sample          : {sample['requested']} of {sample['population']} rows by {sample['sample_by']} ({sample['algorithm']})")
        if sample["identifiers"]:
            print("sampled identifiers:")
            statuses = {row["identifier"]: row["status"] for row in report["rows"]}
            for index, identifier in enumerate(sample["identifiers"], start=1):
                print(f"  {index:>4}. {identifier}  {statuses.get(identifier, '')}")
        problems = [
            check for check in report["checks"]
            if check["status"] in (core.FAIL, core.WARNING)
            or (check["status"] == core.NOT_VERIFIED and check["required"])
        ]
        if problems:
            print("\nproblems:")
            for check in problems[:200]:
                print(f"  {check['status']:<13} [{check['scope']}] {check['id']} ({check['subject']})")
                print(f"  {'':<13} {check['title']}")
                if check["message"]:
                    print(f"  {'':<13} {check['message']}")
                if check["explain"]:
                    print(f"  {'':<13} why: {check['explain']}")
            if len(problems) > 200:
                print(f"  ... {len(problems) - 200} more in the JSON report")
        counts = ", ".join(f"{key} {value}" for key, value in sorted(report["counts"].items()))
        print(f"\nchecks          : {counts}")
    print()
    for key, label in TIER_LABELS:
        print(f"{label:<48}: {report['tiers'][key]}")
    if report["tiers"]["chain_truth"] != core.PASS:
        print("  (the bundle was checked against itself and the pinned cutoff; chain state was not independently proven)")
    print(f"exit status     : {report['exit_code']}")
    if sample["identifiers"]:
        print(f"reproduce       : {sample['reproduce']}")


def write_report(path, report, replace):
    if path.exists() and not replace:
        raise FileExistsError(path)
    partial = Path(str(path) + ".partial")
    with partial.open("x" if not partial.exists() else "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.list_files:
            return list_files(args.bundle)
        report = core.verify_bundle(
            args.bundle,
            sample_size=args.sample_size,
            seed=args.seed,
            sample_by=args.sample_by,
            verify_all_metadata=args.verify_all_metadata,
            expected_manifest_sha256=args.expected_manifest_sha256,
            require_chain_truth=args.require_chain_truth,
            show_row=args.show_row,
        )
    except (core.BundleError, OSError, UnicodeDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return core.EXIT_INPUT
    if args.show_row and not args.sample_size and not args.verify_all_metadata:
        row_status = report["show_row"]["status"]
        metadata = report["tiers"]["bundle_metadata"]
        if core.FAIL in (row_status, metadata):
            report["exit_code"] = core.EXIT_FAIL
        elif row_status == core.PASS and metadata == core.PASS:
            report["exit_code"] = core.EXIT_PASS
        else:
            report["exit_code"] = core.EXIT_INCOMPLETE
    if args.output:
        write_report(args.output, report, args.replace)
    print_summary(report, args.quiet)
    if args.show_row:
        print_row_detail(report["show_row"])
    if args.output:
        print(f"report          : {args.output}")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
