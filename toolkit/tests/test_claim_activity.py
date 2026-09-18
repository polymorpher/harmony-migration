import csv
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[2]
ENRICH = (
    ROOT
    / "toolkit"
    / "scripts"
    / "claims"
    / "enrich-claim-activity.py"
)
SUMMARIZE = (
    ROOT
    / "toolkit"
    / "scripts"
    / "claims"
    / "summarize-claim-activity.py"
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaimActivityTest(unittest.TestCase):
    def setUp(self):
        self.enrich = load_module("claim_activity_enrich", ENRICH)
        self.summarize = load_module(
            "claim_activity_summarize", SUMMARIZE
        )

    def test_merge_selects_latest_shard_activity(self):
        empty = {
            field: "" for field in self.enrich.ACTIVITY_FIELDS
        }
        older = dict(
            empty,
            last_activity_time_utc="2026-01-01T00:00:00Z",
            last_activity_timestamp_unix="1767225600",
            last_activity_block="10",
            last_activity_shard="0",
            last_activity_type="regular",
            last_activity_tx_hash="0x" + "11" * 32,
            last_activity_index="1",
            last_activity_detail="recipient",
        )
        newer = dict(
            older,
            last_activity_time_utc="2026-02-01T00:00:00Z",
            last_activity_timestamp_unix="1769904000",
            last_activity_block="20",
            last_activity_shard="1",
            last_activity_tx_hash="0x" + "22" * 32,
        )
        self.assertEqual(
            self.enrich.select_activity((older, newer)),
            newer,
        )
        self.assertEqual(
            self.enrich.select_activity((empty, empty)),
            empty,
        )

    def test_calendar_months_clamp_month_end(self):
        cutoff = datetime(2024, 3, 31, 14, tzinfo=timezone.utc)
        self.assertEqual(
            self.summarize.subtract_calendar_months(cutoff, 1),
            datetime(2024, 2, 29, 14, tzinfo=timezone.utc),
        )

    def test_activity_summary_must_identify_candidate_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            path.write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "output_sha256": "expected",
                        "candidates": 2,
                        "activity_found": 1,
                        "activity_not_found": 1,
                    }
                )
            )
            result = self.summarize.load_activity_summary(
                path, "expected"
            )
            self.assertEqual(result["candidates"], 2)
            with self.assertRaisesRegex(
                ValueError, "does not identify"
            ):
                self.summarize.load_activity_summary(path, "other")

    def test_cumulative_activity_windows(self):
        cutoff = datetime(2026, 9, 10, 14, tzinfo=timezone.utc)
        times = (
            datetime(2026, 7, 10, 14, tzinfo=timezone.utc),
            datetime(2026, 1, 10, 14, tzinfo=timezone.utc),
            datetime(2020, 1, 10, 14, tzinfo=timezone.utc),
            None,
        )
        amounts = (1000, 2000, 3000, 4000)
        rows = []
        for index, (activity, amount) in enumerate(
            zip(times, amounts), start=1
        ):
            address = f"0x{index:040x}"
            allocation = 900 if index == 1 else amount
            secure_key = (
                "0x"
                + self.summarize.lib.keccak256(
                    bytes.fromhex(address[2:])
                ).hex()
            )
            timestamp = int(activity.timestamp()) if activity else ""
            rows.append(
                {
                    "secure_key": secure_key,
                    "address": address,
                    "wallet_airdrop_atto": str(
                        allocation * 10**18
                    ),
                    "staked_to_vault_atto": "0",
                    "qualification_total_atto": str(amount * 10**18),
                    "total_claim_atto": str(allocation * 10**18),
                    "last_activity_time_utc": (
                        self.summarize.format_utc(activity)
                        if activity
                        else ""
                    ),
                    "last_activity_timestamp_unix": str(timestamp),
                    "last_activity_block": "1" if activity else "",
                    "last_activity_shard": "0" if activity else "",
                    "last_activity_type": (
                        "regular" if activity else ""
                    ),
                    "last_activity_tx_hash": (
                        "0x" + f"{index:064x}" if activity else ""
                    ),
                    "last_activity_index": (
                        str(index) if activity else ""
                    ),
                    "last_activity_detail": (
                        "recipient" if activity else ""
                    ),
                }
            )
        rows.sort(key=lambda row: row["secure_key"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.csv"
            with path.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=rows[0],
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(rows)
            result = self.summarize.summarize(
                path, cutoff, (3, 6, 12, 24, 36, 48)
            )
        self.assertEqual(
            [entry["accounts"] for entry in result["windows"]],
            [1, 1, 2, 2, 2, 2],
        )
        self.assertEqual(
            result["windows"][2]["total_claim_atto"],
            str(2900 * 10**18),
        )
        self.assertEqual(
            result["before_longest_window"]["accounts"], 1
        )
        self.assertEqual(
            result["indexed_activity_not_found"]["accounts"], 1
        )
        self.assertEqual(result["exact_threshold_rows"], 1)


if __name__ == "__main__":
    unittest.main()
