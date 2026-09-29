import copy
import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CUTOFF_DIR = Path(__file__).parents[1] / "scripts" / "cutoff"
sys.path.insert(0, str(CUTOFF_DIR))
import receipt_provenance as provenance  # noqa: E402

CLI = CUTOFF_DIR / "verify-receipt-provenance.py"
AMOUNTS_TOOL = Path(__file__).parents[2] / "airdrop" / "tools" / "verify_cutoff_amounts.py"
CUTOFFS = {0: 1_000_000, 1: 1_000_000}
FIRST = provenance.MAINNET_CROSS_TX_FIRST_BLOCK
RECIPIENT = "0x00000000000000000000000000000000000000aa"
OUTGOING_FIELDS = [
    "source_shard", "destination_shard", "source_block", "source_block_hash",
    "receipt_index", "tx_hash", "from", "to", "amount_atto",
]


def receipt(tx, to=RECIPIENT, amount=5):
    return {"transaction_hash": tx, "to": to, "amount_atto": str(amount)}


def direction(source, spent=(0, 0), pending=(), unsupported=()):
    def groups(items):
        return [{"block_number": block, "receipts": [receipt(tx, amount=amount)]} for block, tx, amount in items]

    return {
        "source_shard": source,
        "spent_receipt_count": spent[0],
        "spent_amount_atto": str(spent[1]),
        "pending_receipt_count": len(pending),
        "pending_amount_atto": str(sum(amount for _, _, amount in pending)),
        "unsupported_receipt_count": len(unsupported),
        "unsupported_amount_atto": str(sum(amount for _, _, amount in unsupported)),
        "source_coverage": {
            "complete": True, "from_block": FIRST, "to_block": CUTOFFS[source],
            "blocks_missing": 0, "blocks_expected": CUTOFFS[source] - FIRST + 1,
        },
        "pending_groups": groups(pending),
        "unsupported_groups": groups(unsupported),
    }


def complete_report():
    history = {"complete": True, "snapdb_marker": False, "missing_probes": 0, "probes": 1000}
    return {
        "shard0_cutoff": CUTOFFS[0],
        "shard1_cutoff": CUTOFFS[1],
        "coverage_complete": True,
        "incomplete_history_allowed": False,
        "coverage_problems": [],
        "databases": {"shard0": {"kind": "chain", "history": history}, "shard1": {"kind": "chain", "history": history}},
        "directions": [
            direction(0, spent=(2, 30), pending=[(900_000, "0x0b", 7)], unsupported=[(800_000, "0x0c", 11)]),
            direction(1, spent=(1, 4)),
        ],
    }


def outgoing_rows():
    rows = [
        (0, 1, 850_000, "0x01", 10), (0, 1, 860_000, "0x02", 20), (0, 1, 900_000, "0x0b", 7),
        (0, 2, 800_000, "0x0c", 11), (0, 1, 1_000_001, "0x0d", 99), (1, 0, 870_000, "0x03", 4),
    ]
    return [
        {"source": s, "destination": d, "block": b, "tx": tx, "to": RECIPIENT, "amount": a}
        for s, d, b, tx, a in rows
    ]


def write_outgoing(path, rows):
    with open(path, "w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(OUTGOING_FIELDS)
        for row in rows:
            writer.writerow([row["source"], row["destination"], row["block"], "0x00", 0, row["tx"],
                             "0x00", row["to"].upper().replace("0X", "0x"), row["amount"]])


class ProvenanceTest(unittest.TestCase):
    def test_complete_report_has_no_problems(self):
        self.assertEqual(provenance.provenance_problems(complete_report(), CUTOFFS), [])

    def test_report_without_coverage_fields_is_rejected(self):
        legacy = {"directions": complete_report()["directions"], "pending_active_atto": "7"}
        problems = provenance.provenance_problems(legacy, CUTOFFS)
        self.assertEqual(len(problems), 1)
        self.assertIn("predates coverage recording", problems[0])

    def test_incomplete_evidence_is_rejected(self):
        cases = {
            "coverage_complete=false": lambda r: r.update(coverage_complete=False),
            "-allow-incomplete-history": lambda r: r.update(incomplete_history_allowed=True),
            "lacks block history": lambda r: r["databases"]["shard0"]["history"].update(complete=False),
            "lacks outgoing receipt groups": lambda r: r["directions"][0]["source_coverage"].update(complete=False),
            "not at the cutoff": lambda r: r["directions"][1]["source_coverage"].update(to_block=5),
            "after the first cross-shard block": lambda r: r["directions"][0]["source_coverage"].update(from_block=FIRST + 1),
            "not both 0 and 1": lambda r: r["directions"].pop(),
            "lookup snapshot ends before the cutoff": lambda r: r["databases"]["shard1"].update(
                kind="cx-lookup-snapshot", lookup_snapshot_cutoff=5),
        }
        for expected, mutate in cases.items():
            report = complete_report()
            mutate(report)
            problems = provenance.provenance_problems(report, CUTOFFS)
            self.assertTrue(any(expected in problem for problem in problems), (expected, problems))


class ComparisonTest(unittest.TestCase):
    def test_matching_list_passes(self):
        problems, stats, unchecked = provenance.comparison(complete_report(), outgoing_rows(), CUTOFFS)
        self.assertEqual(problems, [])
        self.assertEqual(unchecked, [])
        self.assertEqual(stats[0]["independent"], stats[0]["report"])

    def test_receipts_the_scan_never_saw_fail(self):
        report = complete_report()
        report["directions"][0] = direction(0, spent=(1, 10))
        problems, _, _ = provenance.comparison(report, outgoing_rows(), CUTOFFS)
        self.assertTrue(any("active receipts: independent list 3, report 1" in p for p in problems), problems)
        self.assertTrue(any("retired receipts: independent list 1, report 0" in p for p in problems), problems)

    def test_listed_receipt_must_match(self):
        report = complete_report()
        report["directions"][0]["pending_groups"][0]["receipts"][0]["amount_atto"] = "8"
        problems, _, _ = provenance.comparison(report, outgoing_rows(), CUTOFFS)
        self.assertTrue(any("pending receipt 0x0b" in p for p in problems), problems)

    def test_missing_list_is_reported(self):
        rows = [row for row in outgoing_rows() if row["source"] == 0]
        _, _, unchecked = provenance.comparison(complete_report(), rows, CUTOFFS)
        self.assertEqual(unchecked, [1])


class IndependentReportTest(unittest.TestCase):
    def legacy(self):
        report = complete_report()
        for key in ("coverage_complete", "incomplete_history_allowed", "coverage_problems", "databases"):
            del report[key]
        return report

    def test_legacy_report_passes_when_independent_report_agrees(self):
        rows = [row for row in outgoing_rows() if row["source"] == 0]
        problems, _, agreements = provenance.evaluate(self.legacy(), rows, [self.legacy()], CUTOFFS)
        self.assertEqual((problems, agreements), ([], [[]]))

    def test_disagreeing_independent_report_does_not_waive_provenance(self):
        other = self.legacy()
        other["directions"][0]["pending_groups"][0]["receipts"][0]["amount_atto"] = "8"
        problems, _, _ = provenance.evaluate(self.legacy(), outgoing_rows(), [other], CUTOFFS)
        self.assertIn(provenance.LEGACY_REPORT, problems)
        self.assertTrue(any("pending receipts differ" in p for p in problems), problems)

    def test_agreement_does_not_waive_a_report_marked_incomplete(self):
        report = complete_report()
        report["coverage_complete"] = False
        problems, _, _ = provenance.evaluate(report, outgoing_rows(), [copy.deepcopy(report)], CUTOFFS)
        self.assertIn("the report is marked coverage_complete=false", problems)

    def test_shard_without_list_needs_an_agreeing_report(self):
        rows = [row for row in outgoing_rows() if row["source"] == 0]
        problems, _, _ = provenance.evaluate(complete_report(), rows, [], CUTOFFS)
        self.assertTrue(any("source shard 1" in p for p in problems), problems)


class CommandTest(unittest.TestCase):
    def run_cli(self, report, rows):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report.json").write_text(json.dumps(report))
            write_outgoing(root / "outgoing.csv", rows)
            completed = subprocess.run(
                [sys.executable, str(CLI), "--receipts", str(root / "report.json"),
                 "--outgoing", str(root / "outgoing.csv"),
                 "--shard0-cutoff", str(CUTOFFS[0]), "--shard1-cutoff", str(CUTOFFS[1]),
                 "--output", str(root / "verify.json")],
                capture_output=True, text=True,
            )
            return completed.returncode, json.loads((root / "verify.json").read_text())

    def test_passes_complete_matching_report(self):
        code, result = self.run_cli(complete_report(), outgoing_rows())
        self.assertEqual((code, result["status"], result["problems"]), (0, "passed", []))

    def test_fails_report_that_missed_receipts(self):
        report = complete_report()
        report["directions"][0] = direction(0, spent=(1, 10))
        code, result = self.run_cli(report, outgoing_rows())
        self.assertEqual((code, result["status"]), (1, "failed"))

    def test_fails_legacy_report_even_when_counts_match(self):
        legacy = copy.deepcopy(complete_report())
        del legacy["coverage_complete"]
        code, result = self.run_cli(legacy, outgoing_rows())
        self.assertEqual(code, 1)
        self.assertIn("predates coverage recording", result["problems"][0])


class ChainOnlyVerifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("verify_cutoff_amounts", AMOUNTS_TOOL)
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def write_report(self, directory, report):
        path = Path(directory) / "report.json"
        path.write_text(json.dumps(report))
        return str(path)

    def test_reads_pending_per_recipient_from_complete_report(self):
        report = complete_report()
        report["shard0_cutoff"], report["shard1_cutoff"] = (self.tool.CUTOFF[0][0], self.tool.CUTOFF[1][0])
        with tempfile.TemporaryDirectory() as directory:
            pending, problems = self.tool.receipt_report_pending(self.write_report(directory, report))
        self.assertEqual((pending, problems), ({RECIPIENT: 7}, []))

    def test_flags_report_without_coverage(self):
        report = complete_report()
        del report["coverage_complete"]
        with tempfile.TemporaryDirectory() as directory:
            _, problems = self.tool.receipt_report_pending(self.write_report(directory, report))
        self.assertTrue(any("complete history" in problem for problem in problems), problems)

    def test_trusted_inputs_are_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.csv"
            evidence.write_text("address,deduction_atto,pending_cross_shard_atto\n")
            rows = [{"deduction_atto": "3", "pending_cross_shard_atto": "7"}, {"deduction_atto": "0"}]
            trusted = self.tool.trusted_inputs(str(evidence), rows, None, [], [])
        self.assertEqual(trusted["deduction_atto"]["total_atto"], "3")
        self.assertFalse(trusted["deduction_atto"]["verified_on_chain"])
        self.assertEqual(trusted["pending_cross_shard_atto"]["rows"], 1)
        self.assertFalse(trusted["pending_cross_shard_atto"]["verified"])


if __name__ == "__main__":
    unittest.main()
