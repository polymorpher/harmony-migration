"""Checks that a cross-shard-supply report saw every outgoing receipt.

Pending and retired-shard totals come from a scan of each source database's
outgoing receipt groups. A database without full history yields fewer groups,
and nothing in the totals shows it, so a report is accepted only when

1. it records complete history and receipt coverage for both source shards
   (cross-shard-supply refuses to publish otherwise), or it predates that
   recording and every direction agrees exactly with an independent report
   produced from other archive databases, and
2. every source shard is backed by independent evidence: an independently
   produced list of canonical outgoing receipts (forensics/outgoing-cx-scan)
   that the report accounts for receipt for receipt where it lists them and in
   count and amount where it aggregates, or an agreeing independent report.
"""

import csv
from collections import defaultdict

MAINNET_CROSS_TX_FIRST_BLOCK = 786432
ACTIVE_SHARDS = (0, 1)
LEGACY_REPORT = (
    "the report predates coverage recording, so it cannot show which "
    "databases and blocks the receipt scan covered"
)
AGREEMENT_FIELDS = (
    "canonical_receipt_groups", "spent_receipt_groups", "spent_receipt_count", "spent_amount_atto",
    "pending_receipt_groups", "pending_receipt_count", "pending_amount_atto",
    "unsupported_receipt_groups", "unsupported_receipt_count", "unsupported_amount_atto",
)


def provenance_problems(report, cutoffs):
    """Return why the report cannot be shown to cover all history."""
    if "coverage_complete" not in report or "directions" not in report:
        return [LEGACY_REPORT]
    problems = []
    if not report["coverage_complete"]:
        problems.append("the report is marked coverage_complete=false")
        problems.extend(
            f"cross-shard-supply reported: {problem}"
            for problem in report.get("coverage_problems") or []
        )
    if report.get("incomplete_history_allowed"):
        problems.append("the report was produced with -allow-incomplete-history")
    for shard in ACTIVE_SHARDS:
        recorded = report.get(f"shard{shard}_cutoff")
        if int(recorded or 0) != cutoffs[shard]:
            problems.append(f"shard {shard} cutoff is {recorded}, expected {cutoffs[shard]}")
    sources = sorted(direction["source_shard"] for direction in report["directions"])
    if sources != list(ACTIVE_SHARDS):
        problems.append(f"the report scans source shards {sources}, not both 0 and 1")
    for direction in report["directions"]:
        shard = direction["source_shard"]
        coverage = direction.get("source_coverage") or {}
        if not coverage.get("complete"):
            problems.append(
                f"shard {shard} lacks outgoing receipt groups for "
                f"{coverage.get('blocks_missing')} of {coverage.get('blocks_expected')} blocks"
            )
        if int(coverage.get("from_block", MAINNET_CROSS_TX_FIRST_BLOCK + 1)) > MAINNET_CROSS_TX_FIRST_BLOCK:
            problems.append(
                f"shard {shard} coverage starts at block {coverage.get('from_block')}, "
                f"after the first cross-shard block {MAINNET_CROSS_TX_FIRST_BLOCK}"
            )
        if int(coverage.get("to_block", 0)) != cutoffs.get(shard):
            problems.append(
                f"shard {shard} coverage ends at block {coverage.get('to_block')}, "
                f"not at the cutoff {cutoffs.get(shard)}"
            )
    databases = report.get("databases") or {}
    for shard in ACTIVE_SHARDS:
        database = databases.get(f"shard{shard}")
        if database is None:
            problems.append(f"the report does not describe the shard {shard} database")
            continue
        history = database.get("history") or {}
        if not history.get("complete"):
            problems.append(
                f"shard {shard} {database.get('kind')} lacks block history: snapdb marker "
                f"{history.get('snapdb_marker')}, {history.get('missing_probes')} of "
                f"{history.get('probes')} probed blocks missing"
            )
        if database.get("kind") == "cx-lookup-snapshot" and int(
            database.get("lookup_snapshot_cutoff") or 0
        ) < cutoffs[shard]:
            problems.append(f"shard {shard} lookup snapshot ends before the cutoff")
    return problems


def read_outgoing(paths):
    """Read outgoing-cx-scan CSVs."""
    rows = []
    for path in paths:
        with open(path, newline="") as source:
            for row in csv.DictReader(source):
                rows.append({
                    "source": int(row["source_shard"]),
                    "destination": int(row["destination_shard"]),
                    "block": int(row["source_block"]),
                    "tx": row["tx_hash"].lower(),
                    "to": row["to"].lower(),
                    "amount": int(row["amount_atto"]),
                })
    return rows


def comparison(report, outgoing, cutoffs):
    """Compare the report with an independent outgoing-receipt list.

    Returns (problems, per-source statistics, source shards without a list).
    """
    problems = []
    stats = {}
    by_source = defaultdict(list)
    for row in outgoing:
        if row["source"] in cutoffs and row["block"] <= cutoffs[row["source"]]:
            by_source[row["source"]].append(row)
    directions = {direction["source_shard"]: direction for direction in report.get("directions", [])}
    for source, rows in sorted(by_source.items()):
        direction = directions.get(source)
        if direction is None:
            problems.append(f"the list has shard {source} receipts but the report has no shard {source} scan")
            continue
        active = [row for row in rows if row["destination"] in ACTIVE_SHARDS]
        retired = [row for row in rows if row["destination"] not in ACTIVE_SHARDS]
        expected = {
            "active_receipts": len(active),
            "active_atto": sum(row["amount"] for row in active),
            "retired_receipts": len(retired),
            "retired_atto": sum(row["amount"] for row in retired),
        }
        accounted = {
            "active_receipts": int(direction["spent_receipt_count"]) + int(direction["pending_receipt_count"]),
            "active_atto": int(direction["spent_amount_atto"]) + int(direction["pending_amount_atto"]),
            "retired_receipts": int(direction["unsupported_receipt_count"]),
            "retired_atto": int(direction["unsupported_amount_atto"]),
        }
        stats[source] = {"independent": expected, "report": accounted}
        for key, value in expected.items():
            if value != accounted[key]:
                problems.append(
                    f"shard {source} {key.replace('_', ' ')}: independent list {value}, report {accounted[key]}"
                )
        index = {(row["block"], row["tx"]): row for row in rows}
        for kind in ("pending_groups", "unsupported_groups"):
            for group in direction.get(kind) or []:
                for receipt in group.get("receipts") or []:
                    key = (int(group["block_number"]), receipt["transaction_hash"].lower())
                    match = index.get(key)
                    if (
                        match is None
                        or match["to"] != receipt["to"].lower()
                        or match["amount"] != int(receipt["amount_atto"])
                    ):
                        problems.append(
                            f"shard {source} {kind[:-7]} receipt {receipt['transaction_hash']} "
                            f"at block {group['block_number']} is not in the independent list as reported"
                        )
    unchecked = sorted(set(directions) - set(by_source))
    return problems, stats, unchecked


def _receipts(direction, kind):
    return sorted(
        (int(group["block_number"]), receipt["transaction_hash"].lower(),
         receipt["to"].lower(), int(receipt["amount_atto"]))
        for group in direction.get(kind) or []
        for receipt in group.get("receipts") or []
    )


def independent_report_problems(report, independent, cutoffs):
    """Return every difference between the report and an independent report."""
    problems = []
    for shard in ACTIVE_SHARDS:
        if int(independent.get(f"shard{shard}_cutoff") or 0) != cutoffs[shard]:
            problems.append(f"independent report shard {shard} cutoff is {independent.get(f'shard{shard}_cutoff')}")
    theirs = {direction["source_shard"]: direction for direction in independent.get("directions", [])}
    for direction in report.get("directions", []):
        shard = direction["source_shard"]
        other = theirs.get(shard)
        if other is None:
            problems.append(f"independent report has no shard {shard} scan")
            continue
        for field in AGREEMENT_FIELDS:
            if str(direction.get(field)) != str(other.get(field)):
                problems.append(f"shard {shard} {field}: report {direction.get(field)}, independent {other.get(field)}")
        for kind in ("pending_groups", "unsupported_groups"):
            if _receipts(direction, kind) != _receipts(other, kind):
                problems.append(f"shard {shard} {kind[:-7]} receipts differ from the independent report")
    return problems


def evaluate(report, outgoing_rows, independent_reports, cutoffs):
    """Combine every check. Returns (problems, per-source statistics, agreement per independent report)."""
    agreements = [independent_report_problems(report, other, cutoffs) for other in independent_reports]
    agreed = any(not differences for differences in agreements)
    problems = []
    provenance = provenance_problems(report, cutoffs)
    if not (agreed and provenance == [LEGACY_REPORT]):
        problems.extend(provenance)
    if independent_reports and not agreed:
        problems.extend(f"independent report: {difference}" for difference in agreements[0])
    compared, stats, unchecked = comparison(report, outgoing_rows, cutoffs)
    problems.extend(compared)
    if not agreed:
        problems.extend(
            f"no independent outgoing-receipt list or agreeing independent report for source shard {shard}"
            for shard in unchecked
        )
    return problems, stats, agreements
