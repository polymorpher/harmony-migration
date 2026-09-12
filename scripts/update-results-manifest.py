#!/usr/bin/env python3

import hashlib
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "manifests" / "results.sha256"
PRIVATE_ROOTS = (
    ROOT / "docs" / "findings",
    ROOT / "results" / "2026-09-11",
    ROOT / "toolkit" / "cmd" / "cutoff-final-verifier",
)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def private_files():
    for private_root in PRIVATE_ROOTS:
        if not private_root.is_dir():
            raise FileNotFoundError(private_root)
        for path in sorted(private_root.rglob("*")):
            if path.is_symlink():
                raise ValueError(
                    f"private result package contains symlink: "
                    f"{path.relative_to(ROOT)}"
                )
            if path.is_file() and not path.name.endswith(".partial"):
                yield path


def main():
    rows = [
        (
            path.relative_to(ROOT).as_posix(),
            sha256(path),
            path.stat().st_size,
        )
        for path in private_files()
    ]
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
    print(f"wrote {len(rows)} private entries to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
