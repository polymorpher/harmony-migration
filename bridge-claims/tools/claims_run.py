"""The claim run and custody roster a signable package is built from, checked before any chain read.

`summary.json` (`tools/build_claims.py`) commits each contract's label, chain, chain id, funder,
root, row count, list hash, address, and status. `bind` stops unless:

- labels are unique, plain directory names, and every manifest in the run belongs to one;
- each entry's manifest agrees with it on every one of those fields, and names its label;
- each ready contract's `claims.csv` and `tree.txt` hash to what its manifest records;
- every requested label is a ready entry, named once;
- with a published index (the `SHA256SUMS` of the run's public copy, by default fetched from the
  public bucket), every file it lists hashes the same here, including `summary.json` and each ready
  contract's manifest, list, and tree.

`custody_requirements` takes the custody Safe's owner roster from the committed
`custody-roster.json`; a bridge config naming other owners, count, or threshold stops the tools.
"""

import json
import os
import re
import urllib.request
from collections import namedtuple

import claim_tree as ct
import bridge_lib as lib  # noqa: E402  (toolkit/scripts/bridge, on sys.path through claim_tree)

ROSTER_FILE = ct.PROJECT / "custody-roster.json"
PUBLIC_INDEX = "https://storage.googleapis.com/harmony-bridge-claims/{run}/SHA256SUMS"
LABEL = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
SKIP_DIRS = ("quarantine", "superseded", "release")

Contract = namedtuple("Contract", "entry manifest manifest_sha256 out_dir")
Run = namedtuple("Run", "root summary summary_sha256 contracts index")


def _same(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return a == b and type(a) is type(b)


def committed_fields(manifest):
    """The manifest's values of what `summary.json` commits for its contract."""
    d = manifest.get("deployment") or {}
    return {
        "label": manifest.get("label"), "chain": manifest.get("chain"), "chain_id": manifest.get("chain_id"),
        "funder": manifest.get("funder"), "root": manifest.get("root"), "claim_count": manifest.get("claim_count"),
        "list_sha256": manifest.get("list_sha256"), "address": d.get("address"), "status": d.get("status"),
    }


def read_index(source):
    """{relative path: SHA-256} from a `SHA256SUMS` file or URL."""
    if re.match(r"https?://", source):
        with urllib.request.urlopen(source, timeout=60) as response:
            text = response.read().decode("utf-8")
    else:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    index = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not name or name in index:
            raise SystemExit(f"{source}: not a SHA256SUMS index (line {line!r})")
        index[name.strip()] = digest
    return index


def default_index(claims_dir):
    return PUBLIC_INDEX.format(run=os.path.basename(os.path.realpath(claims_dir)))


def bind(claims_dir, labels=None, index=None):
    """The run's summary and its contracts (every ready one, or `labels`), each bound one to one to
    its manifest and files; `index` is a SHA256SUMS path or URL to compare against."""
    root = os.path.realpath(claims_dir)
    summary_path = os.path.join(root, "summary.json")
    summary = lib.load_json(summary_path)
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    entries = summary.get("contracts") or []
    names = [e.get("label") for e in entries]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"{name}: two summary entries")
    for name in names:
        need(isinstance(name, str) and LABEL.match(name), f"{name!r}: not a plain contract label")
    on_disk = sorted(d for d in os.listdir(root) if d not in SKIP_DIRS and not d.startswith((".", "release-"))
                     and os.path.isfile(os.path.join(root, d, "manifest.json")))
    for name in on_disk:
        need(name in names, f"{name}/manifest.json is in the run but not in summary.json")
    if problems:
        raise SystemExit(f"{claims_dir}: the claim run is not bound to its summary:\n  " + "\n  ".join(problems))

    by_label = {}
    for entry in entries:
        label = entry["label"]
        out_dir = os.path.join(root, label)
        path = os.path.join(out_dir, "manifest.json")
        if not os.path.isfile(path) or os.path.islink(out_dir) or os.path.islink(path):
            problems.append(f"{label}: no manifest.json in a plain {label}/")
            continue
        manifest = lib.load_json(path)
        for field, value in committed_fields(manifest).items():
            need(field in entry and _same(entry[field], value),
                 f"{label}: summary {field} {entry.get(field)!r} is not the manifest's {value!r}")
        if entry.get("status") == "ready":
            for kind, name_field, sha_field in (("list", "list_file", "list_sha256"), ("tree", "tree_file", "tree_sha256")):
                name = manifest.get(name_field)
                if not isinstance(name, str) or os.path.basename(name) != name or name in ("", ".", ".."):
                    problems.append(f"{label}: {name_field} {name!r} is not a file beside the manifest")
                    continue
                file_path = os.path.join(out_dir, name)
                actual = lib.file_sha256(file_path) if os.path.isfile(file_path) else None
                need(actual is not None and actual == manifest.get(sha_field),
                     f"{label}: SHA-256 of {name} ({actual}) is not the manifest's {sha_field} {manifest.get(sha_field)}")
        by_label[label] = Contract(entry, manifest, lib.file_sha256(path), out_dir)

    ready = [e["label"] for e in entries if e.get("status") == "ready"]
    if labels:
        for name in sorted({n for n in labels if labels.count(n) > 1}):
            problems.append(f"--labels names {name} twice")
        for name in labels:
            need(name in names, f"{name}: not a contract of summary.json")
            need(name not in names or name in ready, f"{name}: not ready in summary.json")
    chosen = list(labels) if labels else ready

    checked = None
    if index:
        listed = read_index(index)
        required = ["summary.json"] + [
            os.path.join(name, by_label[name].manifest.get(f) or "")
            for name in chosen if name in by_label for f in ("list_file", "tree_file")
        ] + [os.path.join(name, "manifest.json") for name in chosen]
        for rel in required:
            need(rel in listed, f"{rel} is not in the published index")
        for rel, digest in sorted(listed.items()):
            full = os.path.normpath(os.path.join(root, rel))
            if os.path.relpath(full, root).startswith(".."):
                problems.append(f"published index entry {rel} is outside the run")
                continue
            actual = lib.file_sha256(full) if os.path.isfile(full) else None
            need(actual == digest, f"{rel}: SHA-256 {actual} here, {digest} in the published index")
        checked = {"source": index, "entries": len(listed)}
    if problems:
        raise SystemExit(f"{claims_dir}: the claim run is not bound to its summary:\n  " + "\n  ".join(problems))
    return Run(root, summary, lib.file_sha256(summary_path), [by_label[name] for name in chosen], checked)


def custody_requirements(config, path=ROSTER_FILE):
    """The custody Safe, its owners, and threshold from the committed roster; stops when the bridge
    config's `custody_requirements` says otherwise."""
    with open(path, encoding="utf-8") as source:
        roster = json.load(source)
    owners = sorted(lib.address(o) for o in roster["owners"])
    if len(set(owners)) != len(owners) or len(owners) != roster["owner_count"] or not 0 < roster["threshold"] <= len(owners):
        raise SystemExit(f"{path}: owners, owner_count, and threshold do not agree")
    configured = config.get("custody_requirements") or {}
    if configured.get("owners") and sorted(lib.address(o) for o in configured["owners"]) != owners:
        raise SystemExit(f"custody_requirements.owners in the bridge config are not the owners in {path}")
    for field in ("owner_count", "threshold"):
        if field in configured and int(configured[field]) != roster[field]:
            raise SystemExit(f"custody_requirements.{field} in the bridge config is not the {field} in {path}")
    return {"safe": lib.address(roster["safe"]), "owner_count": roster["owner_count"], "threshold": roster["threshold"],
            "owners": owners, "roster_sha256": lib.file_sha256(path)}
