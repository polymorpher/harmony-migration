#!/usr/bin/env python3
"""Write claim-tree.json: a small list built with the Python tree code, for TreeFixture.t.sol.

    forge build && python3 tools/build_fixture.py        # from bridge-claims/

The Solidity test claims every row against a contract deployed with this root, recomputes the
CREATE2 address from the compiled bytecode, and compares the deployed code with the runtime code
the tools rebuild from the artifact. Rerun this after any change to the contract.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claim_tree as ct  # noqa: E402

FIXTURE = ct.PROJECT / "test" / "fixtures" / "claim-tree.json"

CHAIN_ID = 31337
ROWS = 37
FUNDER = "0x" + "f0" * 20
WRAPPED = "0x" + "e0" * 20
TOKENS = [ct.NATIVE_TOKEN, "0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20]
SALT = "0x" + "5a" * 32
LIST_HASH = bytes.fromhex("11" * 32)


def main():
    rows = []
    for i in range(ROWS):
        account = "0x" + ct.common.keccak256(f"account-{i % 29}".encode())[12:].hex()
        token = TOKENS[i % len(TOKENS)]
        amount = 1 + int.from_bytes(ct.common.keccak256(f"amount-{i}".encode()), "big") % 10**24
        rows.append((i, account, token, amount))
    tree = ct.levels(ct.leaves(rows))
    root = ct.root(tree)
    artifact = ct.load_artifact()
    values = (FUNDER, WRAPPED, CHAIN_ID, root, ROWS, LIST_HASH)
    init_code = artifact["creation_code"] + ct.constructor_args(*values)
    fixture = {
        "chain_id": CHAIN_ID,
        "funder": FUNDER,
        "wrapped_native": WRAPPED,
        "tokens": TOKENS[1:],
        "root": ct.hex0x(root),
        "claim_count": ROWS,
        "list_hash": ct.hex0x(LIST_HASH),
        "accounts": [r[1] for r in rows],
        "row_tokens": [r[2] for r in rows],
        "amounts": [str(r[3]) for r in rows],
        "leaves": [ct.hex0x(n) for n in tree[0]],
        "proofs": [[ct.hex0x(n) for n in ct.proof(tree, i)] for i in range(ROWS)],
        "salt": SALT,
        "create2_deployer": ct.CREATE2_DEPLOYER,
        "init_code_keccak256": ct.hex0x(ct.common.keccak256(init_code)),
        "address": ct.create2_address(ct.CREATE2_DEPLOYER, ct.unhex(SALT), init_code),
        "runtime_keccak256": ct.hex0x(ct.common.keccak256(ct.runtime_code(artifact, *values))),
    }
    FIXTURE.write_text(json.dumps(fixture, indent=1) + "\n")
    print(f"wrote {FIXTURE}: root {fixture['root']}, address {fixture['address']}")


if __name__ == "__main__":
    main()
