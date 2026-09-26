import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sample_bundle_fixture as fx


core = fx.core
ROOT = fx.ROOT
CLI = ROOT / "toolkit" / "scripts" / "random-sample-verify.py"
NODE_RUNNER = ROOT / "toolkit" / "verifier" / "node-run.js"
NODE = shutil.which("node")


def statuses(report, check_id, subject=None):
    return [
        check["status"]
        for check in report["checks"]
        if check["id"] == check_id and (subject is None or check["subject"] == subject)
    ]


def by_label(accounts, label):
    return next(acct for acct in accounts if acct["label"] == label)


class BundleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, **kwargs):
        self.bundle, self.accounts = fx.build_bundle(self.tmp, **kwargs)
        return self.bundle

    def verify(self, sample_size=None, seed="20260925", **options):
        if sample_size is None:
            sample_size = len(self.accounts)
        return core.verify_bundle(self.bundle, sample_size=sample_size, seed=seed, **options)

    def key(self, label):
        return by_label(self.accounts, label)["key"]

    def overlay_path(self):
        return self.bundle / "claims" / "wone-overlay-claims.csv"

    def assertRowFails(self, report, check_id, label):
        self.assertIn(core.FAIL, statuses(report, check_id, self.key(label)), check_id)
        self.assertEqual(report["tiers"]["sampled_rows"], core.FAIL)
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)


class CorrectBundleTest(BundleCase):
    def test_correct_claim_rows_pass_every_tier_except_chain_truth(self):
        self.build()
        report = self.verify()
        self.assertEqual(report["exit_code"], core.EXIT_PASS)
        self.assertEqual(report["tiers"]["sampled_rows"], core.PASS)
        self.assertEqual(report["tiers"]["bundle_metadata"], core.PASS)
        self.assertEqual(report["tiers"]["chain_truth"], core.NOT_VERIFIED)
        self.assertNotIn(core.FAIL, report["counts"])
        self.assertTrue(all(row["status"] == core.PASS for row in report["rows"]))
        self.assertEqual(len(report["rows"]), len(self.accounts))
        for check_id in (
            "overlay.liquid_total", "overlay.native_wallet_airdrop", "overlay.total_claim",
            "overlay.wone_airdrop_policy", "stage.deduction_closure", "wallet.allocation_total",
            "routing.deduction_closure", "airdrop.row",
        ):
            self.assertIn(core.PASS, statuses(report, check_id), check_id)
        self.assertEqual(statuses(report, "airdrop.run"), [core.PASS])

    def test_display_values_are_derived_from_integers(self):
        self.build()
        report = self.verify()
        row = next(item for item in report["rows"] if item["identifier"] == self.key("initial"))
        total = row["values"]["total_claim_atto"]
        self.assertEqual(total["one"], core.fixed18(int(total["atto"])))
        fx.edit_csv(self.overlay_path(), "secure_key", self.key("initial"), {"total_claim_one": "1053.000000000000000001"})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "wone_overlay.decimal_display", "initial")

    def test_threshold_boundary_exactly_1000_one_is_included(self):
        self.build()
        report = self.verify()
        row = next(item for item in report["rows"] if item["identifier"] == self.key("exact"))
        self.assertEqual(row["values"]["qualification_total_atto"]["atto"], str(fx.THRESHOLD))
        self.assertTrue(row["qualifies"])
        self.assertEqual(row["eligibility_category"], "automatic")
        self.assertEqual(statuses(report, "eligibility.inclusion", self.key("exact")), [core.PASS])
        self.assertEqual(statuses(report, "policy.threshold"), [core.PASS])

    def test_threshold_boundary_row_dropped_by_a_strict_comparison_fails(self):
        self.build()
        path = self.bundle / "eligibility" / "automatic.csv"
        rows = [row for row in fx.read_csv(path) if row["secure_key"] != self.key("exact")]
        fx.write_csv(path, fx.WONE_VERIFY.MIGRATION_FIELDS, rows)
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertRowFails(report, "eligibility.inclusion", "exact")
        self.assertEqual(statuses(report, "totals.eligibility_population"), [core.FAIL])

    def test_below_threshold_row_is_excluded(self):
        self.build()
        report = self.verify()
        row = next(item for item in report["rows"] if item["identifier"] == self.key("just-below"))
        self.assertEqual(row["values"]["qualification_total_atto"]["atto"], str(fx.THRESHOLD - 1))
        self.assertFalse(row["qualifies"])
        self.assertEqual(row["records"].get("eligibility"), None)
        self.assertEqual(statuses(report, "eligibility.inclusion", self.key("just-below")), [core.PASS])
        self.assertEqual(statuses(report, "stage.inclusion", self.key("just-below")), [core.PASS])

    def test_below_threshold_row_in_eligibility_output_fails(self):
        self.build()
        overlay = {row["secure_key"]: row for row in fx.read_csv(self.overlay_path())}
        path = self.bundle / "eligibility" / "automatic.csv"
        rows = fx.read_csv(path) + [overlay[self.key("just-below")]]
        rows.sort(key=lambda row: row["secure_key"])
        fx.write_csv(path, fx.WONE_VERIFY.MIGRATION_FIELDS, rows)
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "eligibility.inclusion", "just-below")


class ArithmeticTest(BundleCase):
    def mutate_overlay(self, label, **changes):
        fx.edit_csv(self.overlay_path(), "secure_key", self.key(label), {k: str(v) for k, v in changes.items()})
        fx.refreeze(self.bundle)
        return self.verify()

    def test_incorrect_liquid_total(self):
        self.build()
        acct = by_label(self.accounts, "initial")
        wrong = acct["l0"] + acct["l1"] + 1
        report = self.mutate_overlay("initial", liquid_total_atto=wrong, liquid_total_one=fx.fixed(wrong))
        self.assertRowFails(report, "overlay.liquid_total", "initial")
        message = next(c["message"] for c in report["checks"] if c["id"] == "overlay.liquid_total" and c["status"] == core.FAIL)
        self.assertIn(f"found {wrong} atto", message)

    def test_incorrect_wallet_total(self):
        self.build()
        derived = fx.derive(by_label(self.accounts, "initial"))
        wrong = derived["wallet"] + 5
        report = self.mutate_overlay("initial", wallet_airdrop_atto=wrong, wallet_airdrop_one=fx.fixed(wrong))
        self.assertRowFails(report, "overlay.wallet_airdrop", "initial")

    def test_incorrect_native_wallet_components(self):
        self.build()
        derived = fx.derive(by_label(self.accounts, "undelegating"))
        wrong = derived["native_wallet"] - fx.ATTO
        report = self.mutate_overlay(
            "undelegating", native_wallet_airdrop_atto=wrong, native_wallet_airdrop_one=fx.fixed(wrong)
        )
        self.assertRowFails(report, "overlay.native_wallet_airdrop", "undelegating")

    def test_incorrect_total_claim(self):
        self.build()
        derived = fx.derive(by_label(self.accounts, "initial"))
        wrong = derived["total"] - 1
        report = self.mutate_overlay("initial", total_claim_atto=wrong, total_claim_one=fx.fixed(wrong))
        self.assertRowFails(report, "overlay.total_claim", "initial")

    def test_staked_to_vault_must_equal_active_stake(self):
        self.build()
        report = self.mutate_overlay("initial", staked_to_vault_atto=0, staked_to_vault_one=fx.fixed(0))
        self.assertRowFails(report, "overlay.staked_to_vault", "initial")

    def test_negative_component_is_rejected_not_skipped(self):
        self.build()
        report = self.mutate_overlay("initial", pending_undelegation_atto="-5")
        self.assertRowFails(report, "schema.wone_overlay", "initial")
        message = next(c["message"] for c in report["checks"] if c["id"] == "schema.wone_overlay" and c["status"] == core.FAIL)
        self.assertIn("negative", message)
        self.assertEqual(statuses(report, "file.rows_valid", "wone_overlay"), [core.FAIL])
        self.assertEqual(report["tiers"]["bundle_metadata"], core.FAIL)

    def test_invalid_integer_is_rejected(self):
        self.build()
        report = self.mutate_overlay("initial", liquid_shard1_atto="1.5")
        self.assertRowFails(report, "schema.wone_overlay", "initial")

    def test_wone_overlay_mismatch_below_threshold_non_exchange(self):
        self.build()
        acct = by_label(self.accounts, "holder-small")
        derived = fx.derive(acct)
        wallet = derived["native_wallet"] + acct["wone"]
        report = self.mutate_overlay(
            "holder-small",
            wone_airdrop_atto=acct["wone"], wone_airdrop_one=fx.fixed(acct["wone"]),
            wallet_airdrop_atto=wallet, wallet_airdrop_one=fx.fixed(wallet),
            total_claim_atto=wallet, total_claim_one=fx.fixed(wallet),
        )
        self.assertRowFails(report, "overlay.wone_airdrop_policy", "holder-small")

    def test_wone_overlay_changes_native_field(self):
        self.build()
        acct = by_label(self.accounts, "initial")
        report = self.mutate_overlay("initial", nonce_shard0="9")
        self.assertIn(core.FAIL, statuses(report, "xfile.native_preserved", acct["key"]))

    def test_wone_balance_must_match_holder_census(self):
        self.build()
        acct = by_label(self.accounts, "exact")
        fx.edit_csv(self.bundle / "ledgers" / "wone-holders.csv", "address", acct["address"],
                    {"wone_balance_atto": str(acct["wone"] + 1), "wone_balance": fx.fixed(acct["wone"] + 1)})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "xfile.wone_holder_balance", "exact")

    def test_existing_rules_agree_with_existing_row_validator(self):
        """The rule-based checks and verify-wone-allocation's validate_native_row agree."""
        self.build()
        native = fx.read_csv(self.bundle / "claims" / "native-claims.csv")
        for row in native:
            fx.WONE_VERIFY.validate_native_row(row, "fixture")
        broken = dict(native[0])
        broken["liquid_total_atto"] = str(int(broken["liquid_total_atto"]) + 1)
        with self.assertRaisesRegex(ValueError, "liquid"):
            fx.WONE_VERIFY.validate_native_row(broken, "fixture")


class CrossFileTest(BundleCase):
    def test_duplicate_secure_key_in_sorted_ledger(self):
        self.build()
        rows = fx.read_csv(self.overlay_path())
        rows.insert(1, dict(rows[0]))
        fx.write_csv(self.overlay_path(), fx.WONE_VERIFY.MIGRATION_FIELDS, rows)
        fx.refreeze(self.bundle)
        report = self.verify(sample_size=5)
        self.assertEqual(statuses(report, "file.rows_valid", "wone_overlay"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)
        self.assertEqual(len(set(report["sample"]["identifiers"])), 5)

    def test_duplicate_secure_key_in_stage_policy(self):
        self.build()
        path = self.bundle / "policy" / "migration-stage-policy.csv"
        rows = fx.read_csv(path)
        target = next(row for row in rows if row["secure_key"] == self.key("initial"))
        rows.append(dict(target))
        fx.write_csv(path, fx.STAGE_BUILDER.OUTPUT_FIELDS, rows)
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertIn(core.FAIL, statuses(report, "duplicate.stage_policy", self.key("initial")))
        self.assertEqual(statuses(report, "file.rows_valid", "stage_policy"), [core.FAIL])

    def test_address_mismatch_with_secure_key(self):
        self.build()
        other = by_label(self.accounts, "below")["address"]
        fx.edit_csv(self.overlay_path(), "secure_key", self.key("initial"),
                    {"address": fx.address_for("impostor"), "address_or_secure_key": fx.address_for("impostor")})
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertRowFails(report, "identity.wone_overlay", "initial")
        self.assertIn(core.FAIL, statuses(report, "xfile.native_preserved", self.key("initial")))
        self.assertTrue(other)

    def test_address_mismatch_across_files(self):
        self.build()
        path = self.bundle / "allocation" / "wallet-allocations.csv"
        impostor = fx.address_for("impostor")
        fx.edit_csv(path, "source_secure_key", self.key("initial"), {"source_address": impostor, "destination_address": impostor})
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertRowFails(report, "wallet.address_match", "initial")
        self.assertIn(core.FAIL, statuses(report, "identity.wallet_allocations", self.key("initial")))

    def test_routing_stage_mismatch(self):
        self.build()
        path = self.bundle / "routing" / "routing-exceptions.csv"
        fx.edit_csv(path, "source_secure_key", self.key("incident"), {"migration_stage": "deferred"})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "routing.stage_match", "incident")

    def test_routing_treatment_status_mismatch(self):
        self.build()
        path = self.bundle / "routing" / "routing-exceptions.csv"
        fx.edit_csv(path, "source_secure_key", self.key("exchange"), {"destination_status": "ready"})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "routing.treatment_status", "exchange")

    def test_routing_deduction_does_not_close(self):
        self.build()
        path = self.bundle / "routing" / "routing-exceptions.csv"
        acct = by_label(self.accounts, "incident")
        fx.edit_csv(path, "source_secure_key", acct["key"], {"amount_atto": str(acct["existing"] - 1)})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "routing.deduction_closure", "incident")

    def test_wallet_vault_split_and_allocation(self):
        self.build()
        path = self.bundle / "allocation" / "wallet-allocations.csv"
        rows = [row for row in fx.read_csv(path) if row["source_secure_key"] == self.key("initial")]
        wrong = int(rows[0]["amount_atto"]) - 1
        fx.edit_csv(path, "source_secure_key", self.key("initial"), {"amount_atto": str(wrong)})
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertRowFails(report, "wallet.allocation_total", "initial")
        self.assertIn(core.FAIL, statuses(report, "airdrop.row", self.key("initial")))

    def test_exchange_wallet_must_not_receive_airdrop_allocation(self):
        self.build()
        acct = by_label(self.accounts, "exchange")
        stage = next(row for row in fx.read_csv(self.bundle / "policy" / "migration-stage-policy.csv")
                     if row["secure_key"] == acct["key"])
        fx.append_csv_row(self.bundle / "allocation" / "wallet-allocations.csv", {
            "source_secure_key": acct["key"], "source_address": acct["address"], "destination_id": "",
            "destination_address": acct["address"], "destination_status": "ready",
            "amount_atto": stage["migration_wallet_allocation_atto"], "delivery_method": "implicit_same_address",
            "reason": "ordinary code-less wallet", "evidence": "",
        })
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "wallet.allocation_total", "exchange")

    def test_stage_rule_active_wallet_cannot_be_deferred(self):
        self.build()
        path = self.bundle / "policy" / "migration-stage-policy.csv"
        fx.edit_csv(path, "secure_key", self.key("initial"), {"migration_stage": "deferred"})
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertRowFails(report, "stage.deferred_rule", "initial")
        self.assertIn(core.FAIL, statuses(report, "wallet.allocation_total", self.key("initial")))

    def test_incident_deduction_applies_before_threshold(self):
        self.build()
        report = self.verify()
        row = next(item for item in report["rows"] if item["identifier"] == self.key("incident-below"))
        self.assertEqual(row["migration_stage"], "deferred")
        self.assertEqual(row["status"], core.PASS)

    def test_airdrop_amount_mismatch(self):
        self.build()
        path = self.bundle / "airdrop" / "distribution.csv"
        acct = by_label(self.accounts, "initial")
        rows = fx.read_csv(path)
        for row in rows:
            if row["address"].lower() == acct["address"].lower():
                row["amount"] = str(int(row["amount"]) + 1)
        fx.write_csv(path, ("address", "amount"), rows)
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertEqual(statuses(report, "airdrop.run"), [core.FAIL])
        self.assertIn(core.FAIL, statuses(report, "airdrop.row", acct["key"]))
        self.assertEqual(statuses(report, "global.airdrop_destinations"), [core.FAIL])


class BundleMetadataTest(BundleCase):
    def test_missing_file(self):
        self.build()
        (self.bundle / "allocation" / "wallet-allocations.csv").unlink()
        report = self.verify()
        self.assertEqual(statuses(report, "file.present", "allocation/wallet-allocations.csv"), [core.FAIL])
        self.assertEqual(statuses(report, "files.required_roles"), [core.FAIL])
        self.assertIn(core.NOT_VERIFIED, statuses(report, "wallet.allocation_total"))
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_hash_mismatch(self):
        self.build()
        path = self.bundle / "policy" / "migration-stage-policy.csv"
        path.write_text(path.read_text().replace("wallet activity within 6 months", "wallet activity within 6 month!"))
        report = self.verify()
        self.assertEqual(statuses(report, "file.sha256", "policy/migration-stage-policy.csv"), [core.FAIL])
        self.assertEqual(report["tiers"]["bundle_metadata"], core.FAIL)
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_pinned_manifest_hash(self):
        self.build()
        digest = core.file_sha256(self.bundle / core.MANIFEST_NAME)
        self.assertEqual(statuses(self.verify(expected_manifest_sha256=digest), "manifest.pinned_sha256"), [core.PASS])
        report = self.verify(expected_manifest_sha256="0" * 64)
        self.assertEqual(statuses(report, "manifest.pinned_sha256"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_declared_total_mismatch(self):
        self.build()
        path = self.bundle / core.MANIFEST_NAME
        manifest = json.loads(path.read_text())
        field = "wone_overlay.total_claim_atto"
        manifest["declared_totals"][field] = str(int(manifest["declared_totals"][field]) + 1)
        fx.write_json(path, manifest)
        report = self.verify()
        self.assertEqual(statuses(report, "totals.declared", field), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_wrong_cutoff_and_chain_id(self):
        self.build()
        path = self.bundle / core.MANIFEST_NAME
        manifest = json.loads(path.read_text())
        manifest["cutoff"]["shard1"]["block"] += 1
        manifest["chain_ids"]["shard0"] = 1
        fx.write_json(path, manifest)
        report = self.verify()
        self.assertEqual(statuses(report, "cutoff.block", "shard1"), [core.FAIL])
        self.assertEqual(statuses(report, "cutoff.chain_id"), [core.FAIL])

    def test_row_with_wrong_cutoff_block(self):
        self.build()
        fx.edit_csv(self.overlay_path(), "secure_key", self.key("initial"), {"claims_shard1_block": str(fx.SHARD1_BLOCK - 1)})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "overlay.cutoff_blocks", "initial")

    def test_undeclared_file(self):
        self.build()
        (self.bundle / "claims" / "extra.csv").write_text("x\n")
        self.assertEqual(statuses(self.verify(), "files.undeclared"), [core.FAIL])

    def test_empty_bundle_directory(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(core.BundleError, "not found"):
            core.verify_bundle(empty, sample_size=1, seed="x")

    def test_bundle_with_no_rows(self):
        self.bundle, self.accounts = fx.build_bundle(self.tmp, accounts=[], airdrop=False)
        with self.assertRaisesRegex(core.BundleError, "exceeds the population of 0"):
            core.verify_bundle(self.bundle, sample_size=1, seed="x")
        report = core.verify_bundle(self.bundle, sample_size=0, seed="x", verify_all_metadata=True)
        self.assertEqual(report["sample"]["population"], 0)
        self.assertEqual(report["tiers"]["bundle_metadata"], core.PASS)

    def test_verify_all_metadata_checks_every_identity(self):
        self.build()
        report = core.verify_bundle(self.bundle, sample_size=0, seed="x", verify_all_metadata=True)
        self.assertEqual(report["exit_code"], core.EXIT_PASS)
        self.assertEqual(statuses(report, "metadata.identity_all", "wone_overlay"), [core.PASS])
        stage_path = self.bundle / "policy" / "migration-stage-policy.csv"
        fx.edit_csv(stage_path, "secure_key", self.key("validator"), {"address": fx.address_for("impostor").lower()})
        fx.refreeze(self.bundle)
        report = core.verify_bundle(self.bundle, sample_size=0, seed="x", verify_all_metadata=True)
        self.assertEqual(statuses(report, "metadata.identity_all", "stage_policy"), [core.FAIL])


class SamplingTest(BundleCase):
    def test_same_seed_reproduces_the_sample(self):
        self.build()
        first = self.verify(sample_size=6, seed="20260925")
        second = self.verify(sample_size=6, seed="20260925")
        self.assertEqual(first["sample"]["identifiers"], second["sample"]["identifiers"])
        self.assertEqual(len(first["sample"]["identifiers"]), 6)
        self.assertEqual(len(set(first["sample"]["identifiers"])), 6)
        population = sorted(acct["key"] for acct in self.accounts)
        self.assertEqual(
            first["sample"]["identifiers"],
            core.draw_sample(core.load_rules(), "20260925", population, 6),
        )

    def test_different_seeds_give_different_samples(self):
        self.build()
        first = self.verify(sample_size=6, seed="20260925")["sample"]["identifiers"]
        second = self.verify(sample_size=6, seed="20260926")["sample"]["identifiers"]
        self.assertNotEqual(first, second)

    def test_sample_is_independent_of_row_order(self):
        rules = core.load_rules()
        population = [f"0x{index:064x}" for index in range(200)]
        forward = core.draw_sample(rules, "seed", population, 10)
        backward = core.draw_sample(rules, "seed", list(reversed(population)), 10)
        self.assertEqual(forward, backward)

    def test_sampling_is_uniform(self):
        rules = core.load_rules()
        population = [f"id-{index}" for index in range(10)]
        hits = {identifier: 0 for identifier in population}
        for seed in range(2000):
            for identifier in core.draw_sample(rules, str(seed), population, 3):
                hits[identifier] += 1
        for count in hits.values():
            self.assertTrue(500 < count < 700, hits)

    def test_sample_by_address(self):
        self.build()
        report = self.verify(sample_size=4, sample_by="address")
        addresses = {acct["address"].lower() for acct in self.accounts}
        self.assertTrue(set(report["sample"]["identifiers"]) <= addresses)
        self.assertEqual(report["exit_code"], core.EXIT_PASS)

    def test_generated_seed_is_reported(self):
        self.build()
        report = core.verify_bundle(self.bundle, sample_size=3, seed=None)
        self.assertTrue(report["sample"]["seed_generated"])
        again = core.verify_bundle(self.bundle, sample_size=3, seed=report["sample"]["seed"])
        self.assertEqual(report["sample"]["identifiers"], again["sample"]["identifiers"])

    def test_sample_larger_than_population_is_refused(self):
        self.build()
        with self.assertRaisesRegex(core.BundleError, "never reduces"):
            self.verify(sample_size=len(self.accounts) + 1)


class HistoricalEvidenceTest(BundleCase):
    def test_malformed_archived_evidence(self):
        doc = fx.header_evidence("archive-db")
        del doc["block_headers"][0]["state_root"]
        doc["block_headers"][1]["number"] = "95882100"
        self.build(evidence=[doc])
        report = self.verify()
        self.assertEqual(statuses(report, "chain.evidence_schema"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)
        self.assertEqual(report["tiers"]["chain_truth"], core.NOT_VERIFIED)

    def test_evidence_that_is_not_json(self):
        self.build()
        path = self.tmp / "broken.json"
        path.write_text("{\"format\": ")
        shutil.rmtree(self.bundle)
        self.bundle, self.accounts = fx.build_bundle(self.tmp / "again", evidence=[])
        target = self.bundle / "evidence" / "000-broken.json"
        target.parent.mkdir()
        shutil.copyfile(path, target)
        manifest = json.loads((self.bundle / core.MANIFEST_NAME).read_text())
        manifest["files"].append({"role": "historical_evidence", "path": "evidence/000-broken.json",
                                  "sha256": core.file_sha256(target), "bytes": target.stat().st_size,
                                  "source": "archive-db"})
        fx.write_json(self.bundle / core.MANIFEST_NAME, manifest)
        report = self.verify()
        self.assertEqual(statuses(report, "file.json", "evidence/000-broken.json"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_stale_rpc_header_is_a_warning_not_verification(self):
        stale = fx.header_evidence("public-rpc", shard0_hash="0x" + "99" * 32)
        self.build(evidence=[fx.header_evidence("archive-db"), stale])
        report = self.verify()
        self.assertEqual(statuses(report, "chain.header", "public-rpc shard0"), [core.WARNING])
        self.assertEqual(statuses(report, "chain.headers", "shard0"), [core.NOT_VERIFIED])
        self.assertEqual(statuses(report, "chain.headers", "shard1"), [core.PASS])
        self.assertEqual(report["tiers"]["chain_truth"], core.NOT_VERIFIED)
        self.assertEqual(report["exit_code"], core.EXIT_PASS)
        strict = self.verify(require_chain_truth=True)
        self.assertEqual(strict["exit_code"], core.EXIT_INCOMPLETE)

    def test_conflicting_independent_sources_fail(self):
        self.build(evidence=[
            fx.header_evidence("archive-db", parent_hash="0x" + "11" * 32),
            fx.header_evidence("archive-db-2", parent_hash="0x" + "33" * 32),
        ])
        report = self.verify()
        self.assertIn(core.FAIL, statuses(report, "chain.source_agreement", "shard0 parent_hash"))
        self.assertEqual(report["tiers"]["chain_truth"], core.FAIL)

    def test_rpc_marked_authoritative_is_rejected(self):
        self.build()
        path = self.bundle / core.MANIFEST_NAME
        manifest = json.loads(path.read_text())
        for source in manifest["sources"]:
            if source["id"] == "public-rpc":
                source["authoritative"] = True
        fx.write_json(path, manifest)
        report = self.verify()
        self.assertEqual(statuses(report, "sources.metadata"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_stale_rpc_account_value_is_a_warning(self):
        accounts = fx.default_accounts()
        acct = next(item for item in accounts if item["label"] == "initial")
        stale = fx.header_evidence("public-rpc", records=[{
            "shard": 0, "block": fx.SHARD0_BLOCK, "address": acct["address"], "balance_atto": "1",
        }])
        self.build(accounts=accounts, evidence=[stale])
        report = self.verify()
        self.assertIn(core.WARNING, statuses(report, "chain.row", acct["key"]))
        self.assertEqual(report["tiers"]["chain_truth"], core.NOT_VERIFIED)

    def test_independent_archived_evidence_verifies_chain_truth(self):
        accounts = fx.default_accounts()
        records = fx.account_evidence(accounts)
        self.build(accounts=accounts, evidence=[
            fx.header_evidence("archive-db", records=records),
            fx.header_evidence("archive-db-2"),
        ])
        report = self.verify(require_chain_truth=True)
        self.assertEqual(report["tiers"]["chain_truth"], core.PASS)
        self.assertEqual(report["exit_code"], core.EXIT_PASS)

    def test_independent_evidence_contradicting_a_row_fails(self):
        accounts = fx.default_accounts()
        records = fx.account_evidence(accounts)
        target = next(item for item in accounts if item["label"] == "initial")
        for record in records:
            if record["address"] == target["address"] and record["shard"] == 0:
                record["balance_atto"] = str(target["l0"] - 1)
        self.build(accounts=accounts, evidence=[fx.header_evidence("archive-db", records=records)])
        report = self.verify()
        self.assertIn(core.FAIL, statuses(report, "chain.row", target["key"]))
        self.assertEqual(report["tiers"]["chain_truth"], core.FAIL)
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)


class SchemaReuseTest(unittest.TestCase):
    """rules.json must stay aligned with the pipeline's own schemas and constants."""

    def test_rule_schemas_match_pipeline_field_lists(self):
        rules = core.load_rules()
        roles = rules["roles"]
        self.assertEqual(tuple(roles["native_claims"]["fields"]), fx.WONE_VERIFY.NATIVE_FIELDS)
        self.assertEqual(tuple(roles["wone_overlay"]["fields"]), fx.WONE_VERIFY.MIGRATION_FIELDS)
        self.assertEqual(tuple(roles["stage_policy"]["fields"]), fx.STAGE_BUILDER.OUTPUT_FIELDS)
        self.assertEqual(tuple(roles["routing_exceptions"]["fields"]), fx.WONE_VERIFY.ROUTING_EXCEPTION_FIELDS)
        self.assertEqual(tuple(roles["wallet_allocations"]["fields"]), fx.MATERIALIZE.WALLET_FIELDS)
        self.assertEqual(tuple(roles["vault_shares"]["fields"]), fx.MATERIALIZE.SHARE_FIELDS)
        self.assertEqual(tuple(roles["wone_holders"]["fields"]), fx.WONE_VERIFY.HOLDER_FIELDS)
        self.assertTrue(set(roles["eligibility"]["fields"]) <= set(fx.WONE_VERIFY.MIGRATION_FIELDS))

    def test_rule_constants_match_pipeline_and_snapshot(self):
        engine = core.Engine(core.load_rules(), core.load_snapshot())
        self.assertEqual(engine.constants["THRESHOLD_ATTO"], fx.WONE_VERIFY.MINIMUM_ATTO)
        self.assertEqual(engine.constants["THRESHOLD_ATTO"], fx.STAGE_BUILDER.MINIMUM_ATTO)
        self.assertEqual(engine.constants["WONE_ADDRESS"], fx.WONE_VERIFY.WONE_ADDRESS)
        self.assertEqual(
            set(engine.constants["WONE_REQUIRED_EXCLUSIONS"]),
            {fx.WONE_VERIFY.WONE_ADDRESS, *fx.WONE_VERIFY.LAYERZERO_ADDRESSES},
        )
        self.assertEqual(engine.constants["REQUESTED_CUTOFF_UTC"], fx.SNAPSHOT["cutoff"]["requested_time_utc"])
        self.assertEqual(str(engine.constants["THRESHOLD_ATTO"]), fx.SNAPSHOT["eligibility_1000_one"]["threshold_atto"])
        self.assertEqual(
            engine.constants["INITIAL_SINCE_UTC"],
            fx.STAGE_BUILDER.subtract_calendar_months(
                fx.STAGE_BUILDER.parse_utc(fx.SNAPSHOT["cutoff"]["requested_time_utc"]), 6
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


class CliTest(BundleCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True)

    def test_cli_report_and_exit_codes(self):
        self.build()
        output = self.tmp / "sample-report.json"
        result = self.cli("--bundle", self.bundle, "--sample-size", 5, "--seed", "20260925", "--output", output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("seed            : 20260925", result.stdout)
        self.assertIn("Sampled rows passed", result.stdout)
        self.assertIn("Historical chain truth independently verified   : NOT VERIFIED", result.stdout)
        report = json.loads(output.read_text())
        for identifier in report["sample"]["identifiers"]:
            self.assertIn(identifier, result.stdout)
        again = self.cli("--bundle", self.bundle, "--sample-size", 5, "--seed", "20260925", "--output", output)
        self.assertEqual(again.returncode, 2)
        self.assertIn("--replace", again.stderr)

    def test_cli_generates_and_prints_seed(self):
        self.build()
        result = self.cli("--bundle", self.bundle, "--sample-size", 2)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("(generated; record it to reproduce)", result.stdout)

    def test_cli_failure_exit_code(self):
        self.build()
        (self.bundle / "routing" / "routing-exceptions.csv").write_text("broken\n")
        result = self.cli("--bundle", self.bundle, "--sample-size", 3, "--seed", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("why:", result.stdout)

    def test_cli_missing_bundle_and_bad_arguments(self):
        result = self.cli("--bundle", self.tmp / "nope", "--sample-size", 3)
        self.assertEqual(result.returncode, 2)
        result = self.cli("--bundle", self.tmp)
        self.assertEqual(result.returncode, 2)

    def test_cli_list_files_and_show_row(self):
        self.build()
        listing = self.cli("--bundle", self.bundle, "--list-files")
        self.assertEqual(listing.returncode, 0, listing.stdout)
        self.assertIn("claims/wone-overlay-claims.csv", listing.stdout)
        shown = self.cli("--bundle", self.bundle, "--show-row", by_label(self.accounts, "exact")["address"])
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertIn("overlay.qualification_total", shown.stdout)
        self.assertIn("qualifies (>= 1,000 ONE): True", shown.stdout)
        (self.bundle / "claims" / "native-claims.csv").write_text("x\n")
        self.assertEqual(self.cli("--bundle", self.bundle, "--list-files").returncode, 1)

    def test_cli_verify_all_metadata_without_sample(self):
        self.build()
        result = self.cli("--bundle", self.bundle, "--verify-all-metadata")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("NOT REQUESTED", result.stdout)


class AuditRegressionTest(BundleCase):
    """Edge cases where a lenient reader or join would give a wrong verdict."""

    def edit_manifest(self, change):
        path = self.bundle / core.MANIFEST_NAME
        manifest = json.loads(path.read_text())
        change(manifest)
        fx.write_json(path, manifest)

    def test_duplicates_never_silently_shrink_the_sample(self):
        self.build()
        rows = fx.read_csv(self.overlay_path())
        rows.insert(1, dict(rows[0]))
        fx.write_csv(self.overlay_path(), fx.WONE_VERIFY.MIGRATION_FIELDS, rows)
        fx.refreeze(self.bundle)
        with self.assertRaisesRegex(core.BundleError, "distinct identifiers"):
            self.verify(sample_size=len(rows))
        report = self.verify(sample_size=len(rows) - 1)
        self.assertEqual(len(report["sample"]["identifiers"]), len(rows) - 1)

    def test_excluded_wone_holder_uses_the_overlay_summary(self):
        self.build()
        report = self.verify()
        key = self.key("excluded-holder")
        self.assertEqual(statuses(report, "xfile.wone_holder_balance", key), [core.PASS])
        self.assertEqual(statuses(report, "overlay.wone_excluded_holder", key), [core.PASS])
        self.assertEqual(statuses(report, "summary.wone_overlay"), [core.PASS])
        self.assertEqual(report["exit_code"], core.EXIT_PASS)

    def test_excluded_holder_given_wone_fails(self):
        self.build()
        acct = by_label(self.accounts, "excluded-holder")
        wone = acct["wone"]
        derived = fx.derive(acct)
        fx.edit_csv(self.overlay_path(), "secure_key", acct["key"], {
            "wone_balance_atto": str(wone), "wone_balance_one": fx.fixed(wone),
            "qualification_total_atto": str(derived["qualification"] + wone),
            "qualification_total_one": fx.fixed(derived["qualification"] + wone),
        })
        fx.refreeze(self.bundle)
        path = self.bundle / "summaries" / "wone-overlay-summary.json"
        summary = json.loads(path.read_text())
        summary["output_sha256"] = core.file_sha256(self.overlay_path())
        fx.write_json(path, summary)
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertIn(core.FAIL, statuses(report, "overlay.wone_excluded_holder", acct["key"]))

    def test_wone_summary_missing_or_incomplete(self):
        self.build()
        path = self.bundle / "summaries" / "wone-overlay-summary.json"
        summary = json.loads(path.read_text())
        summary["excluded_addresses_requested"] = [fx.WONE_ADDRESS]
        fx.write_json(path, summary)
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertEqual(statuses(report, "summary.wone_overlay"), [core.FAIL])
        self.assertIn(core.NOT_VERIFIED, statuses(report, "xfile.wone_holder_balance"))
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_float_and_bool_integers_are_rejected(self):
        self.build()
        self.edit_manifest(lambda m: m["chain_ids"].update(shard0=float(m["chain_ids"]["shard0"])))
        self.assertEqual(statuses(self.verify(), "cutoff.chain_id"), [core.FAIL])
        self.edit_manifest(lambda m: (m["chain_ids"].update(shard0=int(m["chain_ids"]["shard0"])),
                                      m["cutoff"]["shard0"].update(block=float(m["cutoff"]["shard0"]["block"]))))
        self.assertEqual(statuses(self.verify(), "cutoff.block", "shard0"), [core.FAIL])

    def test_unnormalized_paths_are_rejected(self):
        self.build()
        self.edit_manifest(lambda m: next(e for e in m["files"] if e["role"] == "native_claims").update(path="claims//native-claims.csv"))
        report = self.verify()
        self.assertEqual(statuses(report, "file.entry", "claims//native-claims.csv"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_nested_manifest_is_not_exempt(self):
        self.build()
        (self.bundle / "claims" / core.MANIFEST_NAME).write_text("{}\n")
        self.assertEqual(statuses(self.verify(), "files.undeclared"), [core.FAIL])

    def test_malformed_airdrop_list_fails_without_crashing(self):
        self.build()
        path = self.bundle / "airdrop" / "distribution.csv"
        path.write_text(path.read_text() + "\n0xabc\n")
        report = self.verify()
        self.assertEqual(statuses(report, "airdrop.run"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_seed_is_used_exactly(self):
        self.build()
        for seed in ("", " 20260925", "20260925\n"):
            with self.assertRaisesRegex(core.BundleError, "seed"):
                self.verify(sample_size=2, seed=seed)

    def test_malformed_csv_quoting_fails(self):
        self.build()
        path = self.bundle / "routing" / "routing-exceptions.csv"
        path.write_text(path.read_text() + '"unterminated,field\n')
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertEqual(statuses(report, "file.schema", "routing/routing-exceptions.csv"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_nul_byte_fails(self):
        self.build()
        path = self.bundle / "routing" / "routing-exceptions.csv"
        path.write_text(path.read_text().replace("fixture, synthetic", "fixture\0synthetic", 1))
        fx.refreeze(self.bundle)
        self.assertEqual(statuses(self.verify(), "file.schema", "routing/routing-exceptions.csv"), [core.FAIL])

    def test_long_fields_parse(self):
        self.build()
        path = self.bundle / "allocation" / "wallet-allocations.csv"
        path.write_text(path.read_text().replace("ordinary code-less wallet", "x" * 300_000, 1))
        fx.refreeze(self.bundle)
        self.assertEqual(self.verify()["exit_code"], core.EXIT_PASS)

    def test_evidence_shard_must_be_an_integer(self):
        doc = fx.header_evidence("archive-db")
        doc["block_headers"][1]["shard"] = True
        self.build(evidence=[doc])
        self.assertEqual(statuses(self.verify(), "chain.evidence_schema"), [core.FAIL])

    def test_sampling_by_address_keeps_rows_apart_when_keys_repeat(self):
        self.build()
        rows = fx.read_csv(self.overlay_path())
        twin = dict(rows[0])
        twin["address"] = twin["address_or_secure_key"] = fx.address_for("twin")
        rows.insert(1, twin)
        fx.write_csv(self.overlay_path(), fx.WONE_VERIFY.MIGRATION_FIELDS, rows)
        fx.refreeze(self.bundle)
        report = self.verify(sample_size=len(rows), sample_by="address")
        by_id = {row["identifier"]: row for row in report["rows"]}
        self.assertEqual(by_id[twin["address"].lower()]["address"], twin["address"])
        self.assertEqual(by_id[rows[0]["address"].lower()]["address"], rows[0]["address"])
        self.assertIn(core.FAIL, statuses(report, "identity.wone_overlay", twin["address"].lower()))

    def test_activity_after_cutoff_fails(self):
        self.build()
        path = self.bundle / "policy" / "migration-stage-policy.csv"
        fx.edit_csv(path, "secure_key", self.key("initial"), {"last_activity_time_utc": "2026-09-10T14:00:01Z"})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "stage.activity_before_cutoff", "initial")

    def test_activity_exactly_at_window_start_is_initial(self):
        self.build()
        path = self.bundle / "policy" / "migration-stage-policy.csv"
        fx.edit_csv(path, "secure_key", self.key("initial"), {"last_activity_time_utc": "2026-03-10T14:00:00Z"})
        fx.refreeze(self.bundle)
        self.assertEqual(statuses(self.verify(), "stage.initial_rule", self.key("initial")), [core.PASS])
        fx.edit_csv(path, "secure_key", self.key("initial"), {"last_activity_time_utc": "2026-03-10T13:59:59Z"})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "stage.initial_rule", "initial")

    def test_reviewed_contract_split_must_take_wallet_first(self):
        self.build()
        path = self.bundle / "policy" / "migration-stage-policy.csv"
        stage = next(r for r in fx.read_csv(path) if r["secure_key"] == self.key("smartvault"))
        wallet, staked = int(stage["reviewed_contract_wallet_non_issuance_atto"]), int(stage["reviewed_contract_staked_non_issuance_atto"])
        fx.edit_csv(path, "secure_key", self.key("smartvault"), {
            "reviewed_contract_wallet_non_issuance_atto": str(wallet - 1),
            "reviewed_contract_staked_non_issuance_atto": str(staked + 1),
        })
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "stage.wallet_vault_split", "smartvault")

    def test_ready_wallet_allocation_needs_a_destination(self):
        self.build()
        path = self.bundle / "allocation" / "wallet-allocations.csv"
        fx.edit_csv(path, "source_secure_key", self.key("initial"), {"destination_address": ""})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "wallet.ready_destination", "initial")

    def test_one_atto_below_threshold_after_incident_deduction_is_deferred(self):
        accounts = fx.default_accounts()
        acct = next(item for item in accounts if item["label"] == "incident")
        acct["existing"] = acct["l0"] - fx.THRESHOLD + 1
        self.bundle, self.accounts = fx.build_bundle(self.tmp, accounts=accounts)
        report = self.verify()
        row = next(item for item in report["rows"] if item["identifier"] == acct["key"])
        self.assertEqual(row["migration_stage"], "deferred")
        self.assertEqual(report["exit_code"], core.EXIT_PASS)


class SnapshotTest(BundleCase):
    """Cross-checks between the claim ledger and the published cutoff snapshot files."""

    def snapshot_path(self, name="full-snapshot.csv"):
        return self.bundle / "snapshot" / name

    def edit_snapshot(self, label, changes, name="full-snapshot.csv"):
        address = by_label(self.accounts, label)["address"].lower()
        fx.edit_csv(self.snapshot_path(name), "eth_address", address, changes)
        fx.refreeze(self.bundle)
        return self.verify()

    def test_snapshot_checks_pass_on_a_consistent_bundle(self):
        self.build()
        report = self.verify()
        self.assertEqual(report["exit_code"], core.EXIT_PASS)
        for check_id in ("snapshot.presence", "snapshot.balances", "snapshot.arithmetic", "snapshot.policy",
                         "snapshot.allocation", "snapshot.labels", "snapshot.wone_ledger", "snapshot_public.values"):
            self.assertIn(core.PASS, statuses(report, check_id), check_id)
        self.assertEqual(statuses(report, "summary.snapshot"), [core.PASS])
        self.assertEqual(statuses(report, "totals.snapshot_closure"), [core.PASS])

    def test_snapshot_is_optional(self):
        args, self.accounts = fx.pipeline_outputs(self.tmp / "plain", snapshot=False)
        self.bundle = self.tmp / "plain-bundle"
        fx.BUILDER.build(fx.BUILDER.parse_args(["--output", str(self.bundle), *args]))
        report = self.verify()
        self.assertEqual(report["exit_code"], core.EXIT_PASS)
        self.assertIn(core.NOT_VERIFIED, statuses(report, "snapshot.presence"))
        self.assertFalse(any(c["required"] for c in report["checks"] if c["id"].startswith("snapshot")))

    def test_balance_mismatch(self):
        self.build()
        acct = by_label(self.accounts, "initial")
        report = self.edit_snapshot("initial", {"liquid_shard1_atto": str(acct["l1"] + 1),
                                                "native_total_atto": str(fx.derive(acct)["native_total"] + 1),
                                                "total_balance_atto": str(fx.derive(acct)["native_total"] + 1)})
        self.assertRowFails(report, "snapshot.balances", "initial")

    def test_stake_split_between_self_and_delegated_is_accepted(self):
        self.build()
        acct = by_label(self.accounts, "initial")
        report = self.edit_snapshot("initial", {"self_stake_atto": "1", "delegated_atto": str(acct["active"] - 1)})
        self.assertEqual(statuses(report, "snapshot.balances", acct["key"]), [core.PASS])

    def test_snapshot_totals_must_add_up(self):
        self.build()
        report = self.edit_snapshot("incident", {"exploit_distribution_retained_atto": "1"})
        self.assertRowFails(report, "snapshot.arithmetic", "incident")

    def test_wone_override_must_be_listed(self):
        self.build()
        self.assertEqual(statuses(self.verify(), "snapshot.wone_ledger", self.key("excluded-holder")), [core.PASS])
        path = self.bundle / "summaries" / "snapshot-summary.json"
        summary = json.loads(path.read_text())
        summary["ledger_wone_overrides"] = []
        fx.write_json(path, summary)
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "snapshot.wone_ledger", "excluded-holder")

    def test_exchange_stage_must_be_manual(self):
        self.build()
        report = self.edit_snapshot("exchange", {"migration_stage": "initial", "issuance_treatment": "issue"})
        self.assertRowFails(report, "snapshot.policy", "exchange")

    def test_qualification_flag(self):
        self.build()
        self.assertRowFails(self.edit_snapshot("exact", {"qualified_1000_one": "false"}), "snapshot.policy", "exact")

    def test_missing_label(self):
        self.build()
        self.assertRowFails(self.edit_snapshot("wone-contract", {"special_label": ""}), "snapshot.labels", "wone-contract")

    def test_unknown_label_fails(self):
        self.build()
        self.assertRowFails(self.edit_snapshot("just-below", {"special_label": "made-up"}), "snapshot.label_values", "just-below")

    def test_public_labels_follow_the_full_snapshot(self):
        self.build()
        report = self.edit_snapshot("wone-contract", {"special_label": ""}, "snapshot.csv")
        self.assertRowFails(report, "snapshot_public.labels", "wone-contract")

    def test_victim_only_slug_is_left_out_of_public_files(self):
        self.build()
        slug = "20250202-wallet-theft-case002"
        report = self.edit_snapshot("initial", {"special_label": slug, "related_incident": slug, "incident_role": "reported_victim"})
        self.assertEqual(statuses(report, "snapshot.label_values", self.key("initial")), [core.PASS])
        self.assertEqual(statuses(report, "snapshot_public.labels", self.key("initial")), [core.PASS])
        address = by_label(self.accounts, "initial")["address"].lower()
        fx.edit_csv(self.snapshot_path("snapshot.csv"), "eth_address", address, {"special_label": "not-a-slug"})
        fx.refreeze(self.bundle)
        self.assertRowFails(self.verify(), "snapshot_public.labels", "initial")

    def test_activity_details_must_be_complete(self):
        self.build()
        self.assertRowFails(self.edit_snapshot("initial", {"last_signed_tx_hash": ""}), "snapshot.activity_fields", "initial")

    def test_bech32_mismatch(self):
        self.build()
        other = by_label(self.accounts, "below")["address"]
        report = self.edit_snapshot("initial", {"one1_address": fx.lib.hex_to_bech32(other)})
        self.assertIn(core.FAIL, statuses(report, "identity.cutoff_snapshot", self.key("initial")))

    def test_public_amounts_round_half_up(self):
        self.build()
        report = self.verify()
        key = self.key("half-one")
        self.assertEqual(statuses(report, "snapshot_public.values", key), [core.PASS])
        rows = fx.read_csv(self.snapshot_path("snapshot.csv"))
        address = by_label(self.accounts, "half-one")["address"].lower()
        self.assertEqual(next(r for r in rows if r["eth_address"] == address)["total_balance"], "2")
        self.assertRowFails(self.edit_snapshot("half-one", {"total_balance": "1"}, "snapshot.csv"),
                            "snapshot_public.values", "half-one")

    def test_round_one_boundaries(self):
        engine = core.Engine(core.load_rules(), core.load_snapshot())
        ctx = core.RowContext({})
        cases = {0: 0, fx.ATTO // 2 - 1: 0, fx.ATTO // 2: 1, fx.ATTO + fx.ATTO // 2 - 1: 1, fx.ATTO + fx.ATTO // 2: 2}
        for atto, whole in cases.items():
            self.assertEqual(engine.eval({"round_one": {"int": str(atto)}}, ctx), whole)

    def test_public_inclusion_and_row_counts(self):
        self.build()
        address = by_label(self.accounts, "initial")["address"].lower()
        path = self.snapshot_path("snapshot-small.csv")
        rows = [row for row in fx.read_csv(path) if row["eth_address"] != address]
        fx.write_csv(path, fx.PUBLIC_FIELDS, rows)
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertRowFails(report, "snapshot_small.inclusion", "initial")
        self.assertEqual(statuses(report, "totals.snapshot_small_rows"), [core.FAIL])
        self.assertEqual(statuses(report, "summary.snapshot"), [core.FAIL])

    def test_census_dust_must_be_excluded(self):
        self.build()
        dust = fx.address_for("census-dust").lower()
        fx.append_csv_row(self.snapshot_path(), {field: "" for field in fx.SNAPSHOT_FIELDS} | {
            "one1_address": fx.lib.hex_to_bech32(dust), "eth_address": dust, "row_source": "wone_census",
            "is_contract": "false", "is_validator": "false", "incident_deduction_scope": "none",
            "activity_coverage": "not_collected_below_1_one", "wone_balance_atto": "5", "total_balance_atto": "5",
            "wone_not_delivered_atto": "5", "non_issuing_atto": "5", "qualified_1000_one": "false",
            **{field: "0" for field in ("liquid_shard0_atto", "liquid_shard1_atto", "self_stake_atto", "delegated_atto",
                                          "pending_undelegation_atto", "unclaimed_reward_atto", "pending_cross_shard_atto",
                                          "native_total_atto")},
        })
        rows = sorted(fx.read_csv(self.snapshot_path()), key=lambda row: row["one1_address"])
        fx.write_csv(self.snapshot_path(), fx.SNAPSHOT_FIELDS, rows)
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertEqual(statuses(report, "totals.snapshot_closure"), [core.FAIL])

    def test_summary_reconciliation(self):
        self.build()
        path = self.bundle / "summaries" / "snapshot-summary.json"
        summary = json.loads(path.read_text())
        summary["reconciliation"]["ledger_rows"] = {"value": 1, "expected": 1}
        fx.write_json(path, summary)
        fx.refreeze(self.bundle)
        report = self.verify()
        self.assertEqual(statuses(report, "summary.snapshot"), [core.FAIL])
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)


@unittest.skipIf(NODE is None, "node is not installed; the HTML engine conformance test needs it")
class EngineConformanceTest(BundleCase):
    """The HTML's JavaScript engine must reach exactly the CLI's verdicts."""

    def compare(self, **options):
        size = options.pop("sample_size", len(self.accounts))
        seed = options.pop("seed", "20260925")
        python = core.verify_bundle(self.bundle, sample_size=size, seed=seed, **options)
        args = ["--bundle", str(self.bundle), "--sample-size", str(size), "--seed", seed]
        if options.get("sample_by"):
            args += ["--sample-by", options["sample_by"]]
        if options.get("verify_all_metadata"):
            args.append("--verify-all-metadata")
        if options.get("require_chain_truth"):
            args.append("--require-chain-truth")
        result = subprocess.run([NODE, str(NODE_RUNNER), *args], capture_output=True, text=True)
        self.assertIn(result.returncode, (0, 1, 3), result.stderr)
        javascript = json.loads(result.stdout)

        def keys(report):
            return sorted((c["scope"], c["id"], c["subject"], c["status"], c["required"]) for c in report["checks"])

        self.assertEqual(keys(javascript), keys(python))
        self.assertEqual(javascript["sample"]["identifiers"], python["sample"]["identifiers"])
        self.assertEqual(javascript["tiers"], python["tiers"])
        self.assertEqual(javascript["totals"], python["totals"])
        self.assertEqual(javascript["exit_code"], python["exit_code"])
        self.assertEqual(javascript["rules_sha256"], python["rules_sha256"])
        self.assertEqual(result.returncode, python["exit_code"])
        return python

    def test_clean_bundle(self):
        self.build()
        self.compare()
        self.compare(sample_size=4, sample_by="address", verify_all_metadata=True)

    def test_mutated_bundles(self):
        self.build()
        acct = by_label(self.accounts, "initial")
        fx.edit_csv(self.overlay_path(), "secure_key", acct["key"],
                    {"liquid_total_atto": "7", "pending_undelegation_atto": "-5"})
        path = self.bundle / "routing" / "routing-exceptions.csv"
        fx.edit_csv(path, "source_secure_key", self.key("incident"), {"migration_stage": "deferred"})
        fx.refreeze(self.bundle)
        (self.bundle / "allocation" / "vault-shares.csv").unlink()
        stage = self.bundle / "policy" / "migration-stage-policy.csv"
        stage.write_text(stage.read_text().replace("wallet activity", "wallet  activity"))
        report = self.compare()
        self.assertEqual(report["exit_code"], core.EXIT_FAIL)

    def test_audit_edge_cases(self):
        self.build()
        rows = fx.read_csv(self.overlay_path())
        twin = dict(rows[0])
        twin["address"] = twin["address_or_secure_key"] = fx.address_for("twin")
        rows.insert(1, twin)
        fx.write_csv(self.overlay_path(), fx.WONE_VERIFY.MIGRATION_FIELDS, rows)
        routing = self.bundle / "routing" / "routing-exceptions.csv"
        routing.write_text(routing.read_text().replace("fixture, synthetic", "x" * 200_000, 1))
        stage = self.bundle / "policy" / "migration-stage-policy.csv"
        fx.edit_csv(stage, "secure_key", self.key("initial"), {"last_activity_time_utc": "2026-09-11T00:00:00Z"})
        fx.refreeze(self.bundle)
        distribution = self.bundle / "airdrop" / "distribution.csv"
        distribution.write_text(distribution.read_text() + "\n0xabc\n")
        manifest_path = self.bundle / core.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        manifest["chain_ids"]["shard1"] = float(manifest["chain_ids"]["shard1"])
        manifest["files"].append(dict(manifest["files"][0], path="./" + manifest["files"][0]["path"]))
        fx.write_json(manifest_path, manifest)
        (self.bundle / "claims" / core.MANIFEST_NAME).write_text("{}\n")
        self.compare(sample_by="address")
        self.compare(sample_size=5)

    def test_snapshot_edge_cases(self):
        self.build()
        address = by_label(self.accounts, "exchange")["address"].lower()
        fx.edit_csv(self.bundle / "snapshot" / "full-snapshot.csv", "eth_address", address,
                    {"migration_stage": "initial", "one1_address": fx.lib.hex_to_bech32(fx.address_for("x"))})
        half = by_label(self.accounts, "half-one")["address"].lower()
        fx.edit_csv(self.bundle / "snapshot" / "snapshot.csv", "eth_address", half, {"total_balance": "1"})
        fx.refreeze(self.bundle)
        self.compare()
        self.compare(sample_size=4, verify_all_metadata=True)

    def test_invalid_utf8_midway(self):
        self.build()
        path = self.bundle / "allocation" / "wallet-allocations.csv"
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2] + b"\xff" + data[len(data) // 2:])
        fx.refreeze(self.bundle)
        report = self.compare()
        self.assertEqual(statuses(report, "file.schema", "allocation/wallet-allocations.csv"), [core.FAIL])

    def test_malformed_quoting_and_evidence(self):
        doc = fx.header_evidence("archive-db", records=fx.account_evidence(fx.default_accounts()))
        doc["block_headers"][0]["number"] = float(doc["block_headers"][0]["number"])
        self.build(evidence=[doc, fx.header_evidence("archive-db-2")])
        path = self.bundle / "routing" / "routing-exceptions.csv"
        path.write_text(path.read_text() + '"a"b,c\n')
        fx.refreeze(self.bundle)
        self.compare()

    def test_evidence_bundles(self):
        accounts = fx.default_accounts()
        records = fx.account_evidence(accounts)
        broken = fx.header_evidence("archive-db-2")
        del broken["retrieved_utc"]
        self.build(accounts=accounts, evidence=[
            fx.header_evidence("archive-db", records=records),
            fx.header_evidence("public-rpc", shard0_hash="0x" + "99" * 32),
            broken,
        ])
        self.compare(require_chain_truth=True)


class HtmlBuildTest(unittest.TestCase):
    def test_generated_html_is_current_and_offline(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build-sample-verifier-html.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        html = (ROOT / "toolkit" / "verifier" / "random-sample-verifier.html").read_text()
        self.assertIn("default-src 'none'", html)
        self.assertIn("connect-src 'none'", html)
        # The embedded snapshot is data (it cites the Harmony source repository); scan everything else.
        code = "\n".join(line for line in html.splitlines() if "const SNAPSHOT_TEXT = " not in line)
        for needle in ("http://", "https://", "<script src", "<link ", "fetch(", "XMLHttpRequest", "import(", "WebSocket"):
            self.assertFalse(needle in code, f"offline page references {needle}")


if __name__ == "__main__":
    unittest.main()
