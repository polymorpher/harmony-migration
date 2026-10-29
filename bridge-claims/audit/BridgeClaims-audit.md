# BridgeClaims Security Audit - Second-Pass Remediation Regrade

**Auditor:** hiddenstate.xyz  
**Date:** 29 October 2026  
**Reviewed commit:** `5a8937e103910786dd9b3e3723e3a9f3440a8031`  
**Prior review commit:** `284dc77a7ea575c7b76d4b7d177595d219af233b`  
**Original audit commit:** `4840fa967375bfce1aca12f37158831512f746d5`  
**Audit-plan baseline:** `70371fc6337bb11cc30926ca78ed672b990e9da7`

## Executive conclusion

The second-pass remediation fixes the display amount error in `claims.csv` and improves allowance resets, token-label checks, proof commands, and package finalization. The canonical claim run is now `bridge-allocations-20261029`. Its 22 indexed public objects were anonymously downloaded and matched their SHA-256 entries. The Merkle trees and roots are unchanged; the list hash, CREATE2 address, and expected runtime change on Ethereum, BSC, and Arbitrum. Base and Linea claim files remain byte-identical. The contract source is unchanged. The two original High findings and the CREATE2 routing Medium finding remain closed.

**The two remaining Medium ratings in the preceding report were too broad.** BC-M02's original stale-package finalization failure is fixed: injected seal and activation errors clear the selected current packages. BC-M03's original arbitrary-recipient and unpinned-read release defects are fixed. The reported residual PoCs are real, but they did not change a release transaction or demonstrate a stale Safe hash reaching a signer who follows the documented manifest, nonce, and transaction-hash checks. They are recorded below as informational hardening observations, not continuing Medium findings.

The updated checkout is not a signable operational snapshot. The canonical run is absent locally; the old rejected run remains locally; and the active bridge config still has no independently approved owner-address roster. The team's retained dry runs and fork replay are useful supporting evidence, but no production claim contract or new allowance has been independently established in this review. The current Python tool suite also fails 22 stale exact-file assertions after the package layout changed; this is a QA gap, not a demonstrated contract or signing flaw.

**Decision:** Do not sign from this checkout. Deployment is conditional on materializing and authenticating the exact `20261029` bundle and passing a fresh chain-specific simulation; it does not depend on approval or release package hardening. Do not open claims until ONE stage 1, deployment, exact funding and allowances, and per-asset canaries are independently verified. Signing needs the canonical run, an attested owner roster, current Safe state, and independent decoding of every call and hash. The dust-row policy remains an opening decision.

Open finding count under the plan's impact rubric: **0 Critical, 0 High, 0 Medium, 0 Low, and 3 Informational follow-ups** (BC-I09, BC-I11, BC-I12). BC-L08 is an accepted, documented relayer-cost limit. This count is a code and process finding status, not authorization to sign or open claims.

## Remediation status

| Finding | Severity | Status | Result |
|---|---|---|---|
| BC-H01 | High | Verified remediated | Spender, factory, init code, runtime code, getters, and manifest remain bound fail-closed. |
| BC-H02 | High | Verified remediated and published | Contract payees are held, validator exceptions are supported, and the exact replacement package is now publicly downloadable and checksum-verified. |
| BC-M01 | Medium | Verified remediated | Deployment calls the canonical factory directly; ten retained plans target the committed addresses, and expected runtimes were independently rebuilt offline. |
| BC-M02 | Medium | Verified remediated | Preflight and package finalization fail closed for the tested paths; coordination hardening is tracked separately as BC-I11. |
| BC-M03 | Medium | Verified remediated | Recipient binding and pinned reads protect actual release calls; cross-file consistency hardening is tracked separately as BC-I12. |
| BC-L01 / BC-L06 | Low | Code remediated | The signing sheet now shows pinned on-chain symbols beside differing bridge labels and rejects decimals drift. |
| BC-L04 | Low | Rebuilt; execution pending | ONE stage 1 was rebuilt for Reserve Safe nonce 61 and replayed on a fork; it must be refreshed at signing. |
| BC-L05 | Low | Code remediated | Every observed nonzero allowance receives a zero reset before its new approval. |
| BC-L07 | Low | Artifact remediated | Plain-decimal display amounts are corrected in the new published claim lists; raw amounts and roots did not change. |
| BC-L08 | Low | Accepted operational limit | A hostile native recipient can consume about 3.6 million gas; relayers need per-row estimation and caps. |
| BC-I01 | Info | Accepted / unchanged | Solidity 0.8.36 advisory assessment is unchanged because source and compiler configuration are unchanged. |
| BC-I02 | Info | Accepted / unchanged | Issuer, proxy, and token-success assumptions remain opening-time risks. |
| BC-I09 | Info | Open policy choice | Decide who pays for uneconomic dust claims before opening. |
| BC-I11 | Info | Hardening | Use a consistent run lock and recoverable quarantine; current PoCs did not show a stale signed transaction. |
| BC-I12 | Info | Hardening | Compare summary and manifest fields and restrict explicit labels; the mismatch PoC did not alter release calldata. |

## Audit identity

| Item | Value |
|---|---|
| Contract | `bridge-claims/src/BridgeClaims.sol` |
| Source lines / bytes | 287 / 13,575 |
| Source SHA-256 | `94f8bed2e34d5a55a2304e96ab256cf59e980f5c813271c5659fd397c0f71255` |
| Audit plan SHA-256 | `751141d67095f509d23dda806c6116eacbf6565dd13e0ca0230b5b7403038745` |
| Latest audit response SHA-256 | `f392a48e7e13b2e6cc4a3db976597ab2ab4c2b57d9971ee669a98939e4b7c135` |
| Reviewed commit | `5a8937e103910786dd9b3e3723e3a9f3440a8031` |
| Prior review commit | `284dc77a7ea575c7b76d4b7d177595d219af233b` |
| Source-manifest coverage | 284 files; clean-copy verification passed |
| Compiler | Solidity `0.8.36+commit.8a079791` |
| Settings | Cancun, optimizer enabled, 20,000 runs, legacy codegen, no CBOR metadata |
| Creation code | 5,626 bytes; Keccak `0x63cba28992049e747767818863cd42803dc10e1d8ad093d99e3725db5ffd0cb9` |
| Runtime template | 5,120 bytes; Keccak `0xb36b3fff0e95016effd735265a52c6baaffcea66e90b0485e64c4084aec07cd3` |
| Compiler artifact SHA-256 | `39677ec70b45114040b956f47a1d49ac7b29a20bef36c27f30572570b789bb1a` |

`BridgeClaims.sol`, its compiled artifact, and the committed creation and runtime templates are unchanged. This review covers the diff from `284dc77` to `5a8937e`, the latest response, package and quarantine failure paths, release-run binding, the new public claim package, and selected operational evidence. The separate second-pass audit cited by the team was not in this checkout; its findings are represented here through the team's response and independently checked code and artifacts.

## Scope and threat model

The review retained the original contract, Merkle, CREATE2, Safe, release, token, and payee threat model. The custody Safe remains the source of every allowance and the intended recipient of released collateral. Anyone can submit a valid claim, so every leaf must bind a recipient that can safely receive value. The deployer is intended to be untrusted.

A production signing package must be authenticated against a pinned state and the approved public manifest for its chain. The runbook deliberately refreshes approvals one chain at a time, so different package generations across chains are expected. A checksum in the same mutable location detects accidental change but is not, by itself, an authenticity anchor. Signers must compare the package with independently distributed digests and revalidate dynamic state immediately before use.

Address-level claim data, owner identities, private RPC endpoints, and embargoed balances are not reproduced. The two held contract payees remain unnamed.

### Unchanged contract baseline

The source is byte-identical to the original audit. Six constructor values are immutable; the only post-construction storage written by a claim is the paid-row bitmap (`BridgeClaims.sol:43-64,100-117,209-225`). Each leaf hashes its index, account, token, and amount twice before the sorted-pair proof check (`151-163,269-285`). `claimMany` skips an already paid index without checking that row's proof, but any newly paid row passes `_claim`, and any other failure reverts the batch (`133-144`). The bitmap is set before token or recipient calls, so reentry cannot pay the same row twice (`209-225`).

For a native row, the contract pulls wrapped coin from the funder, unwraps it, and sends coin to the row account; if the account rejects coin, it rewraps and sends the wrapped asset to that account (`228-237`). `sweep` is permissionless but sends only to the immutable funder (`185-200`). `listHash` is an immutable publication fingerprint, not an on-chain membership check (`53-59`). Token calls accept a true or empty successful return; issuer blocks, fees, or dishonest token implementations remain external limits (`248-266`). The team reports no production deployment; this pass did not independently read code at the replacement addresses, so source and test conclusions do not substitute for post-deployment checks.

## Methodology and validation

This review combined manual diff analysis, isolated Python and Forge test runs, package fault injection, overlapping-lock tests, a summary/manifest mismatch PoC, public checksums, full row-arithmetic and independent Go Merkle verification, a row-by-row comparison with the superseded claim lists, and selected private-package inspection. The contract and allocation logic did not change. Historical pinned RPC reads are labeled as a prior snapshot, not current state.

### Second-pass test results

| Test | Result |
|---|---|
| `tools/test_tools.py` at `5a8937e` | **42 run: 22 failed, 19 passed, 1 skipped.** The failed assertions expect the old package layout: 20 reject `.packages.lock`, one rejects `GENERATION`, and one expects a seal without `GENERATION`. No separate runtime failure surfaced in those traces. |
| Three new targeted tool tests | Plain amount formatting, nonzero-allowance reset, and pinned decimals/symbol handling each pass in isolation. |
| `BridgeClaims.t.sol` and `TreeFixture.t.sol` | 26 and 4 passed offline, respectively. |
| Fork suites | 2 skipped because required fork inputs were not configured. |
| `toolkit.tests.test_bridge_allocations` | 14 passed, 0 failed. |
| Clean source manifest | 284 indexed source files passed exact size and SHA-256 checks in a disposable clean copy. |
| Public package | 22 indexed objects downloaded and SHA-256 checked; 836,754 displayed amounts equal raw units at each asset's decimals. |
| Independent Go verifier, current public run | Recomputed every leaf, every Merkle node, and one proof per 836,754 rows against all five roots and tree files. |
| Public-package hygiene script | The whole-repository scan is not green: existing tracked vault PDFs contain binary NUL bytes, and scratch copied fixtures tripped the new provider-key pattern. This is not evidence of a secret in the claim publication bundle. |

The current working checkout's strict source-manifest check fails on the three untracked audit outputs; the same 284-file manifest passes in a clean indexed copy. Forge `1.8.1` with pinned local `solc 0.8.36` passed the offline suites above. The team reports a new Ethereum fork replay and ten deployment dry runs for the `20261029` run; these are retained team-produced records, not our own live simulations.

### Adversarial package tests

The new `package.staged()` catches seal and activation exceptions, seals `GENERATION` with the package, rejects symlinked contents, and uses an advisory file lock. An injected failure while sealing the second target now cleared both old packages and recorded invalidation: the prior finalization PoC no longer reproduces.

Residual scratch counterexamples are narrower and specific:

- A single-label refresh locks `<run>/<label>`, while an all-label refresh locks `<run>`. Both locks can be held at once. Overlapping success and failure left one label's approval absent while another remained current.
- A persistent injected `set_aside()` permission error in the failure handler left an old release batch at the current path. The tool printed `COULD NOT CLEAR`, but its headline fail-closed claim was false for that path.
- Quarantine takes no package lock. An injected second move failure left one legacy signable file in place, the first file moved, and no `QUARANTINE.md`.
- The single-label path check starts at the label directory and accepted that directory as a symlink; a scratch package was written through it. Abrupt termination between sequential target renames also has no run-wide commit marker to let a signer reject a partial generation.
- An unchanged ready summary was accepted with a manifest whose root, chain ID, claim count, list hash, and deployment address differed, and with a tampered `claims.csv`.

The first four cases are BC-I11 hardening observations; the last is BC-I12. They did not demonstrate a stale signed Safe hash or altered release calldata under the documented signing procedure.

### Artifact publication and package evidence

The [current public package](https://storage.googleapis.com/harmony-bridge-claims/bridge-allocations-20261029/README.md) and every object named in its `SHA256SUMS` were downloaded without credentials. All 22 indexed objects recomputed successfully. The index SHA-256 is `718c44d94c5105b5e36603c226ce46f1231d6368d96b2ac59c573d8a72cc46fb`; public `summary.json` SHA-256 is `18917b4c1deebbf8e807667daea855c45f0000b1807027c40e8606a53d639fb5`. Every manifest's chain, chain ID, funder, root, claim count, list hash, and address agrees with the summary. Each actual list and tree hash matches its manifest. An independent Go verifier recomputed every leaf and tree node and checked one proof for each of the 836,754 rows against all five roots. All display amounts equal `amount_raw` at the listed decimals, with no scientific notation. This proves the downloaded bytes' internal coherence, not publisher authenticity. The README is outside the checksum index. The old `20261028/SHA256SUMS` is gone; its public `SUPERSEDED.md` directs readers to this prefix.

The new CSVs have the same row counts and the same values in every non-`amount` column as the prior public run. Only display `amount` changed: 65,837 cells on Ethereum, 20,298 on BSC, and 32 on Arbitrum; none on Base or Linea. Trees and samples are byte-identical on all five chains. Ethereum, BSC, and Arbitrum manifests changed only the list hash and deployment object. The team's narrower claim that 992 dust cells were materially inflated is plausible from the old formatting behavior; our full check establishes the exact changed-cell counts and the new arithmetic, not a valuation of those cells.

A separate private object manifest, SHA-256 `8cbb1614e34923a8829d01d269c662ed8fad6b0e6dee20f01cde84dee0476b39`, indexes 486 objects. We downloaded and checked 61 selected files against that inventory: 47 canonical-run files, four signer-handoff files, and ten redacted Forge logs. These include five operational packages and ten deployment dry-run record sets; we did not download all 486 objects. Every file in the five packages also matched its `PACKAGE.sha256`:

| Package | Seal SHA-256 |
|---|---|
| Release | `923b3cd278fd06462fa7a8d8fcaef604c4609461d8905a00ea96634e0d55e39f` |
| BSC approval | `faeafc6cb309c4a24176c0ad0942dfa4a2f9de9d906cfacfc6879a09c26bd90b` |
| Arbitrum approval | `779655e70616cdcef4c19acaa4f7750f7f11c73f7f524cdef66fc0696af81864` |
| Base approval | `66f35bcd59df976da782b6f17ba73a2cc51fecc9bf606268af1eff7d400b3ccf` |
| Linea approval | `0325fdb67d8e7326ce6da8b6995e2079e88676f0bfcc3985557f5a9dc446bb52` |

The four approval packages are non-draft, record nonce 1 and zero failed preflight checks (57/21/18/18 checks), and bind to the new public manifest hashes. They attest four owners, threshold two, and a consistent roster digest; this does not independently authenticate owner identity. The selected snapshot has no Ethereum approval package; ONE stage 1 remains pending. The release-plan SHA-256 is `39397041a7050cd1fd855774861224b8c4bf558ac4881d7eab03f843bfa7dc95`. It records one ONE stage-1 transfer at Reserve Safe planned/current nonce 61, with amount matching the new public Ethereum ONE total, and no escrow-release batch. These are captured package fields, not a fresh live-chain read. The selected packages do not contain the new `GENERATION` file and therefore predate the latest package activation code; they must be refreshed before signing.

The private inventory also contains two superseded claim runs. Its indexed quarantined files match the records selected for review, and no active signable package appeared in those private superseded runs. The reviewed checkout is different: it still has 13 old signable-looking files outside quarantine in its local ignored `20261018` run, while the `20261029` canonical run and approved owner roster are absent. Its untracked report outputs also make the strict source-manifest check fail in place.

### Prior pinned state snapshot - historical only

The preceding review pinned a block number and hash on each chain, read Safe and target code at those blocks, and rechecked each final hash. Every custody Safe returned four owners and threshold two. These values describe the earlier `20261028` package and its old claim addresses; they do not establish current state or code absence at the three replacement addresses.

| Chain | Pinned block | Block hash | Relevant state |
|---|---:|---|---|
| Ethereum | 26,296,358 | `0x4d23aca8c1a8f4746ede700c865cc7f4322a65660f285a8b837b00c740324a7a` | Custody nonce 7; Reserve nonce 61; ONE stage 1 pending; claim code absent |
| BSC | 130,418,662 | `0x692cc6ee7d3ef4c33e5fe87a78a2a19e7be7e6fc29402ab73ce2833b8609438b` | Custody nonce 1; claim code absent |
| Arbitrum | 519,271,929 | `0xfede3c685c04318530f7197f4514f8014ddb307c74e1d7344ad4214e1c957d16` | Custody nonce 1; claim code absent |
| Base | 53,231,501 | `0x8a0eb73a2f265eff672d6dc1e2b0f80bbd6980af202a49662f844cf7306f7c49` | Custody nonce 1; claim code absent |
| Linea | 32,459,744 | `0x5bcd037f450b930ff9d9c67968b26e1417e84c2e5bf760dff54b8cd654748b15` | Custody nonce 1; claim code absent |

This pass did not independently repin the chains. A Safe transaction, deployment, or reorganization after those blocks can change the state. Every signing and execution package must be refreshed and independently checked at use time.

## Findings

### BC-H01 - Spender binding to CREATE2 init and runtime code

**Original severity:** High  
**Affected invariants:** I4, I6, I9, I10  
**Status:** Verified remediated  
**Location:** `bridge-claims/tools/approve_claims.py:202-256,375-449`; `bridge-claims/script/Deploy.s.sol:21-86`

The original risk was approval to an address that had not been tied to the exact BridgeClaims bytecode. The team changed both tools to derive the spender from exact creation code, constructor encoding, salt, and the canonical factory. The tools check the list hash, init-code hash, EIP-1014 address, runtime code, and all six getters.

**Evidence and team response:** The team's binding claim is supported by negative tests that reject changed factory, creation code, salt, list, address, runtime, and getter-compatible substitutes before output. The unchanged contract source and compiler artifact were rechecked in this review. No second-round regression was found. The external public manifest digest still has to be compared at signing; code consistency alone cannot authenticate an operator-controlled manifest.

### BC-H02 - Source-contract payees and destination control

**Original severity:** High  
**Affected invariants:** I2, I3, I8, I9  
**Status:** Verified remediated and published  
**Location:** `toolkit/scripts/bridge/build-bridge-allocations.py:167-216`; `toolkit/tests/test_bridge_allocations.py:580-602`

The first remediation conservatively changed exactly three source allocation rows to `held_contract`, which removed exactly two aggregated Ethereum payee-token rows and left the other four chains byte-identical. The validator-wrapper exception remains supported by official Harmony source:

- [`ValidatorWrapper` embeds `Validator`](https://github.com/harmony-one/harmony/blob/a41e194761c81fe846994864e789a7d306bbc439/staking/types/validator.go#L89-L93), and [`Address` is the ECDSA validator address](https://github.com/harmony-one/harmony/blob/a41e194761c81fe846994864e789a7d306bbc439/staking/types/validator.go#L224-L229);
- [validator creation and editing require the sender to equal `ValidatorAddress`](https://github.com/harmony-one/harmony/blob/a41e194761c81fe846994864e789a7d306bbc439/core/state_transition.go#L370-L392);
- [the wrapper is RLP-encoded into the account code field](https://github.com/harmony-one/harmony/blob/a41e194761c81fe846994864e789a7d306bbc439/core/state/statedb.go#L1385-L1403).

**Evidence and team response:** The team classified eight Harmony code-bearing payees as validator wrappers and held the remaining two actual contract payees. The prior independent rebuild changed three allocation rows, removed two aggregated Ethereum claim rows, and left the other four chains' claim files byte-identical. In this review, every indexed public file matched the committed digest; the public summary and manifests agreed. BC-H02's publication gate is closed. Historical key existence still does not prove present custody, and the Harmony archive accounts were not reread.

### BC-M01 - Deployment independent of Forge CREATE2 routing

**Original severity:** Medium  
**Affected invariants:** I6, I9, I11  
**Status:** Verified remediated  
**Location:** `bridge-claims/script/Deploy.s.sol:21-86`

The original script depended on Forge's CREATE2 routing, which diverged on Base. The team now calls the canonical deterministic-deployment proxy directly with `salt || initCode`, authenticates its code hash, independently recomputes the returned address, and checks runtime code and getters. Ten retained `20261029` broadcast-plan records, five chains under each of two Forge builds, each contain one transaction to the canonical proxy and the new committed contract address. Their redacted logs report `SIMULATION COMPLETE` and the expected runtime hashes. The records have no receipts and are simulation evidence, not deployed transactions. We independently reconstructed expected init and runtime hashes from the public manifests and compiled artifact, and reran the offline Forge suites.

**Closure condition:** The routing defect is closed. Each actual deployment still needs a fresh simulation and post-deployment check on its target chain.

### BC-M02 - Approval preflight and package finalization

**Original severity:** Medium  
**Status:** Verified remediated  
**Affected invariants:** I4, I7, I10  
**Location:** `bridge-claims/tools/package.py:149-185`; `bridge-claims/tools/approve_claims.py:389-497,707-724`

**Team response and evidence:** The team made signable approval generation require pinned RPC preflight and staged packages. The code checks Safe identity, roster, threshold, nonce, factory, runtime, getters, and funding, then seals every selected package before activation. Seal and activation exceptions now run through the invalidation handler. In scratch tests, injected second-target seal and rename failures each left no selected approval package current. The earlier stale-package finalization PoC no longer reproduces.

**Assessment:** The tested original failure path is closed. Our later overlap, quarantine, permission, and symlink PoCs expose real hardening gaps, but none showed a stale Safe hash being signed under the documented per-chain manifest, nonce, and transaction-hash checks. The preceding report treated those gaps as a continuing Medium finding and required all-chain atomic activation, although the runbook intentionally refreshes each chain separately. That rating and closure condition overstated the evidence. Residuals are recorded as BC-I11. The active checkout still lacks the owner roster, so it cannot generate a signable package here; that is an operational readiness gate, not proof that this code remediation failed.

### BC-M03 - Release recipient and pinned reads

**Original severity:** Medium  
**Status:** Verified remediated  
**Affected invariants:** I8, I9, I10, I11  
**Location:** `bridge-claims/tools/release_plan.py:477-506,509-554,1058-1090`

**Team response and evidence:** The planner derives the one configured custody Safe, rejects a different override, checks the roster and threshold, requires manifest funders to match, pins each chain's block, and rechecks its hash before publication. It also checks allocation-summary inputs, custody-plan and pool hashes, chain coverage, and the ONE amount. The new published summary and five manifests independently agree. These checks close the original arbitrary-recipient and unpinned-read release paths.

**Assessment:** A scratch PoC still makes `claim_run()` accept a manifest that disagrees with the summary, but those fields are recorded as release metadata. The release recipient, ONE amount, and transaction calldata come from the custody config, allocation plan, and pinned chain reads, so the PoC did not alter a release transaction. Approval generation separately binds its manifest to CSV, constructor code, and spender; the signing procedure requires an independent public digest comparison. The preceding Medium rating depended on substituted local artifacts *and* signers skipping that comparison. That exceeded the demonstrated impact of BC-M03. Exact cross-file matching and ready-label enforcement remain worthwhile hardening in BC-I12. Live state must still be refreshed before signing.

### BC-L01 / BC-L06 - Signer-facing token identity

**Severity:** Low  
**Status:** Code remediated; live package to verify  
**Affected invariant:** I10  
**Location:** `bridge-claims/tools/approve_claims.py:440-461,590-666`

The tool now reads `decimals()` and `symbol()` at the pinned block, rejects a decimals mismatch, and shows a differing on-chain symbol next to the bridge label in `APPROVALS.md`. The unit test exercises `USD₮0` and a decimals mismatch. This addresses the previous CAKE, USDT-family, and MOODENG presentation issue in code; the current on-chain symbols and final signer sheet have not been independently reread in this pass. Token address and checked decimals remain the signing identity.

**Signing check:** Independently decode the actual token address, decimals, and amount in each call at the current Safe nonce; a displayed symbol is advisory.

### BC-L04 - ONE stage-1 batch nonce

**Severity:** Low  
**Status:** Snapshot rebuilt; signing still pending  
**Affected invariants:** I8, I10  
**Location:** `bridge-claims/tools/release_plan.py`; canonical `release/release-plan.json`

The old Reserve Safe batch targeted nonce 58. The selected new release plan records planned and observed nonce 61, one ONE stage-1 transfer, and no escrow-release batch. Its seal and private inventory digest pass. The team reports a fork replay that funds the exact ONE approval. We did not execute the transfer or independently refresh the Reserve Safe nonce and balance in this pass. Rebuild and decode the release package at a freshly pinned block immediately before signing; nonce 61 is evidence of the stored snapshot, not a standing instruction.

### BC-L05 - Nonzero allowance reset

**Severity:** Low  
**Status:** Code remediated; actual batch to recheck  
**Affected invariants:** I4, I8, I10  
**Location:** `bridge-claims/tools/approve_claims.py:440-461,535-544`; `bridge-claims/chains.json`

The team reports that real CRV and AAG reject nonzero-to-nonzero `approve`, like USDT. Signable mode now reads each allowance at the pinned block and inserts `approve(0)` before a nonzero approval whenever the existing allowance is nonzero; an isolated test passed. The chain config lists USDT, CRV, and AAG for draft batches without RPC. This fixes the documented path in code. Each final Safe batch still needs independent decoding and simulation against live token state.

### BC-L07 - Display amounts in published claim CSV

**Severity:** Low  
**Status:** Verified remediated in the replacement public package  
**Affected invariants:** I2, I9, I10  
**Location:** `bridge-claims/tools/build_claims.py:129-132`; public `bridge-allocations-20261029/*/claims.csv`

The old formatter could print scientific notation and, according to the team, overstated 992 dust display cells by a factor of one billion. The new formatter uses integer unit conversion. A row-by-row comparison found no changes to index, account, token, symbol, `amount_raw`, or pool fields. Display `amount` changed in 86,167 rows across three chains; every new display value equals the raw amount at its token's decimals. Trees and roots are unchanged because leaves use `amount_raw`, but list hashes, CREATE2 addresses, and expected runtime hashes change on Ethereum, BSC, and Arbitrum. The replacement public package and every proof were independently checked. No signer should reuse a `20261028` address, manifest, or approval.

### BC-L08 - Hostile native recipient gas cost

**Severity:** Low  
**Status:** Documented operational limit  
**Affected invariant:** I8  
**Location:** `bridge-claims/src/BridgeClaims.sol:228-237`; `bridge-claims/docs/DESIGN.md`; `bridge-claims/docs/RUNBOOK.md:147-156`

The native send forwards gas to the recipient. A recipient that consumes it and reverts can leave too little gas for the wrapped-token fallback; the whole claim then reverts and remains unpaid. The team reports about 3.6 million call gas for its hostile-recipient case and has added relayer guidance to estimate and cap gas per row. We did not rerun that benchmark. This is a relayer-cost and availability limit, not a route to redirect value. Relayers should skip pathological rows and publish a way for their payees to self-claim or request a higher-gas attempt.

## Informational observations

### BC-I01 - Solidity 0.8.36 advisory assessment is unchanged

The source and compiler configuration are unchanged. The original review found no source-level trigger for the relevant low-severity optimizer advisory. A compiler upgrade would change creation code and CREATE2 addresses, so it requires regeneration and review of every manifest, approval, and deployment artifact.

### BC-I02 - Token success remains an issuer and proxy trust assumption

The contract intentionally follows SafeERC20-style return handling. It cannot prove that a token returning success actually changed the recipient's balance. Issuer blocks, proxy upgrades, fees, pausing, or no-op behavior can still impair a valid row. Per-asset canaries with balance-delta monitoring and a tested revoke path remain mandatory.

### Second-pass documentation and tooling observations

The response records other informational changes: `proof.py` now puts `--chain` in its cast command; the public-package scanner adds an RPC-key URL pattern; and the design and runbook now describe OpenZeppelin tree construction differences, correction ordering, explorer verification settings, hardware-wallet display limits, gas figures, and current PAXG fee behavior. The code and text changes are present. The whole-repository hygiene scan is still red on unrelated binary PDF files, so its new pattern was not validated as a clean publication gate. BC-I03's legacy manifest field name remains unchanged. BC-I09's dust-claim funding policy is still open and should be settled before opening claims.

### BC-I11 - Package coordination and failure reporting

**Severity:** Informational  
**Affected invariants:** I7, I10  
**Location:** `bridge-claims/tools/package.py:60-72,109-185`; `bridge-claims/tools/quarantine.py:58-95`  
**Team response:** The team claims failed refreshes leave nothing signable and refreshes cannot interleave.

The selected-target seal and rename failures now clear current packages, closing BC-M02. Remaining PoCs show that all-label and single-label commands take different locks; quarantine takes no package lock and can partially move files; a persistent filesystem denial can leave a target current while printing `COULD NOT CLEAR`; and a symlinked label parent can redirect a single-label package. An overlap left one chain's approval absent and another current. That is not itself a stale or wrong signed hash: approvals are intentionally refreshed per chain, and no PoC bypassed independent manifest, Safe nonce, and transaction-hash checks. A run-root lock for every operation, link rejection from the run root, and recoverable quarantine would reduce operator ambiguity. Regrade if a stale hash can be signed under the stated procedure.

### BC-I12 - Redundant claim-run consistency checks

**Severity:** Informational  
**Affected invariants:** I9, I10  
**Location:** `bridge-claims/tools/release_plan.py:509-554`; `bridge-claims/tools/approve_claims.py:707-724`  
**Team response:** The planner binds release to the claim run; approvals default to ready summary entries.

`claim_run()` accepts a scratch manifest whose root, chain ID, count, list hash, address, and CSV differ from the unchanged summary. The mismatch is recorded in release metadata but does not change release destination, amount, or calldata. Explicit approval `--labels` also selects a directory without checking summary-ready status, although the approval builder still checks manifest/CSV/bytecode bindings and signers must compare published digests. Compare all duplicated summary fields with each manifest, hash the CSV, and require explicit labels to be ready. This strengthens cross-file integrity without treating the current metadata PoC as a Medium release exploit.

## Invariant verdicts

| ID | Verdict | Basis and remaining gate |
|---|---|---|
| I1 | Pass | Contract source unchanged; bitmap, rollback, batch, and reentrancy evidence remains applicable. |
| I2 | Pass for published package | The current independent Go verifier recomputed every leaf, node, and one proof for every 836,754 rows in the new public package. |
| I3 | Pass for published package | Leaves bind recipients; validator exceptions are source-supported; actual contract addresses are held. |
| I4 | Pass at code level; live allowance pending | Spender, runtime, and exact approval amounts are bound; final on-chain allowance and funding require execution-time checks. |
| I5 | Pass for contract-controlled flows | Claims transfer directly and sweeps pay only the immutable funder; forced transfers or unusual token behavior remain external assumptions. |
| I6 | Pass at code and dry-run level | Direct CREATE2 routing and exact runtime binding are fixed; execution-time simulation and post-deploy checks remain. |
| I7 | Pass at code level; execution pending | A Safe `approve(spender, 0)` halts token claims; a revoke batch exists but must be rebuilt at the live nonce. |
| I8 | Not established for production | ONE stage 1, deployments, approvals, exact balances, token behavior, and canaries remain incomplete. |
| I9 | Pass for published bytes | The new public index, manifests, lists, roots, addresses, and independently rebuilt proofs and expected runtimes agree; post-deployment immutable checks remain. |
| I10 | Not established for production | No Safe transaction was signed in this review. This checkout lacks the canonical run and owner roster; actual calls and hashes need independent live-nonce checks. |
| I11 | Conditional | The deployment factory is public and release calls are on-chain; issuer, Safe signer, artifact-distribution, and execution-time assumptions remain external. |

## Per-chain production decision

| Chain | Sign approvals | Deploy | Open claims |
|---|---|---|---|
| Ethereum | **NO from this checkout** - ONE stage 1 and a current approval package are absent | **CONDITIONAL** - materialize the verified new manifest and run a fresh target-chain simulation | **NO** - stage 1, deployed code, allowance, and canaries unverified |
| BSC | **NO from this checkout** - archived approval has not been independently decoded | **CONDITIONAL** - materialize the verified new manifest and run a fresh target-chain simulation | **NO** - deployed code, allowance, and canaries unverified |
| Arbitrum | **NO from this checkout** - archived approval has not been independently decoded | **CONDITIONAL** - materialize the verified new manifest and run a fresh target-chain simulation | **NO** - deployed code, allowance, and canaries unverified |
| Base | **NO from this checkout** - archived approval has not been independently decoded | **CONDITIONAL** - materialize the verified manifest and run a fresh target-chain simulation | **NO** - deployed code, allowance, and canaries unverified |
| Linea | **NO from this checkout** - archived approval has not been independently decoded | **CONDITIONAL** - materialize the verified manifest and run a fresh target-chain simulation | **NO** - deployed code, allowance, and canaries unverified |

Deployment is conditional because the exact `20261029` bundle and expected runtime hashes are verified offline, while the canonical run is absent locally and live target-chain state was not refreshed. BC-M02/BC-M03 and the stale Python package assertions do not change the CREATE2 deployment code path. Materialize and authenticate the bundle, then run and retain a fresh simulation and post-deployment code/immutable read for each chain.

Signing may move to Go only after the owner roster is independently attested, the local run matches approved digests, rejected files are quarantined locally, and fresh packages are independently decoded and reproduced at current nonces. This report does not authorize use of the archived Transaction Builder JSON files.

Opening may move to Go only after deployed code and all six immutables are checked on real nodes, exact funding and allowances are confirmed, random and negative proofs are exercised, and per-asset canaries succeed with a documented revoke and abort process.

## Deployment commitments

| Chain | Contract address | Root | Init-code Keccak |
|---|---|---|---|
| Ethereum | `0xcc0407394a4d7eb59f894abbb9c005631183d49a` | `0x964894effbc2fcaa5e87b9583ae6636959dd23fc1d4f2c3cd97db12d52f2523b` | `0xc2e0b6e1c3c316f37e53384093b6550eb07d04dc76150301e78c093cb70464cc` |
| BSC | `0xaffa52187721fb4165071349bbd8c55c8d4356fc` | `0x734794900e6066eac22a05de2031fa848fcf59ac6f8da10fac1de211bb9c81c4` | `0xa562128309af307bf4cab369a18ef1567f0426a0672ed3f04a315f6520183284` |
| Arbitrum | `0x2cf2afd2b8be95f9ba5e8a6b021e7895d6729469` | `0x39e92752231cebd64190cb5cf44e63ae75d38a1d42b63b85c3dbae48ae1037df` | `0x2d0a3eb67c27e37be26eaeb1f39ae8a6ecf53a6394c06c0630b3814ff4f0b3e0` |
| Base | `0xa2bddf296a5a945f0bb92e71629a3e78f401adbb` | `0x183e4b527d29eea5426e643c22ba0d21be7488d207db4cdfdd7278e68dcefe06` | `0x346d2cb3053c6039ad9ab5816c617457178668d9cbaf3aab2b2faa0df46498c7` |
| Linea | `0x8a5607026b92977c34aba988c5e22fc8b615ceaf` | `0xa891a83a107cba0d21c67e9038061be945b2c62e3af6b7bd2299456bb7902772` | `0x7c4b9982fb71fe9ebfcfcde607606508512585bd5da25e465825991fb70c5668` |

### Expected deployed runtime hashes

| Chain | Runtime code Keccak | Verification |
|---|---|---|
| Ethereum | `0xed9957a85f2ff3d089aa2f5cd760ce4afbdb1743b3d66770caeddfda26bdd2d3` | Independently reconstructed expected runtime; team dry-run record |
| BSC | `0xdb4763abb510a261308e26bd3a80e5fa1d32b6f9eabcb08e011858d4311ffc93` | Independently reconstructed expected runtime; team dry-run record |
| Arbitrum | `0xccde05f46873541165bd89c0c6fd06c2cb00a13b5ddb3423a5acbc1f198ba0b9` | Independently reconstructed expected runtime; team dry-run record |
| Base | `0xa301f9ed766c50c19b603208641a535e15d534c023a11e127b031a4825f33db7` | Independently reconstructed expected runtime; team dry-run record |
| Linea | `0x62b4fc5e0faff1ed42efd514607ac655ac09bdaadebdb29efda5719ff753301d` | Independently reconstructed expected runtime; team dry-run record |

## Artifact commitments

The following public artifacts were downloaded from the canonical prefix and independently verified against its checksum index. A matching hash establishes artifact identity, not authorization to sign, deploy, or open.

| Chain | Artifact | SHA-256 |
|---|---|---|
| Ethereum | `manifest.json` | `09f57711875d598b3b1f334ea46e4323ee07858b75521bebe8a2189ad54b9fee` |
| Ethereum | `claims.csv` | `a932076291d645a03f9e67511ae64632c394c2d21afc9cc1093bbc7fe6af9707` |
| Ethereum | `tree.txt` | `39f3c0476bc1170809d0280dd01de78fbd710797ea3411486ed268609d413221` |
| BSC | `manifest.json` | `9284c27532202a576f9acb7e7d8ab581e78e583fa2941cc5771b3e789ce7fdd3` |
| BSC | `claims.csv` | `370c274681f08ab5b16d8bc697944e0a03d3ea35672e9aa793e9db5eaa768b8f` |
| BSC | `tree.txt` | `0f92c14d845a4f26df49528d31e01819f475c890628a3c52d840e2696ced4c72` |
| Arbitrum | `manifest.json` | `e799b90745e97fb3803af342d3292772cfb059d7315e6a32a617500b4dce4861` |
| Arbitrum | `claims.csv` | `63bcab8c72823df0ef275f6cac7ae02e7a1c8ef8d937a0d2a78048ae4f54f0d1` |
| Arbitrum | `tree.txt` | `f5028f2f4bbd5a4fb157f005907edd2cc7ce76ff7925187a7242e510d444845f` |
| Base | `manifest.json` | `ce21f68a73f1bf332862f9b90562f02f560a2a3fdb9ac112c0332eb66dea9233` |
| Base | `claims.csv` | `63f1b1142df38f4531fdd8c536662820668450951834c21b76a8fff9e0dff1e0` |
| Base | `tree.txt` | `ed2872198d95bdc46508331bbd0da5595e8f1de1210d17ceefa43e854412a9e9` |
| Linea | `manifest.json` | `7179e94698f24201a530af677fd9fe2a46e13953df3f2b7f175ff411711ca0bf` |
| Linea | `claims.csv` | `30e030491a0e183cc9f74312b2c88b5a923ab3a21b044a07f613ef36c28f21af` |
| Linea | `tree.txt` | `6fa72956f7c6b7ef3be7f0304ad432a01df9b7deb6a9909e1568201a4bbc66ba` |

### Stored Safe Transaction Builder JSON commitments

These hashes identify the selected private snapshot only. The approval and release files must be regenerated, independently decoded, and checked against fresh Safe state and the published manifest digests before signing. The old local files are rejected.

| Chain | Transaction Builder JSON | SHA-256 |
|---|---|---|
| BSC | `approval/safe-approve.json` | `d4e213ae4a15c097f09b8aeb69896af681c9536af2ab7db7974c6394546716ed` |
| BSC | `approval/safe-revoke.json` | `1906838e43846b10a0fde5f91b229d32818f6511da6b7a2c855a161b480bc353` |
| Arbitrum | `approval/safe-approve.json` | `ca82f63ded0a6e976115cc105ef9d4d004df7efaeee815afa49707d4571755fc` |
| Arbitrum | `approval/safe-revoke.json` | `0c1dfe3cc4a949885e8552ce84ff71abbde4d617e2491e50a3f601a00f421cda` |
| Base | `approval/safe-approve.json` | `69cd770a8154787e7e79b19e7731262f101e33831782df1c8e2158e0c19d95ad` |
| Base | `approval/safe-revoke.json` | `138501155658a6f99068ffa5527180ad6b62f1f45ffbc218d96fee08d6e2bd88` |
| Linea | `approval/safe-approve.json` | `17bc2e23028d5d7d8c7c3c2ea9cc2cd51f23f1347cb5d2847b365d20d08b2475` |
| Linea | `approval/safe-revoke.json` | `9b57ef0c376b045818d324babe6d1947731aa7d6cda4414fa5c18d953287695f` |
| Ethereum Reserve | `release/0-one-reserve/ethereum-0xf868743b-safe.json` | `d5f43eef139302c4b2f2f77f333ffe0e09c98fd1b82797591c9420303fc948f7` |

These hashes identify the selected archive only. The old local run still contains rejected signable files; no file under it is authorized for signing regardless of hash. The current run is absent locally, and the selected private packages predate the latest package format.

## Production gates and recommended hardening

1. Restore the canonical `20261029` run into the signer environment, compare it with the verified public and private digests, persist and independently attest the four-owner custody roster, and quarantine rejected local signing files. Treat archived private batches as evidence, not signing inputs.
2. Rebuild the release plan at current pinned state, independently decode it, verify Reserve Safe nonce and balance, then sign and execute only the required ONE stage-1 transfer.
3. Generate approvals per chain at current nonces and finalized blocks; independently reproduce every call, public manifest checksum, and Safe transaction hash before signing. Recheck nonzero allowance resets, token addresses, and decimals.
4. Run and retain fresh deployment simulations for the verified addresses, deploy, and verify runtime and all six immutables on real nodes. Confirm exact funding and allowances and execute per-asset canaries before public opening. Decide the relayer policy for uneconomic dust rows.
5. Repair the 22 stale Python assertions for `.packages.lock` and `GENERATION` and rerun the full tool suite before relying on it as a regression gate. This QA repair is not an additional Medium security finding.

Recommended BC-I11/BC-I12 hardening: use one run-root lock across refresh and quarantine; reject symlinks from the run root; make quarantine recoverable; compare duplicated summary/manifest fields and CSV digests; and require explicit approval labels to be ready. These improvements should be tested, but the current PoCs do not justify making them prerequisites to deployment or keeping BC-M02/BC-M03 open.

## Limitations and evidence boundary

- Public verification covered all 22 objects named by the *new* `SHA256SUMS` and all rows, nodes, and proofs; the separately served `README.md` is outside that index.
- Private verification covered 61 selected files, including five sealed packages and ten dry-run record sets, not all 486 indexed private objects or every historical escrow transaction.
- The reviewed checkout differs from the canonical private snapshot. Its active config lacks owner addresses, the replacement run is absent, and old local signable and release files remain. The selected private packages lack the latest `GENERATION` file.
- Private package checks recomputed file hashes and seals and matched manifest hashes. They did not independently decode every Safe call, reproduce every Safe transaction hash, or rerun the team's fork replay; the JSON hashes in this report do not authorize signing.
- The public and private object locations did not present immutable versioning or retention-lock evidence. The digests in this report are comparison commitments for the bytes reviewed; authenticity still depends on trusted distribution of the report and digest index.
- The pinned RPC snapshot in the preceding report is historical. No fresh live RPC snapshot of the replacement addresses and Safe state was completed in this pass; stored nonces require finality-aware revalidation at use time.
- Expected runtimes were independently rebuilt offline, not read from deployed contracts. Deployment, runtime, immutable, allowance, funding, canary, and explorer checks remain future gates.
- Current fork suites were not run because their external inputs were not configured. Offline Forge suites passed, but the Python tool suite is red on 22 stale assertions.
- The Harmony archival node remained unavailable. The review relied on frozen classifications, known-app membership, official source behavior, and the conservative published allocation result.
- Present custody of historical validator keys and intended owner-device displays were not proven.
- Future proxy upgrades, issuer actions, fees, blacklists, pauses, and token behavior can change claimability after this report.
- Escrow release received integration-level remediation review, not a full audit of every external LayerZero, Safe, token, or escrow implementation.

## Model and program disclosure

Fable 5.1 Max, GPT-5.6, GPT-6 Astra Ultra, Daybreak Blue, and Mythos 5.1 were used for preparing and reviewing this audit. Model output was advisory; findings were accepted only when supported by code, tests, traces, pinned reads, or independently reproduced hashes.

hiddenstate.xyz participates in OpenAI's Trusted Access for Cyber (TAC) and Anthropic's Cyber Verification Program.
