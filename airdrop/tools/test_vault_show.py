#!/usr/bin/env python3
"""vault-batch show: deposits that repeat a credit delivered before, or go to a delegator already holding one, fail.

    python3 tools/test_vault_show.py
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import safe_batch as sb  # noqa: E402
import vault_batch as vb  # noqa: E402

ONE = 10**18
SAFE = common.to_checksum("0x" + "5a" * 20)
TOKEN = common.to_checksum("0x" + "70" * 20)
VALIDATOR = common.to_checksum("0x" + "11" * 20)
ALICE, BOB, CAROL = (common.to_checksum("0x" + c * 20) for c in ("aa", "bb", "cc"))
ARTIFACT = vb.load_artifact()
VAULT = vb.vault_address(ARTIFACT, TOKEN, VALIDATOR, VALIDATOR)
MULTISEND = sb.MULTISEND_CALL_ONLY["1.4.1"][0]


def deploy() -> tuple[str, bytes]:
    return vb.FACTORY, vb.deploy_data(ARTIFACT, TOKEN, VALIDATOR, VALIDATOR)


def fund(credits: list[tuple[str, int]]) -> list[tuple[str, bytes]]:
    amounts = [a for _, a in credits]
    return [(TOKEN, vb.approve_data(VAULT, sum(amounts))),
            (VAULT, vb.deposit_batch_data(amounts, [d for d, _ in credits]))]


def record(nonce: int, calls: list[tuple[str, bytes]], executed: bool) -> dict:
    """A Safe Transaction Service record of these calls, with its real Safe transaction hash."""
    tx = vb.safe_tx(calls, MULTISEND, nonce)
    return {"safe": SAFE, "to": tx.to, "value": "0", "data": common.hex0x(tx.data), "operation": tx.operation,
            "nonce": nonce, "safeTxGas": 0, "baseGas": 0, "gasPrice": "0", "gasToken": sb.ZERO_ADDRESS,
            "refundReceiver": sb.ZERO_ADDRESS, "isExecuted": executed, "isSuccessful": True if executed else None,
            "confirmations": [], "confirmationsRequired": 2, "safeTxHash": sb.tx_record(1, SAFE, tx)["safeTxHash"]}


class ShowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.full_snapshot = None

    def tearDown(self):
        self.tmp.cleanup()

    def write_full_snapshot(self, rows: list[tuple[str, int, int, str]]) -> None:
        """(address, self stake, delegated, exchange) rows of a snapshot-20260911-full.csv."""
        self.full_snapshot = self.dir / "snapshot-full.csv"
        with self.full_snapshot.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["one1_address", "eth_address", "self_stake_atto", "delegated_atto", "exchange", "migration_stage"])
            for address, self_stake, delegated, exchange in rows:
                writer.writerow([common.to_one1(address), address, self_stake, delegated, exchange,
                                 "exchange_manual" if exchange else "deferred"])

    def run_show(self, records: list[dict], nonce: str | None, credits: dict[str, int] | None = None,
                 governor: str = VALIDATOR, ledger: bool = False):
        """Exit code, stderr and the details rows; credits stands in for each delegator's balance in the vault on-chain,
        and the vault counts as deployed on-chain when it is given. governor is the vault's governor now; ledger puts
        the vault in the deployment ledger."""
        path = self.dir / "queue.json"
        path.write_text(json.dumps(records))
        args = SimpleNamespace(env=self.dir / "none.env", safe=SAFE, token=TOKEN, chain_id=1,
                               nonce=[nonce] if nonce else None, safe_tx_hash=None,
                               rpc_url="http://ethereum.invalid" if credits is not None else None, harmony_rpc=None,
                               compare=None, amount_unit=None, governors=None, governor=None,
                               out=self.dir / "queued.csv", format=None, service_url=None, api_key=None, tx_json=path,
                               full_snapshot=self.full_snapshot)
        states = {VAULT.lower(): {"deployed": True, "code_ok": True, "asset_ok": True, "governor": governor, "fee": 0}}
        deployments = {VAULT.lower(): {"validator_address": VALIDATOR, "governor_address": VALIDATOR}} if ledger else {}

        def balances(rpc, calls, size=20):
            out = []
            for _, (call, _) in calls:
                holder = "0x" + call["data"][-40:]
                out.append(common.hex0x(vb.word((credits or {}).get(common.to_checksum(holder), 0))))
            return out

        stderr = io.StringIO()
        code = 0
        with mock.patch.object(vb, "vault_states", lambda rpc, artifact, token, addresses: states), \
                mock.patch.object(vb, "earlier_deployments", lambda ledger_dir, chain_id: deployments), \
                mock.patch.object(vb, "rpc_batch", balances), contextlib.redirect_stderr(stderr), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                vb.cmd_show(args)
            except SystemExit as exc:
                code = exc.code
        details = self.dir / "queued-details.csv"
        rows = []
        if details.exists():
            with details.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
        return code, stderr.getvalue(), rows

    def test_first_deliveries_pass(self):
        records = [record(0, [deploy(), *fund([(CAROL, 5 * ONE)])], True),
                   record(1, fund([(ALICE, 2 * ONE), (BOB, 3 * ONE)]), False)]
        code, err, rows = self.run_show(records, "1", credits={})
        self.assertEqual(code, 0, err)
        self.assertIn("delivered before : 0 of 2 deposits repeat a credit", err)
        self.assertIn("(read 1 that credited vaults: nonces 0)", err)
        self.assertIn("credits on-chain : 0 of the 2 queued deposits", err)
        self.assertEqual([r["credited_before"] for r in rows], ["", ""])
        self.assertEqual([r["credit_onchain_atto"] for r in rows], ["0", "0"])

    def test_a_credit_delivered_before_fails_even_after_a_withdrawal(self):
        records = [record(0, [deploy(), *fund([(ALICE, 5 * ONE)])], True),
                   record(1, fund([(ALICE, 5 * ONE), (BOB, 3 * ONE)]), False)]
        code, err, rows = self.run_show(records, "1", credits={})  # ALICE has withdrawn: nothing on-chain now
        self.assertEqual(code, 1)
        self.assertIn(f"{ALICE.lower()} was already credited 5 ONE in vault {VAULT.lower()} by executed nonce 0", err)
        self.assertIn("delivered before : 1 of 2 deposits repeat a credit", err)
        self.assertEqual([r["credited_before"] for r in rows], ["nonce 0: 5 ONE", ""])

    def test_a_delegator_already_holding_a_credit_fails(self):
        records = [record(0, [deploy()], True), record(1, fund([(ALICE, 2 * ONE)]), False)]
        code, err, rows = self.run_show(records, "1", credits={ALICE: 7 * ONE})  # deposited by someone else
        self.assertEqual(code, 1)
        self.assertIn(f"{ALICE.lower()} already holds 7 ONE in vault {VAULT.lower()}; this deposit adds to it", err)
        self.assertIn("delivered before : 0 of 1 deposits repeat a credit", err)
        self.assertEqual(rows[0]["credit_onchain_atto"], str(7 * ONE))

    def test_without_an_rpc_the_history_is_still_checked(self):
        records = [record(0, [deploy(), *fund([(ALICE, 5 * ONE)])], True), record(1, fund([(ALICE, 5 * ONE)]), False)]
        code, err, _ = self.run_show(records, None)  # both listed: the vault is deployed by these transactions
        self.assertEqual(code, 1)
        self.assertIn("is credited twice in vault", err)
        self.assertNotIn("already credited", err)  # the repeat is inside the listed transactions, reported once
        self.assertIn("(read 0 that credited vaults)", err)

    def test_a_vault_whose_governor_was_handed_on_is_found_through_the_deployment_ledger(self):
        records = [record(0, [deploy()], True), record(1, fund([(ALICE, 2 * ONE)]), False)]
        code, err, _ = self.run_show(records, "1", credits={}, governor=CAROL)
        self.assertEqual(code, 1)  # neither the list nor the governor names the validator
        self.assertIn("is not the vault of any validator", err)
        code, err, _ = self.run_show(records, "1", credits={}, governor=CAROL, ledger=True)
        self.assertEqual(code, 0, err)
        self.assertIn(f"deployed with governor {VALIDATOR}, who has handed the role on to {CAROL}", err)

    def test_credits_beyond_the_stake_at_the_cutoff_fail(self):
        records = [record(0, [deploy(), *fund([(ALICE, 5 * ONE)])], True),
                   record(1, fund([(BOB, 3 * ONE), (CAROL, 4 * ONE)]), False)]
        # ALICE's earlier credit counts towards her stake too, but she is not in the transaction reviewed
        self.write_full_snapshot([(ALICE, 0, 5 * ONE, ""), (BOB, ONE, 2 * ONE, ""), (CAROL, 0, 10 * ONE, "")])
        code, err, _ = self.run_show(records, "1", credits={})
        self.assertEqual(code, 0, err)
        self.assertIn("1 of 2 delegators would hold exactly their stake at the cutoff", err)
        self.assertIn("1 less", err)
        self.write_full_snapshot([(BOB, 0, 2 * ONE, ""), (CAROL, 0, 4 * ONE, "okx")])
        code, err, _ = self.run_show(records, "1", credits={})
        self.assertEqual(code, 1)
        self.assertIn(f"{BOB.lower()} would hold 3 ONE of vault credits in total (0 from earlier executed Safe "
                      "transactions), more than the 2 it staked at the cutoff", err)
        self.assertIn(f"{CAROL.lower()} is an exchange wallet in snapshot-full.csv (okx)", err)

    def test_earlier_credits_count_towards_the_stake(self):
        records = [record(0, [deploy(), *fund([(ALICE, 5 * ONE)])], True), record(1, fund([(ALICE, 5 * ONE)]), False)]
        self.write_full_snapshot([(ALICE, 0, 10 * ONE, "")])
        code, err, _ = self.run_show(records, "1", credits={})
        self.assertEqual(code, 1)  # a repeat of the earlier credit, though within the stake
        self.assertIn("was already credited 5 ONE", err)
        self.assertIn("1 of 1 delegators would hold exactly their stake", err)

    def test_a_failed_execution_is_not_a_delivery(self):
        failed = record(0, [deploy(), *fund([(ALICE, 5 * ONE)])], True)
        failed["isSuccessful"] = False
        records = [failed, record(1, fund([(ALICE, 5 * ONE)]), False)]
        code, err, _ = self.run_show(records, "1", credits={})
        self.assertEqual(code, 0, err)
        self.assertIn("executed nonce 0 failed on-chain; its deposits are not counted as delivered", err)


if __name__ == "__main__":
    unittest.main()
