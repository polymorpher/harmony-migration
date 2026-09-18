#!/usr/bin/env python3

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_PATHS = (
    "docs/findings/",
    "embargoed/",
    "repro/as-run/",
    "results/2026-09-11/",
    "routing/local/",
    "exchanges/",
    "toolkit/cmd/cutoff-final-verifier/",
)
SENSITIVE_FILES = {
    "manifests/releases/2026-09-11.json",
    "manifests/results.sha256",
    "results/claims-at-least-1000-one-summary.json",
    "results/claims-over-1000-one-summary.json",
    "results/cutoff-claims-summary.json",
    "results/selected-eligibility-summary.json",
}
PUBLIC_EXCEPTIONS = set()
TEXT_SUFFIXES = {
    ".csv",
    ".go",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
MONETARY_RESULT = re.compile(
    r"(?:"
    r"\b\d[\d,]*\.\d+\s+(?:ONE|WONE)\b|"
    r"\b(?:\d{4,}|\d{1,3}(?:,\d{3})+)\s+(?:ONE|WONE)\b|"
    r"\b\d{20,}\s+atto(?:-ONE)?\b"
    r")",
    re.IGNORECASE,
)
POPULATION_RESULT = re.compile(
    r"(?<![-\w])\d[\d,]*\s+(?:"
    r"accounts|claims|contracts|delegations|receipts|rows|validators|wallets"
    r")\b",
    re.IGNORECASE,
)
RESULT_JSON_VALUE = re.compile(
    r'"[^"]*(?:'
    r"amount|balance|claim|count|issuance|payout|reclaim|rows|supply|total"
    r')[^"]*"\s*:\s*"?-?\d{7,}"?',
    re.IGNORECASE,
)
PUBLIC_NUMERIC_CONSTANTS = (
    "1,000 ONE",
    "1000 ONE",
    "1,000,000 ONE",
    "1,000,000,000,000,000,000 atto-ONE",
    "1,000,000,000,000,000,000,000 atto-ONE",
)


def public_files():
    process = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        item.decode("utf-8")
        for item in process.stdout.split(b"\0")
        if item
    ]


def result_json_value(line):
    if '"threshold_atto"' in line or '"minimum_atto"' in line:
        return False
    return bool(RESULT_JSON_VALUE.search(line))


def main():
    files = public_files()
    exposed = [
        relative
        for relative in files
        if relative not in PUBLIC_EXCEPTIONS
        and (
            relative in SENSITIVE_FILES
            or any(relative.startswith(prefix) for prefix in SENSITIVE_PATHS)
        )
    ]
    if exposed:
        raise ValueError(
            "embargoed files are publishable under current ignore rules: "
            + ", ".join(exposed)
        )

    findings = []
    for relative in files:
        path = ROOT / relative
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, original in enumerate(text.splitlines(), start=1):
            line = original
            for value in PUBLIC_NUMERIC_CONSTANTS:
                line = line.replace(value, "")
            if MONETARY_RESULT.search(line):
                findings.append(
                    f"{relative}:{line_number}: possible monetary result"
                )
            if POPULATION_RESULT.search(line):
                findings.append(
                    f"{relative}:{line_number}: possible population result"
                )
            if path.suffix.lower() == ".json" and result_json_value(line):
                findings.append(
                    f"{relative}:{line_number}: numerical result field"
                )
    if findings:
        raise ValueError(
            "possible numerical result outside embargo:\n"
            + "\n".join(findings)
        )

    print(f"PASS numerical embargo: {len(files)} publishable files checked")


if __name__ == "__main__":
    main()
