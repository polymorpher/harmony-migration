#!/usr/bin/env python3

"""Extend contract-review facts from a recorded historical eth_getCode cache."""

import argparse
import csv
import json
import os

import contract_review_lib as lib


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--facts", required=True)
    parser.add_argument("--claims", required=True)
    parser.add_argument("--code-cache", required=True)
    parser.add_argument(
        "--delegations",
        required=True,
        help="per-validator delegation CSV from historical validator RPC",
    )
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    if os.path.exists(args.output) or os.path.exists(args.output + ".partial"):
        raise FileExistsError(args.output)
    with open(args.facts, encoding="utf-8") as source:
        facts = json.load(source)
    with open(args.code_cache, encoding="utf-8") as source:
        cache = {
            lib.normalize_address(address): code
            for address, code in json.load(source).items()
        }
    claims = {}
    with open(args.claims, newline="") as source:
        for row in csv.DictReader(source):
            claims[lib.normalize_address(row["address"])] = row
    validator_addresses = set()
    with open(args.delegations, newline="") as source:
        for row in csv.DictReader(source):
            validator_addresses.add(
                lib.normalize_address(row["validator_address"])
            )

    added = 0
    validators = 0
    contracts = 0
    for address, claim in claims.items():
        if address in facts:
            continue
        code = cache.get(address)
        if code in (None, "0x"):
            raise ValueError(f"missing non-empty cached code for {address}")
        raw = bytes.fromhex(code[2:])
        wrapper_address = lib.rlp_validator_wrapper_address(code)
        wrapper_matches = wrapper_address == address
        validator_state_present = address in validator_addresses
        is_validator = wrapper_matches and validator_state_present
        if wrapper_matches != validator_state_present:
            raise ValueError(
                f"validator evidence disagrees for {address}: "
                f"rlp={wrapper_matches} state={validator_state_present}"
            )
        validators += int(is_validator)
        contracts += int(not is_validator)
        facts[address] = {
            "basic": {
                "balance_cutoff_atto": claim["liquid_shard0_atto"],
                "balance_latest_atto": None,
                "bytecode_selectors": [],
                "code_cutoff": code,
                "code_cutoff_hash": "0x" + lib.keccak256(raw).hex(),
                "code_cutoff_len": len(raw),
                "code_latest_len": None,
                "code_latest_same": None,
                "errors": [
                    "imported from recorded cutoff eth_getCode cache; "
                    "latest-state enrichment pending"
                ],
                "latest_block_at_fetch": None,
                "nonce_latest": None,
                "staking_tx_count_all": None,
                "storage_slot0": None,
                "tx_count_all": None,
                "tx_count_received": None,
                "tx_count_sent": None,
            },
            "validator": {
                "is_validator": is_validator,
                "rlp_wrapper_address": wrapper_address,
                "rlp_wrapper_matches": wrapper_matches,
                "validator_state_present": validator_state_present,
                "active_status": (
                    "unknown (cache-imported)" if is_validator else ""
                ),
                "epos_status": "",
                "name": "",
                "identity": "",
                "website": "",
                "creation_height": None,
                "delegation_count": None,
                "bls_key_count": None,
                "error": (
                    None
                    if is_validator
                    else "validator checks failed"
                ),
            },
        }
        added += 1

    with open(args.output + ".partial", "x", encoding="utf-8") as output:
        json.dump(facts, output, indent=1, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(args.output + ".partial", args.output)
    print(
        json.dumps(
            {
                "added": added,
                "validator_wrappers": validators,
                "genuine_contracts": contracts,
                "output": args.output,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
