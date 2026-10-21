#!/usr/bin/env python3
"""Summarize a built transaction archive: counts, distribution, addresses and file sizes.

Reads the build-archive.py output directory and writes --output-json and
--output-md. Compressed sizes are measured with gzip -9 (standard library) and
zstd -19 --long when a zstd binary is on PATH.
"""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402

BUCKETS = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000]
ADDRESS_COLUMNS = {"transactions.csv": ("from", "to"), "internal_transfers.csv": ("from", "to"),
                   "cross_shard.csv": ("from", "to"), "staking.csv": ("delegator", "validator")}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def bucket(value):
    label = f"<{BUCKETS[0]}"
    for limit in BUCKETS:
        if value >= limit * lib.ONE:
            label = f">={limit:,}"
    return label


def compressed_sizes(path):
    sizes = {"csv": path.stat().st_size}
    with open(path, "rb") as source:
        sizes["gzip-9"] = len(gzip.compress(source.read(), 9))
    if shutil.which("zstd"):
        with open(path, "rb") as source:
            result = subprocess.run(["zstd", "-19", "--long=27", "-q", "-c"], stdin=source,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            sizes["zstd-19"] = len(result.stdout)
    return sizes


def megabytes(value):
    return f"{value / 1e6:,.1f} MB"


def main():
    args = parse_args()
    for path in (args.output_json, args.output_md):
        if os.path.exists(path) and not args.replace:
            raise SystemExit(f"{path} exists; pass --replace")
    archive = Path(args.archive)
    manifest = lib.load_json(archive / "manifest.json")
    addresses = set()
    tables = {}
    for name, columns in ADDRESS_COLUMNS.items():
        path = archive / name
        stats = {"by_group": Counter(), "by_month": defaultdict(Counter), "buckets": Counter(),
                 "failed": Counter(), "value_one": Counter()}
        value_column = "amount_atto" if name == "staking.csv" else "value_atto"
        for row in lib.iter_csv(path):
            branch = row["branch"]
            shard = row.get("shard", row.get("from_shard"))
            group = f"shard{shard}/{branch}"
            value = int(row[value_column])
            stats["by_group"][group] += 1
            month = datetime.fromtimestamp(int(row["timestamp"]), timezone.utc).strftime("%Y-%m")
            stats["by_month"][group][month] += 1
            stats["buckets"][bucket(value)] += 1
            stats["value_one"][group] += value // lib.ONE
            if row.get("status") == "0" or row.get("effective") == "0":
                stats["failed"][group] += 1
            for column in columns:
                if row[column]:
                    addresses.add(row[column])
        tables[name] = {
            "rows": sum(stats["by_group"].values()),
            "by_group": dict(sorted(stats["by_group"].items())),
            "failed_or_reverted": dict(sorted(stats["failed"].items())),
            "value_one": {k: str(v) for k, v in sorted(stats["value_one"].items())},
            "value_buckets": dict(stats["buckets"]),
            "by_month": {g: dict(sorted(m.items())) for g, m in sorted(stats["by_month"].items())},
            "sizes": compressed_sizes(path),
        }
    summary = {"min_value_atto": manifest["min_value_atto"], "distinct_addresses": len(addresses),
               "runs": [{k: run[k] for k in ("shard", "branch", "start", "end", "boundaries")}
                        for run in manifest["runs"]],
               "notes": manifest["notes"], "tables": tables}
    lib.write_json(args.output_json, summary)

    lines = ["# Transaction archive summary", "",
             f"Minimum value: {int(manifest['min_value_atto']) / lib.ONE:g} ONE. "
             f"Distinct addresses: {len(addresses):,}.", "", "## Runs", "",
             "| Shard | Branch | Blocks | From (UTC) | To (UTC) |", "|---|---|---|---|---|"]
    for run in manifest["runs"]:
        bounds = run["boundaries"]
        lines.append(f"| {run['shard']} | {run['branch']} | {run['start']:,} – {run['end']:,} | "
                     f"{bounds['start']['time_utc']} | {bounds['end']['time_utc']} |")
    if manifest["notes"]:
        lines += ["", "Coverage notes:", ""] + [f"- {note}" for note in manifest["notes"]]
    lines += ["", "## Rows", "", "| Table | Group | Rows | Failed or reverted |", "|---|---|---:|---:|"]
    for name, table in tables.items():
        for group, rows in table["by_group"].items():
            lines.append(f"| {name} | {group} | {rows:,} | {table['failed_or_reverted'].get(group, 0):,} |")
    lines += ["", "## Sizes", "", "| Table | CSV | gzip -9 | zstd -19 |", "|---|---:|---:|---:|"]
    for name, table in tables.items():
        sizes = table["sizes"]
        zstd = megabytes(sizes["zstd-19"]) if "zstd-19" in sizes else "n/a"
        lines.append(f"| {name} | {megabytes(sizes['csv'])} | {megabytes(sizes['gzip-9'])} | {zstd} |")
    lines += ["", "## Value distribution of transactions.csv", "", "| Range (ONE) | Rows |", "|---|---:|"]
    order = [f"<{BUCKETS[0]}"] + [f">={limit:,}" for limit in BUCKETS]
    for label in order:
        if label in tables["transactions.csv"]["value_buckets"]:
            lines.append(f"| {label} | {tables['transactions.csv']['value_buckets'][label]:,} |")
    partial = args.output_md + ".partial"
    with open(partial, "w", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    os.replace(partial, args.output_md)
    print(f"wrote {args.output_json} and {args.output_md}", flush=True)


if __name__ == "__main__":
    main()
