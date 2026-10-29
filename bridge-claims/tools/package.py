"""Signable packages: built whole in an empty staging directory, then published, or not at all.

A package is one directory: a contract's approval batch (`<run>/<label>/approval/`) or a release
plan (`<run>/release/`). `build` stages it beside its path, seals it with `PACKAGE.sha256`, and
renames it into place only when the whole build succeeds; an earlier package at that path first
moves to `superseded/<name>-<UTC time>/` beside it. When a build fails, the staged directory is
removed and an earlier package is moved there the same way with `INVALIDATED.md`, so the package
path only ever holds the output of the latest successful build. `shasum -a 256 -c PACKAGE.sha256`
inside a package checks every file.
"""

import contextlib
import hashlib
import os
import shutil
import tempfile
import time

MANIFEST = "PACKAGE.sha256"
SUPERSEDED = "superseded"
INVALIDATED = "INVALIDATED.md"


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def seal(directory):
    """Write `PACKAGE.sha256` over every file in `directory`; return the SHA-256 of that list."""
    lines = []
    for root, dirs, names in os.walk(directory):
        dirs.sort()
        for name in names:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, directory)
            if rel != MANIFEST:
                lines.append(f"{file_sha256(path)}  {rel}")
    text = "".join(line + "\n" for line in sorted(lines, key=lambda line: line[66:]))
    with open(os.path.join(directory, MANIFEST), "w", encoding="utf-8") as out:
        out.write(text)
    return hashlib.sha256(text.encode()).hexdigest()


def set_aside(target, note=None):
    """Move the package at `target` to `superseded/` beside it, with `INVALIDATED.md` holding `note`
    if given. Returns where it went, or None when there was nothing at `target`."""
    if not os.path.lexists(target):
        return None
    root = os.path.join(os.path.dirname(os.path.abspath(target)), SUPERSEDED)
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


class Staging:
    def __init__(self):
        self.dirs = {}       # target -> staging directory
        self.published = {}  # target -> SHA-256 of its PACKAGE.sha256


@contextlib.contextmanager
def staged(targets, replace):
    """Stage one package per target path; the block writes each into `staging.dirs[target]`. Publishes
    them all when the block completes; when it raises, publishes none and invalidates every earlier
    package at those paths. Without `replace` an existing package stops before anything changes."""
    existing = [t for t in targets if os.path.lexists(t)]
    if existing and not replace:
        raise SystemExit(f"{', '.join(existing)} exists; pass --replace (the earlier package moves to {SUPERSEDED}/)")
    staging = Staging()
    try:
        for target in targets:
            parent = os.path.dirname(os.path.abspath(target))
            os.makedirs(parent, exist_ok=True)
            staging.dirs[target] = tempfile.mkdtemp(
                prefix=f".{os.path.basename(os.path.normpath(target))}-staging-", dir=parent)
        yield staging
    except BaseException as error:
        for directory in staging.dirs.values():
            shutil.rmtree(directory, ignore_errors=True)
        reason = str(error) or type(error).__name__
        for target in targets:
            moved = set_aside(target, invalidation_note(target, reason))
            if moved:
                print(f"invalidated the earlier package {target}: moved to {moved}")
        raise
    umask = os.umask(0)
    os.umask(umask)
    for target, directory in staging.dirs.items():
        os.chmod(directory, 0o777 & ~umask)
        digest = seal(directory)
        previous = set_aside(target)
        os.replace(directory, target)
        staging.published[target] = digest
        print(f"published {target} (PACKAGE.sha256 {digest})"
              + (f"; the earlier package moved to {previous}" if previous else ""))
