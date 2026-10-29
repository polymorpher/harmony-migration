#!/usr/bin/env python3
"""Signable packages of a claims run, and the run record that says which of them are current.

    python3 tools/package.py verify runs/<run> [<label>/approval | release ...]

A run (`runs/<run>/`) holds packages: each contract's approval batch (`<label>/approval/`, or
`<label>/approval-draft/` for one not to be signed) and the release plan (`release/`,
`release-<name>/`). A package is current only while the run record `PACKAGES.json` lists it with the
SHA-256 of its `PACKAGE.sha256` and its `GENERATION`, and marks no operation pending. `verify` checks
that, then every file against `PACKAGE.sha256`, and names every unlisted directory that looks like a
package. Anything not listed, including `superseded/` and `quarantine/`, is not to be signed.

Every change to a run's packages holds one lock on the run directory (`.packages.lock`), so a
refresh (`staged`) and a quarantine (`tools/quarantine.py`) never overlap, whichever packages they
touch. A refresh first takes its targets out of the record and marks it pending; that write is the
first change, and when it fails nothing changes. It then builds every package in an empty staging
directory, seals each with its `GENERATION`, moves the earlier packages to `superseded/<name>-<UTC
time>/` beside them, renames the new ones into place, and lists them in the record, clearing pending,
only after all of them are in place. So a refresh that fails or is interrupted after its first write
leaves none of its targets listed. On a failure it also moves whatever is at them, earlier or new, to
`superseded/` with `INVALIDATED.md`. Symlinks are refused from the run directory down through every
target, and anywhere in a package.

Without this tool: the record's `packages.<path>.package_sha256` is `shasum -a 256 <path>/PACKAGE.sha256`,
`<path>/GENERATION` holds its `generation`, `pending` is null, and `shasum -a 256 -c PACKAGE.sha256`
passes inside the package. The SHA-256 of `PACKAGES.json`, printed by every refresh, identifies the
committed state; compare it with the one the builder sends.
"""

import argparse
import contextlib
import errno
import fcntl
import glob
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
import time

MANIFEST = "PACKAGE.sha256"
GENERATION = "GENERATION"
SUPERSEDED = "superseded"
QUARANTINE = "quarantine"
INVALIDATED = "INVALIDATED.md"
LOCK = ".packages.lock"
RECORD = "PACKAGES.json"
RECORD_SCHEMA = 1


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def regular_tree(directory):
    """Relative paths of every file under `directory`; refuses symlinks and anything but plain
    files and directories, so nothing reaches outside it."""
    found = []
    for root, dirs, names in os.walk(directory):
        for name in dirs + names:
            path = os.path.join(root, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"{path}: not a plain file or directory")
            if stat.S_ISREG(mode):
                found.append(os.path.relpath(path, directory))
    return sorted(found)


def run_root(run):
    """The run directory, resolved once: every lock, record, and target path is taken from it."""
    root = os.path.realpath(run)
    if not os.path.isdir(root):
        raise SystemExit(f"{run}: not a run directory")
    return root


def run_path(root, path):
    """`path` (relative to the run, or absolute inside it) as a normalized relative path, refusing
    `..`, the run itself, and a symlink in any component from the run down."""
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    rel = os.path.relpath(full, root)
    if rel == "." or rel.startswith(".." + os.sep) or rel == "..":
        raise SystemExit(f"{path}: not a path inside the run {root}")
    current = root
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise SystemExit(f"{current} is a symlink; packages are written only through plain directories")
    return rel


def seal(directory):
    """Write `PACKAGE.sha256` over every file in `directory`; return the SHA-256 of that list."""
    lines = [f"{file_sha256(os.path.join(directory, rel))}  {rel}" for rel in regular_tree(directory) if rel != MANIFEST]
    text = "".join(line + "\n" for line in sorted(lines, key=lambda line: line[66:]))
    with open(os.path.join(directory, MANIFEST), "w", encoding="utf-8") as out:
        out.write(text)
    return hashlib.sha256(text.encode()).hexdigest()


def set_aside(target, note=None):
    """Move the package at `target` to `superseded/` beside it, with `INVALIDATED.md` holding `note`
    if given. Returns where it went, or None when there was nothing at `target`."""
    if not os.path.lexists(target):
        return None
    if os.path.islink(target) or not os.path.isdir(target):
        raise ValueError(f"{target}: a package must be a directory, not a link or file")
    root = os.path.join(os.path.dirname(os.path.abspath(target)), SUPERSEDED)
    if os.path.islink(root):
        raise ValueError(f"{root} is a symlink")
    os.makedirs(root, exist_ok=True)
    base = os.path.join(root, f"{os.path.basename(os.path.normpath(target))}-{utc_stamp()}")
    dest, n = base, 1
    while os.path.lexists(dest):
        n += 1
        dest = f"{base}-{n}"
    os.replace(target, dest)
    if note:
        with open(os.path.join(dest, INVALIDATED), "w", encoding="utf-8") as out:
            out.write(note)
    return dest


def invalidation_note(target, reason):
    return (f"# Invalidated, do not sign\n\nThis package was moved out of `{os.path.basename(os.path.normpath(target))}/` "
            f"at {utc_stamp()} because a rebuild of it failed:\n\n```text\n{reason}\n```\n\n"
            "Nothing in this directory may be signed or sent. Fix the cause and rebuild.\n")


# ------------------------------------------------------------------ the run record and lock


def empty_record():
    return {"schema_version": RECORD_SCHEMA, "revision": 0, "updated_at": None, "pending": None, "packages": {}}


def read_record(root):
    path = os.path.join(root, RECORD)
    if not os.path.lexists(path):
        return empty_record()
    if os.path.islink(path) or not os.path.isfile(path):
        raise SystemExit(f"{path}: not a plain file")
    with open(path, encoding="utf-8") as source:
        record = json.load(source)
    if record.get("schema_version") != RECORD_SCHEMA or not isinstance(record.get("packages"), dict):
        raise SystemExit(f"{path}: not a run record this tool writes")
    return record


def record_sha256(root):
    path = os.path.join(root, RECORD)
    return file_sha256(path) if os.path.exists(path) else None


def write_record(root, record):
    """Replace `PACKAGES.json` atomically with `record` at the next revision; returns its SHA-256."""
    record.update(revision=int(record.get("revision") or 0) + 1, updated_at=utc_stamp())
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    partial = os.path.join(root, f".{RECORD}.{secrets.token_hex(4)}.partial")
    try:
        with open(partial, "x", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(partial, os.path.join(root, RECORD))
    finally:
        if os.path.lexists(partial):
            os.remove(partial)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(text.encode()).hexdigest()


def unlist(record, paths):
    """Drop every listed package at or below one of `paths`; returns the dropped paths."""
    dropped = [p for p in record["packages"] if any(p == q or p.startswith(q + os.sep) for q in paths)]
    for p in dropped:
        del record["packages"][p]
    return dropped


@contextlib.contextmanager
def run_lock(root):
    """Exclusive lock on the run directory `root`; stops at once if another refresh or quarantine
    holds it."""
    path = os.path.join(root, LOCK)
    if os.path.islink(path):
        raise SystemExit(f"{path} is a symlink")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise SystemExit(f"{root}: another refresh or quarantine is running; try again when it is done")
            raise
        yield
    finally:
        os.close(fd)


# ------------------------------------------------------------------ refresh


class Staging:
    def __init__(self):
        self.dirs = {}       # target -> staging directory
        self.published = {}  # target -> SHA-256 of its PACKAGE.sha256
        self.generation = f"{utc_stamp()}-{secrets.token_hex(4)}"


def staging_prefix(rel):
    return f".{os.path.basename(rel)}-staging-"


def fail_closed(root, rels, staging, record, reason):
    """Remove the staged directories, move whatever is at every target to `superseded/`, and clear
    `pending`; the targets are already unlisted, so even what cannot be moved does not verify."""
    for directory in staging.dirs.values():
        shutil.rmtree(directory, ignore_errors=True)
    problems = []
    for rel in rels:
        target = os.path.join(root, rel)
        try:
            moved = set_aside(target, invalidation_note(target, reason))
            if moved:
                print(f"invalidated {target}: moved to {moved}")
        except Exception as error:  # keep going: every other target must still be cleared
            problems.append(f"{target}: {error}")
    if problems:
        print(f"could not move these; they are not listed in {RECORD}, so `tools/package.py verify` refuses them, "
              "and nothing there may be signed:\n  " + "\n  ".join(problems))
    try:
        record["pending"] = None
        write_record(root, record)
    except Exception as error:
        print(f"{RECORD} still marks the refresh pending ({error}): nothing in the run verifies until a refresh "
              "succeeds")


@contextlib.contextmanager
def staged(run, targets, replace):
    """Stage one package per target (a path relative to the run); the block writes each into
    `staging.dirs[target]`. Lists all of them in the run record when the block completes and every
    one seals and is in place; otherwise lists none, and leaves no package at any of the paths.
    Without `replace` an existing package stops before anything changes."""
    root = run_root(run)
    rels = {target: run_path(root, target) for target in targets}
    if len(set(rels.values())) != len(rels):
        raise SystemExit("the same package path is named twice")
    with run_lock(root):
        record = read_record(root)
        pending = record.get("pending")
        if pending and pending.get("operation") == "quarantine":
            raise SystemExit(f"{root}: a quarantine was interrupted ({pending.get('journal')}); finish it with "
                             "`tools/quarantine.py <run> --recover` first")
        if pending:
            print(f"an earlier refresh of {', '.join(pending.get('targets') or [])} was interrupted; none of its "
                  "targets is listed")
            for rel in pending.get("targets") or []:
                for leftover in glob.glob(os.path.join(root, os.path.dirname(rel), staging_prefix(rel) + "*")):
                    shutil.rmtree(leftover, ignore_errors=True)
        existing = [os.path.join(root, r) for r in rels.values() if os.path.lexists(os.path.join(root, r))]
        if existing and not replace:
            raise SystemExit(f"{', '.join(existing)} exists; pass --replace (the earlier package moves to {SUPERSEDED}/)")
        unlist(record, list(rels.values()))
        record["pending"] = {"operation": "refresh", "targets": sorted(rels.values()), "started_at": utc_stamp()}
        write_record(root, record)

        staging = Staging()
        try:
            for target, rel in rels.items():
                parent = os.path.join(root, os.path.dirname(rel))
                os.makedirs(parent, exist_ok=True)
                if os.path.dirname(rel):
                    run_path(root, parent)
                staging.dirs[target] = tempfile.mkdtemp(prefix=staging_prefix(rel), dir=parent)
            yield staging
            umask = os.umask(0)
            os.umask(umask)
            for target, directory in staging.dirs.items():
                with open(os.path.join(directory, GENERATION), "w", encoding="utf-8") as out:
                    out.write(staging.generation + "\n")
                os.chmod(directory, 0o777 & ~umask)
                staging.published[target] = seal(directory)
            previous = {target: set_aside(os.path.join(root, rel)) for target, rel in rels.items()}
            for target, directory in staging.dirs.items():
                os.replace(directory, os.path.join(root, rels[target]))
            stamp = utc_stamp()
            for target, rel in rels.items():
                record["packages"][rel] = {"generation": staging.generation,
                                           "package_sha256": staging.published[target], "published_at": stamp}
            record["pending"] = None
            committed = write_record(root, record)
        except BaseException as error:
            fail_closed(root, list(rels.values()), staging, record, str(error) or type(error).__name__)
            raise
        for target, rel in rels.items():
            print(f"published {os.path.join(root, rel)} (generation {staging.generation}, PACKAGE.sha256 "
                  f"{staging.published[target]})"
                  + (f"; the earlier package moved to {previous[target]}" if previous[target] else ""))
        print(f"{os.path.join(root, RECORD)} revision {record['revision']}, SHA-256 {committed}")


# ------------------------------------------------------------------ verify


def check_package(root, rel, entry):
    """Why the package at `rel` is not the one `entry` lists; empty when it is."""
    path = os.path.join(root, rel)
    try:
        run_path(root, rel)
        if not os.path.isdir(path):
            return ["missing"]
        files = regular_tree(path)
    except (SystemExit, ValueError) as error:
        return [str(error)]
    if MANIFEST not in files:
        return [f"no {MANIFEST}"]
    problems = []
    if file_sha256(os.path.join(path, MANIFEST)) != entry.get("package_sha256"):
        problems.append(f"{MANIFEST} is not the one {RECORD} lists")
    listed = {}
    with open(os.path.join(path, MANIFEST), encoding="utf-8") as source:
        for line in source:
            digest, _, name = line.rstrip("\n").partition("  ")
            listed[name] = digest
    for name in sorted(set(files) - {MANIFEST} | set(listed)):
        if name not in listed:
            problems.append(f"{name} is not in {MANIFEST}")
        elif name not in files:
            problems.append(f"{name} is missing")
        elif file_sha256(os.path.join(path, name)) != listed[name]:
            problems.append(f"{name} differs from {MANIFEST}")
    if GENERATION in files:
        with open(os.path.join(path, GENERATION), encoding="utf-8") as source:
            if source.read().strip() != entry.get("generation"):
                problems.append(f"{GENERATION} is not the one {RECORD} lists")
    else:
        problems.append(f"no {GENERATION}")
    return problems


def unlisted_packages(root, record):
    """Directories of the run holding a `PACKAGE.sha256` that the record does not list (outside
    `superseded/`, `quarantine/`, and staging directories)."""
    found = []
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in (SUPERSEDED, QUARANTINE) and not d.startswith("."))
        rel = os.path.relpath(base, root)
        if MANIFEST in names and rel != "." and rel not in record["packages"]:
            found.append(rel)
    return found


def verify(run, only=None):
    """(record SHA-256, record, [(path, current, details)]) for every listed package, or only those
    named, then every unlisted package-like directory (never current)."""
    root = run_root(run)
    record = read_record(root)
    pending = record.get("pending")
    wanted = [run_path(root, p) for p in only] if only else sorted(record["packages"])
    results = []
    for rel in wanted:
        entry = record["packages"].get(rel)
        if entry is None:
            results.append((rel, False, [f"not in {RECORD}: not to be signed"]))
            continue
        problems = check_package(root, rel, entry)
        if pending:
            problems.insert(0, f"a {pending.get('operation')} started {pending.get('started_at')} is pending")
        results.append((rel, not problems, problems or [f"generation {entry['generation']}"]))
    if not only:
        results += [(rel, False, [f"not in {RECORD}: not to be signed"]) for rel in unlisted_packages(root, record)]
    return record_sha256(root), record, results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("verify", help="check which packages of a run are current")
    check.add_argument("run")
    check.add_argument("packages", nargs="*", help="package paths relative to the run (default: every listed one)")
    args = parser.parse_args()
    digest, record, results = verify(args.run, args.packages)
    print(f"{RECORD} revision {record['revision']}, SHA-256 {digest}"
          + (f"; PENDING {record['pending']['operation']}" if record.get("pending") else ""))
    for rel, ok, details in results:
        print(f"{'current' if ok else 'NOT CURRENT'}  {rel}: {'; '.join(details)}")
    sys.exit(0 if results and all(ok for _, ok, _ in results) else 1)


if __name__ == "__main__":
    main()
