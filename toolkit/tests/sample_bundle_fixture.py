"""Synthetic, internally consistent evidence bundles for verifier tests.

The fixture computes every derived field from its own components so tests do
not depend on the verifier's rules to produce valid input.
"""

import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "toolkit" / "verifier"))
import sample_core as core  # noqa: E402

WONE_VERIFY = core.WONE
lib = core.lib
airdrop_common = core.airdrop_common

ATTO = 10**18
THRESHOLD = 1000 * ATTO
SNAPSHOT = core.load_snapshot()
SHARD0_BLOCK = SNAPSHOT["cutoff"]["shard0"]["block"]
SHARD1_BLOCK = SNAPSHOT["cutoff"]["shard1"]["block"]
PRICE = SNAPSHOT["valuation"]["usd_per_one"]
PRICE_BLOCK = SNAPSHOT["valuation"]["reference_shard0_block"]
EMPTY_CODE = WONE_VERIFY.EMPTY_CODE_HASH
CONTRACT_CODE = "0x" + "ab" * 32
WONE_ADDRESS = WONE_VERIFY.WONE_ADDRESS
EXCHANGE_REASON = "exchange_manual_reserve_delivery"
RECENT = "2026-08-01T00:00:00Z"
OLD = "2025-12-01T00:00:00Z"


def load_script(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_script("build_evidence_bundle", "toolkit/scripts/build-evidence-bundle.py")
STAGE_BUILDER = load_script(
    "build_migration_stage_policy",
    "toolkit/scripts/claims/build-migration-stage-policy.py",
)
MATERIALIZE = load_script(
    "materialize_initial_stage",
    "toolkit/scripts/routing/materialize-initial-stage.py",
)


def address_for(label):
    raw = hashlib.sha256(f"sample-fixture|{label}".encode()).hexdigest()[:40]
    return lib.to_checksum("0x" + raw)


def secure_key(address):
    return "0x" + lib.keccak256(bytes.fromhex(address[2:].lower())).hex()


VALIDATOR = address_for("validator-vault")
EXCHANGE_DESTINATION = address_for("exchange-destination")


def account(label, kind="wallet", *, address=None, l0=0, l1=0, active=0, undelegating=0,
            reward=0, cross=0, wone=0, native=True, activity="", existing=0, l0_atto=None):
    address = address or address_for(label)
    return {
        "label": label,
        "kind": kind,
        "address": address,
        "key": secure_key(address),
        "l0": l0 * ATTO if l0_atto is None else l0_atto,
        "l1": l1 * ATTO,
        "active": active * ATTO,
        "undelegating": undelegating * ATTO,
        "reward": reward * ATTO,
        "cross": cross * ATTO,
        "wone": wone * ATTO,
        "native": native,
        "activity": activity,
        "existing": existing * ATTO,
    }


def default_accounts():
    accounts = [
        account("initial", l0=900, l1=50, active=100, reward=3, activity=RECENT),
        account("below", l0=10),
        account("exact", l0=900, wone=100),
        account("just-below", l0_atto=THRESHOLD - 1),
        account("wone-only", wone=1500, native=False, activity=RECENT),
        account("exchange", "exchange", l0=5000, active=200, activity=RECENT),
        account("exchange-small", "exchange", l0=10, wone=5),
        account("smartvault", "smartvault", l0=2000, active=300),
        account("incident", l0=1500, existing=200, activity=RECENT),
        account("incident-below", l0=1100, existing=200, activity=RECENT),
        account("validator", "validator", l0=3000, activity=RECENT),
        account("wone-contract", "wone_contract", address=lib.to_checksum(WONE_ADDRESS), l0=2000),
        account("holder-small", l0=1, wone=3),
        account("undelegating", l0=950, undelegating=40, cross=20, activity=OLD),
        account("excluded-holder", "excluded", l0=2, wone=7),
    ]
    for index in range(12):
        if index % 2 == 0:
            accounts.append(account(f"filler-{index}", l0=1000 + index, activity=RECENT))
        else:
            accounts.append(account(f"filler-{index}", l0=index))
    return accounts


def fixed(value):
    return WONE_VERIFY.fixed(value)


def usd(value):
    return WONE_VERIFY.usd_value(value, PRICE, "fixture")


def derive(acct, redistributed_total=None):
    native_wallet = acct["l0"] + acct["l1"] + acct["undelegating"] + acct["reward"] + acct["cross"]
    staked = acct["active"]
    native_total = native_wallet + staked
    wone = 0 if acct["kind"] in ("excluded", "wone_contract") else acct["wone"]
    qualification = native_total + wone
    exchange = acct["kind"] == "exchange"
    qualifies = qualification >= THRESHOLD
    wone_airdrop = wone if (qualifies or exchange) else 0
    wallet = native_wallet + wone_airdrop
    return {
        "native_wallet": native_wallet,
        "staked": staked,
        "native_total": native_total,
        "wone_balance": wone,
        "wone_airdrop": wone_airdrop,
        "wallet": wallet,
        "total": wallet + staked,
        "qualification": qualification,
        "qualifies": qualifies,
        "exchange": exchange,
    }


def native_row(acct):
    d = derive(acct)
    code = CONTRACT_CODE if acct["kind"] in ("smartvault", "wone_contract", "validator") else EMPTY_CODE
    values = {
        "liquid_shard0": acct["l0"],
        "liquid_shard1": acct["l1"],
        "liquid_total": acct["l0"] + acct["l1"],
        "active_staked_or_delegated": acct["active"],
        "pending_undelegation": acct["undelegating"],
        "unclaimed_staking_reward": acct["reward"],
        "pending_cross_shard": acct["cross"],
        "wallet_airdrop": d["native_wallet"],
        "staked_to_vault": d["staked"],
        "total_claim": d["native_total"],
    }
    row = {
        "secure_key": acct["key"],
        "address": acct["address"],
        "address_or_secure_key": acct["address"],
        "address_resolved": "true",
        "claims_shard0_block": str(SHARD0_BLOCK),
        "claims_shard1_block": str(SHARD1_BLOCK),
        "valuation_price_reference_shard0_block": str(PRICE_BLOCK),
        "valuation_price_usd_per_one": PRICE,
        "wallet_airdrop_usd": usd(d["native_wallet"]),
        "total_usd": usd(d["native_total"]),
        "nonce_shard0": "1",
        "nonce_shard1": "",
        "code_hash_shard0": code,
        "code_hash_shard1": "",
    }
    for name, value in values.items():
        row[f"{name}_atto"] = str(value)
        row[f"{name}_one"] = fixed(value)
    return {field: row[field] for field in WONE_VERIFY.NATIVE_FIELDS}


def overlay_row(acct):
    d = derive(acct)
    if acct["native"]:
        row = dict(native_row(acct))
    else:
        row = {
            "secure_key": acct["key"],
            "address": acct["address"],
            "address_or_secure_key": acct["address"],
            "address_resolved": "true",
            "claims_shard0_block": str(SHARD0_BLOCK),
            "claims_shard1_block": str(SHARD1_BLOCK),
            "valuation_price_reference_shard0_block": str(PRICE_BLOCK),
            "valuation_price_usd_per_one": PRICE,
            "nonce_shard0": "0",
            "nonce_shard1": "",
            "code_hash_shard0": EMPTY_CODE,
            "code_hash_shard1": "",
        }
        for name in ("liquid_shard0", "liquid_shard1", "liquid_total", "active_staked_or_delegated",
                     "pending_undelegation", "unclaimed_staking_reward", "pending_cross_shard"):
            row[f"{name}_atto"] = "0"
            row[f"{name}_one"] = fixed(0)
    extra = {
        "native_wallet_airdrop": d["native_wallet"],
        "wone_balance": d["wone_balance"],
        "wone_airdrop": d["wone_airdrop"],
        "wallet_airdrop": d["wallet"],
        "staked_to_vault": d["staked"],
        "qualification_total": d["qualification"],
        "native_total_claim": d["native_total"],
        "total_claim": d["total"],
    }
    for name, value in extra.items():
        row[f"{name}_atto"] = str(value)
        row[f"{name}_one"] = fixed(value)
    row["wallet_airdrop_usd"] = usd(d["wallet"])
    row["total_usd"] = usd(d["total"])
    return {field: row[field] for field in WONE_VERIFY.MIGRATION_FIELDS}


def eligibility_category(acct):
    if acct["kind"] == "exchange":
        return "excluded_address"
    if acct["kind"] in ("smartvault", "wone_contract"):
        return "contract_review"
    return "automatic"


def stage_row(acct):
    d = derive(acct)
    gross = d["total"]
    existing = acct["existing"]
    wone_offset = acct["l0"] if acct["kind"] == "wone_contract" else 0
    before = gross - existing - wone_offset
    contract = acct["kind"] in ("smartvault", "wone_contract")
    contract_non_issuance = before if contract else 0
    before_wallet, before_staked = STAGE_BUILDER.split_final_components(d["wallet"], d["staked"], before)
    if contract:
        classification = "genuine_contract"
        group = "smartvault" if acct["kind"] == "smartvault" else "other_reviewed_contract"
        routing = "contract_policy"
        allocation = 0
        stage = ""
        reason = "reviewed contract allocation retained in 2050 premint reserve"
        category = "smartvault-wallet" if acct["kind"] == "smartvault" else "other"
    else:
        classification = "validator_wallet" if acct["kind"] == "validator" else "wallet"
        group = classification
        routing = "exchange_or_manual" if acct["kind"] == "exchange" else "automatic_policy"
        allocation = before
        category = ""
        activity = STAGE_BUILDER.parse_utc(acct["activity"]) if acct["activity"] else None
        since = STAGE_BUILDER.parse_utc("2026-03-10T14:00:00Z")
        stage, reason, _meets = STAGE_BUILDER.wallet_stage(
            allocation, d["qualification"] - existing, activity, since, 6
        )
    wallet, staked = STAGE_BUILDER.split_final_components(d["wallet"], d["staked"], allocation)
    return {
        "secure_key": acct["key"],
        "address": acct["address"].lower(),
        "account_classification": classification,
        "contract_category": category,
        "policy_group": group,
        "routing_category": routing,
        "migration_stage": stage,
        "issuance_treatment": "issue" if allocation > 0 else "not_issued",
        "qualification_total_atto": str(d["qualification"]),
        "gross_allocation_atto": str(gross),
        "existing_non_issuance_atto": str(existing),
        "historical_retained_cap_atto": "0",
        "wone_source_offset_atto": str(wone_offset),
        "reviewed_contract_non_issuance_atto": str(contract_non_issuance),
        "reviewed_contract_wallet_non_issuance_atto": str(before_wallet if contract else 0),
        "reviewed_contract_staked_non_issuance_atto": str(before_staked if contract else 0),
        "migration_wallet_allocation_atto": str(wallet),
        "migration_staked_to_vault_atto": str(staked),
        "migration_allocation_atto": str(allocation),
        "last_activity_time_utc": acct["activity"],
        "stage_reason": reason,
    }


def routing_row(acct, component, amount, stage, treatment, status, *, destination="",
                reason="", exception_type="explicit_route", route_id="route", category="ordinary_eoa"):
    return {
        "component": component,
        "source_secure_key": acct["key"],
        "source_address": acct["address"],
        "source_category": category,
        "source_code_bearing": "true" if acct["kind"] in ("smartvault", "wone_contract", "validator") else "false",
        "migration_stage": stage,
        "issuance_treatment": treatment,
        "validator_secure_key": secure_key(VALIDATOR) if component == "vault_shares" else "",
        "validator_address": VALIDATOR if component == "vault_shares" else "",
        "amount_atto": str(amount),
        "exception_type": exception_type,
        "route_id": route_id,
        "route_priority": "100",
        "destination_id": "",
        "destination_address": destination,
        "destination_status": status,
        "reason": reason,
        "evidence": "fixture, synthetic",
    }


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def pipeline_outputs(directory, accounts=None, *, airdrop=True):
    """Write every pipeline output for the accounts; return builder arguments."""
    directory = Path(directory)
    accounts = sorted(default_accounts() if accounts is None else accounts, key=lambda item: item["key"])
    derived = {acct["key"]: derive(acct) for acct in accounts}
    qualified = [acct for acct in accounts if derived[acct["key"]]["qualifies"]]
    stages = {acct["key"]: stage_row(acct) for acct in qualified}
    redistributed = sum(item["wone_airdrop"] for item in derived.values())

    native = [native_row(acct) for acct in accounts if acct["native"]]
    overlay = [overlay_row(acct) for acct in accounts]
    write_csv(directory / "native.csv", WONE_VERIFY.NATIVE_FIELDS, native)
    write_csv(directory / "overlay.csv", WONE_VERIFY.MIGRATION_FIELDS, overlay)
    excluded = sorted({*WONE_VERIFY.LAYERZERO_ADDRESSES, WONE_ADDRESS,
                       *(acct["address"].lower() for acct in accounts if acct["kind"] == "excluded")})
    write_json(directory / "overlay-summary.json", {
        "output_sha256": core.file_sha256(directory / "overlay.csv"),
        "excluded_addresses_requested": excluded,
    })
    overlay_by_key = {row["secure_key"]: row for row in overlay}
    categories = {"automatic": [], "contract_review": [], "excluded_address": []}
    for acct in qualified:
        categories[eligibility_category(acct)].append(overlay_by_key[acct["key"]])
    eligibility_args = []
    for category, rows in categories.items():
        path = directory / f"eligibility-{category}.csv"
        write_csv(path, WONE_VERIFY.MIGRATION_FIELDS, rows)
        eligibility_args.append(f"{category}={path}")
    write_csv(directory / "stage.csv", STAGE_BUILDER.OUTPUT_FIELDS, [stages[a["key"]] for a in qualified])

    routing = []
    for acct in accounts:
        d = derived[acct["key"]]
        stage = stages.get(acct["key"])
        if acct["kind"] == "exchange":
            if d["wallet"]:
                routing.append(routing_row(acct, "wallet_airdrop", d["wallet"], "exchange_manual",
                                           "manual_from_reserve", "exchange_manual", destination=EXCHANGE_DESTINATION,
                                           reason=EXCHANGE_REASON, category="excluded"))
            if d["staked"]:
                routing.append(routing_row(acct, "vault_shares", d["staked"], "exchange_manual",
                                           "manual_from_reserve", "exchange_manual", destination=EXCHANGE_DESTINATION,
                                           reason=EXCHANGE_REASON, category="excluded"))
        elif acct["kind"] == "smartvault":
            routing.append(routing_row(acct, "wallet_airdrop", int(stage["reviewed_contract_wallet_non_issuance_atto"]),
                                       "", "not_issued", "not_issuing", reason="reviewed_contract_allocation_not_issued",
                                       category="contract_review"))
            routing.append(routing_row(acct, "vault_shares", int(stage["reviewed_contract_staked_non_issuance_atto"]),
                                       "", "not_issued", "not_issuing", reason="reviewed_contract_allocation_not_issued",
                                       category="contract_review"))
        elif acct["kind"] == "wone_contract":
            routing.append(routing_row(acct, "wallet_airdrop", redistributed, "", "redistributed", "redistributed",
                                       reason="wone_holder_delivery_redistribution",
                                       route_id="wone-priority-holder-redistribution", category="contract_review"))
            routing.append(routing_row(acct, "wallet_airdrop", acct["l0"] - redistributed, "", "not_issued",
                                       "not_issuing", reason="wone_reserve_remainder_retained_not_issued",
                                       route_id="wone-reserve-remainder-not-issued", category="contract_review"))
        elif acct["kind"] == "validator" and stage:
            routing.append(routing_row(acct, "wallet_airdrop", int(stage["migration_wallet_allocation_atto"]),
                                       stage["migration_stage"], "issue", "ready", destination=acct["address"],
                                       reason="verified_validator_account", exception_type="validator_same_address",
                                       category="validator_account"))
        if acct["existing"] and stage:
            routing.append(routing_row(acct, "wallet_airdrop", acct["existing"], stage["migration_stage"],
                                       "not_issued", "not_issuing", reason="existing_non_issuance"))
    write_csv(directory / "routing.csv", WONE_VERIFY.ROUTING_EXCEPTION_FIELDS, routing)

    wallets, shares, delegations, holders, exchanges = [], [], [], [], []
    for acct in accounts:
        d = derived[acct["key"]]
        stage = stages.get(acct["key"])
        if d["staked"]:
            delegations.append({
                "validator_address": VALIDATOR, "validator_secure_key": secure_key(VALIDATOR),
                "delegator_address": acct["address"], "delegator_secure_key": acct["key"],
                "staked_to_vault_atto": str(d["staked"]),
            })
        if acct["wone"]:
            holders.append({"address": acct["address"], "wone_balance_atto": str(acct["wone"]),
                            "wone_balance": fixed(acct["wone"])})
        if acct["kind"] == "exchange":
            exchanges.append({"address_hex": acct["address"]})
        if not stage or stage["migration_stage"] != "initial" or acct["kind"] == "exchange":
            continue
        wallet = int(stage["migration_wallet_allocation_atto"])
        explicit = [row for row in routing if row["source_secure_key"] == acct["key"]
                    and row["component"] == "wallet_airdrop" and row["issuance_treatment"] == "issue"]
        for row in explicit:
            wallets.append({
                "source_secure_key": acct["key"], "source_address": acct["address"], "destination_id": "",
                "destination_address": row["destination_address"], "destination_status": row["destination_status"],
                "amount_atto": row["amount_atto"], "delivery_method": "explicit_route",
                "reason": row["reason"], "evidence": row["evidence"],
            })
        implicit = wallet - sum(int(row["amount_atto"]) for row in explicit)
        if implicit:
            wallets.append({
                "source_secure_key": acct["key"], "source_address": acct["address"], "destination_id": "",
                "destination_address": acct["address"], "destination_status": "ready",
                "amount_atto": str(implicit), "delivery_method": "implicit_same_address",
                "reason": "ordinary code-less wallet", "evidence": "",
            })
        staked = int(stage["migration_staked_to_vault_atto"])
        if staked:
            shares.append({
                "validator_secure_key": secure_key(VALIDATOR), "validator_address": VALIDATOR,
                "source_secure_key": acct["key"], "source_address": acct["address"],
                "beneficiary_address": acct["address"], "destination_id": "", "destination_status": "ready",
                "amount_atto": str(staked), "delivery_method": "implicit_same_address",
                "reason": "ordinary wallet vault shares", "evidence": "",
            })
    write_csv(directory / "wallets.csv", MATERIALIZE.WALLET_FIELDS, wallets)
    write_csv(directory / "shares.csv", MATERIALIZE.SHARE_FIELDS, shares)
    write_csv(directory / "delegations.csv",
              ("validator_address", "validator_secure_key", "delegator_address", "delegator_secure_key",
               "staked_to_vault_atto"), delegations)
    write_csv(directory / "holders.csv", WONE_VERIFY.HOLDER_FIELDS, holders)
    write_csv(directory / "exchanges.csv", ("address_hex",), exchanges)

    stage_hash = core.file_sha256(directory / "stage.csv")
    write_json(directory / "stage-summary.json", {"output_sha256": stage_hash, "qualified_rows": len(qualified)})
    write_json(directory / "routing-summary.json", {
        "migration_stages_sha256": stage_hash,
        "outputs": {"routing_exceptions": {"sha256": core.file_sha256(directory / "routing.csv")}},
    })
    write_json(directory / "eligibility-summary.json", {
        "comparison": "ge",
        "minimum_atto": str(THRESHOLD),
        "categories": {
            category: {
                "rows": len(rows),
                "output_sha256": core.file_sha256(directory / f"eligibility-{category}.csv"),
            }
            for category, rows in categories.items()
        },
    })
    sources = [
        {"id": "pipeline", "kind": "pipeline", "description": "toolkit pipeline outputs",
         "authoritative": True, "retrieved_utc": "2026-09-20T00:00:00Z"},
        {"id": "archive-db", "kind": "archival_database", "description": "operator archival database",
         "authoritative": True, "retrieved_utc": "2026-09-11T00:00:00Z"},
        {"id": "archive-db-2", "kind": "archived_block_header", "description": "second archived header copy",
         "authoritative": True, "retrieved_utc": "2026-09-11T00:00:00Z"},
        {"id": "public-rpc", "kind": "archival_rpc", "description": "public archival RPC",
         "authoritative": False, "retrieved_utc": "2026-09-12T00:00:00Z"},
    ]
    write_json(directory / "sources.json", sources)

    args = [
        "--bundle-id", "fixture-bundle",
        "--created-utc", "2026-09-25T00:00:00Z",
        "--sources", str(directory / "sources.json"),
        "--default-source", "pipeline",
        "--native-claims", str(directory / "native.csv"),
        "--wone-overlay", str(directory / "overlay.csv"),
        "--stage-policy", str(directory / "stage.csv"),
        "--routing-exceptions", str(directory / "routing.csv"),
        "--wallet-allocations", str(directory / "wallets.csv"),
        "--vault-shares", str(directory / "shares.csv"),
        "--vault-delegations", str(directory / "delegations.csv"),
        "--wone-holders", str(directory / "holders.csv"),
        "--exchange-wallets", str(directory / "exchanges.csv"),
        "--stage-summary", str(directory / "stage-summary.json"),
        "--routing-summary", str(directory / "routing-summary.json"),
        "--eligibility-summary", str(directory / "eligibility-summary.json"),
        "--wone-overlay-summary", str(directory / "overlay-summary.json"),
    ]
    for item in eligibility_args:
        args += ["--eligibility", item]
    if airdrop:
        ready = {}
        for row in wallets:
            if row["destination_status"] == "ready":
                address = lib.to_checksum(row["destination_address"].lower())
                ready[address] = ready.get(address, 0) + int(row["amount_atto"])
        rows = [airdrop_common.Row(address, amount, "fixture") for address, amount in sorted(ready.items())]
        airdrop_common.write_run(
            directory / "airdrop-run", rows, 4,
            {"sources": [], "address_column": "address", "amount_column": "amount_atto", "amount_unit": "atto"},
            "fixture", "input",
        )
        args += ["--airdrop-run", str(directory / "airdrop-run"), "--airdrop-scope", "complete"]
    return args, accounts


def header_evidence(source_id, *, shard0_hash=None, parent_hash="0x" + "11" * 32, records=()):
    headers = []
    for shard in (0, 1):
        pinned = SNAPSHOT["cutoff"][f"shard{shard}"]
        headers.append({
            "shard": shard,
            "number": pinned["block"],
            "hash": (shard0_hash if shard == 0 and shard0_hash else pinned["hash"]),
            "parent_hash": parent_hash,
            "state_root": pinned["state_root"],
            "transactions_root": "0x" + "22" * 32,
            "timestamp": core.utc_to_unix(pinned["timestamp_utc"]),
        })
    return {
        "format": "harmony-historical-evidence/v1",
        "source_id": source_id,
        "retrieved_utc": "2026-09-11T00:00:00Z",
        "block_headers": headers,
        "accounts": list(records),
    }


def account_evidence(accounts):
    records = []
    for acct in accounts:
        records.append({
            "shard": 0, "block": SHARD0_BLOCK, "address": acct["address"],
            "balance_atto": str(acct["l0"]),
            "active_staked_or_delegated_atto": str(acct["active"]),
            "pending_undelegation_atto": str(acct["undelegating"]),
            "unclaimed_staking_reward_atto": str(acct["reward"]),
            "pending_cross_shard_atto": str(acct["cross"]),
            "wone_balance_atto": str(acct["wone"]),
        })
        records.append({"shard": 1, "block": SHARD1_BLOCK, "address": acct["address"],
                        "balance_atto": str(acct["l1"])})
    return records


def build_bundle(directory, accounts=None, *, airdrop=True, evidence=None, extra_args=()):
    """Build a frozen bundle under directory/bundle and return its path."""
    directory = Path(directory)
    args, accounts = pipeline_outputs(directory / "pipeline", accounts, airdrop=airdrop)
    for index, doc in enumerate(evidence or []):
        path = directory / "pipeline" / f"evidence-{index}.json"
        write_json(path, doc)
        args += ["--historical-evidence", str(path)]
    bundle = directory / "bundle"
    BUILDER.build(BUILDER.parse_args(["--output", str(bundle), *args, *extra_args]))
    return bundle, accounts


def refreeze(bundle):
    """Re-hash files and totals after a deliberate edit (simulates a consistent but wrong bundle)."""
    bundle = Path(bundle)
    manifest = json.loads((bundle / core.MANIFEST_NAME).read_text())
    for entry in manifest["files"]:
        path = bundle / entry["path"]
        entry["sha256"] = core.file_sha256(path)
        entry["bytes"] = path.stat().st_size
        if "rows" in entry:
            try:
                entry["rows"] = core.count_csv_rows(path)
            except (csv.Error, UnicodeDecodeError):
                pass  # deliberately malformed CSV: keep the declared count
    try:
        manifest["declared_totals"] = core.compute_declared_totals(bundle, manifest)
    except ValueError:
        pass  # malformed rows cannot be totalled; keep the previously declared totals
    write_json(bundle / core.MANIFEST_NAME, manifest)


def edit_csv(path, key_field, key, changes):
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    hit = 0
    for row in rows:
        if row[key_field].lower() == key.lower():
            row.update(changes)
            hit += 1
    if not hit:
        raise KeyError(key)
    write_csv(path, fields, rows)


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def append_csv_row(path, row):
    rows = read_csv(path)
    with Path(path).open(newline="", encoding="utf-8") as handle:
        fields = next(csv.reader(handle))
    rows.append(row)
    write_csv(path, fields, rows)
