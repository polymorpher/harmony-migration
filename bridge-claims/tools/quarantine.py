#!/usr/bin/env python3
"""Move rejected signable files out of a claims run, keeping them for identification.

    python3 tools/quarantine.py runs/<run> --reason "<why>" [--include ethereum] [--dry-run]
    python3 tools/quarantine.py runs/<run> --recover

By default this takes every file a signer or key holder could load from the run:

- approval files beside a manifest, left by versions before approval packages
  (`safe-approve.json`, `safe-revoke.json`, `approvals.json`, `APPROVALS.md`, and their `.draft.`
  forms), and the `approval/` and `approval-draft/` packages;
- every release directory of the run (`release/`, `release-*/`).

`--include` adds other paths of the run, for example a rejected contract's whole directory. Each
moves to `quarantine/<UTC time>/` under the same relative path, and `QUARANTINE.md` there records
the reason and the SHA-256 of every moved file. Nothing is deleted.

It holds the run's package lock (`tools/package.py`), so no refresh runs meanwhile. Before moving
anything it writes the plan and every file's SHA-256 to `quarantine/<time>/QUARANTINE.json`, takes
the packages among the paths out of `PACKAGES.json`, and marks the record pending. If a move fails, it
puts back what it moved and clears pending; the packages stay unlisted. If it is interrupted, pending
stays set, so nothing in the run verifies, and `--recover` completes the moves the journal lists.
Run it only as a reviewed step of its own; the approval tool refuses to run while legacy files remain
beside a manifest.
"""

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approve_claims as ac  # noqa: E402
import package  # noqa: E402

QUARANTINE = package.QUARANTINE
JOURNAL = "QUARANTINE.json"
RESERVED = (QUARANTINE, package.SUPERSEDED, package.RECORD, package.LOCK, ".")


def signable_paths(run):
    """Relative paths of the run's signable files and packages, as `quarantine` takes them by default."""
    found = []
    for name in sorted(os.listdir(run)):
        path = os.path.join(run, name)
        if not os.path.isdir(path) or name in (QUARANTINE, package.SUPERSEDED):
            continue
        if name == "release" or name.startswith("release-"):
            found.append(name)
        elif os.path.exists(os.path.join(path, "manifest.json")):
            for output in ac.OUTPUTS:
                for candidate in (output, ac.draft_name(output)):
                    if os.path.lexists(os.path.join(path, candidate)):
                        found.append(os.path.join(name, candidate))
            for directory in (ac.APPROVAL_DIR, ac.DRAFT_DIR):
                if os.path.isdir(os.path.join(path, directory)):
                    found.append(os.path.join(name, directory))
    return found


def files_under(root, rel):
    path = os.path.join(root, rel)
    if os.path.isfile(path):
        return [rel]
    return sorted(os.path.relpath(os.path.join(base, name), root)
                  for base, _, names in os.walk(path) for name in names)


def write_journal(path, journal):
    with open(path + ".partial", "w", encoding="utf-8") as out:
        json.dump(journal, out, indent=2, sort_keys=True)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(path + ".partial", path)


def write_note(dest, run_name, journal):
    lines = [
        "# Quarantined, do not sign or send",
        "",
        f"Moved out of `{run_name}/` at {os.path.basename(dest)}. Reason: {journal['reason']}",
        "",
        "Each path below kept its place relative to the run. Nothing here may be signed or sent.",
        "",
        "| File | SHA-256 |",
        "|---|---|",
    ]
    lines += [f"| `{f}` | `{h}` |" for f, h in journal["files"]]
    with open(os.path.join(dest, "QUARANTINE.md"), "w", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")


def planned_paths(root, include):
    paths = signable_paths(root)
    for rel in include:
        full = os.path.abspath(os.path.join(root, rel))
        rel = os.path.relpath(full, root)
        if rel.startswith("..") or rel.split(os.sep)[0] in RESERVED or not os.path.lexists(full):
            raise SystemExit(f"--include {rel}: not a path inside {root}")
        paths.append(rel)
    unique = []
    for rel in sorted(set(paths)):
        if not any(rel.startswith(kept + os.sep) for kept in unique):
            unique.append(rel)
    for rel in unique:
        package.run_path(root, rel)
    return unique


def quarantine(run, reason, include=(), dry_run=False):
    """Move the run's signable paths and `include` to `quarantine/<UTC time>/`, all or none; returns
    that directory (None for a dry run or when there is nothing to move) and the moved relative paths."""
    root = package.run_root(run)
    with package.run_lock(root):
        record = package.read_record(root)
        pending = record.get("pending")
        if pending and pending.get("operation") == "quarantine":
            raise SystemExit(f"a quarantine was interrupted ({pending.get('journal')}); run with --recover first")
        paths = planned_paths(root, include)
        if not paths or dry_run:
            return None, paths
        if os.path.lexists(os.path.join(root, QUARANTINE)):
            package.run_path(root, QUARANTINE)
        base = os.path.join(root, QUARANTINE, package.utc_stamp())
        dest, n = base, 1
        while os.path.lexists(dest):
            n += 1
            dest = f"{base}-{n}"
        journal = {"status": "moving", "reason": reason, "moves": paths,
                   "files": [[f, package.file_sha256(os.path.join(root, f))]
                             for rel in paths for f in files_under(root, rel)]}
        journal_path = os.path.join(dest, JOURNAL)
        os.makedirs(dest)
        unlisted = package.unlist(record, paths)
        record["pending"] = {"operation": "quarantine", "journal": os.path.relpath(journal_path, root),
                             "started_at": package.utc_stamp()}
        try:
            write_journal(journal_path, journal)
            package.write_record(root, record)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise

        moved = []
        try:
            for rel in paths:
                os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
                os.replace(os.path.join(root, rel), os.path.join(dest, rel))
                moved.append(rel)
        except Exception as error:
            try:
                for rel in reversed(moved):
                    os.replace(os.path.join(dest, rel), os.path.join(root, rel))
                write_journal(journal_path, journal | {"status": "rolled_back", "error": str(error)})
                record["pending"] = None
                package.write_record(root, record)
            except Exception as second:
                raise SystemExit(
                    f"quarantine stopped part-way ({error}) and could not put back what it moved ({second}); "
                    f"{package.RECORD} still marks it pending, so nothing in the run verifies. Fix the cause and "
                    "run again with --recover") from error
            raise SystemExit(
                f"quarantine failed ({error}); everything was put back and nothing moved. "
                + (f"{', '.join(unlisted)} stay out of {package.RECORD}. " if unlisted else "")
                + "Fix the cause and run again") from error
        finish(root, dest, journal_path, journal, record)
        return dest, paths


def finish(root, dest, journal_path, journal, record):
    write_note(dest, os.path.basename(root), journal)
    write_journal(journal_path, journal | {"status": "done"})
    record["pending"] = None
    package.write_record(root, record)


def recover(run):
    """Complete an interrupted quarantine: move what its journal lists and is still in the run."""
    root = package.run_root(run)
    with package.run_lock(root):
        record = package.read_record(root)
        pending = record.get("pending")
        if not pending or pending.get("operation") != "quarantine":
            raise SystemExit("no interrupted quarantine" + (
                f"; an interrupted {pending.get('operation')} clears with its next successful run" if pending else ""))
        journal_path = os.path.join(root, package.run_path(root, pending["journal"]))
        dest = os.path.dirname(journal_path)
        with open(journal_path, encoding="utf-8") as source:
            journal = json.load(source)
        missing = []
        for rel in journal["moves"]:
            src, dst = os.path.join(root, rel), os.path.join(dest, rel)
            if os.path.lexists(dst) and not os.path.lexists(src):
                continue
            if os.path.lexists(src) and not os.path.lexists(dst):
                package.run_path(root, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
            else:
                missing.append(rel)
        if missing:
            raise SystemExit(f"{', '.join(missing)}: neither in the run alone nor in {dest} alone; sort it out by "
                             f"hand against {journal_path}")
        package.unlist(record, journal["moves"])
        finish(root, dest, journal_path, journal, record)
        return dest, journal["moves"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="claims run directory (runs/<run>)")
    parser.add_argument("--reason")
    parser.add_argument("--include", nargs="*", default=[], help="other paths of the run to quarantine")
    parser.add_argument("--dry-run", action="store_true", help="list what would move")
    parser.add_argument("--recover", action="store_true", help="complete an interrupted quarantine")
    args = parser.parse_args()
    if args.recover:
        dest, paths = recover(args.run)
        print(f"completed the quarantine in {dest}: {', '.join(paths)}")
        return
    if not args.reason:
        parser.error("--reason is required")
    dest, paths = quarantine(args.run, args.reason, args.include, args.dry_run)
    for rel in paths:
        print(("would move " if args.dry_run else "moved ") + rel)
    if dest:
        print(f"quarantined {len(paths)} paths in {dest}")
    elif not paths:
        print("nothing to quarantine")


if __name__ == "__main__":
    main()
