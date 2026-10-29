"""Rows, leaves, Merkle tree, and CREATE2 address of the BridgeClaims contract.

Must match `src/BridgeClaims.sol` and `test/lib/ClaimTree.sol`; `test/TreeFixture.t.sol` checks a
fixture built here (`tools/build_fixture.py`) against the contract.

- leaf(i) = keccak256(keccak256(abi.encode(uint256 i, address account, address token, uint256 amount)));
  native-coin rows use token address 0.
- The tree is the airdrop's batch tree (`airdrop/tools/common.py`): leaves in index order, each
  pair hashed smaller-first, an unpaired last node carried up unchanged.

Hashing uses the airdrop tools' keccak (pure Python, or pycryptodome when installed). Large trees
are hashed in worker processes.
"""

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parent
sys.path.insert(0, str(REPO / "airdrop" / "tools"))
sys.path.insert(0, str(REPO / "toolkit" / "scripts" / "bridge"))
import common  # noqa: E402

NATIVE_TOKEN = "0x" + "0" * 40
CREATE2_DEPLOYER = "0x4e59b44847b379578588920ca78fbf26c0b4956c"
# keccak256 of the deterministic-deployment proxy's 69-byte runtime code, identical on every chain.
CREATE2_DEPLOYER_CODEHASH = "0x2fa86add0aed31f33a762c9d88e807c475bd51d0f52bd0955754b2608f7e4989"
CHAINS_FILE = PROJECT / "chains.json"
ARTIFACT = PROJECT / "out" / "BridgeClaims.sol" / "BridgeClaims.json"
LEAF_SCHEME = (
    "keccak256(keccak256(abi.encode(uint256 index, address account, address token, uint256 amount))); "
    "token 0x0 is the native coin"
)
TREE_SCHEME = (
    "binary tree over row leaves in index order; pairs hashed keccak256(min || max); "
    "unpaired node carried up"
)
CHUNK = 8192
PARALLEL_MIN = 4 * CHUNK


def _word(value):
    return int(value).to_bytes(32, "big")


def encode_row(index, account, token, amount):
    """Byte-for-byte `abi.encode(index, account, token, amount)`."""
    return _word(index) + _word(int(account, 16)) + _word(int(token, 16)) + _word(amount)


def leaf(index, account, token, amount):
    return common.keccak256(common.keccak256(encode_row(index, account, token, amount)))


def _leaf_chunk(rows):
    return b"".join(leaf(*row) for row in rows)


def _pair_chunk(nodes):
    hashed = []
    for offset in range(0, len(nodes), 64):
        a, b = nodes[offset:offset + 32], nodes[offset + 32:offset + 64]
        hashed.append(common.hash_pair(a, b))
    return b"".join(hashed)


def _split(blob, size):
    return [blob[i:i + 32] for i in range(0, len(blob), 32)] if size else []


def default_workers():
    return max(1, min(16, os.cpu_count() or 1))


def _pool(workers, size):
    """A process pool, or None for small inputs or where processes cannot be started."""
    if workers <= 1 or size < PARALLEL_MIN:
        return None
    try:
        return ProcessPoolExecutor(max_workers=workers)
    except (OSError, NotImplementedError) as error:
        print(f"claim_tree: hashing in one process ({error})", file=sys.stderr)
        return None


def leaves(rows, workers=1):
    """Leaves of (index, account, token, amount) rows, in row order."""
    rows = list(rows)
    pool = _pool(workers, len(rows))
    if pool is None:
        return [leaf(*row) for row in rows]
    chunks = [rows[i:i + CHUNK] for i in range(0, len(rows), CHUNK)]
    with pool:
        blob = b"".join(pool.map(_leaf_chunk, chunks))
    return _split(blob, len(rows))


def levels(leaf_list, workers=1):
    """Every level of the tree, leaves first, root last."""
    if not leaf_list:
        raise ValueError("no leaves")
    out = [list(leaf_list)]
    pool = _pool(workers, len(leaf_list))
    try:
        while len(out[-1]) > 1:
            prev = out[-1]
            paired = len(prev) - len(prev) % 2
            if pool is not None and paired >= PARALLEL_MIN:
                step = 2 * CHUNK
                blobs = [b"".join(prev[i:min(i + step, paired)]) for i in range(0, paired, step)]
                nxt = _split(b"".join(pool.map(_pair_chunk, blobs)), paired // 2)
            else:
                nxt = [common.hash_pair(prev[i], prev[i + 1]) for i in range(0, paired, 2)]
            if paired < len(prev):
                nxt.append(prev[-1])
            out.append(nxt)
    finally:
        if pool is not None:
            pool.shutdown()
    return out


def root(tree_levels):
    return tree_levels[-1][0]


def proof(tree_levels, index):
    return common.tree_proof(tree_levels, index)


def verify(proof_nodes, root_node, leaf_node):
    return common.tree_verify(proof_nodes, root_node, leaf_node)


def write_tree(path, tree_levels):
    """One 0x-prefixed node per line, level by level from the leaves to the root."""
    path = str(path)
    partial = path + ".partial"
    with open(partial, "x", encoding="ascii") as output:
        for level in tree_levels:
            output.write("".join("0x" + node.hex() + "\n" for node in level))
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def read_tree(path, level_sizes):
    with open(path, encoding="ascii") as source:
        nodes = [bytes.fromhex(line[2:].strip()) for line in source]
    if len(nodes) != sum(level_sizes):
        raise ValueError(f"{path}: {len(nodes)} nodes, manifest expects {sum(level_sizes)}")
    out, offset = [], 0
    for size in level_sizes:
        out.append(nodes[offset:offset + size])
        offset += size
    return out


def constructor_args(funder, wrapped_native, chain_id, root_node, claim_count, list_hash):
    """`abi.encode` of the BridgeClaims constructor arguments, in declaration order."""
    return (
        _word(int(funder, 16))
        + _word(int(wrapped_native or NATIVE_TOKEN, 16))
        + _word(chain_id)
        + root_node
        + _word(claim_count)
        + list_hash
    )


def load_chains(path=CHAINS_FILE):
    """Chain constants for the claim contracts (`chains.json`): chain id, wrapped native coin, tokens
    whose approval must be reset to 0 first, and the CREATE2 deployer and salt."""
    with open(path, encoding="utf-8") as source:
        chains = json.load(source)
    if len(unhex(chains["salt"])) != 32:
        raise ValueError(f"{path}: salt must be 32 bytes")
    if len(unhex(chains["create2_deployer"])) != 20:
        raise ValueError(f"{path}: create2_deployer must be an address")
    return chains


def create2_address(deployer, salt, init_code):
    digest = common.keccak256(b"\xff" + bytes.fromhex(deployer[2:]) + salt + common.keccak256(init_code))
    return "0x" + digest[12:].hex()


def load_artifact(path=ARTIFACT):
    """The compiled contract (`forge build`): creation code, the runtime code with zeroed immutables,
    and where each immutable is written. solc numbers AST nodes in source order, so ascending node
    ids are the immutables' declaration order, which is also the constructor's argument order."""
    if not Path(path).exists():
        raise SystemExit(f"no bytecode at {path}: run `forge build`")
    with open(path, encoding="utf-8") as source:
        artifact = json.load(source)
    refs = artifact["deployedBytecode"].get("immutableReferences") or {}
    return {
        "creation_code": unhex(artifact["bytecode"]["object"]),
        "runtime_template": unhex(artifact["deployedBytecode"]["object"]),
        "immutables": [[(r["start"], r["length"]) for r in refs[key]] for key in sorted(refs, key=int)],
    }


def runtime_code(artifact, funder, wrapped_native, chain_id, root_node, claim_count, list_hash):
    """The code the constructor leaves at the contract's address: the runtime template with every
    immutable written in."""
    blob = constructor_args(funder, wrapped_native, chain_id, root_node, claim_count, list_hash)
    words = [blob[i:i + 32] for i in range(0, len(blob), 32)]
    if len(artifact["immutables"]) != len(words):
        raise ValueError(f"artifact has {len(artifact['immutables'])} immutables, the constructor sets {len(words)}")
    code = bytearray(artifact["runtime_template"])
    for positions, value in zip(artifact["immutables"], words):
        for start, length in positions:
            if length != 32 or any(code[start:start + 32]):
                raise ValueError(f"no zeroed 32-byte immutable at runtime offset {start}")
            code[start:start + 32] = value
    return bytes(code)


def hex0x(data):
    return "0x" + data.hex()


def unhex(text):
    return bytes.fromhex(text[2:] if text.startswith("0x") else text)
