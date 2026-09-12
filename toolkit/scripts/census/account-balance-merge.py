#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
from contextlib import ExitStack


EMPTY_CODE_HASH = "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Merge sorted per-shard Harmony account balance exports."
    )
    parser.add_argument("--shard0", required=True)
    parser.add_argument("--shard1", required=True)
    parser.add_argument("--threshold-atto", required=True, type=int)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary-output", required=True)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def account_rows(file_handle, path):
    previous_key = None
    for line_number, row in enumerate(csv.DictReader(file_handle), start=2):
        key = row["secure_key"]
        if previous_key is not None and key <= previous_key:
            raise ValueError(
                f"{path}:{line_number}: secure keys are not strictly increasing"
            )
        previous_key = key
        balance = int(row["balance_atto"])
        if balance <= 0:
            raise ValueError(f"{path}:{line_number}: non-positive exported balance")
        yield {
            "secure_key": key,
            "address": row["address"],
            "balance": balance,
            "nonce": row["nonce"],
            "code_hash": row["code_hash"].lower(),
        }


def next_or_none(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def merge(args):
    for path in (args.output, args.summary_output):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(f"refusing to overwrite {path}")

    input_hashes = {
        "shard0": file_sha256(args.shard0),
        "shard1": file_sha256(args.shard1),
    }
    output_partial = args.output + ".partial"
    summary_partial = args.summary_output + ".partial"

    summary = {
        "input_sha256": input_hashes,
        "threshold_atto": str(args.threshold_atto),
        "shard0_positive": 0,
        "shard1_positive": 0,
        "unique_positive": 0,
        "positive_overlap": 0,
        "shard0_individually_qualifying": 0,
        "shard1_individually_qualifying": 0,
        "qualifying_overlap": 0,
        "new_cross_shard_threshold_crossers": 0,
        "unique_qualifying_after_sum": 0,
        "qualifying_code_less": 0,
        "qualifying_code_bearing": 0,
        "qualifying_with_address": 0,
        "qualifying_missing_address": 0,
        "total_balance_atto": 0,
        "qualifying_balance_atto": 0,
    }

    try:
        with ExitStack() as stack:
            shard0_file = stack.enter_context(open(args.shard0, newline=""))
            shard1_file = stack.enter_context(open(args.shard1, newline=""))
            output_file = stack.enter_context(
                open(output_partial, "x", newline="", encoding="utf-8")
            )
            writer = csv.writer(output_file, lineterminator="\n")
            writer.writerow(
                [
                    "secure_key",
                    "address",
                    "balance_shard_0_atto",
                    "balance_shard_1_atto",
                    "balance_total_atto",
                    "nonce_shard_0",
                    "nonce_shard_1",
                    "code_hash_shard_0",
                    "code_hash_shard_1",
                ]
            )

            shard0 = iter(account_rows(shard0_file, args.shard0))
            shard1 = iter(account_rows(shard1_file, args.shard1))
            row0 = next_or_none(shard0)
            row1 = next_or_none(shard1)

            while row0 is not None or row1 is not None:
                if row1 is None or (
                    row0 is not None and row0["secure_key"] < row1["secure_key"]
                ):
                    current0, current1 = row0, None
                    row0 = next_or_none(shard0)
                elif row0 is None or row1["secure_key"] < row0["secure_key"]:
                    current0, current1 = None, row1
                    row1 = next_or_none(shard1)
                else:
                    current0, current1 = row0, row1
                    row0 = next_or_none(shard0)
                    row1 = next_or_none(shard1)
                    summary["positive_overlap"] += 1

                if current0 is not None:
                    summary["shard0_positive"] += 1
                if current1 is not None:
                    summary["shard1_positive"] += 1
                summary["unique_positive"] += 1

                address0 = current0["address"] if current0 else ""
                address1 = current1["address"] if current1 else ""
                if address0 and address1 and address0.lower() != address1.lower():
                    raise ValueError(
                        f"address mismatch for {current0['secure_key']}: "
                        f"{address0} != {address1}"
                    )
                address = address0 or address1

                balance0 = current0["balance"] if current0 else 0
                balance1 = current1["balance"] if current1 else 0
                total = balance0 + balance1
                qualifies0 = balance0 > args.threshold_atto
                qualifies1 = balance1 > args.threshold_atto
                qualifies = total > args.threshold_atto

                if qualifies0:
                    summary["shard0_individually_qualifying"] += 1
                if qualifies1:
                    summary["shard1_individually_qualifying"] += 1
                if qualifies0 and qualifies1:
                    summary["qualifying_overlap"] += 1
                if qualifies and not qualifies0 and not qualifies1:
                    summary["new_cross_shard_threshold_crossers"] += 1

                summary["total_balance_atto"] += total
                if qualifies:
                    summary["unique_qualifying_after_sum"] += 1
                    summary["qualifying_balance_atto"] += total
                    code_bearing = (
                        current0 is not None
                        and current0["code_hash"] != EMPTY_CODE_HASH
                    ) or (
                        current1 is not None
                        and current1["code_hash"] != EMPTY_CODE_HASH
                    )
                    summary[
                        "qualifying_code_bearing"
                        if code_bearing
                        else "qualifying_code_less"
                    ] += 1
                    summary[
                        "qualifying_with_address"
                        if address
                        else "qualifying_missing_address"
                    ] += 1

                writer.writerow(
                    [
                        (current0 or current1)["secure_key"],
                        address,
                        str(balance0),
                        str(balance1),
                        str(total),
                        current0["nonce"] if current0 else "",
                        current1["nonce"] if current1 else "",
                        current0["code_hash"] if current0 else "",
                        current1["code_hash"] if current1 else "",
                    ]
                )

            output_file.flush()
            os.fsync(output_file.fileno())

        os.replace(output_partial, args.output)
        summary["output_path"] = args.output
        summary["output_sha256"] = file_sha256(args.output)
        summary["total_balance_atto"] = str(summary["total_balance_atto"])
        summary["qualifying_balance_atto"] = str(summary["qualifying_balance_atto"])

        with open(summary_partial, "x", encoding="utf-8") as summary_file:
            json.dump(summary, summary_file, indent=2, sort_keys=True)
            summary_file.write("\n")
            summary_file.flush()
            os.fsync(summary_file.fileno())
        os.replace(summary_partial, args.summary_output)
        return summary
    except BaseException:
        for path in (output_partial, summary_partial):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        raise


def main():
    summary = merge(parse_args())
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
