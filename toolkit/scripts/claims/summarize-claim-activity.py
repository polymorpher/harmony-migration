#!/usr/bin/env python3

"""Summarize cutoff claim value by latest indexed account activity."""

import argparse
import calendar
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


CONTRACT_REVIEW = Path(__file__).resolve().parents[1] / "contract-review"
sys.path.insert(0, str(CONTRACT_REVIEW))
import contract_review_lib as lib  # noqa: E402


ATTO_PER_ONE = 10**18
COMPONENTS = (
    "wallet_airdrop",
    "wone_airdrop",
    "staked_to_vault",
    "total_claim",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--activity-summary", required=True)
    parser.add_argument("--snapshot-manifest", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument(
        "--months",
        default="3,6,12,24,36,48",
        help="comma-separated cumulative calendar-month windows",
    )
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        args.months = tuple(
            sorted({int(value) for value in args.months.split(",")})
        )
    except ValueError as error:
        parser.error(f"invalid --months: {error}")
    if not args.months or any(months <= 0 for months in args.months):
        parser.error("--months must contain positive integers")
    return args


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc(value):
    if not value.endswith("Z"):
        raise ValueError(f"UTC timestamp must end in Z: {value}")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"timestamp is not UTC: {value}")
    return parsed


def format_utc(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def subtract_calendar_months(value, months):
    absolute_month = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def one(value):
    whole, fraction = divmod(int(value), ATTO_PER_ONE)
    return f"{whole:,}.{fraction:018d}"


def percentage(value, total):
    if not total:
        return "0.0000"
    scaled = (int(value) * 1_000_000 + int(total) // 2) // int(total)
    whole, fraction = divmod(scaled, 10_000)
    return f"{whole}.{fraction:04d}"


def empty_totals():
    return {component: 0 for component in COMPONENTS}


def add_row(totals, row):
    for component in COMPONENTS:
        value = int(row.get(f"{component}_atto", "0") or 0)
        if value < 0:
            raise ValueError(
                f"negative {component} for {row['secure_key']}"
            )
        totals[component] += value


def serializable_totals(totals):
    result = {}
    for component in COMPONENTS:
        result[f"{component}_atto"] = str(totals[component])
        result[f"{component}_one"] = lib.atto_to_one_str(
            totals[component]
        )
    return result


def load_cutoff(path):
    with open(path, encoding="utf-8") as source:
        manifest = json.load(source)
    cutoff = (manifest.get("cutoff") or {}).get("requested_time_utc")
    if not cutoff:
        raise ValueError("snapshot manifest is missing cutoff time")
    return cutoff, parse_utc(cutoff)


def load_activity_summary(path, input_sha256):
    with open(path, encoding="utf-8") as source:
        summary = json.load(source)
    if summary.get("status") != "passed":
        raise ValueError("activity enrichment did not pass")
    if summary.get("output_sha256") != input_sha256:
        raise ValueError(
            "activity enrichment summary does not identify the input CSV"
        )
    candidates = summary.get("candidates")
    if (
        not isinstance(candidates, int)
        or summary.get("activity_found", 0)
        + summary.get("activity_not_found", 0)
        != candidates
    ):
        raise ValueError("activity enrichment coverage does not close")
    return summary


def summarize(path, cutoff, months):
    cutoffs = {
        window: subtract_calendar_months(cutoff, window)
        for window in months
    }
    windows = {
        window: {"accounts": 0, "totals": empty_totals()}
        for window in months
    }
    all_totals = empty_totals()
    found_totals = empty_totals()
    not_found_totals = empty_totals()
    before_longest_totals = empty_totals()
    candidates = 0
    found = 0
    not_found = 0
    before_longest = 0
    previous_key = None
    exact_threshold_rows = 0

    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "secure_key",
            "address",
            "total_claim_atto",
            "wallet_airdrop_atto",
            "staked_to_vault_atto",
            "last_activity_time_utc",
            "last_activity_timestamp_unix",
            "last_activity_block",
            "last_activity_shard",
            "last_activity_type",
            "last_activity_tx_hash",
            "last_activity_index",
            "last_activity_detail",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"input is missing fields: {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            key = row["secure_key"].lower()
            if previous_key is not None and key <= previous_key:
                raise ValueError(
                    f"secure keys are not increasing at line {line}"
                )
            previous_key = key
            lib.require_address_secure_key(
                row["address"], key, f"input line {line}"
            )
            total_claim = int(row["total_claim_atto"])
            if total_claim < 1000 * ATTO_PER_ONE:
                raise ValueError(
                    f"input line {line}: claim is below 1,000 ONE"
                )
            if (
                int(row["wallet_airdrop_atto"])
                + int(row["staked_to_vault_atto"])
                != total_claim
            ):
                raise ValueError(
                    f"input line {line}: delivery components do not close"
                )
            exact_threshold_rows += int(
                total_claim == 1000 * ATTO_PER_ONE
            )
            candidates += 1
            add_row(all_totals, row)

            timestamp_text = row["last_activity_timestamp_unix"]
            time_text = row["last_activity_time_utc"]
            evidence = (
                row["last_activity_block"],
                row["last_activity_shard"],
                row["last_activity_type"],
                row["last_activity_tx_hash"],
                row["last_activity_index"],
                row["last_activity_detail"],
            )
            if not timestamp_text:
                if time_text or any(evidence):
                    raise ValueError(
                        f"input line {line}: partial activity evidence"
                    )
                not_found += 1
                add_row(not_found_totals, row)
                continue
            if not time_text or not all(evidence):
                raise ValueError(
                    f"input line {line}: partial activity evidence"
                )
            timestamp = int(timestamp_text)
            activity_time = parse_utc(time_text)
            if int(activity_time.timestamp()) != timestamp:
                raise ValueError(
                    f"input line {line}: activity timestamp mismatch"
                )
            if activity_time > cutoff:
                raise ValueError(
                    f"input line {line}: activity is after cutoff"
                )
            found += 1
            add_row(found_totals, row)
            matched = False
            for window in months:
                if activity_time >= cutoffs[window]:
                    windows[window]["accounts"] += 1
                    add_row(windows[window]["totals"], row)
                    matched = True
            if not matched:
                before_longest += 1
                add_row(before_longest_totals, row)

    previous_accounts = 0
    previous_total = 0
    serializable_windows = []
    for window in months:
        record = windows[window]
        total = record["totals"]["total_claim"]
        if record["accounts"] < previous_accounts or total < previous_total:
            raise ValueError("activity windows are not cumulative")
        previous_accounts = record["accounts"]
        previous_total = total
        serializable_windows.append(
            {
                "months": window,
                "since_time_utc": format_utc(cutoffs[window]),
                "accounts": record["accounts"],
                **serializable_totals(record["totals"]),
                "share_of_all_claim_percent": percentage(
                    total, all_totals["total_claim"]
                ),
            }
        )

    if found + not_found != candidates:
        raise ValueError("activity coverage does not close")
    if (
        windows[months[-1]]["accounts"] + before_longest
        != found
    ):
        raise ValueError("longest activity window does not close")
    for component in COMPONENTS:
        if (
            found_totals[component] + not_found_totals[component]
            != all_totals[component]
        ):
            raise ValueError(f"{component} activity totals do not close")

    return {
        "candidates": candidates,
        "exact_threshold_rows": exact_threshold_rows,
        "all_candidates": serializable_totals(all_totals),
        "indexed_activity_found": {
            "accounts": found,
            **serializable_totals(found_totals),
        },
        "indexed_activity_not_found": {
            "accounts": not_found,
            **serializable_totals(not_found_totals),
        },
        "before_longest_window": {
            "accounts": before_longest,
            **serializable_totals(before_longest_totals),
        },
        "windows": serializable_windows,
    }


def markdown_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend(
        "| " + " | ".join(str(value) for value in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def render_report(summary):
    rows = [
        (
            f"within {entry['months']} months",
            entry["since_time_utc"],
            f"{entry['accounts']:,}",
            one(entry["total_claim_atto"]),
            one(entry["wone_airdrop_atto"]),
            entry["share_of_all_claim_percent"] + "%",
        )
        for entry in summary["windows"]
    ]
    no_activity = summary["indexed_activity_not_found"]
    before = summary["before_longest_window"]
    longest = summary["windows"][-1]["months"]
    provenance = summary.get("activity_provenance") or {}
    shard0 = (provenance.get("by_shard") or {}).get("0") or {}
    breakdown = shard0.get("provenance_breakdown") or {}
    provenance_lines = [
        f"- Activity source classification: `{provenance.get('classification', 'unknown')}`."
    ]
    for kind in ("database-derived", "RPC-derived"):
        bucket = breakdown.get(kind)
        if bucket:
            provenance_lines.append(
                f"- Shard-0 {kind}: `{bucket['rows']:,}` rows "
                f"(`{bucket['activity_found']:,}` with activity, "
                f"`{bucket['activity_not_found']:,}` without)."
            )
    provenance_text = "\n".join(provenance_lines)
    return f"""# Prioritized-claim account activity

Generated from the inclusive `>= 1,000 ONE` cutoff candidate ledger.

## Definition

For each candidate, **last activity** is the latest indexed direct regular
transaction involving the address on shard 0 or shard 1, or indexed shard-0
staking transaction, whose block is canonical and at or before
`{summary["cutoff_time_utc"]}`.

This is transaction activity, not proof that a person still controls the
account. The per-address index does not include internal EVM traces or
validator consensus signatures, so those events are excluded. An empty
activity time means no qualifying indexed transaction was found; it does not
prove that the account was never used.

The index records top-level transaction involvement, including failed,
reverted, and zero-value transactions. A regular `recipient` entry means the
address was `tx.To`; for a cross-shard transaction it records source-side
destination intent and does not prove that the destination receipt was
applied. A validator-side staking entry can be a delegation or undelegation
submitted by somebody else. Contract creation indexes the creator, not the new
contract address.

Calendar-month windows are measured backwards from the cutoff. They are
cumulative: an account in the 3-month row is also in every longer row.

## Cumulative claim value by recent activity

{markdown_table(
    (
        "Last indexed activity",
        "On or after (UTC)",
        "Accounts",
        "Total claim ONE",
        "WONE airdrop ONE",
        "Share of all candidate claims",
    ),
    rows,
)}

## Coverage outside the windows

- All candidates: `{summary["candidates"]:,}` accounts,
  `{one(summary["all_candidates"]["total_claim_atto"])} ONE`, including
  `{one(summary["all_candidates"]["wone_airdrop_atto"])} ONE` from WONE.
- Indexed activity found: `{summary["indexed_activity_found"]["accounts"]:,}`
  accounts.
- Last indexed activity before the {longest}-month window:
  `{before["accounts"]:,}` accounts,
  `{one(before["total_claim_atto"])} ONE`.
- No indexed direct/staking activity found:
  `{no_activity["accounts"]:,}` accounts,
  `{one(no_activity["total_claim_atto"])} ONE`.
- Accounts exactly at the inclusive 1,000 ONE threshold:
  `{summary["exact_threshold_rows"]:,}`.

## Evidence and use

{provenance_text}
- Candidate CSV: `{summary["input"]}`
- Candidate CSV SHA-256: `{summary["input_sha256"]}`
- Activity enrichment summary: `{summary["activity_summary"]}`
- Activity enrichment summary SHA-256:
  `{summary["activity_summary_sha256"]}`
- Snapshot manifest: `{summary["snapshot_manifest"]}`
- Snapshot manifest SHA-256:
  `{summary["snapshot_manifest_sha256"]}`

These activity fields and aggregates are context only. They do not change
claim amounts, eligibility, or routing.
"""


def main():
    args = parse_args()
    for path in (args.summary, args.report):
        if os.path.exists(path + ".partial"):
            raise FileExistsError(path + ".partial")
        if os.path.exists(path) and not args.replace:
            raise FileExistsError(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    cutoff_text, cutoff = load_cutoff(args.snapshot_manifest)
    input_sha256 = file_sha256(args.input)
    activity_summary = load_activity_summary(
        args.activity_summary, input_sha256
    )
    result = {
        "schema_version": 1,
        "status": "passed",
        "activity_definition": (
            "latest indexed direct regular transaction involving the "
            "address on shard 0 or shard 1, or indexed shard-0 staking "
            "transaction whose block is canonical and at or before the "
            "claim cutoff"
        ),
        "window_definition": (
            "cumulative calendar months measured backward from cutoff"
        ),
        "limitations": [
            (
                "failed, reverted, and zero-value top-level transactions "
                "remain indexed"
            ),
            (
                "a cross-shard recipient entry is source-side destination "
                "intent, not proof that its destination receipt was applied"
            ),
            (
                "validator-side staking activity may have been submitted "
                "by another delegator"
            ),
            (
                "internal EVM calls, validator consensus signatures, and "
                "protocol-driven balance changes are excluded"
            ),
            (
                "contract creation indexes the creator rather than the "
                "new contract address"
            ),
        ],
        "cutoff_time_utc": cutoff_text,
        "input": args.input,
        "input_sha256": input_sha256,
        "activity_summary": args.activity_summary,
        "activity_summary_sha256": file_sha256(
            args.activity_summary
        ),
        "activity_provenance": {
            "classification": activity_summary.get(
                "classification", "unknown"
            ),
            "by_shard": {
                shard: {
                    "classification": source["scan"].get(
                        "classification", "unknown"
                    ),
                    "source_kind": source["scan"].get("source_kind"),
                    "provenance_breakdown": source["scan"].get(
                        "provenance_breakdown", {}
                    ),
                }
                for shard, source in (
                    activity_summary.get("sources") or {}
                ).items()
            },
        },
        "snapshot_manifest": args.snapshot_manifest,
        "snapshot_manifest_sha256": file_sha256(
            args.snapshot_manifest
        ),
        **summarize(args.input, cutoff, args.months),
    }
    if result["candidates"] != activity_summary["candidates"]:
        raise ValueError(
            "activity summary candidate count does not match the CSV"
        )
    summary_partial = args.summary + ".partial"
    with open(summary_partial, "x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(summary_partial, args.summary)

    report_partial = args.report + ".partial"
    with open(report_partial, "x", encoding="utf-8") as output:
        output.write(render_report(result))
        output.flush()
        os.fsync(output.fileno())
    os.replace(report_partial, args.report)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
