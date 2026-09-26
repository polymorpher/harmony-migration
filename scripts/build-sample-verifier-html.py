#!/usr/bin/env python3

"""Build the self-contained offline HTML random-sample verifier.

The page embeds the shared JavaScript engine, the rules file that both engines
interpret, and the pinned public snapshot, so it needs no network access and
cannot drift from the CLI's formulas. Run with --check in CI to prove the
committed page matches its sources.
"""

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "toolkit" / "verifier"
TEMPLATE = VERIFIER / "verifier.template.html"
CORE = VERIFIER / "sample_core.js"
RULES = VERIFIER / "rules.json"
OUTPUT = VERIFIER / "random-sample-verifier.html"


def js_string(text):
    return json.dumps(text, ensure_ascii=True).replace("</", "<\\/").replace("<!--", "<\\!--")


def render():
    rules_text = RULES.read_text(encoding="utf-8")
    snapshot_path = ROOT / json.loads(rules_text)["pinned_snapshot"]
    core = CORE.read_text(encoding="utf-8")
    if "</script" in core.lower() or "<!--" in core:
        raise ValueError("sample_core.js must not contain '</script' or '<!--'")
    page = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "/*@@CORE@@*/": core,
        "/*@@RULES@@*/": js_string(rules_text),
        "/*@@SNAPSHOT@@*/": js_string(snapshot_path.read_text(encoding="utf-8")),
    }
    for marker, value in replacements.items():
        if page.count(marker) != 1:
            raise ValueError(f"template must contain {marker} exactly once")
        page = page.replace(marker, value)
    return page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed page is out of date")
    args = parser.parse_args()
    page = render()
    relative = OUTPUT.relative_to(ROOT)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else None
        if current != page:
            print(f"FAIL {relative} is out of date; run scripts/build-sample-verifier-html.py", file=sys.stderr)
            return 1
        print(f"PASS {relative} matches its sources")
        return 0
    partial = Path(str(OUTPUT) + ".partial")
    partial.write_text(page, encoding="utf-8")
    os.replace(partial, OUTPUT)
    print(f"wrote {relative}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
