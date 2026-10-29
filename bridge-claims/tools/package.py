"""Signable packages: built whole in an empty staging directory, then published, or not at all.

A package is one directory: a contract's approval batch (`<run>/<label>/approval/`) or a release
plan (`<run>/release/`). `staged` builds every package of one refresh beside its path, seals each
with `PACKAGE.sha256` and the refresh's `GENERATION`, and only then activates them: every earlier
package at those paths moves to `superseded/<name>-<UTC time>/` beside it, and the new ones are
renamed into place. Any failure, while building, sealing, or activating, removes the staged
directories and moves whatever is at the package paths, earlier or new, to `superseded/` with
`INVALIDATED.md`, so after a failed refresh none of its paths holds a package. A lock on the run
directory keeps two refreshes (or a refresh and a quarantine) from interleaving. Symlinks and other
non-regular files are refused anywhere in a package. `shasum -a 256 -c PACKAGE.sha256` inside a
package checks every file.
"""

import contextlib
import errno
import fcntl
import hashlib
import os
import secrets
import shutil
import stat
import tempfile
import time

MANIFEST = "PACKAGE.sha256"
GENERATION = "GENERATION"
SUPERSEDED = "superseded"
INVALIDATED = "INVALIDATED.md"
LOCK = ".packages.lock"


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


def no_links_below(root, path):
    """Refuse a symlink in any component of `path` below `root` (the run directory)."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if rel.startswith(".."):
        raise ValueError(f"{path} is outside {root}")
    current = os.path.abspath(root)
    for part in [] if rel == "." else rel.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise ValueError(f"{current} is a symlink")


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


@contextlib.contextmanager
def run_lock(root):
    """Exclusive lock on the run directory `root`; stops at once if another refresh holds it."""
    os.makedirs(root, exist_ok=True)
    fd = os.open(os.path.join(root, LOCK), os.O_CREAT | os.O_RDWR, 0o644)
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


class Staging:
    def __init__(self):
        self.dirs = {}       # target -> staging directory
        self.published = {}  # target -> SHA-256 of its PACKAGE.sha256
        self.generation = f"{utc_stamp()}-{secrets.token_hex(4)}"


def fail_closed(targets, staging, reason):
    """Remove the staged directories and move whatever is at every target to `superseded/`."""
    for directory in staging.dirs.values():
        shutil.rmtree(directory, ignore_errors=True)
    problems = []
    for target in targets:
        try:
            moved = set_aside(target, invalidation_note(target, reason))
            if moved:
                print(f"invalidated {target}: moved to {moved}")
        except Exception as error:  # keep going: every other target must still be cleared
            problems.append(f"{target}: {error}")
    if problems:
        print("COULD NOT CLEAR, do not sign anything at these paths:\n  " + "\n  ".join(problems))


@contextlib.contextmanager
def staged(targets, replace):
    """Stage one package per target path; the block writes each into `staging.dirs[target]`. Publishes
    all of them when the block completes and every package seals; otherwise publishes none and leaves
    no package at any of the paths. Without `replace` an existing package stops before anything
    changes."""
    targets = [os.path.abspath(t) for t in targets]
    root = os.path.commonpath([os.path.dirname(t) for t in targets])
    for target in targets:
        try:
            no_links_below(root, target)
        except ValueError as error:
            raise SystemExit(str(error))
    existing = [t for t in targets if os.path.lexists(t)]
    if existing and not replace:
        raise SystemExit(f"{', '.join(existing)} exists; pass --replace (the earlier package moves to {SUPERSEDED}/)")
    with run_lock(root):
        staging = Staging()
        try:
            for target in targets:
                parent = os.path.dirname(target)
                os.makedirs(parent, exist_ok=True)
                staging.dirs[target] = tempfile.mkdtemp(
                    prefix=f".{os.path.basename(os.path.normpath(target))}-staging-", dir=parent)
            yield staging
            umask = os.umask(0)
            os.umask(umask)
            for target, directory in staging.dirs.items():
                with open(os.path.join(directory, GENERATION), "w", encoding="utf-8") as out:
                    out.write(staging.generation + "\n")
                os.chmod(directory, 0o777 & ~umask)
                staging.published[target] = seal(directory)
            previous = {target: set_aside(target) for target in targets}
            for target, directory in staging.dirs.items():
                os.replace(directory, target)
        except BaseException as error:
            fail_closed(targets, staging, str(error) or type(error).__name__)
            raise
        for target in targets:
            print(f"published {target} (generation {staging.generation}, PACKAGE.sha256 "
                  f"{staging.published[target]})"
                  + (f"; the earlier package moved to {previous[target]}" if previous[target] else ""))
