# BridgeClaims audit: team response

Response to the hiddenstate.xyz report of 28 October 2026 (repository commit `4840fa9`, plan
baseline `70371fc`), covering its High and Medium findings. `src/BridgeClaims.sol` is unchanged, so
the creation code is the one the report verified. The BNB Chain, Arbitrum, Base, and Linea
manifests, roots, list hashes, and CREATE2 addresses are also unchanged. Ethereum's list was rebuilt
under BC-H02 and has a new root and address.

| Finding | Severity | Response |
|---|---|---|
| BC-H01 | High | Fixed: both tools recompute the spender from the bytecode and check code hashes |
| BC-H02 | High | Resolved: eight of the ten payees are validator keys (no issue); the other two are Harmony contracts and are now held, with a new Ethereum list |
| BC-M01 | Medium | Fixed: the deployment is a plain call to the canonical deployer |
| BC-M02 | Medium | Fixed: the approval tool fails closed; the team must add the owner roster |
| BC-M03 | Medium | Fixed: the release planner derives and verifies the recipient |

## Where the process stood

The audit plan called the custody Safe "mid-release" and asked the auditor to reconcile it. It did
not say plainly how far the process had got:

- **The escrow release is complete.** The release plan rebuilt on 23 October reads every escrow on
  all five chains as empty ("nothing locked to release"), so there are no `1-release/` files. The
  report's "no release entries for the other four chains" reflects this: nothing is left to release.
- **ONE stage 1 is the only funding step left.** At Ethereum block 26,294,658 the custody Safe held
  no ONE, and the Reserve Safe's nonce was 61. The stored `0-one-reserve/` batch was built for nonce
  58 and must be rebuilt.
- **Next are deployment and the final approvals** from the custody Safe on each chain.

BC-M02 therefore concerns the very next signing step. `tools/approve_claims.py` builds the final
token approvals and was in scope (plan §1.4 and §5). BC-M03 concerns a step that has already run
with the intended recipient, as the report confirms. It still applies to the ONE stage-1 batch,
which the same planner rebuilds.

## BC-H01: spender bound to CREATE2 init code

`tools/approve_claims.py` no longer takes `deployment.address` on trust. It rebuilds the init code
from the compiled creation code (`forge build`) and the manifest's constructor arguments. It then
recomputes the EIP-1014 address through the canonical deployer and the salt in `chains.json`, and
stops before writing anything when any of these differ:

- the address;
- the deployer or salt;
- the creation-code or init-code hash;
- the constructor encoding;
- the top-level manifest fields;
- the SHA-256 of `claims.csv`.

With `--rpc` it also checks the CREATE2 deployer's runtime code hash. Once the contract exists, it
checks the address's `extcodehash` against the exact expected runtime code (immutables written in)
and reads the six getters.

`script/Deploy.s.sol` does the same:

- it recomputes the address with `vm.computeCreate2Address` and requires the manifest to agree;
- it checks the deployer's code hash;
- after deploying, or when the contract already exists, it compares `extcodehash` with the code of
  a local, never-broadcast copy, then reads the getters.

The tools rebuild the expected runtime code from the artifact's immutable references.
`TreeFixture.t.sol::test_PythonRuntimeCodeMatchesDeployment` ties that reconstruction to the
compiler.

New negative tests in `tools/test_tools.py` cover:

- an EOA spender;
- a changed factory, in the manifest or in `chains.json`;
- changed creation code;
- a changed salt;
- a changed list;
- a getter-compatible contract at the address.

Each one stops the tool before it writes a file.

## BC-H02: payees with Harmony code

Eight of the ten payees (nine rows: four on Ethereum, five on BNB Chain) are Harmony validator
accounts, not contracts:

- **Why they show code.** Harmony stores a validator's staking record in the account's code field
  (`core/state/statedb.go`, `UpdateValidatorWrapper` calls `SetCode(addr, rlp(validator), true)`),
  so `eth_getCode` returns bytes for a validator address.
- **Why they are keys.** A validator address must be the signer of its staking transactions
  (`core/state_transition.go` rejects `msg.From() != stkMsg.ValidatorAddress` with
  `errInvalidSigner`). It is therefore an ECDSA key, and the same key controls the address on every
  EVM chain.
- **Already handled.** `classify-holders.py` already recognizes these accounts by decoding the
  record (`validator_wrapper`) and treats them as key-controlled.

These rows need no change.

The other two payees are real. They had two ONE rows in the Ethereum list, worth under $5 together
at the run's prices. Both are application contracts on Harmony, listed in the contract review's
registry (`toolkit/scripts/contract-review/known-apps.json`), and neither is a validator or a key:

- **Not validators.** The archival classification at the cutoff block (93,623,067) found code that
  is not a validator record. The detector does not depend on the record's format: in every revision
  of `staking/types/validator.go` (107 of them), `ValidatorWrapper` begins with the embedded
  `Validator`, whose first field is the validator's own address (`Address`, earlier
  `StakingAddress`). Neither address appears in any staking transaction in the transaction archive.
- **Not keys.** A Harmony account has non-EVM code only when it is a validator. In the transaction
  archive, one of the two received and made thousands of calls without ever signing a transaction;
  the other has no native-ONE activity since 2025.

The archival node was unreachable during this review (SSH timed out), so the code was not re-read
live.

**How they got into the list.** Their bridged ONE on BNB Chain and Ethereum arrived through five plain
ERC-20 transfers from five individual wallets, and nothing ever left. People sent bridged ONE to these
well-known Harmony contract addresses on chains where no such contract exists. The pipeline classified
each holding only on the chain where it was held (`classes[(holder_chain, account)]`). There the
addresses have no code, so they counted as keys. Their own Harmony balances were held, because on
Harmony the same addresses have code. The audit plan's §4.6 scan for exactly this case was an audit
item, not a build gate.

**Fix.** `build-bridge-allocations.py` now treats an account without code where it holds as
key-controlled only if no other classified chain has a contract at that address. Otherwise it is
held as that contract would be (`held_contract`, `held_multisig`, …), with a reason, until a reviewed
override decides it. A test in `toolkit/tests/test_bridge_allocations.py` covers it.

**Rebuild.** The allocation run was rebuilt into `artifacts/bridge-allocations-20261018/allocations-20261028/`
from the same snapshots, state, plan, messages, classifications, and exclusions:

- **Config.** The config carried the custody multisigs as they were at the run
  (`config-20261018-custody.json`), so the custody plan is byte-identical.
- **Verifier.** The pipeline verifier reports no failures.
- **What changed.** Only the three allocation rows of these two accounts changed, to
  `held_contract`. The two ONE pools' claimable totals fell by those rows and their held totals rose
  by the same.
- **What did not.** The BNB Chain, Arbitrum, Base, and Linea claim lists are byte-identical, and the
  Ethereum list lost exactly the two rows and nothing else.

The claim contracts were rebuilt into `bridge-claims/runs/bridge-allocations-20261028/`. BNB Chain,
Arbitrum, Base, and Linea keep the audited `manifest.json`, `claims.csv`, and `tree.txt` byte for
byte, so their roots and addresses are unchanged. Ethereum is new:

| Item | Value |
|---|---|
| Address | `0x74dc0fbd9363cef31b41602876bf61f14b8ff690` |
| Root | `0x964894effbc2fcaa5e87b9583ae6636959dd23fc1d4f2c3cd97db12d52f2523b` |
| Init-code keccak256 | `0x18c20d02f262425bafd4f3e286e04edfdb9a4ca05949bad273c79e98f1b45896` |
| Runtime code keccak256 | `0x6e0707943d9d7b6202b8c292b11aadb46746e097150f0c7f966e0f46dc251a0b` |
| `manifest.json` SHA-256 | `717a5aae65bdbdbb848c951c8e051a52412ab09f360cad50bb4ee7e74a217c80` |
| `claims.csv` SHA-256 | `c9cfb400d4c3efa79c178a2d72a05c55a2c87c35b2b0999b3fef58c57337f353` |
| `tree.txt` SHA-256 | `39f3c0476bc1170809d0280dd01de78fbd710797ea3411486ed268609d413221` |

Checks on the new Ethereum list:

- **Independent verifier.** A Go verifier, sharing no code with the builder, recomputed every leaf
  and tree node from `claims.csv`. It matched `tree.txt` and the root, and every row's proof
  verifies.
- **Dry runs.** Both Forge versions land on the address above with that runtime hash.
- **Fork test.** `Fork.t.sol` pays the sample row of all 27 assets.
- **ONE totals.** The ONE approval and the ONE stage-1 amount both fall by the two rows' total and
  still match exactly.

The new Ethereum artifacts need the auditor's re-verification.

## BC-M01: deployment independent of Forge's CREATE2 routing

`Deploy.s.sol` no longer uses `new BridgeClaims{salt: …}`, which Forge routes differently across
versions. Inside the broadcast it calls `0x4e59…956c` with `salt ++ initCode`, as `Fork.t.sol`
already did. It requires the call to return the recomputed address, then checks the code there.

The recorded transaction is a call to the deployer with exactly that data. Dry runs of the audited
manifests against current heads land on the manifest address on all five chains. The runtime hash
each prints equals the hash the approval tool computes. The rebuilt Ethereum manifest (BC-H02) does
the same at its new address.

| Chain | Address | Runtime code keccak256 |
|---|---|---|
| Ethereum | `0x817eda9dd7461176873a28be5777d6f7918dac04` | `0x31c1e308d0527bafd05877ca45619023393a79a6529ff83294233382da0ea90f` |
| BSC | `0xffcc6017f66a924012017115c6b4aa205037a84e` | `0xfbc29c2bae58fbb2f46483f72761161bfe08958c733f89a0bdceec2ec9f22067` |
| Arbitrum | `0x69900956b2c4ceb5a257a19a3243e84822c2d7e2` | `0x8ed588e359153daf6da7f4a2ca314142c1a7d13f9b86dc12ca2b647df8729b54` |
| Base | `0xa2bddf296a5a945f0bb92e71629a3e78f401adbb` | `0xa301f9ed766c50c19b603208641a535e15d534c023a11e127b031a4825f33db7` |
| Linea | `0x8a5607026b92977c34aba988c5e22fc8b615ceaf` | `0x62b4fc5e0faff1ed42efd514607ac655ac09bdaadebdb29efda5719ff753301d` |

Both the installed Forge 1.6.0-nightly (`513ae2b`) and Forge 1.8.1 (`982849d`, the version that
failed for the auditor on Base) produce these results on all five chains.

Forge 1.8.1 has one more defect on Base: every contract creation outside a broadcast fails, even a
trivial contract and even through the deployer. Broadcast creations work. Two consequences:

- The script never creates a contract on a fresh deployment, so 1.8.1 can deploy on Base.
- The script builds a local copy, for an exact code comparison, only when the contract already
  exists. Under 1.8.1 on Base that check stops with a message pointing to `approve_claims.py --rpc`,
  which makes the same comparison.

A local anvil fork confirmed the already-deployed branch: the deployed contract passes, and the same
code with one byte changed is rejected.

The Foundry binary is still not pinned. The address no longer depends on it, and `solc`, which
determines the bytecode, is pinned in `foundry.toml`. A Forge version that compiled different
bytecode or reached a different address would stop the script before broadcasting.

## BC-M02: approval preflight fails closed

`tools/approve_claims.py` now has two modes:

- **Signable mode** writes `safe-approve.json`, `safe-revoke.json`, `approvals.json`, and
  `APPROVALS.md` only with `--rpc` and only when every check passes. Otherwise it lists the failures
  and writes nothing.
- **`--draft`** writes `*.draft.*` files whose titles and Transaction Builder batch names read
  "DRAFT, do not sign".

The checks:

- **One block.** Every read uses one pinned block, and the tool confirms that block's hash is
  unchanged at the end. The block number and hash appear on the signing sheet.
- **Safe identity.** The funder must be an unmodified Safe 1.3.0: canonical proxy code, the
  canonical singleton and its code, `VERSION()`, no module, no guard, and either no fallback handler
  or the canonical `CompatibilityFallbackHandler`. On all five chains the handler is set to that
  handler; the report's "zero fallback-handler slot" does not match the live state.
- **Owners.** They must equal `custody_requirements.owners` in the bridge config, with the required
  threshold. Without that roster the check fails; the tool never takes owners from the Ethereum Safe.
- **Nonce.** `--nonce` must equal the Safe's nonce.
- **Contract code.** MultiSendCallOnly and the CREATE2 deployer must have their official code
  hashes.
- **Funding.** Every asset must be funded.

The signing sheet also shows the manifest SHA-256, which signers compare with the published manifest.

Against the live chains, every check passes on all five except two: the owner roster (not yet in the
config) and Ethereum's ONE funding (stage 1 not yet executed).

The Approve and Revoke files under `runs/` from 21 October are the ones the report rejected. Rebuild
them with `--rpc --replace` right before signing.

## BC-M03: release recipient derived and verified

`tools/release_plan.py` no longer defaults `--safe` to a hard-coded address or accepts any value:

- **Recipient.** It derives the recipient from the bridge config's `custody_multisig`, which must be
  the same on every chain of the plan and equal the ONE custody. This is also how the claim builder
  sets the contracts' funder. An explicit `--safe` must match it.
- **Custody Safe checks.** Before writing anything, the custody Safe on every chain must pass the
  same Safe 1.3.0 identity checks. It must also be owned by the configured roster with the required
  threshold, or be a 1-of-1 Safe whose owner is on the roster, which gets an owner-setup batch.
- **Owner-setup target.** The setup batch uses the configured roster, not the Ethereum Safe's live
  owners.

The allocation run's `custody-plan.json` is out of date on this point: it names a different BNB
Chain custody multisig and none on the L2s. The config names the same custody Safe on all five
chains, matching the five manifests.

## Remaining before signing

1. Have the auditor re-verify the new Ethereum artifacts (table under BC-H02).
2. Add the four custody Safe owners to `custody_requirements.owners` in `bridge/local/config.json`,
   from a source independent of the chain.
3. Rebuild `0-one-reserve/` from the new allocations (the Reserve Safe is now at nonce 61; the amount
   is the new claimable ONE) and execute it:
   `python3 tools/release_plan.py --allocations-dir ../artifacts/bridge-allocations-20261018/allocations-20261028 --output-dir runs/bridge-allocations-20261028/release`.
4. Run `tools/approve_claims.py --claims-dir runs/bridge-allocations-20261028 --rpc --replace` per chain
   right before signing, deploy from the same run, and open as in `RUNBOOK.md` §4 to §6.
