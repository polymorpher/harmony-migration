import csv
import hashlib
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
    / "extend-account-activity.py"
)
FIELDS = (
    "secure_key",
    "address",
    "last_activity_time_utc",
    "last_activity_timestamp_unix",
    "last_activity_block",
    "last_activity_shard",
    "last_activity_type",
    "last_activity_tx_hash",
    "last_activity_index",
    "last_activity_detail",
)


def write_csv(path, rows, fields=FIELDS):
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=fields,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


class ExtendAccountActivityTest(unittest.TestCase):
    def test_records_exact_hybrid_breakdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = root / "candidates.csv"
            existing = root / "existing.csv"
            incremental = root / "incremental.csv"
            existing_summary = root / "existing.json"
            incremental_summary = root / "incremental.json"
            output = root / "output.csv"
            summary = root / "summary.json"
            candidate_rows = [
                {"secure_key": "0x01", "address": "0x" + "01" * 20},
                {"secure_key": "0x02", "address": "0x" + "02" * 20},
                {"secure_key": "0x03", "address": "0x" + "03" * 20},
            ]
            write_csv(
                candidates,
                candidate_rows,
                fields=("secure_key", "address"),
            )

            def activity(row, present):
                return {
                    **row,
                    "last_activity_time_utc": (
                        "2026-01-01T00:00:00Z" if present else ""
                    ),
                    "last_activity_timestamp_unix": (
                        "1767225600" if present else ""
                    ),
                    "last_activity_block": "1" if present else "",
                    "last_activity_shard": "0" if present else "",
                    "last_activity_type": "regular" if present else "",
                    "last_activity_tx_hash": (
                        "0x" + "11" * 32 if present else ""
                    ),
                    "last_activity_index": "0" if present else "",
                    "last_activity_detail": "sender" if present else "",
                }

            write_csv(
                existing,
                [
                    activity(candidate_rows[0], True),
                    activity(candidate_rows[1], False),
                ],
            )
            write_csv(
                incremental,
                [activity(candidate_rows[2], True)],
            )
            existing_hash = hashlib.sha256(existing.read_bytes()).hexdigest()
            incremental_hash = hashlib.sha256(
                incremental.read_bytes()
            ).hexdigest()
            common = {
                "status": "passed",
                "shard": 0,
                "cutoff_block": 10,
                "cutoff_hash": "0xabc",
                "transaction_index_tail": None,
            }
            existing_summary.write_text(
                json.dumps(
                    {
                        **common,
                        "source_kind": (
                            "local explorer-node address index and "
                            "canonical headers"
                        ),
                        "db_path": "fixture://harmony-db-shard0",
                        "explorer_db_path": "fixture://explorer-db-shard0",
                        "candidates": 2,
                        "activity_found": 1,
                        "activity_not_found": 1,
                        "output_sha256": existing_hash,
                    }
                )
            )
            incremental_summary.write_text(
                json.dumps(
                    {
                        **common,
                        "source_kind": "archival RPC",
                        "rpc": "http://archive",
                        "candidates": 1,
                        "activity_found": 1,
                        "activity_not_found": 0,
                        "output_sha256": incremental_hash,
                    }
                )
            )
            subprocess.run(
                (
                    sys.executable,
                    str(SCRIPT),
                    "merge",
                    "--candidates",
                    str(candidates),
                    "--existing-activity",
                    str(existing),
                    "--existing-summary",
                    str(existing_summary),
                    "--incremental-activity",
                    str(incremental),
                    "--incremental-summary",
                    str(incremental_summary),
                    "--output",
                    str(output),
                    "--summary",
                    str(summary),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(summary.read_text())
            self.assertEqual(result["classification"], "hybrid")
            self.assertEqual(
                result["provenance_breakdown"]["database-derived"]["rows"],
                2,
            )
            self.assertEqual(
                result["provenance_breakdown"]["RPC-derived"]["rows"],
                1,
            )
            self.assertEqual(
                len(
                    result["provenance_breakdown"]["database-derived"][
                        "sources"
                    ]
                ),
                1,
            )
            self.assertEqual(
                len(
                    result["provenance_breakdown"]["RPC-derived"][
                        "sources"
                    ]
                ),
                1,
            )
            self.assertIn(
                "2 database-derived rows plus 1 RPC-derived row",
                result["source_kind"],
            )


if __name__ == "__main__":
    unittest.main()
