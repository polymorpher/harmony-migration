import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPT = (
    ROOT
    / "toolkit"
    / "scripts"
    / "claims"
    / "build-pre-wone-archive-manifest.py"
)


class PreWoneArchiveManifestTest(unittest.TestCase):
    def test_build_check_and_detect_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "pre-wone"
            claims = archive / "claims"
            claims.mkdir(parents=True)
            captured = claims / "example.csv"
            captured.write_text("value\n1\n")
            snapshot = archive / "snapshot-2026-09-10.json"
            snapshot.write_text("{}\n")
            canonical = root / "canonical.csv"
            canonical.write_text("value\n2\n")
            manifest = root / "manifest.json"
            build = (
                sys.executable,
                str(SCRIPT),
                "--archive-root",
                str(archive),
                "--output",
                str(manifest),
                "--external",
                (
                    "artifacts/cutoff-20260910/claims/source.csv="
                    + str(canonical)
                ),
            )
            subprocess.run(build, check=True, capture_output=True, text=True)
            result = json.loads(manifest.read_text())
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(result["entries"]), 2)

            check = (
                sys.executable,
                str(SCRIPT),
                "--archive-root",
                str(archive),
                "--output",
                str(manifest),
                "--check",
            )
            subprocess.run(check, check=True, capture_output=True, text=True)
            captured.write_text("value\nchanged\n")
            failed = subprocess.run(
                check,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(
                "manifest entries do not match",
                failed.stderr,
            )


if __name__ == "__main__":
    unittest.main()
