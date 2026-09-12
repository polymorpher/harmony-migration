#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os


PRICE_NUMERATOR = 74_801
ATTO_SCALE = 18
USD_SCALE = 26
USD_THRESHOLD = 10**USD_SCALE
COMPONENTS = (
    "liquid_shard0",
    "liquid_shard1",
    "liquid_total",
    "active_staked_or_delegated",
    "pending_undelegation",
    "unclaimed_staking_reward",
    "pending_cross_shard",
    "total_claim",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-all", required=True)
    parser.add_argument("--new-all", required=True)
    parser.add_argument("--old-original-population", required=True)
    parser.add_argument("--all-diff", required=True)
    parser.add_argument("--threshold-union-diff", required=True)
    parser.add_argument("--original-population-diff", required=True)
    parser.add_argument("--summary", required=True)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rows(path):
    with open(path, newline="") as source:
        previous = None
        for line, row in enumerate(csv.DictReader(source), start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(f"{path}:{line}: keys are not strictly increasing")
            previous = key
            row["secure_key"] = key
            yield row


def next_or_none(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def atto(row, component):
    if row is None:
        return 0
    value = int(row[f"{component}_atto"])
    if value < 0:
        raise ValueError(f"negative {component} for {row['secure_key']}")
    return value


def fixed(value, scale):
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, fraction = divmod(value, 10**scale)
    return f"{sign}{whole}.{fraction:0{scale}d}"


def one(value):
    return fixed(value, ATTO_SCALE)


def usd(atto_value):
    return fixed(atto_value * PRICE_NUMERATOR, USD_SCALE)


def output_fields():
    fields = [
        "secure_key",
        "address",
        "change_type",
        "old_claims_shard0_block",
        "old_claims_shard1_block",
        "final_claims_shard0_block",
        "final_claims_shard1_block",
        "valuation_price_reference_shard0_block",
        "valuation_price_usd_per_one",
        "old_over_1usd",
        "final_over_1usd",
    ]
    for component in COMPONENTS:
        fields.extend(
            (
                f"old_{component}_atto",
                f"final_{component}_atto",
                f"delta_{component}_atto",
                f"old_{component}_one",
                f"final_{component}_one",
                f"delta_{component}_one",
            )
        )
    fields.extend(
        (
            "old_total_usd",
            "final_total_usd",
            "delta_total_usd",
            "old_nonce_shard0",
            "final_nonce_shard0",
            "old_nonce_shard1",
            "final_nonce_shard1",
            "old_code_hash_shard0",
            "final_code_hash_shard0",
            "old_code_hash_shard1",
            "final_code_hash_shard1",
        )
    )
    return fields


def make_diff(old, new):
    key = (old or new)["secure_key"]
    addresses = [
        row["address"]
        for row in (old, new)
        if row is not None and row["address"]
    ]
    if not addresses:
        raise ValueError(f"no address for {key}")
    if any(address.lower() != addresses[0].lower() for address in addresses[1:]):
        raise ValueError(f"address changed for {key}")
    old_values = {component: atto(old, component) for component in COMPONENTS}
    new_values = {component: atto(new, component) for component in COMPONENTS}
    deltas = {
        component: new_values[component] - old_values[component]
        for component in COMPONENTS
    }
    old_over = old_values["total_claim"] * PRICE_NUMERATOR > USD_THRESHOLD
    new_over = new_values["total_claim"] * PRICE_NUMERATOR > USD_THRESHOLD
    if old is None:
        change_type = "created"
    elif new is None:
        change_type = "deleted"
    elif any(deltas.values()):
        change_type = "amount_changed"
    elif any(
        old[field] != new[field]
        for field in (
            "nonce_shard0",
            "nonce_shard1",
            "code_hash_shard0",
            "code_hash_shard1",
        )
    ):
        change_type = "metadata_changed"
    else:
        change_type = "unchanged"
    result = {
        "secure_key": key,
        "address": addresses[0],
        "change_type": change_type,
        "old_claims_shard0_block": old["claims_shard0_block"] if old else "",
        "old_claims_shard1_block": old["claims_shard1_block"] if old else "",
        "final_claims_shard0_block": new["claims_shard0_block"] if new else "",
        "final_claims_shard1_block": new["claims_shard1_block"] if new else "",
        "valuation_price_reference_shard0_block": (
            (new or old)["valuation_price_reference_shard0_block"]
        ),
        "valuation_price_usd_per_one": (
            (new or old)["valuation_price_usd_per_one"]
        ),
        "old_over_1usd": str(old_over).lower(),
        "final_over_1usd": str(new_over).lower(),
        "old_total_usd": usd(old_values["total_claim"]),
        "final_total_usd": usd(new_values["total_claim"]),
        "delta_total_usd": usd(deltas["total_claim"]),
    }
    for component in COMPONENTS:
        result.update(
            {
                f"old_{component}_atto": str(old_values[component]),
                f"final_{component}_atto": str(new_values[component]),
                f"delta_{component}_atto": str(deltas[component]),
                f"old_{component}_one": one(old_values[component]),
                f"final_{component}_one": one(new_values[component]),
                f"delta_{component}_one": one(deltas[component]),
            }
        )
    for field in (
        "nonce_shard0",
        "nonce_shard1",
        "code_hash_shard0",
        "code_hash_shard1",
    ):
        result[f"old_{field}"] = old[field] if old else ""
        result[f"final_{field}"] = new[field] if new else ""
    return result, old_values, new_values, deltas, old_over, new_over


def load_original_keys(path):
    result = set()
    for row in rows(path):
        result.add(row["secure_key"])
    return result


def main():
    args = parse_args()
    output_paths = {
        "all": args.all_diff,
        "threshold_union": args.threshold_union_diff,
        "original_population": args.original_population_diff,
    }
    for path in (*output_paths.values(), args.summary):
        if os.path.exists(path) or os.path.exists(path + ".partial"):
            raise FileExistsError(path)

    original_keys = load_original_keys(args.old_original_population)
    old_iterator = iter(rows(args.old_all))
    new_iterator = iter(rows(args.new_all))
    old = next_or_none(old_iterator)
    new = next_or_none(new_iterator)
    fields = output_fields()
    handles = {}
    writers = {}
    try:
        for label, path in output_paths.items():
            handles[label] = open(path + ".partial", "x", newline="")
            writers[label] = csv.DictWriter(
                handles[label], fieldnames=fields, lineterminator="\n"
            )
            writers[label].writeheader()

        summary = {
            "inputs_sha256": {
                "old_all": file_sha256(args.old_all),
                "new_all": file_sha256(args.new_all),
                "old_original_population": file_sha256(
                    args.old_original_population
                ),
            },
            "union_rows": 0,
            "created": 0,
            "deleted": 0,
            "amount_changed": 0,
            "metadata_changed": 0,
            "unchanged": 0,
            "old_over_1usd": 0,
            "final_over_1usd": 0,
            "threshold_union_rows": 0,
            "original_population_rows": len(original_keys),
            "original_population_in_claim_union": 0,
            "output_rows": {
                "all": 0,
                "threshold_union": 0,
                "original_population": 0,
            },
            "outputs": {},
            "component_totals": {
                component: {"old_atto": 0, "final_atto": 0, "delta_atto": 0}
                for component in COMPONENTS
            },
        }
        while old is not None or new is not None:
            key = min(
                row["secure_key"] for row in (old, new) if row is not None
            )
            current_old = old if old and old["secure_key"] == key else None
            current_new = new if new and new["secure_key"] == key else None
            if current_old is not None:
                old = next_or_none(old_iterator)
            if current_new is not None:
                new = next_or_none(new_iterator)

            (
                diff,
                old_values,
                new_values,
                deltas,
                old_over,
                new_over,
            ) = make_diff(current_old, current_new)
            summary["union_rows"] += 1
            summary[diff["change_type"]] += 1
            summary["old_over_1usd"] += int(old_over)
            summary["final_over_1usd"] += int(new_over)
            summary["threshold_union_rows"] += int(old_over or new_over)
            summary["original_population_in_claim_union"] += int(
                key in original_keys
            )
            for component in COMPONENTS:
                totals = summary["component_totals"][component]
                totals["old_atto"] += old_values[component]
                totals["final_atto"] += new_values[component]
                totals["delta_atto"] += deltas[component]

            if diff["change_type"] != "unchanged":
                writers["all"].writerow(diff)
                summary["output_rows"]["all"] += 1
                if old_over or new_over:
                    writers["threshold_union"].writerow(diff)
                    summary["output_rows"]["threshold_union"] += 1
                if key in original_keys:
                    writers["original_population"].writerow(diff)
                    summary["output_rows"]["original_population"] += 1

        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        for label, path in output_paths.items():
            os.replace(path + ".partial", path)
            summary["outputs"][label] = {
                "path": path,
                "sha256": file_sha256(path),
                "bytes": os.path.getsize(path),
            }
        for component in COMPONENTS:
            for name, value in summary["component_totals"][component].items():
                summary["component_totals"][component][name] = str(value)
        summary["original_population_absent_from_claim_union"] = (
            summary["original_population_rows"]
            - summary["original_population_in_claim_union"]
        )

        with open(args.summary + ".partial", "x") as output:
            json.dump(summary, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(args.summary + ".partial", args.summary)
        print(json.dumps(summary, sort_keys=True))
    except BaseException:
        for handle in handles.values():
            if not handle.closed:
                handle.close()
        for path in (*output_paths.values(), args.summary):
            try:
                os.remove(path + ".partial")
            except FileNotFoundError:
                pass
        raise


if __name__ == "__main__":
    main()
