#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "releases" / "2026-09-11.json"


def inspect(path):
    digest = hashlib.sha256()
    lines = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            lines += chunk.count(b"\n")
    return path.stat().st_size, max(lines - 1, 0), digest.hexdigest()


def main():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported release manifest schema")
    seen = set()
    valid_domains = {"claim-accounting", "destination-mapping"}
    valid_classifications = {
        "database-derived",
        "policy-scenario",
        "RPC-derived",
    }
    for entry in manifest["entries"]:
        relative = entry["path"]
        if relative in seen:
            raise ValueError(f"duplicate release path: {relative}")
        seen.add(relative)
        if entry["domain"] not in valid_domains:
            raise ValueError(f"invalid release domain: {relative}")
        if entry["classification"] not in valid_classifications:
            raise ValueError(
                f"invalid release classification: {relative}"
            )
        path = ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        size, rows, digest = inspect(path)
        if size != entry["bytes"]:
            raise ValueError(f"release size mismatch: {relative}")
        if rows != entry["rows"]:
            raise ValueError(f"release row-count mismatch: {relative}")
        if digest != entry["sha256"]:
            raise ValueError(f"release hash mismatch: {relative}")
    print(f"PASS private release manifest: {len(seen)} files")


if __name__ == "__main__":
    main()
