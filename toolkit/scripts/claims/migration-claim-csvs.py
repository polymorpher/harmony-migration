#!/usr/bin/env python3

"""Build historical compatibility ledgers used by difference tooling."""

import argparse
import csv
import hashlib
import json
import os


PRICE_NUMERATOR = 74_801
PRICE_SCALE = 8
ATTO_SCALE = 18
USD_SCALE = ATTO_SCALE + PRICE_SCALE
ONE_ATTO = 10 ** ATTO_SCALE
ONE_USD_SCALED = 10 ** USD_SCALE

COMPONENTS = (
    "liquid_shard0",
    "liquid_shard1",
    "liquid_total",
    "active_staked_or_delegated",
    "pending_undelegation",
    "unclaimed_staking_reward",
    "pending_cross_shard",
    "wallet_airdrop",
    "staked_to_vault",
    "total_claim",
)

ACTUAL_FIELDS = {
    "liquid_shard0": "liquid_shard0_atto",
    "liquid_shard1": "liquid_shard1_atto",
    "liquid_total": "liquid_total_atto",
    "active_staked_or_delegated": "active_delegation_atto",
    "pending_undelegation": "pending_undelegation_atto",
    "unclaimed_staking_reward": "unclaimed_reward_atto",
    "pending_cross_shard": "pending_cross_shard_atto",
    "wallet_airdrop": "wallet_airdrop_atto",
    "staked_to_vault": "staked_to_vault_atto",
    "total_claim": "total_claim_atto",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build resolved Harmony migration claim CSVs with USD values."
    )
    parser.add_argument("--actual-ledger", required=True)
    parser.add_argument("--original-qualifying", required=True)
    parser.add_argument("--actual-recovery-summary", required=True)
    parser.add_argument("--qualifying-recovery-summary", required=True)
    parser.add_argument("--all-output", required=True)
    parser.add_argument(
        "--over-one-usd-output",
        help="optional historical USD-filter output; not needed for migration",
    )
    parser.add_argument("--original-output", required=True)
    parser.add_argument("--summary-output", required=True)
    parser.add_argument("--claims-shard0-block", required=True, type=int)
    parser.add_argument("--claims-shard1-block", required=True, type=int)
    parser.add_argument("--price-reference-shard0-block", required=True, type=int)
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decimal_string(value, scale):
    if value < 0:
        raise ValueError("negative decimal value")
    whole, fraction = divmod(value, 10 ** scale)
    return f"{whole}.{fraction:0{scale}d}"


def one_string(atto):
    return decimal_string(atto, ATTO_SCALE)


def usd_string(atto):
    return decimal_string(atto * PRICE_NUMERATOR, USD_SCALE)


def strict_over_one_usd(atto):
    return atto * PRICE_NUMERATOR > ONE_USD_SCALED


def sorted_rows(path):
    with open(path, newline="") as source:
        previous = None
        for line_number, row in enumerate(csv.DictReader(source), start=2):
            key = row["secure_key"].lower()
            if previous is not None and key <= previous:
                raise ValueError(
                    f"{path}:{line_number}: secure keys are not strictly increasing"
                )
            previous = key
            row["secure_key"] = key
            yield row


def next_or_none(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def actual_values(row):
    if row is None:
        return {component: 0 for component in COMPONENTS}
    values = {
        component: int(row[field])
        for component, field in ACTUAL_FIELDS.items()
        if field in row and row[field] != ""
    }
    if any(value < 0 for value in values.values()):
        raise ValueError(f"negative claim component for {row['secure_key']}")
    if values["liquid_total"] != (
        values["liquid_shard0"] + values["liquid_shard1"]
    ):
        raise ValueError(f"liquid total mismatch for {row['secure_key']}")
    wallet = sum(
        values[component]
        for component in (
            "liquid_shard0",
            "liquid_shard1",
            "pending_undelegation",
            "unclaimed_staking_reward",
            "pending_cross_shard",
        )
    )
    staked_to_vault = values["active_staked_or_delegated"]
    total_claim = wallet + staked_to_vault
    expected = {
        "wallet_airdrop": wallet,
        "staked_to_vault": staked_to_vault,
        "total_claim": total_claim,
    }
    for component, amount in expected.items():
        if component in values and values[component] != amount:
            raise ValueError(
                f"{component} mismatch for {row['secure_key']}"
            )
        values[component] = amount
    legacy_total = row.get("total_claim_atto")
    if (
        legacy_total not in (None, "")
        and int(legacy_total) != total_claim
    ):
        raise ValueError(
            f"legacy total claim mismatch for {row['secure_key']}"
        )
    return values


def output_fields(include_original=False):
    fields = [
        "secure_key",
        "address",
        "address_or_secure_key",
        "address_resolved",
        "claims_shard0_block",
        "claims_shard1_block",
        "valuation_price_reference_shard0_block",
        "valuation_price_usd_per_one",
    ]
    if include_original:
        fields.extend([
            "original_liquid_shard0_atto",
            "original_liquid_shard1_atto",
            "original_liquid_total_atto",
            "original_liquid_shard0_one",
            "original_liquid_shard1_one",
            "original_liquid_total_one",
            "original_liquid_usd",
        ])
    for component in COMPONENTS:
        fields.append(f"{component}_atto")
    for component in COMPONENTS:
        fields.append(f"{component}_one")
    fields.extend([
        "wallet_airdrop_usd",
        "total_usd",
        "nonce_shard0",
        "nonce_shard1",
        "code_hash_shard0",
        "code_hash_shard1",
    ])
    return fields


def make_claim_row(args, row, values):
    address = row["address"]
    result = {
        "secure_key": row["secure_key"],
        "address": address,
        "address_or_secure_key": address or row["secure_key"],
        "address_resolved": str(bool(address)).lower(),
        "claims_shard0_block": str(args.claims_shard0_block),
        "claims_shard1_block": str(args.claims_shard1_block),
        "valuation_price_reference_shard0_block":
            str(args.price_reference_shard0_block),
        "valuation_price_usd_per_one": "0.00074801",
        "wallet_airdrop_usd": usd_string(values["wallet_airdrop"]),
        "total_usd": usd_string(values["total_claim"]),
        "nonce_shard0": row["nonce_shard0"],
        "nonce_shard1": row["nonce_shard1"],
        "code_hash_shard0": row["code_hash_shard0"],
        "code_hash_shard1": row["code_hash_shard1"],
    }
    for component in COMPONENTS:
        result[f"{component}_atto"] = str(values[component])
        result[f"{component}_one"] = one_string(values[component])
    return result


def new_stats():
    return {
        "rows": 0,
        "rows_with_address": 0,
        "rows_without_address": 0,
        **{f"{component}_atto": 0 for component in COMPONENTS},
    }


def add_stats(stats, values, has_address):
    stats["rows"] += 1
    stats[
        "rows_with_address" if has_address else "rows_without_address"
    ] += 1
    for component in COMPONENTS:
        stats[f"{component}_atto"] += values[component]


def finalize_stats(stats):
    for component in COMPONENTS:
        stats[f"{component}_atto"] = str(stats[f"{component}_atto"])


def open_atomic_outputs(paths):
    handles = {}
    for label, path in paths.items():
        partial = path + ".partial"
        if os.path.exists(path) or os.path.exists(partial):
            raise FileExistsError(path)
        handles[label] = open(partial, "x", newline="")
    return handles


def cleanup_partials(paths):
    for path in paths.values():
        try:
            os.remove(path + ".partial")
        except FileNotFoundError:
            pass


def build(args):
    outputs = {
        "all": args.all_output,
        "original": args.original_output,
    }
    if args.over_one_usd_output:
        outputs["over_one_usd"] = args.over_one_usd_output
    if os.path.exists(args.summary_output) or os.path.exists(
        args.summary_output + ".partial"
    ):
        raise FileExistsError(args.summary_output)

    input_hashes = {
        "actual_ledger": file_sha256(args.actual_ledger),
        "original_qualifying": file_sha256(args.original_qualifying),
        "actual_recovery_summary": file_sha256(
            args.actual_recovery_summary
        ),
        "qualifying_recovery_summary": file_sha256(
            args.qualifying_recovery_summary
        ),
    }
    with open(args.actual_recovery_summary) as source:
        actual_recovery = json.load(source)
    with open(args.qualifying_recovery_summary) as source:
        qualifying_recovery = json.load(source)

    handles = open_atomic_outputs(outputs)
    try:
        all_fields = output_fields()
        original_fields = output_fields(include_original=True)
        writers = {
            "all": csv.DictWriter(
                handles["all"], fieldnames=all_fields, lineterminator="\n"
            ),
            "original": csv.DictWriter(
                handles["original"],
                fieldnames=original_fields,
                lineterminator="\n",
            ),
        }
        if "over_one_usd" in handles:
            writers["over_one_usd"] = csv.DictWriter(
                handles["over_one_usd"],
                fieldnames=all_fields,
                lineterminator="\n",
            )
        for writer in writers.values():
            writer.writeheader()

        stats = {
            "all": new_stats(),
            "original": new_stats(),
        }
        if "over_one_usd" in writers:
            stats["over_one_usd"] = new_stats()
        original_liquid_total = 0
        original_liquid_usd_scaled = 0

        actual_iterator = iter(sorted_rows(args.actual_ledger))
        original_iterator = iter(sorted_rows(args.original_qualifying))
        actual = next_or_none(actual_iterator)
        original = next_or_none(original_iterator)

        while actual is not None or original is not None:
            key = min(
                row["secure_key"]
                for row in (actual, original)
                if row is not None
            )
            current_actual = (
                actual if actual and actual["secure_key"] == key else None
            )
            current_original = (
                original
                if original and original["secure_key"] == key
                else None
            )
            if current_actual is not None:
                actual = next_or_none(actual_iterator)
            if current_original is not None:
                original = next_or_none(original_iterator)

            addresses = [
                row["address"]
                for row in (current_actual, current_original)
                if row is not None and row["address"]
            ]
            if addresses and any(
                address.lower() != addresses[0].lower()
                for address in addresses[1:]
            ):
                raise ValueError(f"address mismatch for {key}: {addresses}")
            if (
                current_actual is not None
                and not current_actual["address"]
                and addresses
            ):
                current_actual = dict(current_actual)
                current_actual["address"] = addresses[0]

            if current_actual is not None:
                values = actual_values(current_actual)
                claim_row = make_claim_row(
                    args, current_actual, values
                )
                writers["all"].writerow(claim_row)
                add_stats(
                    stats["all"], values, bool(current_actual["address"])
                )
                if (
                    "over_one_usd" in writers
                    and strict_over_one_usd(values["total_claim"])
                ):
                    writers["over_one_usd"].writerow(claim_row)
                    add_stats(
                        stats["over_one_usd"],
                        values,
                        bool(current_actual["address"]),
                    )

            if current_original is not None:
                values = actual_values(current_actual)
                base = current_actual or {
                    "secure_key": key,
                    "address": current_original["address"],
                    "nonce_shard0": "",
                    "nonce_shard1": "",
                    "code_hash_shard0": "",
                    "code_hash_shard1": "",
                }
                if not base["address"] and current_original["address"]:
                    base = dict(base)
                    base["address"] = current_original["address"]
                original_row = make_claim_row(args, base, values)
                original0 = int(
                    current_original["balance_shard_0_atto"]
                )
                original1 = int(
                    current_original["balance_shard_1_atto"]
                )
                original_total = int(
                    current_original["balance_total_atto"]
                )
                if original_total != original0 + original1:
                    raise ValueError(
                        f"original liquid mismatch for {key}"
                    )
                original_row.update({
                    "original_liquid_shard0_atto": str(original0),
                    "original_liquid_shard1_atto": str(original1),
                    "original_liquid_total_atto": str(original_total),
                    "original_liquid_shard0_one": one_string(original0),
                    "original_liquid_shard1_one": one_string(original1),
                    "original_liquid_total_one": one_string(original_total),
                    "original_liquid_usd": usd_string(original_total),
                })
                writers["original"].writerow(original_row)
                add_stats(
                    stats["original"],
                    values,
                    bool(original_row["address"]),
                )
                original_liquid_total += original_total
                original_liquid_usd_scaled += (
                    original_total * PRICE_NUMERATOR
                )

        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        for label, path in outputs.items():
            os.replace(path + ".partial", path)

        for output_stats in stats.values():
            finalize_stats(output_stats)
        stats["original"]["original_liquid_total_atto"] = str(
            original_liquid_total
        )
        stats["original"]["original_liquid_total_usd"] = decimal_string(
            original_liquid_usd_scaled, USD_SCALE
        )

        result = {
            "valuation": {
                "price_usd_per_one": "0.00074801",
                "price_reference_shard0_block":
                    args.price_reference_shard0_block,
                "strict_threshold": "total_claim_usd > 1.00",
            },
            "claims_snapshot": {
                "shard0_block": args.claims_shard0_block,
                "shard1_block": args.claims_shard1_block,
            },
            "input_sha256": input_hashes,
            "preimage_recovery": {
                "actual": actual_recovery,
                "original_qualifying": qualifying_recovery,
            },
            "outputs": {},
            "stats": stats,
        }
        for label, path in outputs.items():
            result["outputs"][label] = {
                "path": path,
                "bytes": os.path.getsize(path),
                "sha256": file_sha256(path),
            }

        summary_partial = args.summary_output + ".partial"
        with open(summary_partial, "x") as summary_file:
            json.dump(result, summary_file, indent=2, sort_keys=True)
            summary_file.write("\n")
            summary_file.flush()
            os.fsync(summary_file.fileno())
        os.replace(summary_partial, args.summary_output)
        return result
    except BaseException:
        for handle in handles.values():
            if not handle.closed:
                handle.close()
        cleanup_partials(outputs)
        try:
            os.remove(args.summary_output + ".partial")
        except FileNotFoundError:
            pass
        raise


def main():
    result = build(parse_args())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
