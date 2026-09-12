#!/usr/bin/env python3

import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "source-code.sha256"
GENERATOR = ROOT / "scripts" / "update-source-manifest.py"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "source_manifest_generator", GENERATOR
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load source-manifest generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    listed = {}
    with MANIFEST.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            digest, size, relative = line.rstrip("\n").split("  ", 2)
            if relative in listed:
                raise ValueError(
                    f"duplicate manifest path at line {line_number}"
                )
            listed[relative] = (digest, int(size))

    generator = load_generator()
    expected = {
        path.relative_to(ROOT).as_posix(): path
        for path in generator.public_files()
    }
    if set(listed) != set(expected):
        missing = sorted(set(expected) - set(listed))
        extra = sorted(set(listed) - set(expected))
        raise ValueError(
            "source manifest file set mismatch: "
            f"missing={missing} extra={extra}"
        )
    for relative, path in expected.items():
        digest, size = listed[relative]
        if path.stat().st_size != size:
            raise ValueError(f"source manifest size mismatch: {relative}")
        if sha256(path) != digest:
            raise ValueError(f"source manifest hash mismatch: {relative}")
    print(f"PASS source manifest: {len(listed)} files")


if __name__ == "__main__":
    main()
