#!/usr/bin/env python3

"""Compare database and archival-RPC per-validator delegation exports."""

import argparse
import csv
import hashlib
import itertools
import json
import os


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    args = parse_args()
    if os.path.exists(args.output) or os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)
    rows = 0
    total = 0
    with open(args.database, newline="") as database, open(
        args.rpc, newline=""
    ) as rpc:
        database_reader = csv.DictReader(database)
        rpc_reader = csv.DictReader(rpc)
        if database_reader.fieldnames != rpc_reader.fieldnames:
            raise ValueError("delegation headers differ")
        for line, pair in enumerate(
            itertools.zip_longest(database_reader, rpc_reader),
            start=2,
        ):
            database_row, rpc_row = pair
            if database_row is None or rpc_row is None:
                raise ValueError(
                    f"delegation row counts differ at line {line}"
                )
            if database_row != rpc_row:
                raise ValueError(f"delegation rows differ at line {line}")
            amount = int(database_row["staked_to_vault_atto"])
            if amount <= 0:
                raise ValueError(
                    f"non-positive vault principal at line {line}"
                )
            total += amount
            rows += 1

    result = {
        "status": "passed",
        "rows": rows,
        "staked_to_vault_atto": str(total),
        "database_sha256": file_sha256(args.database),
        "rpc_sha256": file_sha256(args.rpc),
        "files_identical": file_sha256(args.database)
        == file_sha256(args.rpc),
    }
    if not result["files_identical"]:
        raise ValueError("normalized delegation rows match but files differ")
    with open(args.output + ".partial", "x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
