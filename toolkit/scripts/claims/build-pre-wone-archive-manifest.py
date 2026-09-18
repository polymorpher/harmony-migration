#!/usr/bin/env python3

"""Index the immutable pre-WONE files and their capture-time paths."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--external",
        action="append",
        default=[],
        metavar="ORIGINAL=CANONICAL",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def original_path(relative):
    parts = relative.parts
    if parts[:1] == ("claims",):
        return str(
            Path("artifacts/cutoff-20260910/claims").joinpath(*parts[1:])
        )
    if parts[:1] == ("contract-review",):
        return str(
            Path("artifacts/contract-review-20260911").joinpath(
                *parts[1:]
            )
        )
    if relative == Path("snapshot-2026-09-10.json"):
        return "manifests/snapshot-2026-09-10.json"
    raise ValueError(f"unrecognized pre-WONE archive path: {relative}")


def parse_external(values):
    result = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"invalid --external mapping: {value}")
        original, canonical = value.split("=", 1)
        if not original or not canonical:
            raise ValueError(f"invalid --external mapping: {value}")
        path = Path(canonical)
        if not path.is_file():
            raise FileNotFoundError(path)
        result.append(
            {
                "original_path": original,
                "canonical_path": canonical,
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    return sorted(result, key=lambda item: item["original_path"])


def main():
    args = parse_args()
    root = Path(args.archive_root)
    output = Path(args.output)
    partial = Path(str(output) + ".partial")
    if not root.is_dir():
        raise NotADirectoryError(root)
    if partial.exists():
        raise FileExistsError(partial)
    if output.exists() and not (args.replace or args.check):
        raise FileExistsError(output)

    output_resolved = output.resolve()
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.resolve() == output_resolved:
            continue
        relative = path.relative_to(root)
        entries.append(
            {
                "archive_path": str(path),
                "original_path": original_path(relative),
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )

    originals = [entry["original_path"] for entry in entries]
    if len(originals) != len(set(originals)):
        raise ValueError("archive contains duplicate original paths")
    if args.check:
        if args.external:
            raise ValueError("--external is not accepted with --check")
        with output.open(encoding="utf-8") as handle:
            recorded = json.load(handle)
        if (
            recorded.get("schema_version") != 1
            or recorded.get("status") != "passed"
            or recorded.get("archive_root") != str(root)
            or recorded.get("entries") != entries
        ):
            raise ValueError("pre-WONE archive manifest entries do not match")
        for item in recorded.get("external_dependencies", []):
            path = Path(item["canonical_path"])
            if (
                not path.is_file()
                or path.stat().st_size != item["bytes"]
                or file_sha256(path) != item["sha256"]
            ):
                raise ValueError(
                    "pre-WONE external dependency mismatch: "
                    f"{item['canonical_path']}"
                )
        print(
            json.dumps(
                {
                    "archive_files": len(entries),
                    "external_dependencies": len(
                        recorded.get("external_dependencies", [])
                    ),
                    "status": "passed",
                },
                sort_keys=True,
            )
        )
        return

    external = parse_external(args.external)
    overlap = set(originals) & {
        entry["original_path"] for entry in external
    }
    if overlap:
        raise ValueError(f"archive and external mappings overlap: {overlap}")

    result = {
        "schema_version": 1,
        "status": "passed",
        "note": (
            "Archived JSON files preserve capture-time paths and hashes. "
            "Resolve those paths through this manifest instead of the "
            "post-WONE working tree."
        ),
        "archive_root": str(root),
        "entries": entries,
        "external_dependencies": external,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, output)
    print(json.dumps({"files": len(entries), "status": "passed"}))


if __name__ == "__main__":
    main()
