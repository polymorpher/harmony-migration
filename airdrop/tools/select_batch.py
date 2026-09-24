#!/usr/bin/env python3
"""Choose which recipients go into the next payment batch.

    python3 tools/select_batch.py --input list.csv --budget 250000 --out-dir runs/batch-1-selection \
        --include 0xYourPriorityAddress --hold hold.txt --paid runs/batch-0/recipients.csv

Every recipient is paid its full amount or not at all; amounts are never split between batches.

  --budget     largest total (in whole tokens) this batch may pay; optional
  --include    address, or file of addresses, that must be in this batch (repeatable)
  --hold       file of addresses to keep out of this batch; they stay in remaining.csv (repeatable)
  --paid       CSV of recipients already paid, with an amount_atto or amount_one column (for example a
               previous batch's recipients.csv); they are removed from the list, and the tool stops if
               a paid amount differs from the list amount (repeatable)

Strategies for filling the budget after the --include addresses:

  smallest-first  (default) the smallest amounts first, which pays the most recipients per batch
  largest-first   the largest amounts first, which moves the most value per recipient
  input           the input order, taking every recipient that still fits
  random          a reproducible draw from --seed across three size tiers (below)

Random strategy. Recipients are split by amount into large (at least --large-from tokens), medium
(at least --medium-from) and small (the rest). With --large-to, amounts at or above it are not drawn
at all and stay in remaining.csv. The tool draws --large-count large recipients, then
--medium-count medium ones, each tier with equal chances, then small recipients until the budget,
--small-count or --max-recipients is reached. In the small tier the chance of each draw is
proportional to a weight that favours smaller amounts (--small-weight: inverse, the default, means
proportional to 1/amount). A drawn recipient that no longer fits the budget is skipped and drawing
continues. The draw is exact and reproducible in any language:

  pool    every eligible recipient of the tier (not paid, held or required), sorted by lower-case address
  weight  1 in the large and medium tiers; in the small tier 10**80 // f(amount in the smallest unit),
          f = 1 (uniform), isqrt(amount) (inverse-sqrt), amount (inverse), amount**2 (inverse-square)
  draw k  r = int(sha256(f"{seed}|{tier}|{k}"), big-endian) mod (sum of the pool's weights), for
          k = 0, 1, 2, ... within the tier; the pick is the first pool entry whose running weight
          total exceeds r; it leaves the pool whether it was selected or skipped

Use a seed nobody can choose after seeing the list, for example the hash of an Ethereum block whose
height was announced beforehand. Every draw is written to draws.csv.

Writes three files to --out-dir (four with the random strategy): selected.csv (address,amount_atto;
--include addresses first in the order given, then the rest by address), remaining.csv (everyone
not selected, including held addresses), selection.json (inputs with their SHA-256, parameters,
counts and totals) and draws.csv. selected.csv is ready for `safe_batch.py build` or `build.py`.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

ADDRESS_RE = re.compile(r"^0[xX][0-9a-fA-F]{40}$")
RANDOM_ALGORITHM = "sha256-weighted-draw/v1"
TIERS = ("large", "medium", "small")
WEIGHT_SCALE = 10 ** 80
SMALL_WEIGHTS = {
    "uniform": lambda amount: 1,
    "inverse-sqrt": math.isqrt,
    "inverse": lambda amount: amount,
    "inverse-square": lambda amount: amount * amount,
}


def tier_of(amount: int, medium_from: int, large_from: int, large_to: int | None = None) -> str | None:
    """The tier of an amount, or None when it is at or above large_to and therefore not drawn."""
    if large_to is not None and amount >= large_to:
        return None
    if amount >= large_from:
        return "large"
    return "medium" if amount >= medium_from else "small"


def draw_value(seed: str, tier: str, k: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}|{tier}|{k}".encode("utf-8")).digest(), "big")


def random_select(candidates: list[common.Row], seed: str, medium_from: int, large_from: int,
                  counts: dict[str, int | None], small_weight: str, budget_left: int | None,
                  slots_left: int | None, large_to: int | None = None) -> tuple[list[common.Row], list[dict]]:
    """Draw per tier as described in the module docstring. Returns the picks and a log of every draw."""
    chosen: list[common.Row] = []
    log: list[dict] = []
    for tier in TIERS:
        pool = sorted((r for r in candidates if tier_of(r.amount, medium_from, large_from, large_to) == tier),
                      key=lambda r: r.address.lower())
        if tier == "small":
            weights = [max(1, WEIGHT_SCALE // SMALL_WEIGHTS[small_weight](r.amount)) for r in pool]
        else:
            weights = [1] * len(pool)
        wanted = counts[tier]
        taken = 0
        k = 0
        total = sum(weights)
        while pool and (wanted is None or taken < wanted) and (slots_left is None or slots_left > 0):
            r = draw_value(seed, tier, k) % total
            running = 0
            for index, weight in enumerate(weights):
                running += weight
                if running > r:
                    break
            row = pool.pop(index)
            total -= weights.pop(index)
            fits = budget_left is None or row.amount <= budget_left
            log.append({"tier": tier, "draw": k, "address": row.address, "amount_atto": row.amount,
                        "outcome": "selected" if fits else "skipped_over_budget"})
            k += 1
            if not fits:
                continue
            chosen.append(row)
            taken += 1
            if budget_left is not None:
                budget_left -= row.amount
            if slots_left is not None:
                slots_left -= 1
    return chosen, log


def read_address_file(path: Path) -> list[str]:
    """Addresses from a .csv with an address column, or from a text file with one address per line."""
    if not path.is_file():
        raise common.InputError(f"address list not found: {path}")
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                raise common.InputError(f"{path}: empty file") from None
            column = common._pick_column(header, None, common.ADDRESS_COLUMNS, "address", str(path))
            index = header.index(column)
            out = []
            for line_no, record in enumerate(reader, start=2):
                if not record or all(not cell.strip() for cell in record):
                    continue
                out.append(common.normalize_address(record[index], f"{path}:{line_no}"))
            return out
    out = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        token = re.split(r"[\s,]+", line)[0]
        if not ADDRESS_RE.match(token):
            raise common.InputError(f"{path}:{line_no}: expected an address at the start of the line: {raw!r}")
        out.append(common.normalize_address(token, f"{path}:{line_no}"))
    return out


def resolve_includes(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if ADDRESS_RE.match(value.strip()):
            out.append(common.normalize_address(value, "--include"))
        else:
            out.extend(read_address_file(Path(value)))
    seen = set()
    unique = []
    for address in out:
        if address.lower() not in seen:
            seen.add(address.lower())
            unique.append(address)
    return unique


def write_list(path: Path, rows: list[common.Row]) -> str:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["address", "amount_atto"])
        for row in rows:
            writer.writerow([row.address, str(row.amount)])
    return common.sha256_file(path)


def summarize(rows: list[common.Row]) -> dict:
    total = sum(r.amount for r in rows)
    return {"recipients": len(rows), "total_amount": str(total), "total_amount_display": common.format_one(total)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="full recipient list: CSV file or directory of CSV files")
    parser.add_argument("--out-dir", required=True, type=Path, help="where to write selected.csv, remaining.csv, selection.json")
    parser.add_argument("--budget", help="largest total this batch may pay, in whole tokens (for example 250000 or 1234.5)")
    parser.add_argument("--max-recipients", type=int, help="largest number of recipients in this batch")
    parser.add_argument("--include", action="append", default=[], help="address or address file that must be selected")
    parser.add_argument("--hold", action="append", default=[], type=Path, help="address file to keep out of this batch")
    parser.add_argument("--paid", action="append", default=[], type=Path, help="CSV of recipients already paid (address, amount)")
    parser.add_argument("--strategy", choices=["smallest-first", "largest-first", "input", "random"], default="smallest-first")
    parser.add_argument("--seed", help="random strategy: the seed text (for example an announced block hash)")
    parser.add_argument("--large-from", default="10000000", help="random strategy: smallest large amount, whole tokens (default 10000000)")
    parser.add_argument("--large-to", help="random strategy: amounts at or above this (whole tokens) are not drawn (default: no limit)")
    parser.add_argument("--medium-from", default="100000", help="random strategy: smallest medium amount, whole tokens (default 100000)")
    parser.add_argument("--large-count", type=int, default=1, help="random strategy: large recipients to draw (default 1)")
    parser.add_argument("--medium-count", type=int, default=5, help="random strategy: medium recipients to draw (default 5)")
    parser.add_argument("--small-count", type=int, help="random strategy: small recipients to draw (default: until the budget is used)")
    parser.add_argument("--small-weight", choices=sorted(SMALL_WEIGHTS), default="inverse",
                        help="random strategy: how strongly smaller amounts are favoured in the small tier (default inverse)")
    parser.add_argument("--label", default="", help="free-text description stored in selection.json")
    parser.add_argument("--address-column", help="name of the address column in --input (auto-detected by default)")
    parser.add_argument("--amount-column", help="name of the amount column in --input (auto-detected by default)")
    parser.add_argument("--amount-unit", choices=["atto", "one"], help="unit of the amount column in --input")
    parser.add_argument("--force", action="store_true", help="overwrite files in an existing --out-dir")
    args = parser.parse_args(argv)

    try:
        rows, info = common.read_rows(args.input, args.address_column, args.amount_column, args.amount_unit)
        rows = common.consolidate(rows, "input", merge_duplicates=False)
        by_address = {r.address.lower(): r for r in rows}

        paid_sources = []
        paid: dict[str, common.Row] = {}
        for path in args.paid:
            paid_rows, paid_info = common.read_rows(path)
            paid_sources.extend(paid_info["sources"])
            for row in paid_rows:
                key = row.address.lower()
                listed = by_address.get(key)
                if listed is None:
                    raise common.InputError(f"{row.source}: {row.address} was paid but is not in the input list")
                if listed.amount != row.amount:
                    raise common.InputError(
                        f"{row.source}: {row.address} was paid {row.amount} but the list says {listed.amount}; "
                        "the list changed or the payment was partial, resolve this before selecting"
                    )
                if key in paid:
                    raise common.InputError(f"{row.source}: {row.address} appears in more than one paid file")
                paid[key] = listed

        held: dict[str, str] = {}
        hold_sources = []
        not_listed_holds = []
        for path in args.hold:
            addresses = read_address_file(path)
            hold_sources.append({"path": str(path), "sha256": common.sha256_file(path), "addresses": len(addresses)})
            for address in addresses:
                key = address.lower()
                if key not in by_address:
                    not_listed_holds.append(address)
                elif key not in paid:
                    held[key] = str(path)

        includes = resolve_includes(args.include)
        for address in includes:
            key = address.lower()
            if key not in by_address:
                raise common.InputError(f"--include {address} is not in the input list")
            if key in paid:
                raise common.InputError(f"--include {address} is already paid")
            if key in held:
                raise common.InputError(f"--include {address} is also on hold list {held[key]}")

        budget = common.parse_amount(args.budget, "one", "--budget") if args.budget else None
        if args.max_recipients is not None and args.max_recipients < 1:
            raise common.InputError("--max-recipients must be at least 1")
        if args.strategy == "random":
            if not args.seed:
                raise common.InputError("--strategy random needs --seed")
            medium_from = common.parse_amount(args.medium_from, "one", "--medium-from")
            large_from = common.parse_amount(args.large_from, "one", "--large-from")
            if medium_from >= large_from:
                raise common.InputError("--medium-from must be smaller than --large-from")
            large_to = common.parse_amount(args.large_to, "one", "--large-to") if args.large_to else None
            if large_to is not None and large_to <= large_from:
                raise common.InputError("--large-to must be larger than --large-from")
            counts = {"large": args.large_count, "medium": args.medium_count, "small": args.small_count}
            if any(c is not None and c < 0 for c in counts.values()):
                raise common.InputError("tier counts cannot be negative")
            if budget is None and args.small_count is None and args.max_recipients is None:
                raise common.InputError("--strategy random needs --budget, --small-count or --max-recipients")
        elif args.seed:
            raise common.InputError("--seed is only used with --strategy random")

        forced = [by_address[a.lower()] for a in includes]
        forced_total = sum(r.amount for r in forced)
        if budget is not None and forced_total > budget:
            raise common.InputError(
                f"the --include addresses need {common.format_one(forced_total)}, more than the budget {common.format_one(budget)}"
            )
        if args.max_recipients is not None and len(forced) > args.max_recipients:
            raise common.InputError(f"{len(forced)} --include addresses exceed --max-recipients {args.max_recipients}")

        forced_keys = {r.address.lower() for r in forced}
        candidates = [r for r in rows if r.address.lower() not in paid and r.address.lower() not in held
                      and r.address.lower() not in forced_keys]
        if args.strategy == "smallest-first":
            candidates.sort(key=lambda r: (r.amount, r.address.lower()))
        elif args.strategy == "largest-first":
            candidates.sort(key=lambda r: (-r.amount, r.address.lower()))

        chosen: list[common.Row] = []
        draws: list[dict] = []
        if args.strategy == "random":
            chosen, draws = random_select(
                candidates, args.seed, medium_from, large_from, counts, args.small_weight,
                None if budget is None else budget - forced_total,
                None if args.max_recipients is None else args.max_recipients - len(forced),
                large_to,
            )
        else:
            spent = forced_total
            for row in candidates:
                if args.max_recipients is not None and len(forced) + len(chosen) >= args.max_recipients:
                    break
                if budget is not None and spent + row.amount > budget:
                    if args.strategy == "smallest-first":
                        break
                    continue
                chosen.append(row)
                spent += row.amount

        chosen.sort(key=lambda r: r.address.lower())
        selected = forced + chosen
        selected_keys = {r.address.lower() for r in selected}
        remaining = sorted((r for r in rows if r.address.lower() not in paid and r.address.lower() not in selected_keys),
                           key=lambda r: r.address.lower())

        args.out_dir.mkdir(parents=True, exist_ok=True)
        names = ["selected.csv", "remaining.csv", "selection.json"] + (["draws.csv"] if args.strategy == "random" else [])
        targets = [args.out_dir / name for name in names]
        existing = [str(p) for p in targets if p.exists()]
        if existing and not args.force:
            raise common.InputError(f"{', '.join(existing)} already exist; pass --force to overwrite")
    except common.InputError as exc:
        common.die(str(exc))
        return

    selected_sha = write_list(args.out_dir / "selected.csv", selected)
    remaining_sha = write_list(args.out_dir / "remaining.csv", remaining)
    unselected = [r for r in remaining if r.address.lower() not in held]
    random_report = None
    if args.strategy == "random":
        draws_path = args.out_dir / "draws.csv"
        with draws_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["tier", "draw", "address", "amount_atto", "outcome"])
            for d in draws:
                writer.writerow([d["tier"], d["draw"], d["address"], d["amount_atto"], d["outcome"]])
        tiers = {}
        for tier in TIERS:
            pool = [r for r in candidates if tier_of(r.amount, medium_from, large_from, large_to) == tier]
            picked = [r for r in chosen if tier_of(r.amount, medium_from, large_from, large_to) == tier]
            tiers[tier] = {
                "requested": counts[tier],
                "eligible": summarize(pool),
                "selected": summarize(picked),
                "skipped_over_budget": sum(1 for d in draws if d["tier"] == tier and d["outcome"] != "selected"),
            }
        random_report = {
            "algorithm": RANDOM_ALGORITHM,
            "seed": args.seed,
            "medium_from": str(medium_from),
            "large_from": str(large_from),
            "large_to": str(large_to) if large_to is not None else None,
            "not_drawn_at_or_above_large_to": summarize(
                [r for r in candidates if large_to is not None and r.amount >= large_to]
            ),
            "small_weight": args.small_weight,
            "weight_scale": str(WEIGHT_SCALE),
            "tiers": tiers,
            "draws_file": str(draws_path),
            "draws_sha256": common.sha256_file(draws_path),
        }
    report = {
        "format": "batch-selection/v1",
        "created_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": args.label,
        "input": {"sources": info["sources"], "address_column": info["address_column"],
                  "amount_column": info["amount_column"], "amount_unit": info["amount_unit"], **summarize(rows)},
        "paid_sources": paid_sources,
        "hold_sources": hold_sources,
        "strategy": args.strategy,
        "random": random_report,
        "budget": str(budget) if budget is not None else None,
        "budget_display": common.format_one(budget) if budget is not None else None,
        "max_recipients": args.max_recipients,
        "include": [r.address for r in forced],
        "already_paid": summarize(list(paid.values())),
        "held": summarize([by_address[k] for k in held]),
        "hold_addresses_not_in_list": not_listed_holds,
        "selected": {**summarize(selected), "included": summarize(forced), "filled": summarize(chosen),
                     "largest_filled_amount": str(max((r.amount for r in chosen), default=0)),
                     "file": str(args.out_dir / "selected.csv"), "sha256": selected_sha},
        "remaining": {**summarize(remaining), "not_selected_excluding_held": summarize(unselected),
                      "smallest_not_selected_amount": str(min((r.amount for r in unselected), default=0)),
                      "file": str(args.out_dir / "remaining.csv"), "sha256": remaining_sha},
    }
    (args.out_dir / "selection.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")

    print(f"input            : {len(rows)} recipients, {common.format_one(sum(r.amount for r in rows))}")
    print(f"already paid     : {len(paid)} recipients, {report['already_paid']['total_amount_display']}")
    print(f"held back        : {len(held)} recipients, {report['held']['total_amount_display']}")
    if not_listed_holds:
        print(f"note: {len(not_listed_holds)} hold-list address(es) are not in the input list", file=sys.stderr)
    print(f"strategy         : {args.strategy}")
    if budget is not None:
        print(f"budget           : {common.format_one(budget)}")
    if random_report:
        print(f"seed             : {args.seed!r} ({RANDOM_ALGORITHM}, small weight {args.small_weight})")
        for tier, t in random_report["tiers"].items():
            asked = "all that fit" if t["requested"] is None else t["requested"]
            print(f"  {tier:<6} tier    : {t['selected']['recipients']} drawn (asked {asked}) of {t['eligible']['recipients']} "
                  f"eligible, {t['selected']['total_amount_display']}; {t['skipped_over_budget']} skipped over budget")
    print(f"selected         : {len(selected)} recipients ({len(forced)} required), {report['selected']['total_amount_display']}")
    if chosen:
        print(f"largest filled   : {common.format_one(max(r.amount for r in chosen))}")
    if unselected:
        print(f"smallest left    : {common.format_one(min(r.amount for r in unselected))}")
    print(f"remaining        : {len(remaining)} recipients, {report['remaining']['total_amount_display']}")
    print(f"selected.csv     : sha256 {selected_sha}")
    print(f"written to       : {args.out_dir}")


if __name__ == "__main__":
    main()
