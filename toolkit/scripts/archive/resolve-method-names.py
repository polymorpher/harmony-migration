#!/usr/bin/env python3
"""Name the function selectors of a built transaction archive.

Collects the distinct 4-byte selectors of calls in <archive>/transactions.csv
(a contract creation's leading bytes are code, not a selector) and looks
them up in the OpenChain signature database, with 4byte.directory as fallback.
A selector can match several signatures (collisions, spam registrations):
signatures seen in verified contracts are preferred, then the database's own
order. The choice is a label, not a decoding proof; `verified = 0` marks a name
no verified contract uses.

Writes <archive>/method_names.csv (selector, signature, verified, candidates,
source) and records it in manifest.json.
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_lib as lib  # noqa: E402

OPENCHAIN = "https://api.openchain.xyz/signature-database/v1/lookup"
FOURBYTE = "https://www.4byte.directory/api/v1/signatures/"
FIELDS = ["selector", "signature", "verified", "candidates", "source"]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", required=True)
    parser.add_argument("--batch", type=int, default=50)
    return parser.parse_args()


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "harmony-migration-tx-archive/1",
                                                   "Accept": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except (OSError, ValueError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)


def openchain(selectors):
    query = urllib.parse.urlencode({"function": ",".join(selectors), "filter": "true"})
    found = (get_json(f"{OPENCHAIN}?{query}").get("result") or {}).get("function") or {}
    out = {}
    for selector in selectors:
        candidates = [c for c in found.get(selector) or [] if not c.get("filtered")]
        if candidates:
            best = next((c for c in candidates if c.get("hasVerifiedContract")), candidates[0])
            out[selector] = {"signature": best["name"], "verified": int(bool(best.get("hasVerifiedContract"))),
                             "candidates": len(candidates), "source": "openchain"}
    return out


def fourbyte(selector):
    results = get_json(f"{FOURBYTE}?{urllib.parse.urlencode({'hex_signature': selector})}").get("results") or []
    if not results:
        return None
    best = min(results, key=lambda r: r["id"])
    return {"signature": best["text_signature"], "verified": 0, "candidates": len(results), "source": "4byte"}


def main():
    args = parse_args()
    archive = Path(args.archive)
    manifest_path = archive / "manifest.json"
    manifest = lib.load_json(manifest_path)
    selectors = sorted({row["method"] for row in lib.iter_csv(archive / "transactions.csv")
                        if len(row["method"]) == 10 and row["kind"] != "create"})
    names = {}
    for start in range(0, len(selectors), args.batch):
        names.update(openchain(selectors[start:start + args.batch]))
    for selector in selectors:
        if selector not in names:
            found = fourbyte(selector)
            if found:
                names[selector] = found
    rows = [{"selector": s, **names[s]} for s in selectors if s in names]
    count, digest = lib.write_csv(archive / "method_names.csv", FIELDS, rows)
    manifest["files"]["method_names.csv"] = {
        "rows": count, "sha256": digest, "bytes": (archive / "method_names.csv").stat().st_size,
        "selectors": len(selectors), "unnamed": len(selectors) - count,
        "sources": [OPENCHAIN, FOURBYTE], "retrieved_utc": lib.utc(time.time()),
    }
    lib.write_json(manifest_path, manifest)
    print(f"named {count} of {len(selectors)} selectors "
          f"({sum(r['verified'] for r in rows)} from verified contracts)", flush=True)


if __name__ == "__main__":
    main()
