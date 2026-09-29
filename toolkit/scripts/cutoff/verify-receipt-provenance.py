#!/usr/bin/env python3
"""Verify that a cross-shard-supply report covers every outgoing receipt.

    verify-receipt-provenance.py --receipts cross-shard-supply-cutoff.json \
        --outgoing shard0-outgoing-receipts.csv --outgoing shard1-outgoing-receipts.csv \
        [--independent-report reviewer-cross-shard-supply.json] \
        --output receipt-provenance.verify.json

The --outgoing lists come from forensics/outgoing-cx-scan run on archive
databases; --independent-report is a report produced by someone else from
other archive databases. The report fails when it neither records complete
history and receipt coverage nor agrees exactly with an independent report,
when a source shard has no independent evidence, or when its spent, pending
and retired-shard receipts differ from a list.
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import receipt_provenance  # noqa: E402


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path):
    with open(path) as source:
        return json.load(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--receipts", required=True, help="cross-shard-supply JSON report")
    parser.add_argument("--outgoing", action="append", default=[], help="outgoing-cx-scan CSV (repeat per shard)")
    parser.add_argument("--independent-report", action="append", default=[],
                        help="cross-shard-supply report from independent archive databases")
    parser.add_argument("--shard0-cutoff", type=int, default=93623067)
    parser.add_argument("--shard1-cutoff", type=int, default=95882100)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not args.outgoing and not args.independent_report:
        parser.error("give --outgoing lists, --independent-report, or both")
    if os.path.exists(args.output) or os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)

    cutoffs = {0: args.shard0_cutoff, 1: args.shard1_cutoff}
    problems, stats, agreements = receipt_provenance.evaluate(
        load(args.receipts),
        receipt_provenance.read_outgoing(args.outgoing),
        [load(path) for path in args.independent_report],
        cutoffs,
    )
    result = {
        "status": "failed" if problems else "passed",
        "problems": problems,
        "comparison": {str(shard): value for shard, value in stats.items()},
        "independent_report_agreement": {
            path: differences for path, differences in zip(args.independent_report, agreements)
        },
        "cutoff_blocks": {"shard0": cutoffs[0], "shard1": cutoffs[1]},
        "input_sha256": {path: sha256(path) for path in [args.receipts, *args.outgoing, *args.independent_report]},
    }
    with open(args.output + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
    os.replace(args.output + ".partial", args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
