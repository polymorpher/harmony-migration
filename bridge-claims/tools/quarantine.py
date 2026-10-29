#!/usr/bin/env python3
"""Move rejected signable files out of a claims run, keeping them for identification.

    python3 tools/quarantine.py runs/<run> --reason "<why>" [--include ethereum] [--dry-run]

By default this takes every file a signer or key holder could load from the run:

- approval files beside a manifest, left by versions before approval packages
  (`safe-approve.json`, `safe-revoke.json`, `approvals.json`, `APPROVALS.md`, and their `.draft.`
  forms), and the `approval/` and `approval-draft/` packages;
- every release directory of the run (`release/`, `release-*/`).

`--include` adds other paths of the run, for example a rejected contract's whole directory. Each
moves to `quarantine/<UTC time>/` under the same relative path, and `QUARANTINE.md` there records
the reason and the SHA-256 of every moved file. Nothing is deleted. Run it only as a reviewed step
of its own; the approval tool refuses to run while such files remain beside a manifest.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approve_claims as ac  # noqa: E402
import package  # noqa: E402

QUARANTINE = "quarantine"


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


def quarantine(run, reason, include=(), dry_run=False):
    """Move the run's signable paths and `include` to `quarantine/<UTC time>/`; returns that directory
    (None for a dry run or when there is nothing to move) and the moved relative paths."""
    run = os.path.abspath(run)
    paths = signable_paths(run)
    for rel in include:
        full = os.path.abspath(os.path.join(run, rel))
        rel = os.path.relpath(full, run)
        if rel.startswith("..") or rel.split(os.sep)[0] in (QUARANTINE, ".") or not os.path.lexists(full):
            raise SystemExit(f"--include {rel}: not a path inside {run}")
        paths.append(rel)
    unique = []
    for rel in sorted(set(paths)):
        if not any(rel.startswith(kept + os.sep) for kept in unique):
            unique.append(rel)
    paths = unique
    if not paths or dry_run:
        return None, paths
    dest = os.path.join(run, QUARANTINE, package.utc_stamp())
    if os.path.lexists(dest):
        raise SystemExit(f"{dest} exists; run again in a second")
    hashes = [(f, package.file_sha256(os.path.join(run, f))) for rel in paths for f in files_under(run, rel)]
    for rel in paths:
        os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
        os.replace(os.path.join(run, rel), os.path.join(dest, rel))
    lines = [
        "# Quarantined, do not sign or send",
        "",
        f"Moved out of `{os.path.basename(run)}/` at {os.path.basename(dest)}. Reason: {reason}",
        "",
        "Each path below kept its place relative to the run. Nothing here may be signed or sent.",
        "",
        "| File | SHA-256 |",
        "|---|---|",
    ]
    lines += [f"| `{f}` | `{h}` |" for f, h in hashes]
    with open(os.path.join(dest, "QUARANTINE.md"), "w", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")
    return dest, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", help="claims run directory (runs/<run>)")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--include", nargs="*", default=[], help="other paths of the run to quarantine")
    parser.add_argument("--dry-run", action="store_true", help="list what would move")
    args = parser.parse_args()
    dest, paths = quarantine(args.run, args.reason, args.include, args.dry_run)
    for rel in paths:
        print(("would move " if args.dry_run else "moved ") + rel)
    if dest:
        print(f"quarantined {len(paths)} paths in {dest}")
    elif not paths:
        print("nothing to quarantine")


if __name__ == "__main__":
    main()
