# Bridge claims

The contract and tools that pay the LayerZero bridge collateral to its payees. The allocation
pipeline (`bridge/`, `docs/bridge-allocations.md`) decides who is owed what on each chain; this
directory turns each chain's claim list into one immutable claim contract that pays from the
chain's custody multisig.

- **One contract per chain** (and per funding multisig) pays every asset of that chain.
- **Nothing changes after deployment.** The funder, the list's Merkle root, the row count, the
  wrapped native coin, the chain id, and the list's SHA-256 are constructor arguments. There is no
  owner, admin, pause, or upgrade.
- **The tokens stay in the multisig.** The multisig approves the contract for exactly each
  asset's total; a claim moves one row's amount from the multisig to the payee. Lowering an
  approval stops that asset; the revoke batch stops everything.
- **Anyone can submit a claim; the tokens always go to the row's account**, once per row.
- **Native ETH and BNB** are paid by unwrapping WETH/WBNB that the multisig approves; an account
  that refuses the coin receives the wrapped token.
- **The address is known before deployment** (CREATE2 through the standard deployer), so the
  approval batch can be signed first and anyone can deploy.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the design and the per-chain decision,
[`docs/RELEASE.md`](docs/RELEASE.md) for releasing the collateral from the bridge escrows to the
custody Safe, and [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for the steps from a verified allocation run
to open claims.

## Layout

```text
src/BridgeClaims.sol         the claim contract
test/BridgeClaims.t.sol      unit and fuzz tests with odd tokens (no return value, false, fee,
                             no code), native payout, fallback, reentry, sweep, batches
test/TreeFixture.t.sol       a list built by the Python tools pays in full against the contract,
                             and the Python CREATE2 address matches the bytecode
test/Fork.t.sol              a run's contract on a fork of the real chain: collateral moved from
                             the real escrows, the Safe's approvals, one claim per asset
test/EscrowRelease.t.sol     a run's release files replayed on a fork of each chain (Safe batches
                             through the real Safe, at the signing sheet's hash): every escrow
                             emptied to the custody Safe, its settings unchanged, approvals funded
script/Deploy.s.sol          CREATE2 deployment from a run manifest, with every check
chains.json                  per-chain constants: wrapped native coin, approval quirks, CREATE2
                             deployer and salt
tools/claim_tree.py          leaves, tree, proofs, CREATE2 address (shared by the tools)
tools/build_claims.py        claim lists -> published list, tree, samples, manifest per contract
tools/approve_claims.py      Safe Transaction Builder batches (approve and revoke) and signing sheet
tools/proof.py               one payee's rows and proofs, and a claimMany command
tools/release_plan.py        the owner transactions that release each escrow on its own chain to
                             the custody Safe, with no cross-chain message (Safe batches, EOA
                             calldata, and a handoff folder per key holder)
tools/send_calldata.py       sends one EOA calldata file with `cast`, simulating each transaction
                             first and logging each receipt so a rerun resumes
tools/package.py             signable packages (approval/, release/): built in an empty staging
                             directory and published whole; a failed build invalidates the old one
tools/quarantine.py          moves a run's rejected signable files to quarantine/ with their hashes
tools/build_fixture.py       regenerates test/fixtures/claim-tree.json
tools/test_tools.py          tests of the tools
runs/                        generated runs (ignored: address-level amounts)
```

The tools reuse `airdrop/tools` (keccak, tree, Safe hashing and Transaction Builder checksum) and
`toolkit/scripts/bridge/bridge_lib.py` (bridge config, RPC). Foundry uses `airdrop/lib/forge-std`.

## Commands

From this directory, after a verified allocation run:

```sh
forge build && forge test                       # contract tests; the fork test skips itself
python3 tools/test_tools.py                     # tool tests

python3 tools/build_claims.py \
  --allocations-dir ../artifacts/bridge-allocations-<date>/allocations \
  --output-dir runs/bridge-allocations-<date>

set -a; . ../bridge/local/rpc.env; set +a       # archive RPC URLs (never print them)
python3 tools/approve_claims.py --claims-dir runs/bridge-allocations-<date> --rpc   # --draft: unsignable files

CLAIMS_DIR=runs/bridge-allocations-<date>/ethereum FORK_RPC_URL="$ETHEREUM_RPC" \
  forge test --match-contract Fork -vv

MANIFEST=runs/bridge-allocations-<date>/ethereum/manifest.json \
  forge script script/Deploy.s.sol --rpc-url "$ETHEREUM_RPC"     # add --broadcast to deploy

python3 tools/proof.py runs/bridge-allocations-<date>/ethereum <payee> --cast
```

The bridge config's `custody_multisig` names each chain's funder; a chain without one gets a tree
but no deployment address until `--funder <label>=<address>` or the config names a qualifying Safe.
`make bridge-claims-test` and `make bridge-claims` run the same steps from the repository root.
