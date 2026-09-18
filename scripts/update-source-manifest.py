#!/usr/bin/env python3

import hashlib
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "manifests" / "source-code.sha256"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def public_files():
    process = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    for encoded in sorted(item for item in process.stdout.split(b"\0") if item):
        relative = encoded.decode("utf-8")
        if relative == OUTPUT.relative_to(ROOT).as_posix():
            continue
        path = ROOT / relative
        if path.is_symlink():
            raise ValueError(f"public package contains symlink: {relative}")
        if not path.is_file():
            if not path.exists():
                continue
            raise FileNotFoundError(path)
        yield path


def main():
    rows = []
    seen = set()
    for path in public_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in seen:
            raise ValueError(f"duplicate manifest path: {relative}")
        seen.add(relative)
        rows.append((relative, sha256(path), path.stat().st_size))
    rows.sort()

    partial = Path(str(OUTPUT) + ".partial")
    if partial.exists():
        raise FileExistsError(partial)
    with partial.open("x", encoding="utf-8") as output:
        for path, digest, size in rows:
            output.write(f"{digest}  {size}  {path}\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, OUTPUT)
    print(f"wrote {len(rows)} entries to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
