# BridgeClaims runbook

From a verified bridge allocation run to open claims, per chain. Commands run from
`bridge-claims/`; `<run>` is the run directory under `runs/`, `<label>` a contract (`ethereum`,
`bsc`, ...). Load the archive RPC URLs with `set -a; . ../bridge/local/rpc.env; set +a` and never
print them.

## 1. Build the lists

```sh
forge build
python3 tools/build_claims.py --allocations-dir ../artifacts/<allocation run>/allocations --output-dir runs/<run>
```

The builder refuses inputs whose hashes differ from the allocation summary and any asset whose
rows do not add up to its pools' claimable total. Read `runs/<run>/CLAIM_CONTRACTS.md`: one row per
contract with its funder, root, address, and status. `no_funder` means the chain has no custody
multisig yet (step 7).

## 2. Test against the real chain

```sh
CLAIMS_DIR=runs/<run>/<label> FORK_RPC_URL="$ETHEREUM_RPC" forge test --match-contract Fork -vv
MANIFEST=runs/<run>/<label>/manifest.json forge script script/Deploy.s.sol --rpc-url "$ETHEREUM_RPC"
```

The fork test must print `ok` for every asset. The script without `--broadcast` simulates the
deployment and must land on the manifest address; run it for all five chains. It recomputes the
CREATE2 address from the compiled init code, checks the deployer's code, and deploys with a plain
call to the deployer, so the result does not depend on the Forge version (only `solc`, pinned in
`foundry.toml`, shapes the bytecode, and the script stops if it differs from the manifest). It
prints the runtime code hash the deployment leaves; it must equal the one in `APPROVALS.md`.
Delete `cache/Deploy.s.sol/` afterwards: Forge stores the RPC URL there.

## 3. Fund the custody multisig

The escrows have no withdrawal. Each escrow's owner releases it on its own chain by delivering one
LayerZero packet it attests itself, with no cross-chain message and no LayerZero service or fee.
Build the release plan as described in [`RELEASE.md`](RELEASE.md), replay it on a fork of every
chain, then have the Reserve Safe execute `0-one-reserve/` (stage 1 of the ONE funding: exactly the
claimable ONE; the held and retained rest stays in the Reserve Safe until decided) and each escrow owner
execute its `1-release/` file. The planner pays only the custody multisig the bridge config names on
every chain (the claim contracts' funder), and stops unless that Safe is an unmodified Safe 1.3.0
owned by the configured roster (below) on every chain:

```sh
CLAIMS_DIR=runs/<run>/<label> RELEASE_DIR=runs/<run>/release FORK_RPC_URL="$ETHEREUM_RPC" \
  forge test --match-contract EscrowRelease -vv
```

## 4. Approve

```sh
python3 tools/approve_claims.py --claims-dir runs/<run> --labels <label> --rpc --replace
```

First list the four custody Safe owners in the bridge config as `custody_requirements.owners`,
taken from a source independent of the chain (the owners themselves), not from what the Safe
reports. The tool never takes the spender from the manifest: it recomputes the CREATE2 address
from the compiled bytecode (`forge build`) and the manifest's constructor arguments and stops on
any difference. It then reads the chain at one block and writes `safe-approve.json`, `safe-revoke.json`,
and `APPROVALS.md` only if every check passes: the funder is an unmodified Safe 1.3.0 (proxy code,
singleton, fallback handler, no module, no guard) owned by exactly the configured roster with the
required threshold; MultiSendCallOnly and the CREATE2 deployer have their official code; the
contract, if deployed, has exactly the expected code and immutables; the Safe holds every asset's
total; and `--nonce`, if given, is the Safe's nonce. Otherwise it lists the failed checks and writes
nothing. The batch is built for the Safe's nonce at that block, which `APPROVALS.md` records.
`--draft` writes the same files as `*.draft.*`, titled not to be signed, without `--rpc` or despite
failed checks; never load a draft into the Safe. Then:

1. In the Safe web app, open Transaction Builder and load `safe-approve.json`. It wraps the native
   total (when the chain has native rows) and approves the contract for exactly each asset's total
   (USDT is approved for 0 first).
2. Each signer compares the Safe transaction hash on the device with the one in `APPROVALS.md`,
   and checks the spender and constructor arguments listed there against the published manifest.
3. Two of the four owners sign; one executes.

Approving before deployment is safe: the spender is the CREATE2 address of this exact list, and
only that contract can ever exist there.

## 5. Deploy

```sh
MANIFEST=runs/<run>/<label>/manifest.json forge script script/Deploy.s.sol \
  --rpc-url "$ETHEREUM_RPC" --broadcast --account <keystore>
```

Any account can deploy; the address and behaviour do not depend on it. The script checks the
chain, the deployer's code, the bytecode hash, the recomputed address, the runtime code left there,
and every immutable. Publish the manifest, `claims.csv`, and `tree.txt` together with the deployment
so anyone can recompute the root and the address.

## 6. Open

`available(token)` on the contract must equal each approval. Payees (or a relayer on their behalf)
claim with `claim` or `claimMany`; `python3 tools/proof.py runs/<run>/<label> <payee> --cast` prints
a payee's rows, proofs, and a ready `claimMany` command.

## 7. Chains without a custody multisig

Create a Safe with the required owners and threshold, set it as the chain's `custody_multisig` in
the bridge config (or pass `--funder <label>=<address>`), and rebuild. The root does not change;
the address does, because the funder is a constructor argument.

## Stopping and closing

- Stop everything: load `safe-revoke.json` (approves 0 for every asset). Claims resume when the
  approvals are given again.
- Stop one asset: approve 0 for that token only.
- Close: revoke the approvals; unclaimed assets stay in the Safe.
- Correct a list: see "Corrections" in [`DESIGN.md`](DESIGN.md).
