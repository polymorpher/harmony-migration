#!/usr/bin/env python3

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    "private macOS path": re.compile(rb"/Users/"),
    "private Linux home": re.compile(rb"/home/(?:ubuntu|polymorpher)"),
    "private mount": re.compile(rb"/mnt/data"),
    "production database path": re.compile(rb"/data/harmony_db"),
    "private RPC hostname": re.compile(rb"\brpc\.hs\b"),
    "archival host IP": re.compile(rb"\b69\.67\.149\.19\b"),
    "private LAN address": re.compile(rb"\b192\.168\."),
    "embedded SSH target": re.compile(rb"\bubuntu@"),
    "private key marker": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
}
FORBIDDEN_SUFFIXES = {
    ".download",
    ".dylib",
    ".ldb",
    ".partial",
    ".so",
    ".sst",
}
MAX_PUBLIC_BYTES = 1_000_000


def files():
    process = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    for encoded in process.stdout.split(b"\0"):
        if not encoded:
            continue
        path = ROOT / encoded.decode("utf-8")
        if path.is_symlink():
            raise ValueError(
                f"public package contains symlink: {path.relative_to(ROOT)}"
            )
        if path.is_dir():
            # git submodule (e.g. airdrop/lib/forge-std); pinned by commit in .gitmodules
            continue
        if not path.is_file():
            raise FileNotFoundError(path)
        yield path


def main():
    problems = []
    checked = 0
    for path in files():
        if path.resolve() == Path(__file__).resolve():
            continue
        relative = path.relative_to(ROOT)
        checked += 1
        if path.suffix in FORBIDDEN_SUFFIXES:
            problems.append(f"{relative}: forbidden generated/binary suffix")
        if path.stat().st_size > MAX_PUBLIC_BYTES:
            problems.append(f"{relative}: exceeds {MAX_PUBLIC_BYTES} bytes")
        encoded = path.read_bytes()
        if b"\x00" in encoded:
            problems.append(f"{relative}: contains binary NUL")
            continue
        for label, pattern in FORBIDDEN.items():
            if pattern.search(encoded):
                problems.append(f"{relative}: {label}")
    if problems:
        raise ValueError(
            "public-package check failed:\n" + "\n".join(problems)
        )
    print(f"PASS public-package hygiene: {checked} files")


if __name__ == "__main__":
    main()
