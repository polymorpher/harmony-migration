import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
BRIDGE = ROOT / "toolkit" / "scripts" / "bridge"
sys.path.insert(0, str(BRIDGE))
import bridge_lib as lib  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BRIDGE / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLAN = load_script("build-snapshot-plan.py")
SCAN = load_script("scan-bridge-messages.py")

HARMONY_PROXY = "0x" + "a1" * 20
LEGACY_PROXY = "0x" + "a2" * 20
HARMONY_TOKEN = "0x" + "b1" * 20
LEGACY_TOKEN = "0x" + "b2" * 20
ESCROW = "0x" + "c1" * 20
REMOTE_TOKEN = "0x" + "d1" * 20
ONE_LOCK = "0x" + "e1" * 20
ONE_ADAPTER = "0x" + "e2" * 20
ONE_REMOTE = "0x" + "e3" * 20
CLAIM_TOKEN = "0x" + "f1" * 20
RESERVE = "0x" + "f2" * 20
CUSTODY = "0x" + "f3" * 20
ALICE = "0x" + "01" * 20
BOB = "0x" + "02" * 20
CAROL = "0x" + "03" * 20
POOL_CONTRACT = "0x" + "04" * 20
SAFE = "0x" + "05" * 20
DAVE = "0x" + "06" * 20


def read_rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def nonces(h_out, h_in, r_in, r_out):
    return h_out, h_in, r_in, r_out


def harmony_entry(proxy, token, h_out, h_in, extra):
    labels = {}
    for label in ("rollback", "cutoff"):
        values = {
            "outbound_nonce": h_out[label],
            "inbound_nonce": h_in[label],
            "inbound_from_previous": {},
        }
        values.update(extra)
        labels[label] = values
    return {"proxy": proxy, "token": token, "by_label": labels}


def remote_entry(escrow, r_in, r_out, owner=CUSTODY):
    return {
        "endpoint": "0x" + "99" * 20,
        "owner": owner,
        "owner_facts": {"is_safe": True},
        "previous_adapter_owners": {},
        "by_label": {
            label: {
                "escrow_balance": str(escrow),
                "inbound_nonce": r_in[label],
                "outbound_nonce": r_out[label],
                "previous_adapter_balances": {},
                "previous_adapter_nonces": {},
            }
            for label in ("rollback", "cutoff", "state")
        },
    }


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.holders = self.root / "holders"
        self.holders.mkdir()
        self.out = self.root / "out"
        self.routes = self.root / "routes.json"
        self.config = self.root / "config.json"
        self.state = self.root / "state.json"
        self.plan = self.root / "plan.json"
        self.messages = self.root / "messages.csv"
        self.messages_summary = self.root / "messages-summary.json"
        self.classes = self.root / "classes.csv"

    def write(self, escrow=1_000, one_lock=10_000):
        routes = {
            "schema_version": 1,
            "harmony": {
                "chain": "harmony",
                "layerzero_chain_id": 116,
                "endpoint": "0x" + "98" * 20,
                "wone": "0x" + "97" * 20,
            },
            "chains": {"ethereum": {"chain_id": 1, "layerzero_chain_id": 101}},
            "system_addresses": [
                {"chain": "*", "address": "0x000000000000000000000000000000000000dead", "role": "burn"}
            ],
            "pools": [
                {
                    "pool_id": "ethereum-usdc",
                    "kind": "remote_escrow",
                    "claim_asset": "escrow",
                    "chain": "ethereum",
                    "symbol": "USDC",
                    "decimals": 6,
                    "remote_adapter": ESCROW,
                    "remote_token": REMOTE_TOKEN,
                    "previous_remote_adapters": [],
                    "harmony": [
                        {"proxy": LEGACY_PROXY, "token": LEGACY_TOKEN},
                        {"proxy": HARMONY_PROXY, "token": HARMONY_TOKEN},
                    ],
                },
                {
                    "pool_id": "ethereum-one",
                    "kind": "harmony_origin",
                    "claim_asset": "one",
                    "chain": "ethereum",
                    "symbol": "ONE",
                    "decimals": 18,
                    "remote_adapter": ONE_ADAPTER,
                    "remote_token": ONE_REMOTE,
                    "harmony": [{"proxy": ONE_LOCK, "token": "native"}],
                },
            ],
        }
        self.routes.write_text(json.dumps(routes))
        config = {
            "schema_version": 1,
            "times": {"rollback": "2026-08-11T23:25:37Z", "cutoff": "2026-09-10T14:00:00Z"},
            "chains": {
                "harmony": {"rpc_env": "HARMONY_RPC", "chain_id": 1666600000},
                "ethereum": {"rpc_env": "ETHEREUM_RPC", "chain_id": 1, "custody_multisig": CUSTODY},
            },
            "custody_requirements": {"owner_count": 4, "threshold": 2},
            "one_claims": {
                "claim_chain": "ethereum",
                "claim_token": CLAIM_TOKEN,
                "reserve_safe": RESERVE,
                "routing_destination_id": "layerzero-nativeoft-custody",
            },
            "allocation_policy": {"minimum_claim_raw": "0"},
        }
        self.config.write_text(json.dumps(config))
        same = {"rollback": 5, "cutoff": 5}
        state = {
            "blocks": {
                "harmony": {
                    "rollback": {"number": 100, "hash": "0xh1"},
                    "cutoff": {"number": 200, "hash": "0xh2"},
                },
                "ethereum": {
                    "rollback": {"number": 1000, "hash": "0xe1"},
                    "cutoff": {"number": 2000, "hash": "0xe2"},
                    "state": {"number": 3000, "hash": "0xe3"},
                },
            },
            "pools": {
                "ethereum-usdc": {
                    "harmony": [
                        harmony_entry(LEGACY_PROXY, LEGACY_TOKEN, same, same, {"proxy_token_balance": "0"}),
                        harmony_entry(HARMONY_PROXY, HARMONY_TOKEN, same, same, {"proxy_token_balance": "0"}),
                    ],
                    "remote": remote_entry(
                        escrow,
                        {label: [5, 5] for label in ("rollback", "cutoff", "state")},
                        {"rollback": 10, "cutoff": 10, "state": 10},
                    ),
                },
                "ethereum-one": {
                    "harmony": [
                        harmony_entry(
                            ONE_LOCK, "native", {"rollback": 7, "cutoff": 7},
                            {"rollback": 3, "cutoff": 3},
                            {"native_balance": str(one_lock), "wone_balance": "0"},
                        )
                    ],
                    "remote": remote_entry(
                        0,
                        {"rollback": [6], "cutoff": [9], "state": [9]},
                        {"rollback": 3, "cutoff": 4, "state": 4},
                    ),
                },
            },
            "custody": {
                "ethereum": {
                    CUSTODY: {"is_safe": True, "meets_requirements": True},
                    RESERVE: {"claim_token_balance": str(10 ** 30)},
                }
            },
        }
        self.state.write_text(json.dumps(state))

    def holder_file(self, stem, block, block_hash, balances):
        rows = [{"address": a, "balance_raw": str(v), "balance": ""} for a, v in sorted(balances.items())]
        path = self.holders / f"{stem}.csv"
        write_csv(path, ("address", "balance_raw", "balance"), rows)
        (self.holders / f"{stem}.summary.json").write_text(json.dumps({
            "status": "passed",
            "block": block,
            "block_hash": block_hash,
            "total_supply_raw": str(sum(balances.values())),
            "output_sha256": lib.file_sha256(path),
        }))

    def run_plan(self):
        subprocess.run([
            sys.executable, BRIDGE / "build-snapshot-plan.py",
            "--routes", self.routes, "--config", self.config,
            "--state", self.state, "--output", self.plan,
        ], check=True, capture_output=True)
        return json.loads(self.plan.read_text())

    def write_inputs(self, plan, messages):
        usdc_block = plan["pools"]["ethereum-usdc"]["snapshot_block"]["number"]
        stems = {(item["chain"], item["token"]): item["stem"] for item in plan["holder_snapshots"]}
        self.holder_file(stems[("harmony", LEGACY_TOKEN)], usdc_block, "0xh2", {ALICE: 300, POOL_CONTRACT: 200})
        self.holder_file(
            stems[("harmony", HARMONY_TOKEN)], usdc_block, "0xh2",
            {ALICE: 100, BOB: 600, "0x000000000000000000000000000000000000dead": 50},
        )
        one_block = plan["pools"]["ethereum-one"]["snapshot_block"]
        self.holder_file(
            stems[("ethereum", ONE_REMOTE)], one_block["number"], one_block["hash"],
            {CAROL: 4_000, SAFE: 3_000, ONE_ADAPTER: 25},
        )
        write_csv(self.classes, ("chain", "address", "block", "class"), [
            {"chain": "harmony", "address": ALICE, "block": usdc_block, "class": "eoa"},
            {"chain": "harmony", "address": BOB, "block": usdc_block, "class": "eoa"},
            {"chain": "harmony", "address": POOL_CONTRACT, "block": usdc_block, "class": "contract"},
            {"chain": "ethereum", "address": CAROL, "block": one_block["number"], "class": "eoa"},
            {"chain": "ethereum", "address": SAFE, "block": one_block["number"], "class": "safe"},
        ])
        fields = ("pool_id", "category", "adjustment_account", "adjustment_raw", "amount_raw")
        write_csv(self.messages, fields, messages)
        self.messages_summary.write_text(json.dumps({
            "status": "passed",
            "output_sha256": lib.file_sha256(self.messages),
            "policy": {"orphaned_payouts": "debit_recipient"},
            "pools": {
                "ethereum-usdc": {"status": "complete", "categories": {}},
                "ethereum-one": {"status": "complete", "categories": {}},
            },
        }))

    def build(self, *extra):
        return subprocess.run([
            sys.executable, BRIDGE / "build-bridge-allocations.py",
            "--routes", self.routes, "--config", self.config, "--state", self.state,
            "--plan", self.plan, "--messages", self.messages,
            "--messages-summary", self.messages_summary,
            "--holders-dir", self.holders, "--classes", self.classes,
            "--output-dir", self.out, *extra,
        ], capture_output=True, text=True)

    def verify(self, name="verify.json"):
        return subprocess.run([
            sys.executable, BRIDGE / "verify-bridge-allocations.py",
            "--routes", self.routes, "--config", self.config, "--state", self.state,
            "--plan", self.plan, "--messages", self.messages,
            "--holders-dir", self.holders, "--allocations-dir", self.out,
            "--output", self.root / name,
        ], capture_output=True, text=True)

    def summarize(self, *extra, name="summary.md"):
        return subprocess.run([
            sys.executable, BRIDGE / "build-summary-report.py",
            "--routes", self.routes, "--plan", self.plan,
            "--messages-summary", self.messages_summary,
            "--allocations-dir", self.out,
            "--output", self.root / name, *extra,
        ], capture_output=True, text=True)


class BridgeLibTest(unittest.TestCase):
    def test_decodes_harmony_and_upstream_oft_payloads(self):
        harmony_payload = (
            "0x" + lib.word(0) + lib.word(128) + lib.word(192) + lib.word(98000)
            + lib.word(20) + ALICE[2:].ljust(64, "0")
            + lib.word(20) + BOB[2:].ljust(64, "0")
        )
        self.assertEqual(lib.decode_oft_payload(harmony_payload), (ALICE, BOB, 98000))
        upstream = "0x" + lib.word(0) + lib.word(96) + lib.word(7) + lib.word(20) + BOB[2:].ljust(64, "0")
        self.assertEqual(lib.decode_oft_payload(upstream), (None, BOB, 7))

    def test_inbound_nonce_call_packs_the_path(self):
        data = lib.inbound_nonce_data(116, ONE_LOCK, ONE_ADAPTER)
        self.assertTrue(data.startswith(lib.SEL["getInboundNonce"]))
        words = [data[10 + i:10 + i + 64] for i in range(0, len(data) - 10, 64)]
        self.assertEqual(int(words[0], 16), 116)
        self.assertEqual(int(words[1], 16), 64)
        self.assertEqual(int(words[2], 16), 40)
        self.assertEqual(words[3] + words[4][:16], ONE_LOCK[2:] + ONE_ADAPTER[2:])

    def test_reserved_addresses_have_no_key(self):
        self.assertIsNotNone(lib.reserved_address_reason("0x0000000000000000000000000000000000000001"))
        self.assertIsNotNone(lib.reserved_address_reason("0x0000000000000000000000000000000000001004"))
        self.assertIsNotNone(lib.reserved_address_reason("0x" + "e" * 40))
        self.assertIsNone(lib.reserved_address_reason("0x0000000000000000000000000000000000010000"))
        self.assertIsNone(lib.reserved_address_reason(ALICE))

    def test_migration_exclusions_follow_full_non_issuance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fields = ("address_hex", "category", "remaining_original_address_allocation_atto")
            write_csv(root / "ni.csv", fields, [
                {"address_hex": ALICE, "category": "reported_wallet_theft_perpetrator",
                 "remaining_original_address_allocation_atto": "0"},
                {"address_hex": BOB, "category": "blacklisted_extra_mint_recipient",
                 "remaining_original_address_allocation_atto": "5"},
            ])
            (root / "burn.json").write_text(json.dumps({
                "positive_inaccessible_or_precompile_addresses": [
                    {"address": "0x000000000000000000000000000000000000dEaD", "reason": "burn"}
                ]
            }))
            write_csv(root / "leak.csv", ("address_hex", "incident", "unbacked_credit_atto", "retained_cap_atto"), [
                {"address_hex": CAROL, "incident": "june-july-2026",
                 "unbacked_credit_atto": "10", "retained_cap_atto": "4"},
            ])
            result = subprocess.run([
                sys.executable, BRIDGE / "build-migration-exclusions.py",
                "--non-issuance", root / "ni.csv", "--burn-audit", root / "burn.json",
                "--revert-leak", root / "leak.csv",
                "--output", root / "exclusions.csv", "--summary", root / "summary.json",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            excluded = {row["address"] for row in read_rows(root / "exclusions.csv")}
            self.assertEqual(excluded, {ALICE, "0x000000000000000000000000000000000000dead"})
            summary = json.loads((root / "summary.json").read_text())
            self.assertIn(BOB, summary["partial_non_issuance"])
            self.assertEqual(summary["uncovered_deductions"][CAROL]["uncovered_atto"], "6")

    def test_known_selectors_and_topics(self):
        self.assertEqual(lib.SEL["getOutboundNonce"], "0x7a145748")
        self.assertEqual(lib.SEL["getInboundNonce"], "0xfdc07c70")
        self.assertEqual(
            lib.TOPIC["ReceiveFromChain"],
            "0x776434b505c7beb3db155c58df6c88985bf7c31730767e43ec773005059fed7a",
        )
        self.assertEqual(lib.format_units(1234567, 6), "1.234567")
        self.assertEqual(lib.format_units(-5, 2), "-0.05")


class MessageEventTest(unittest.TestCase):
    def test_native_oft_debit_counts_as_a_send(self):
        topics = SCAN.send_topics(116, ESCROW)
        self.assertEqual(topics["native_send"][2], "0x" + lib.address_word(ESCROW))
        log = {
            "topics": [lib.TOPIC["Transfer"], "0x" + lib.address_word(ALICE), "0x" + lib.address_word(ESCROW)],
            "data": "0x" + lib.word(60_000),
        }
        self.assertEqual(
            SCAN.decode_event(log, "native_send"),
            {"sender": ALICE, "recipient": "", "amount": 60_000},
        )
        self.assertTrue(SCAN.accept_native_send("native_send", log))
        mint = dict(log, topics=[lib.TOPIC["Transfer"], "0x" + lib.word(0), "0x" + lib.address_word(ESCROW)])
        self.assertFalse(SCAN.accept_native_send("native_send", mint))

    def test_receive_event_decodes_recipient_and_amount(self):
        log = {
            "topics": [lib.TOPIC["ReceiveFromChain"], "0x" + lib.word(116), "0x" + lib.address_word(BOB)],
            "data": "0x" + lib.word(64) + lib.word(500) + lib.word(20) + ALICE[2:].ljust(64, "0"),
        }
        self.assertEqual(
            SCAN.decode_event(log, "receive"),
            {"sender": ALICE, "recipient": BOB, "amount": 500},
        )


class SnapshotPlanTest(unittest.TestCase):
    def analyze(self, h_out, r_in, h_in=(3, 3), r_out=(3, 3, 3), previous=None):
        """previous: (remote inbound from the proxy, its outbound, Harmony inbound from it)."""
        labels = ("rollback", "cutoff", "state")
        old = "0x" + "77" * 20
        pool = {
            "pool_id": "p",
            "harmony": [{}],
            "remote_adapter": ESCROW,
            "previous_remote_adapters": [old] if previous else [],
        }
        state = {"pools": {"p": {
            "harmony": [{"by_label": {
                label: {
                    "outbound_nonce": h_out[i],
                    "inbound_nonce": h_in[i],
                    "inbound_from_previous": {old: previous[2]} if previous else {},
                }
                for i, label in enumerate(("rollback", "cutoff"))
            }}],
            "remote": {"by_label": {
                label: {
                    "inbound_nonce": [r_in[i]],
                    "outbound_nonce": r_out[i],
                    "previous_adapter_nonces": (
                        {old: {"inbound_nonce": [previous[0]], "outbound_nonce": previous[1]}}
                        if previous else {}
                    ),
                }
                for i, label in enumerate(labels)
            }},
        }}}
        return PLAN.analyze_pool(pool, state)

    def test_counts_orphaned_and_undelivered_messages(self):
        orphaned = self.analyze((10, 10), (9, 14, 14))
        self.assertEqual(orphaned["status"], "ok")
        self.assertEqual(orphaned["orphaned_harmony_to_remote"], 4)
        clean = self.analyze((10, 10), (8, 8, 8))
        self.assertEqual(clean["orphaned_harmony_to_remote"], 0)
        self.assertEqual(clean["harmony_to_remote"][0]["undelivered_harmony_to_remote"], 2)

    def test_previous_adapters_count_as_delivery_paths(self):
        result = self.analyze((10, 10), (9, 9, 9), h_in=(3, 3), r_out=(3, 3, 3), previous=(1, 2, 2))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["harmony_to_remote"][0]["undelivered_harmony_to_remote"], 0)
        self.assertEqual(result["harmony_to_remote"][0]["remote_path_offset"], 1)
        self.assertEqual([item["undelivered"] for item in result["remote_to_harmony"]], [0, 0])

    def test_flags_collisions_and_incomparable_counters(self):
        collision = self.analyze((10, 12), (9, 14, 14))
        self.assertEqual(collision["status"], "review")
        self.assertIn("collide", collision["reason"])
        ahead = self.analyze((10, 10), (11, 11, 11))
        self.assertEqual(ahead["status"], "review")


class BuildAndVerifyTest(unittest.TestCase):
    def test_build_scales_pools_and_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.write(escrow=1_000, one_lock=10_000)
            plan = fixture.run_plan()
            self.assertEqual(plan["exposure"]["ethereum"]["snapshot_label"], "rollback")
            self.assertEqual(plan["exposure"]["ethereum"]["orphaned_one_deliveries"], 2)
            fixture.write_inputs(plan, [
                {"pool_id": "ethereum-usdc", "category": "undelivered_deposit",
                 "adjustment_account": DAVE, "adjustment_raw": "100", "amount_raw": "100"},
                {"pool_id": "ethereum-one", "category": "canonical_mint_after_snapshot",
                 "adjustment_account": CAROL, "adjustment_raw": "500", "amount_raw": "500"},
                {"pool_id": "ethereum-one", "category": "orphaned_mint",
                 "adjustment_account": "", "adjustment_raw": "0", "amount_raw": "9000"},
            ])
            result = fixture.build()
            self.assertEqual(result.returncode, 0, result.stderr)
            pools = {row["pool_id"]: row for row in read_rows(fixture.out / "pools.csv")}
            usdc = pools["ethereum-usdc"]
            self.assertEqual(usdc["eligible_raw"], "1300")
            self.assertEqual((usdc["scale_numerator"], usdc["scale_denominator"]), ("1000", "1300"))
            self.assertEqual(usdc["excluded_raw"], "50")
            one = pools["ethereum-one"]
            self.assertEqual(one["eligible_raw"], "7500")
            self.assertEqual(one["claimable_raw"], "7500")
            self.assertEqual(one["retained_raw"], "2500")
            rows = {
                (row["pool_id"], row["account"]): row
                for row in read_rows(fixture.out / "allocations.csv")
            }
            self.assertEqual(rows[("ethereum-usdc", ALICE)]["allocation_raw"], str(400 * 1000 // 1300))
            self.assertEqual(rows[("ethereum-usdc", POOL_CONTRACT)]["status"], "held_contract")
            self.assertEqual(rows[("ethereum-usdc", DAVE)]["class"], "adjustment_target")
            self.assertEqual(rows[("ethereum-one", SAFE)]["status"], "claimable")
            self.assertEqual(rows[("ethereum-one", ONE_ADAPTER)]["status"], "excluded_system")
            claims = read_rows(fixture.out / "claims" / "ethereum.csv")
            by_key = {(row["account"], row["token"]): int(row["amount_raw"]) for row in claims}
            self.assertEqual(by_key[(CAROL, CLAIM_TOKEN)], 4_500)
            self.assertNotIn((POOL_CONTRACT, REMOTE_TOKEN), by_key)
            custody = json.loads((fixture.out / "custody-plan.json").read_text())
            funding = custody["one_funding"]
            self.assertEqual(funding["amount_raw"], "10000")
            self.assertTrue(funding["safe_transaction"]["data"].startswith("0xa9059cbb"))
            self.assertEqual(custody["chains"]["ethereum"]["transfers"][0]["amount_raw"], "1000")

            verified = fixture.verify()
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

            summarized = fixture.summarize()
            self.assertEqual(summarized.returncode, 0, summarized.stderr)
            report = (fixture.root / "summary.md").read_text()
            for heading in ("## Per chain", "## Bridged ONE", "## Per asset and chain (pools)", "## Review items"):
                self.assertIn(heading, report)
            self.assertIn("pro rata 76.92%", report)
            self.assertIn("is short", report)

            prices = fixture.root / "prices.json"
            prices.write_text(json.dumps({
                "fetched_at": "2026-10-20T00:00:00Z",
                "source": "test",
                "stablecoins_at_par": ["USDC"],
                "assets": {
                    "ethereum-usdc": {"token": REMOTE_TOKEN, "basis": "stablecoin_par",
                                      "price_usd": "1", "match": "USD stablecoin at par"},
                    "ethereum-one": {"token": ONE_REMOTE, "basis": "cmc", "cmc_id": 3945,
                                     "cmc_name": "Harmony", "cmc_symbol": "ONE",
                                     "price_usd": "1000000000000000", "match": "Harmony ONE"},
                },
            }))
            priced = fixture.summarize("--prices", str(prices), name="summary-usd.md")
            self.assertEqual(priced.returncode, 0, priced.stderr)
            report = (fixture.root / "summary-usd.md").read_text()
            for heading in ("## USD values", "### Top accounts by USD (all chains", "## Prices"):
                self.assertIn(heading, report)
            carol = next(line for line in report.splitlines() if CAROL in line and "| 1 |" in line)
            self.assertIn("$4.50", carol)

            path = fixture.out / "allocations.csv"
            text = path.read_text()
            path.write_text(text.replace(f"{BOB},600,0,600,0,eoa,claimable,{BOB},461", f"{BOB},600,0,600,0,eoa,claimable,{BOB},462"))
            self.assertNotEqual(text, path.read_text())
            tampered = fixture.verify("verify-tampered.json")
            self.assertNotEqual(tampered.returncode, 0)

    def test_previous_escrow_balance_is_collateral_and_moves_to_custody(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.write(escrow=1_000)
            old = "0x" + "c2" * 20
            routes = json.loads(fixture.routes.read_text())
            routes["pools"][0]["previous_remote_adapters"] = [old]
            fixture.routes.write_text(json.dumps(routes))
            state = json.loads(fixture.state.read_text())
            remote = state["pools"]["ethereum-usdc"]["remote"]
            remote["previous_adapter_owners"] = {old: CUSTODY}
            for label in ("rollback", "cutoff", "state"):
                remote["by_label"][label]["previous_adapter_balances"] = {old: "300"}
                remote["by_label"][label]["previous_adapter_nonces"] = {
                    old: {"outbound_nonce": 0, "inbound_nonce": [0, 0]}
                }
            for entry in state["pools"]["ethereum-usdc"]["harmony"]:
                for label in ("rollback", "cutoff"):
                    entry["by_label"][label]["inbound_from_previous"] = {old: 0}
            fixture.state.write_text(json.dumps(state))
            plan = fixture.run_plan()
            fixture.write_inputs(plan, [])
            result = fixture.build()
            self.assertEqual(result.returncode, 0, result.stderr)
            pools = {row["pool_id"]: row for row in read_rows(fixture.out / "pools.csv")}
            self.assertEqual(pools["ethereum-usdc"]["collateral_raw"], "1300")
            self.assertEqual(pools["ethereum-usdc"]["scale_numerator"], "1")
            custody = json.loads((fixture.out / "custody-plan.json").read_text())
            transfers = custody["chains"]["ethereum"]["transfers"]
            self.assertEqual(
                sorted((item["escrow"], item["amount_raw"]) for item in transfers),
                sorted([(ESCROW, "1000"), (old, "300")]),
            )
            verified = fixture.verify()
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_contract_on_another_chain_is_not_a_key(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.write()
            plan = fixture.run_plan()
            fixture.write_inputs(plan, [])
            classes = read_rows(fixture.classes)
            overrides = fixture.root / "overrides.csv"
            write_csv(overrides, ("pool_id", "address", "action", "destination", "reason"), [
                {"pool_id": "ethereum-one", "address": CAROL, "action": "claim", "destination": "",
                 "reason": "deployer controls the address on ethereum"},
            ])
            cases = (("validator_wrapper", (), "claimable"), ("contract", (), "held_contract"),
                     ("contract", ("--overrides", str(overrides)), "claimable"))
            for harmony_class, extra, status in cases:
                write_csv(fixture.classes, ("chain", "address", "block", "class"), classes + [
                    {"chain": "harmony", "address": CAROL, "block": classes[0]["block"], "class": harmony_class},
                ])
                result = fixture.build("--replace", *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                carol = next(row for row in read_rows(fixture.out / "allocations.csv")
                             if (row["pool_id"], row["account"]) == ("ethereum-one", CAROL))
                self.assertEqual((carol["class"], carol["status"]), ("eoa", status), harmony_class)
                payees = {row["account"] for row in read_rows(fixture.out / "claims" / "ethereum.csv")}
                self.assertEqual(CAROL in payees, status == "claimable")
            fixture.build("--replace")
            carol = next(row for row in read_rows(fixture.out / "allocations.csv") if row["account"] == CAROL)
            self.assertEqual(carol["reason"], "contract on harmony, no code on ethereum: contract holder needs review")

    def test_overrides_and_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.write(escrow=5_000)
            plan = fixture.run_plan()
            fixture.write_inputs(plan, [])
            overrides = fixture.root / "overrides.csv"
            write_csv(overrides, ("pool_id", "address", "action", "destination", "reason"), [
                {"pool_id": "ethereum-usdc", "address": POOL_CONTRACT, "action": "redirect",
                 "destination": DAVE, "reason": "approved LP destination"},
            ])
            config = json.loads(fixture.config.read_text())
            config["allocation_policy"]["minimum_claim_raw_by_pool"] = {"ethereum-one": "3500"}
            fixture.config.write_text(json.dumps(config))
            result = fixture.build("--overrides", str(overrides))
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {
                (row["pool_id"], row["account"]): row
                for row in read_rows(fixture.out / "allocations.csv")
            }
            redirected = rows[("ethereum-usdc", POOL_CONTRACT)]
            self.assertEqual((redirected["status"], redirected["payee"]), ("claimable", DAVE))
            self.assertEqual(rows[("ethereum-one", SAFE)]["status"], "below_minimum")
            self.assertEqual(rows[("ethereum-one", CAROL)]["status"], "claimable")
            pools = {row["pool_id"]: row for row in read_rows(fixture.out / "pools.csv")}
            self.assertEqual(pools["ethereum-usdc"]["scale_numerator"], "1")
            self.assertEqual(pools["ethereum-one"]["below_minimum_raw"], "3000")
            self.assertEqual(pools["ethereum-one"]["retained_raw"], "6000")
            verified = fixture.verify()
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)


if __name__ == "__main__":
    unittest.main()
