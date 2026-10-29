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
every chain. Before reading any chain it checks the claim run (`--claims-dir`):

- the run was built from these allocations;
- every ready contract's funder is that Safe;
- the ONE contract approves exactly the claimable ONE that stage 1 transfers.

- every ready manifest agrees field by field with its `summary.json` entry, and its list and tree
  with the manifest;
- every file of the run's published index (`SHA256SUMS` in the public bucket, or
  `--published-index`) hashes the same locally.

It then reads each chain at one pinned block, checked again before anything is written, and stops
unless the Safe is an unmodified Safe 1.3.0 owned by the roster (below) on every chain. The package
`runs/<run>/release/` is published only whole and listed in the run record `PACKAGES.json` (see
"Packages" in step 4). A failed build leaves no `release/`, and moves the earlier one to
`superseded/` with `INVALIDATED.md`:

```sh
CLAIMS_DIR=runs/<run>/<label> RELEASE_DIR=runs/<run>/release FORK_RPC_URL="$ETHEREUM_RPC" \
  forge test --match-contract EscrowRelease -vv
```

## 4. Approve

```sh
python3 tools/approve_claims.py --claims-dir runs/<run> --labels <label> --rpc --replace
```

The custody Safe, its four owners, and the threshold come from the committed
`custody-roster.json`; a bridge config naming other owners stops the tools. Before the first
signable package, each owner confirms that their own address is in that file, from their own
device rather than from what the Safe reports, and the confirmations are kept with the run.

Before reading any chain, the tool binds the run to its summary (`tools/claims_run.py`):

- each contract's manifest agrees with its `summary.json` entry on label, chain, chain id, funder,
  root, row count, list hash, address, and status;
- `claims.csv` and `tree.txt` hash to what the manifest records;
- `--labels` names only ready contracts, each once;
- no manifest in the run lacks an entry;
- every file of the run's published index (`SHA256SUMS` in the public bucket, or
  `--published-index`) hashes the same locally.

It never takes the spender from the manifest: it recomputes the CREATE2 address from the compiled
bytecode (`forge build`) and the manifest's constructor arguments and stops on any difference. It
then reads the chain at one block and publishes the package `runs/<run>/<label>/approval/`
(`safe-approve.json`, `safe-revoke.json`, `approvals.json`, `APPROVALS.md`, `GENERATION`,
`PACKAGE.sha256`) only if every check passes:

- the funder is the roster's custody Safe, an unmodified Safe 1.3.0 (proxy code, singleton,
  fallback handler, no module, no guard), owned by exactly the roster's owners with its threshold;
- MultiSendCallOnly and the CREATE2 deployer have their official code;
- the contract, if deployed, has exactly the expected code and immutables;
- the Safe holds every asset's total;
- `--nonce`, if given, is the Safe's nonce.

Every contract of the run must pass, or none is published. When a check fails, the tool lists the
failures. Any earlier `approval/` then moves to `superseded/` with `INVALIDATED.md`, so after a
failed refresh there is nothing to sign. A successful refresh moves the earlier package there too.
The batch is built for the Safe's nonce at that block, which `APPROVALS.md` records.

`--draft` writes the same files as `*.draft.*` into `approval-draft/`, titled not to be signed,
without `--rpc` or despite failed checks; never load a draft into the Safe.

### Packages

A package is current only while the run record `runs/<run>/PACKAGES.json` lists it, with the
SHA-256 of its `PACKAGE.sha256` and its `GENERATION`, and marks no operation pending. Every
refresh, approval or release, and every quarantine takes one lock on the run directory, so they
never overlap, whichever contracts they touch. A refresh first takes its targets out of the record
and marks it pending; it lists the new packages only after all of them are in place. A refresh that
fails, or a process killed between two renames, therefore leaves none of its packages listed, and a
pending record makes nothing in the run current. Symlinks are refused from the run directory down.
Before signing:

```sh
python3 tools/package.py verify runs/<run> <label>/approval   # or release
```

It must print `current` and exit 0. Compare the `PACKAGES.json` SHA-256 it prints with the one the
builder sends with the package. Without the tool: `shasum -a 256 <label>/approval/PACKAGE.sha256`
equals the record's `package_sha256`, `GENERATION` holds its `generation`, `pending` is null, and
`shasum -a 256 -c PACKAGE.sha256` passes inside the package.

Approval files that earlier versions wrote directly beside a manifest stop the tool until they are
quarantined, a reviewed step of its own that keeps them, with their SHA-256, under
`runs/<run>/quarantine/<time>/`:

```sh
python3 tools/quarantine.py runs/<run> --reason "<why>" --dry-run   # then without --dry-run
```

Quarantine holds the same lock and writes its plan, with every file's SHA-256, to
`QUARANTINE.json` before moving anything. A failed move puts everything back. An interrupted
quarantine leaves the record pending, so nothing in the run is current, until
`tools/quarantine.py runs/<run> --recover` completes it.

Then:

1. In the Safe web app, open Transaction Builder and load `approval/safe-approve.json`, after
   `tools/package.py verify` reports the package current. It wraps the native
   total (when the chain has native rows) and approves the contract for exactly each asset's total
   (a token with a nonzero allowance is approved for 0 first).
2. Each signer compares the Safe transaction hash on the device with the one in `APPROVALS.md`,
   and checks the spender and constructor arguments listed there against the published manifest.
   Hardware wallets cannot show the spender and amount inside a MultiSend batch; Ledger hides the
   nonce, and Trezor shows hashes only. The sheet, rebuilt at the live nonce, is therefore the
   reference. Compare its domain and message hashes with the device, and have a second person
   recompute the Safe transaction hash independently.
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

Then verify the source on each chain's explorer, so payees can read the code. Use the exact
settings:

- Solidity 0.8.36, EVM version Cancun, optimizer enabled with 20,000 runs, legacy codegen (no
  `viaIR`);
- metadata `bytecodeHash` none and `appendCBOR` false, no libraries;
- the six constructor arguments from the manifest's `constructor_args`.

Etherscan-style explorers need the exact standard-JSON input (`forge verify-contract` with
`--show-standard-json-input`). Sourcify cannot full-match a build without metadata. Verification
changes nothing about the bytecode.

Where the packages live:

- **Public.** The publication bundle goes to the public bucket `gs://harmony-bridge-claims/<run>/`
  (`https://storage.googleapis.com/harmony-bridge-claims/<run>/`): each chain's `manifest.json`,
  `claims.csv`, `tree.txt`, and `samples.json`, with `SHA256SUMS`.
- **Private.** The whole run, including approval and release packages and the dry-run output kept as
  evidence, goes to the private bucket `gs://harmony-migration/harmony-migration/bridge-claims/runs/<run>/`
  with a SHA-256 index. The allocation run goes beside it under `artifacts/`.

## 6. Open

`available(token)` on the contract must equal each approval. Payees (or a relayer on their behalf)
claim with `claim` or `claimMany`; `python3 tools/proof.py runs/<run>/<label> <payee> --cast` prints
a payee's rows, proofs, and a ready `claimMany` command. The command carries `--chain`, so an RPC
of another chain rejects it.

Relayers paying other people's rows:

- Estimate gas per row and cap it.
- Skip rows that revert. A native row whose recipient burns all its gas needs about 3.6 million gas
  (see `DESIGN.md`).
- Many rows are worth less than the gas to claim them. Who pays for those, a relayer or a minimum
  claim in the allocation config, is decided before opening.

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
