#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "results.sha256"
RESULTS = ROOT / "results" / "2026-09-11"
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


def current_files():
    files = set()
    for private_root in PRIVATE_ROOTS:
        if not private_root.is_dir():
            raise FileNotFoundError(private_root)
        for path in private_root.rglob("*"):
            if path.is_file() and not path.name.endswith(".partial"):
                files.add(path.relative_to(ROOT).as_posix())
    return files


def main():
    listed = {}
    with MANIFEST.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            digest, size, relative = line.rstrip("\n").split("  ", 2)
            if relative in listed:
                raise ValueError(
                    f"duplicate private manifest path at line {line_number}"
                )
            listed[relative] = (digest, int(size))

    actual = current_files()
    if set(listed) != actual:
        missing = sorted(actual - set(listed))
        extra = sorted(set(listed) - actual)
        raise ValueError(
            f"private manifest file set mismatch: missing={missing} "
            f"extra={extra}"
        )
    for relative, (digest, size) in listed.items():
        path = ROOT / relative
        if path.stat().st_size != size:
            raise ValueError(f"private manifest size mismatch: {relative}")
        if sha256(path) != digest:
            raise ValueError(f"private manifest hash mismatch: {relative}")

    index_path = RESULTS / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("schema_version") != 1:
        raise ValueError("unsupported private result index schema")
    indexed = set()
    valid_domains = {"claim-accounting", "destination-mapping"}
    valid_classifications = {
        "database-derived",
        "policy-scenario",
        "RPC-derived",
        "hybrid",
    }
    for entry in index["entries"]:
        relative = entry["path"]
        if relative in indexed:
            raise ValueError(f"duplicate indexed private result: {relative}")
        indexed.add(relative)
        if entry["domain"] not in valid_domains:
            raise ValueError(f"invalid result domain: {relative}")
        if entry["classification"] not in valid_classifications:
            raise ValueError(f"invalid result classification: {relative}")
        path = RESULTS / relative
        source = ROOT / entry["source"]
        if path.stat().st_size != entry["bytes"]:
            raise ValueError(f"indexed result size mismatch: {relative}")
        if sha256(path) != entry["sha256"]:
            raise ValueError(f"indexed result hash mismatch: {relative}")
        if sha256(source) != entry["source_sha256"]:
            raise ValueError(f"indexed result source mismatch: {relative}")
    actual_results = {
        path.relative_to(RESULTS).as_posix()
        for path in RESULTS.rglob("*")
        if path.is_file() and path.name not in {"README.md", "index.json"}
    }
    if indexed != actual_results:
        raise ValueError(
            "private result index file set mismatch: "
            f"missing={sorted(actual_results - indexed)} "
            f"extra={sorted(indexed - actual_results)}"
        )

    provenance_path = ROOT / "docs" / "findings" / "SOURCE-HASHES.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if provenance.get("schema_version") != 1:
        raise ValueError("unsupported private finding provenance schema")
    for entry in provenance["entries"]:
        source = ROOT / entry["source"]
        target = ROOT / entry["target"]
        if sha256(source) != entry["source_sha256"]:
            raise ValueError(f"private finding source mismatch: {source}")
        if sha256(target) != entry["target_sha256"]:
            raise ValueError(f"private finding target mismatch: {target}")
    print(
        f"PASS private result manifest: {len(listed)} files; "
        f"{len(indexed)} indexed results; "
        f"{len(provenance['entries'])} finding provenance records"
    )


if __name__ == "__main__":
    main()
