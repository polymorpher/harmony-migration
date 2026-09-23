#!/usr/bin/env python3
"""Turn a recipient list into a run directory: the canonical list, the batches, their
fingerprints, the proofs, and the single root that gets baked into the contract.

    python3 tools/build.py --input recipients.csv --run-dir runs/mainnet-priority --label "priority wallets"
    python3 tools/build.py --input lists/ --run-dir runs/full --amount-unit one --order input

Input: one CSV file, or a directory of CSV files that are read in name order. Each file needs a
header row with an address column and an amount column. Column names are detected automatically
(address / destination_address / recipient ...; amount_atto / amount_one / amount ...) or can be
given explicitly. Amounts are either whole numbers in the smallest unit (atto, 18 decimals) or
decimal numbers of whole tokens; say which with --amount-unit if the column name does not.

The tool refuses to continue on any of: an address with a wrong checksum, the zero address, a
non-positive amount, a duplicate address (unless --merge-duplicates), or an existing run directory
(unless --force).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="CSV file or directory of CSV files")
    parser.add_argument("--run-dir", required=True, type=Path, help="output directory, e.g. runs/mainnet-priority")
    parser.add_argument("--label", default="", help="free-text description stored in the manifest")
    parser.add_argument("--batch-size", type=int, default=common.DEFAULT_BATCH_SIZE,
                        help=f"recipients per transaction (default {common.DEFAULT_BATCH_SIZE}, max {common.MAX_BATCH_SIZE})")
    parser.add_argument("--address-column", help="name of the address column (auto-detected by default)")
    parser.add_argument("--amount-column", help="name of the amount column (auto-detected by default)")
    parser.add_argument("--amount-unit", choices=["atto", "one"], help="unit of the amount column")
    parser.add_argument("--order", choices=["address", "input"], default="address",
                        help="'address' sorts recipients by address (default, reproducible); 'input' keeps file order")
    parser.add_argument("--merge-duplicates", action="store_true", help="add up repeated addresses instead of failing")
    parser.add_argument("--force", action="store_true", help="replace an existing run directory")
    args = parser.parse_args(argv)

    started = time.time()
    try:
        rows, info = common.read_rows(args.input, args.address_column, args.amount_column, args.amount_unit)
        raw_count = len(rows)
        rows = common.consolidate(rows, args.order, args.merge_duplicates)
        if args.run_dir.exists():
            if not args.force:
                raise common.InputError(f"{args.run_dir} already exists; pass --force to replace it")
            shutil.rmtree(args.run_dir)
        manifest = common.write_run(args.run_dir, rows, args.batch_size, info, args.label, args.order)
    except common.InputError as exc:
        common.die(str(exc))
        return

    print(f"input files      : {len(info['sources'])} ({raw_count} rows read, {len(rows)} recipients after consolidation)")
    print(f"columns          : address={info['address_column']!r} amount={info['amount_column']!r} unit={info['amount_unit']}")
    print(f"order            : {args.order}")
    print(f"batch size       : {manifest['batch_size']}")
    print(f"batches          : {manifest['batch_count']}")
    print(f"recipients       : {manifest['recipient_count']}")
    print(f"total (atto)     : {manifest['total_amount']}")
    print(f"total (tokens)   : {manifest['total_amount_display']}")
    print(f"root             : {manifest['root']}")
    print(f"list sha256      : {manifest['list_sha256']}")
    print(f"written to       : {args.run_dir}")
    print(f"keccak backend   : {common.KECCAK_BACKEND} ({time.time() - started:.1f}s)")
    if manifest["batch_size"] > 450:
        print("note: batches above ~450 recipients approach the per-transaction gas cap; "
              "make sure the executor's gas estimate stays below 16,777,216.", file=sys.stderr)


if __name__ == "__main__":
    main()
