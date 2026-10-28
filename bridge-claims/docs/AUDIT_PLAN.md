# BridgeClaims audit plan

Scope: `bridge-claims/src/BridgeClaims.sol`, the setup and deployment process that puts one copy of
it on each of five chains (list build, custody-Safe funding, approvals, CREATE2 deployment), and the
claim process payees go through. This document is the plan; a separate agent performs the audit and
writes the report. Drafted 2026-10-28 against repository commit `70371fc` (last change under
`bridge-claims/`: `440ec9b`) and run `runs/bridge-allocations-20261018`.

The auditor should treat every statement in this plan as a hypothesis to confirm, not as a fact.
Where the plan says "the contract does X", read the code and check.

---

## 0. Ground rules for the auditing agent

- **Read-only on the repository's generated artifacts.** Never regenerate anything under
  `bridge-claims/runs/` in place; write scratch output to `/tmp` or a directory you own. `runs/` is
  gitignored because it holds address-level amounts under an embargo: do not copy it out of the
  machine, paste rows into the report, or publish it.
- **Chain data sources.** Non-Harmony chains: the paid Alchemy archive RPC only (URLs in
  `harmony-migration/bridge/local/rpc.env`; load with `set -a; . ../bridge/local/rpc.env; set +a`).
  Harmony: the shard-0 archival node only. Never an explorer website or API, never a free public RPC
  for anything recorded in the report. Never print, log, or commit RPC URLs or keys; record hosts only.
- **Forge writes the RPC URL into `cache/`.** Delete `bridge-claims/cache/Deploy.s.sol/` and any
  `cache/*/` fork caches after fork runs. Check nothing under `broadcast/` contains a URL before it
  is published.
- **Proof-of-concept tests** go outside the repository (for example `/tmp/bridge-claims-audit/`
  with a `foundry.toml` whose `src`/`libs` point at the repo) unless the team asks to keep them.
- **Report** as Markdown (`bridge-claims/docs/AUDIT_REPORT.md`), using the rubric in §9.
- **Live state moves.** The custody Safe is mid-release (see §7.0). Snapshot live state at a pinned
  block before reasoning about it, and re-snapshot before signing off; do not trust the nonce,
  balances, or "not yet" flags in any generated `APPROVALS.md` or `RELEASE.md`.

---

## 1. System under audit

### 1.1 What it does

For each chain, one `BridgeClaims` contract pays a fixed, published list of rows
`(index, account, token, amount)`. The list is committed to by a Merkle root baked into the contract
at deployment. Tokens never sit in the contract: each claim does `transferFrom(funder → account)`
against an ERC-20 approval the custody Safe gives the contract for exactly each asset's total.
Native ETH/BNB rows are funded as WETH/WBNB; the contract pulls the wrapped coin, unwraps, and
sends, falling back to the wrapped token if the account rejects the coin. There is no owner, admin,
pause, deadline, or upgrade; the only control is the Safe's allowance.

### 1.2 Trust model (to be confirmed)

| Party | Can | Cannot (claimed) |
|---|---|---|
| Custody Safe (`0x19565f47…b06c`, 2-of-4, same address on all five chains) | stop/resume any asset by changing allowance; receive sweeps | change the list, the root, or who gets paid |
| Anyone | submit any valid row's claim (relayer model); call `sweep` | redirect tokens; pay a row twice; forge a row |
| Deployer (any EOA) | put the contract at its predetermined address | influence any behaviour or address |
| Token issuers (USDC, USDT, BUSD, PAXG, …) | block transfers (blacklist/pause) | anything else |
| WETH/WBNB | are trusted canonical contracts | — |
| LayerZero (release step only) | nothing off-chain is relied on; only deployed endpoint + ReceiveUln301 code | — |

### 1.3 Deployments in the current run

| Label | Chain | Rows | Payees | Assets | Native rows | Address (CREATE2, not yet deployed) |
|---|---|---:|---:|---:|---|---|
| ethereum | 1 | 187,007 | 184,845 | 27 | ETH via WETH | `0x817eda9dd7461176873a28be5777d6f7918dac04` |
| bsc | 56 | 620,276 | 495,131 | 15 | BNB via WBNB | `0xffcc6017f66a924012017115c6b4aa205037a84e` |
| arbitrum | 42161 | 27,839 | 25,498 | 3 | none | `0x69900956b2c4ceb5a257a19a3243e84822c2d7e2` |
| base | 8453 | 245 | 245 | 2 | none | `0xa2bddf296a5a945f0bb92e71629a3e78f401adbb` |
| linea | 59144 | 1,389 | 1,352 | 2 | none | `0x8a5607026b92977c34aba988c5e22fc8b615ceaf` |

Roots, totals, and per-asset tables: `runs/bridge-allocations-20261018/CLAIM_CONTRACTS.md` and
each `<label>/manifest.json`. Tree depth: 18 proof nodes on Ethereum, 20 on BSC.

### 1.4 Files in scope

| File | Role |
|---|---|
| `src/BridgeClaims.sol` | the contract (288 lines) |
| `script/Deploy.s.sol` | CREATE2 deployment from a manifest, with checks |
| `foundry.toml`, `chains.json` | pinned compiler settings; per-chain constants (wrapped native, approve-reset tokens, CREATE2 deployer and salt) |
| `tools/claim_tree.py` | leaf/tree/proof/CREATE2 in Python (uses `airdrop/tools/common.py`) |
| `tools/build_claims.py` | allocation claim lists → `claims.csv`, `tree.txt`, `samples.json`, `manifest.json` |
| `tools/approve_claims.py` | Safe Transaction Builder approve/revoke batches, signing sheet, chain preflight (uses `airdrop/tools/safe_batch.py`) |
| `tools/proof.py` | payee lookup, proof output, `claimMany` cast command |
| `tools/release_plan.py`, `tools/send_calldata.py` | escrow release to the custody Safe (prerequisite funding, §7) |
| `test/BridgeClaims.t.sol`, `test/TreeFixture.t.sol`, `test/Fork.t.sol`, `test/EscrowRelease.t.sol`, `test/lib/ClaimTree.sol`, `test/mocks/Mocks.sol` | existing tests |
| `docs/DESIGN.md`, `docs/RUNBOOK.md`, `docs/RELEASE.md`, `README.md` | design and process as documented |
| `runs/bridge-allocations-20261018/**` | the concrete artifacts that will be signed and published |

Out of scope (but see §4.6 for the boundary checks): how the allocation pipeline decided who is
owed what (`bridge/`, `docs/bridge-allocations.md`). The audit takes `allocations/claims/<chain>.csv`
as given and verifies everything downstream of it.

---

## 2. Security properties to establish

The audit should end with an explicit verdict on each invariant, with the evidence (test, trace,
RPC read) that supports it.

| ID | Invariant |
|---|---|
| I1 | A row pays at most once, under any interleaving, reentrancy, or batch composition. |
| I2 | Only rows of the published list can pay: no (index, account, token, amount) outside `claims.csv` verifies against `root`. |
| I3 | Value from a claim reaches only the row's `account` (coin, or wrapped coin as fallback), never the submitter or a third party. |
| I4 | The contract can never move more of a token than the Safe's current allowance; the allowance given equals the sum of that asset's rows. |
| I5 | The contract holds no assets between transactions; anything it does hold can leave only to `funder`. |
| I6 | Nothing is mutable after deployment; the address commits to bytecode and all six constructor arguments; no selfdestruct/delegatecall/upgrade path. |
| I7 | The Safe can halt any asset at any time with a single `approve(spender, 0)`, and resume by re-approving. |
| I8 | Every row in the list is claimable given funding and allowance, except for token-level blocks (blacklist/pause) that the contract cannot influence. |
| I9 | Published artifacts and on-chain immutables agree: `root`, `claimCount`, `listHash == sha256(claims.csv)`, `funder`, `wrappedNative`, `chainId`, and the CREATE2 address are all mutually consistent and independently recomputable. |
| I10 | Every Safe transaction the team signs does exactly what its signing sheet says, at the Safe's live nonce, with hashes an independent implementation reproduces. |
| I11 | Nothing in the process depends on a party that can be withheld or coerced beyond the 2-of-4 Safe (no LayerZero off-chain services, no privileged deployer, no tooling-only secrets). |

---

## 3. Phase A — contract review (`src/BridgeClaims.sol`)

Read the whole file first, then work through this checklist. For each item record: confirmed /
refuted / finding, with a one-line justification or a PoC.

### A.1 Construction and immutables

- [ ] All six parameters are `immutable`; no storage writable after construction except `_claimedBitmap`.
- [ ] `funder != 0`, `root != 0`, `claimCount != 0`, `chainId_ == block.chainid` enforced. Decide whether `wrappedNative_ == 0` together with native rows should also revert at construction (currently only at claim time via `NativeNotSupported`); check the builder refuses that combination (`build_claims.py` "native rows but no wrapped_native").
- [ ] `listHash` is informational only; confirm it is never used in a check (so a wrong value is a publication bug, not a fund bug).
- [ ] No `selfdestruct`, `delegatecall`, `create`, proxy pattern, or assembly beyond `_sendNative` and `_hashPair`. Review both assembly blocks for `memory-safe` correctness (scratch space 0x00–0x3f only in `_hashPair`).

### A.2 Merkle verification

- [ ] Leaf = `keccak256(bytes.concat(keccak256(abi.encode(index, account, token, amount))))`: 32-byte preimage for the outer hash vs 64-byte preimage for inner nodes. Confirm this makes leaf/inner-node confusion infeasible (second-preimage attack of OpenZeppelin's note).
- [ ] Sorted-pair hashing (`h < p ? hash(h,p) : hash(p,h)`) with the "unpaired last node carried up unchanged" construction (`ClaimTree.levels`, `common.tree_levels`). Confirm proofs are the sibling at every level that has one, and that proof length is not required to equal depth. Reason about whether position-free proofs plus carry-up admit any proof malleability that produces a valid proof for a leaf not in the tree (it should not without a preimage; write the argument down).
- [ ] Differential test: for random lists of 1–300 rows, compare `_verify`'s acceptance with OpenZeppelin `MerkleProof.verify` (same sorted-pair convention) on the same (root, proof, leaf), including altered leaves, truncated proofs, extended proofs, reordered proofs, and empty proofs.
- [ ] Single-row list: `root == leaf`, empty proof verifies; two-row list; odd counts at every level (e.g. 3, 5, 7, 9, 2^k+1).
- [ ] Index binding: identical `(account, token, amount)` at two indexes yields distinct leaves; `index >= claimCount` rejected before any proof work.
- [ ] Cross-chain / cross-contract replay: a proof from chain A's tree is useless on chain B (different root), and a leaf never encodes chain or contract address — confirm this is fine given one root per contract.

### A.3 `claim`, `claimMany`, bitmap

- [ ] Checks-effects-interactions: bit set and `Claimed` emitted before any external call (`_claim`). Confirm no code path performs an external call before the bit is set.
- [ ] Bitmap arithmetic: `index >> 8`, `1 << (index & 0xff)`; `isClaimed` and `_claim` use the same word/bit; `claimedBitmap(word)` matches. Fuzz indexes up to 2^20.
- [ ] `claimMany`: skip condition `c.index < claimCount && isClaimed(c.index)` — an out-of-range index falls through to `_claim` and reverts with `IndexOutOfRange`; an already-claimed index is skipped **without** proof verification (so a malformed row with a claimed index is silently accepted — confirm this is benign and document it). Any other failure reverts the whole batch; `paid` counts only rows this call paid.
- [ ] Reentrancy (the only external calls are to tokens, WETH, and `account`): from `account.receive()` during a native payout, try re-entering `claim` (same row, other rows), `claimMany`, `sweep(NATIVE)`, `sweep(wrapped)`. Confirm I1/I3/I5 hold. Write the reentrancy PoC with a nested *different* native row owned by the attacker and confirm accounting.
- [ ] Gas griefing by `account`: a receive hook that burns all forwarded gas then reverts leaves 1/64 for the fallback (`deposit` + `transfer`). Confirm the worst case is a revert of the submitter's own transaction (retry with more gas), never a stuck or mis-paid row. Measure the minimum gas at which a hostile-recipient claim succeeds.
- [ ] Revert surfaces: `TransferFailed` carries `(token, to, amount)`; bubble-up of token revert data is intentionally dropped — decide whether that hurts payee support (e.g. USDC blacklist reasons) and note as informational.
- [ ] Events: `Claimed` indexed on (index, account, token); `PaidWrapped`; `Swept`. Sufficient for monitoring the I4 sum per token.

### A.4 Native path (`_payNative`, `_sendNative`, `receive`)

- [ ] Sequence: `transferFrom(wrapped, funder → this)`, `withdraw(amount)`, `_sendNative(account)`; on failure `deposit{value}` then `transfer(wrapped → account)` and `PaidWrapped`. Confirm the contract ends every path with zero coin and zero wrapped balance.
- [ ] WETH9 `withdraw` pays with `transfer` (2,300 gas). `receive()` compares `msg.sender` to an immutable and reverts otherwise: confirm it fits in 2,300 gas under Cancun pricing on **each** chain's wrapped contract (WETH `0xc02a…`, WBNB `0xbb4c…`); confirm both are WETH9-style (`withdraw` → `msg.sender.transfer`), not a variant that forwards full gas or uses `call`.
- [ ] `_sendNative` forwards all gas and copies no return data (return-bomb safe). Confirm `call` with `value` moves the coin before the callee runs, so the contract's coin balance is zero during the callback.
- [ ] Forced coin (`selfdestruct`-funded pre-Cancun, or coinbase/precompile oddities) can only be recovered to `funder` by `sweep(NATIVE)`.
- [ ] Payee equal to `wrappedNative` (WETH itself as an account): sending coin to WETH mints WETH to this contract — funds end in the contract, recoverable via `sweep(wrapped)` to the funder; the "payee" gets nothing. Classify (upstream list hygiene; see §4.6 anomaly scan).
- [ ] Contract without `wrappedNative` (Arbitrum, Base, Linea): native rows revert `NativeNotSupported`, `available(NATIVE) == 0`, `receive()` rejects everyone.

### A.5 Token calls (`_tokenCall`, `_safeTransfer(From)`, `sweep`, `available`)

- [ ] `_tokenCall` semantics: revert → false; `ok` with empty return → true iff `token.code.length != 0`; `ok` with ≥32 bytes → first word must equal 1; `ok` with 1–31 bytes → false. Build a mock for each case plus: returns `true` in >32 bytes, returns a non-boolean word (e.g. the amount), returns 2. Decide which real listed tokens could hit the non-standard branches (then verify them in §3.6 fork runs).
- [ ] Code check happens *after* the call. Under EIP-6780 a token cannot selfdestruct mid-call outside its creation transaction; confirm no residual issue.
- [ ] `_safeTransferFrom` error reports `to` not `from`; fine, note.
- [ ] `sweep(token)`: anyone can call; destination always `funder`; zero-balance sweep may revert for tokens that reject zero transfers (harmless). Confirm `sweep` cannot be used during a claim to divert funds (no reentrancy window while the contract holds wrapped coin). Confirm sweeping `wrappedNative` to the Safe is harmless.
- [ ] `sweep(NATIVE)` → Safe 1.3.0 accepts coin via its fallback handler/`receive`; confirm on a fork that the Safe proxy at `0x19565f47…` accepts a plain value call.
- [ ] `available(token)` = min(allowance, balance); for `NATIVE` reads the wrapped token; returns 0 if asset is zero. Informational only; confirm no code path depends on it.

### A.6 Compiler, EVM target, bytecode

- [ ] `solc 0.8.36`, `evm_version = cancun`, optimizer 20,000 runs, `via_ir = false`, `bytecode_hash = none`, `cbor_metadata = false`. Check the Solidity known-bugs list for 0.8.36 against the features used (abi.encode, bytes.concat, calldata arrays, assembly).
- [ ] Which Cancun/Shanghai opcodes does the runtime bytecode actually contain (`PUSH0`, `MCOPY`, `TLOAD/TSTORE`)? Disassemble and list them. Then confirm each target chain executes them **on the real node**, not in a Foundry fork (Foundry forks run revm with the local `evm_version`, so `Fork.t.sol` passing proves nothing about opcode support on Linea/BSC/Arbitrum/Base). `DESIGN.md` says `eth_call` probes were done; re-run them (`PUSH0`, `MCOPY`, `TSTORE` probes via `eth_call` with `data` = tiny init code or via `eth_call` on a probe contract with state override) and record block numbers. After deployment, call `verify()`/`leaf()` on the real RPC for sample rows as the definitive check (§6.5).
- [ ] Reproducible build: fresh clone, `forge build`, compare `keccak256(creationCode)` with `deployment.bytecode_keccak256` and `init_code_keccak256` in every manifest; repeat on a second machine or in a clean container. Note the installed `forge` is a nightly (`1.6.0-nightly`, 2026-03-07); the solc version, not forge, determines bytecode, but confirm forge's `solc` download is the official 0.8.36 release (`+commit.8a079791`).
- [ ] Gas: single claim ≈ 50k execution + calldata (proof up to 20 × 32 bytes on BSC); deployment ≈ 1.6M (`0x187779` in today's dry runs). Measure `claimMany` with 1, 5, 27 rows (the max assets a payee can hold on Ethereum), and a full-depth proof on each chain's gas schedule; compare with per-chain block gas limits and the EIP-7825 per-transaction cap where relevant.

### A.7 Static analysis and existing tests

- [ ] Run Slither (and optionally Aderyn / 4naly3er) on `src/`; triage every result.
- [ ] `forge test` (unit + fuzz), `forge coverage` — report uncovered lines/branches of `BridgeClaims.sol` and write tests for them (outside the repo).
- [ ] Write an invariant/stateful fuzz campaign (Foundry `invariant_*`): random claims, batches, sweeps, allowance changes, hostile recipients; invariants I1, I3, I4, I5.
- [ ] Review `test/mocks/Mocks.sol` for realism (e.g. `MockWETH.withdraw` uses `transfer` — matches WETH9; `FeeToken` 1% — PAXG's real fee is 0.02%).

---

## 4. Phase B — list build and tree (`tools/build_claims.py`, `tools/claim_tree.py`, `airdrop/tools/common.py`)

### 4.1 Independent re-derivation (I2, I9)

- [ ] Re-implement leaf, tree, root, and proof in a **different language/stack** (e.g. TypeScript with viem/ethers, or Rust/`cast`), without importing the repo's Python. From each chain's `claims.csv` recompute the root and compare with `manifest.json` and `CLAIM_CONTRACTS.md`. Five roots, all must match.
- [ ] Verify **every** row's proof (not the ~2,000 the builder samples): 187k (Ethereum) and 620k (BSC) leaves are cheap enough. Also verify a random sample on-chain later via `verify()` (§6.5).
- [ ] `sha256(claims.csv) == manifest.list_sha256 == deployment.constructor.listHash`; `sha256(tree.txt) == tree_sha256`; `level_sizes` match the tree you built; `claim_count == len(rows) == level_sizes[0]`; `payees` equals distinct accounts.
- [ ] `tree.txt` round-trips (`read_tree`) and its level 0 equals your leaves.

### 4.2 Builder validation logic

- [ ] Input integrity chain: `summary.json.status == "built"`; SHA-256 of `pools.csv`, `custody-plan.json`, and each `claims/<chain>.csv` must match the allocation summary (`main()`); `chains.json` chain ids must match the bridge config. Confirm these fail closed (try a tampered copy in `/tmp`).
- [ ] `read_claims`: rejects zero account, unknown token for the chain, amount ≤ 0 or ≥ 2^256, duplicate `(account, token)`. Confirm `lib.address()` normalisation (lowercase, checksum validation on mixed case) so that duplicates differing only by case are caught.
- [ ] Per-asset total must equal the pools' `claimable_raw` (fail closed). Recompute from `pools.csv` yourself.
- [ ] Row ordering: sorted by `(account, token)` as lowercase hex strings, indexes assigned from 0. Confirm determinism (stable sort, no locale effects) so anyone re-running the builder gets the same `claims.csv` byte-for-byte (compare SHA-256 of a `/tmp` rebuild with the committed run — this is also the reproducibility test for `listHash`).
- [ ] `contract_groups`: one contract per chain unless `one_claims.custody_multisig` differs — currently `null`, so ONE rows share the Ethereum contract funded by the custody Safe. Confirm the ONE approval (62,179,080.2358 ONE) equals the Reserve Safe stage-1 transfer in the release plan.
- [ ] Approvals aggregation: native rows and any explicit wrapped-token rows share one approval keyed by `approve_token` (summed). Check no chain has both; check no approval token appears twice.
- [ ] `format_amount` / decimals: cross-check `decimals()` of every token on-chain against `pools.csv` and the manifest; a wrong decimal does not change raw amounts but misleads signers reading the sheet.

### 4.3 Token identity

- [ ] For each of the 49 approval tokens, read `symbol()`, `decimals()`, and code hash on the real chain and compare with the manifest. Confirm each address is the canonical token (e.g. Ethereum USDC `0xa0b8…`, Arbitrum USDC.e `0xff97…` not native USDC `0xaf88…`, BSC Binance-Peg tokens, Linea USDC `0x1762…`). Cross-reference against the bridge config's pool definitions and the escrow's `token()`.
- [ ] `approve_reset_tokens` completeness: on a fork, for every approval token run `approve(spender, 1)` then `approve(spender, 2)` from the Safe; any token that reverts needs the reset entry (currently only Ethereum USDT). This matters for re-approval after a revoke.
- [ ] Transfer behaviour per token (fork): fee-on-transfer (PAXG 0.02%), transfer hooks (FXS), pausable/blacklist (USDC, USDT, BUSD, sDAI?), deprecated issuers (BUSD: Paxos wind-down — confirm transfers still work), rebasing or balance-changing tokens (none expected — confirm), tokens that return nothing (USDT), tokens with `transferFrom` that checks `msg.sender` allowance in a non-standard way. `Fork.t.sol` covers the first row of each asset; extend to the **largest** row and a **mid-size** row of each asset.

### 4.4 Fee-on-transfer and shortfall accounting (I4, I8)

- [ ] PAXG: the Safe receives the escrow balance less the fee, and each claim delivers `amount − fee`. Confirm Safe balance after release ≥ the PAXG approval (1.3105 PAXG) — the manifest shows a 1.4602 PAXG source; recompute the margin.
- [ ] For every asset: `sources[].amount_raw` summed ≥ approval amount; identify assets with zero margin (e.g. DAI, FRAX, FXS, SUSHI, xSUSHI, ONE have source == approval) and confirm a 1-wei shortfall cannot occur (no fee, no rounding) — otherwise the last row would fail.

### 4.5 Sampling and self-checks

- [ ] `SAMPLE_PROOFS = 2000` plus the last row; `samples.json` has the first row of every asset. Confirm the sample set used by `Fork.t.sol` is sufficient for its purpose, and recommend the full verification in §4.1 as a release gate.
- [ ] `write_tree` uses an atomic rename; `prepare_outputs` refuses to overwrite without `--replace`. Confirm no partial-file risk for the published artifacts.

### 4.6 Boundary with the allocation pipeline (scan, do not re-derive)

The audit does not re-decide allocations, but the claim contract will pay whatever `account` the
list names. Run an anomaly scan over all accounts per chain and report counts (no addresses in the
report unless already public):

- [ ] accounts equal to: the funder Safe, the claim contract address, any approval token, the wrapped native, any escrow, the CREATE2 deployer, precompiles `0x01–0x0a`, `0x…dead`, `0x…0000` variants;
- [ ] accounts **with code** on the destination chain (contracts: can they receive ERC-20 / coin? multisigs? exchange deposit contracts?) — native rows to such accounts will pay WETH/WBNB;
- [ ] accounts that are **contracts on Harmony** (shard-0 archival RPC `eth_getCode`) but have no code on the destination chain — nobody may control that address there. Count and value them; this is an upstream policy question, flag it as such;
- [ ] duplicate payees across chains (expected and fine) and rows below the gas cost of claiming on that chain (economic viability; relayer recommendation).

---

## 5. Phase C — approvals and the custody Safe (`tools/approve_claims.py`, `airdrop/tools/safe_batch.py`)

### 5.1 Batch contents (I4, I10)

- [ ] Independently decode every `safe-approve.json` and `safe-revoke.json` (do not use the repo's decoder): each call's `to`, `value`, `data` → `approve(spender, amount)` or `deposit()`. Confirm `spender == manifest.deployment.address`, `amount == approvals[].amount_raw`, every approval token present exactly once (USDT twice: 0 then total), `deposit()` value == native total (Ethereum 14.397… ETH; BSC 163.277… BNB), and the revoke batch approves 0 for every token.
- [ ] The wrap amount logic: `wrap = min(native_total, max(0, wrapped_need − held))` when `--rpc` state is present; confirm the Safe's **native** balance at signing time ≥ the deposit value (RELEASE.md snapshot shows 19.22 ETH / 163.55 BNB, consistent with the native escrows already released — confirm live).
- [ ] Tx Builder file: `chainId`, `createdFromSafeAddress`, `meta.checksum` recomputed with an independent implementation of the Safe Transaction Builder checksum; `contractMethod`/`contractInputsValues` decode to the same calldata the Safe app will build.
- [ ] MultiSendCallOnly address for Safe 1.3.0 (`0x40a2accbd92bca938b02010e17a5b8929b49130d`) matches the canonical deployment on each of the five chains (read code, compare with safe-deployments); operation = DELEGATECALL (1) for batches, CALL (0) when a batch has one call.

### 5.2 Safe transaction hashes (I10)

- [ ] Recompute `domainHash`, `messageHash`, `safeTxHash` for approve and revoke on every chain with an independent tool (e.g. Cyfrin `safe-tx-hashes-util`, or a hand-rolled EIP-712 encoding of `SafeTx` with the 1.3.0 domain `(chainId, verifyingContract)`), at the **live** nonce. Compare with the signing sheet and with `Safe.getTransactionHash` via `eth_call`.
- [ ] Nonce freshness: the committed Ethereum `APPROVALS.md` was built at nonce 6, while the later `release/RELEASE.md` snapshot shows the Ethereum Safe at nonce 7. Confirm the process (sheet step 4) requires rerunning `approve_claims.py --rpc` immediately before signing, and recommend the auditor's own hash be recomputed at signing time.
- [ ] Signing UX: what the Safe web app shows for a MultiSend delegatecall (decoded inner calls vs. raw). Confirm signers have a way to compare each inner `approve` spender and amount on-device (hardware wallet blind-signing concerns) — the sheet lists calldata; check hardware wallet EIP-712 support for Safe 1.3.0 messages.

### 5.3 Custody Safe identity on five chains

Same address `0x19565f4771843467aad632d6b56c75396785b06c` on Ethereum, BSC, Arbitrum, Base, Linea.

- [ ] For each chain: proxy bytecode is the canonical Safe 1.3.0 proxy; singleton (`masterCopy` storage slot 0) is the canonical 1.3.0 singleton (L1 `0xd9Db27…` or L2 `0x3E5c63…`; confirm against safe-deployments); fallback handler is the canonical `CompatibilityFallbackHandler`; **no modules enabled**, **no guard set**; `VERSION() == "1.3.0"`; owners (4) and threshold (2) read live and compared with a **pinned list of expected owner addresses** supplied by the team.
- [ ] `approve_claims.py` derives `expected_owners` from the **Ethereum** Safe's live owners when the config lacks an explicit list (`custody_requirements` currently has only counts). This is self-referential (a compromised Ethereum Safe would validate itself). Recommend pinning owners in config; the auditor must obtain the intended owner list out-of-band.
- [ ] How the same address came to exist on all chains (CREATE2 via the canonical `SafeProxyFactory` 1.3.0 with the same initializer and salt nonce): confirm the creation transaction on each chain and that the initial owner/threshold matches the plan's `0-safe-owners/` step (which appears to have been executed — all five show 4/2 in the latest `RELEASE.md`).
- [ ] Owner key custody: who holds the 4 keys, on what devices, in which jurisdictions; any two keys can revoke or re-approve. Document; not a code finding.

### 5.4 Approve-before-deploy argument (I6)

- [ ] The spender is a CREATE2 address through the Arachnid proxy with salt `0x00…00`; the proxy does not mix `msg.sender` into the salt, so **anyone** can deploy, but only this exact init code can ever land there. Confirm with the EIP-1014 formula and the deployer's bytecode. Confirm that if the contract is ever changed, the address changes and the old approval becomes a dead approval (recommend revoking it anyway).
- [ ] An attacker cannot deploy *and selfdestruct* at that address to later redeploy different code: `BridgeClaims` has no selfdestruct, and under EIP-6780 a different init code gives a different address. Write the argument down.

### 5.5 Preflight reads (`preflight()`)

- [ ] Checks performed: chain id, funder is a Safe, owners/threshold, owner set, contract deployed & immutables match **or** CREATE2 deployer present, each asset funded (balance + native for wrapped). Confirm each check fails closed and that a failing check blocks the process (today it only prints "not yet"; the sheet must read `ok` on every line before signing per RUNBOOK §4 — confirm who enforces that).

---

## 6. Phase D — deployment (`script/Deploy.s.sol`, `foundry.toml`, CREATE2)

### 6.1 Script logic

- [ ] Preconditions: `deployment.status == "ready"`, `create2_deployer` equals the constant and has code, `block.chainid == chainId`, `keccak256(initCode) == init_code_keccak256`; deploy with `new BridgeClaims{salt}` (Foundry routes `CREATE2` through the same deployer — confirm the resulting address formula is identical to a raw call to the proxy, which is what `Fork.t.sol._deploy` does); post-conditions read all six immutables.
- [ ] "Already deployed" path: logs and continues without comparing the existing code. Argue acceptability via CREATE2 binding, but add a post-deploy `extcodehash(expected) == keccak256(runtimeCode)` check to the verification list (§6.5).
- [ ] The script does not recompute the root from `claims.csv` or `claimCount` from the tree; it trusts the manifest. That trust is anchored by §4.1 — make the dependency explicit in the report.
- [ ] `fs_permissions` limits reads to `./test/fixtures` and `./runs`. Confirm `MANIFEST` cannot be pointed outside (it can be any path under `runs/` — fine).

### 6.2 Deployer proxy on each chain

- [ ] `0x4e59b44847b379578588920ca78fbf26c0b4956c` has the known 69-byte runtime code on all five chains (compare bytes with the upstream deterministic-deployment-proxy; preflight already reports 69 bytes).

### 6.3 Dry runs and gas

- [ ] `broadcast/Deploy.s.sol/<chainid>/dry-run/run-latest.json` exists for all five chains (today's runs, commit `70371fc`, sender = Foundry's default `0x1804c8ab…`). Confirm each lands on the manifest address and gas ≈ 1.6M. Confirm the files contain no RPC URL before they are published with the deployment record.
- [ ] Who deploys with which key; the key only needs gas. Recommend a fresh/low-value EOA; confirm the deployer's address cannot appear in any state that matters (it cannot).

### 6.4 Reproducibility and publication

- [ ] Bytecode reproducibility as in §3.6, performed by the auditor on a clean machine.
- [ ] Source verification on each chain's explorer (Etherscan, BscScan, Arbiscan, BaseScan, LineaScan) is **not** in the runbook. Recommend verifying with the exact settings (0.8.36, cancun, optimizer 20,000, no metadata) so payees can read the source; verification is a publication step and does not change trust in the bytecode.
- [ ] Publication bundle per RUNBOOK §5: `manifest.json`, `claims.csv`, `tree.txt` "published with the deployment". Define where (release asset, IPFS, repo), how hashed (the on-chain `listHash` ties `claims.csv`; `tree_sha256` in the manifest ties `tree.txt`; what ties the manifest? — recommend signing the manifest hash from a Safe owner key or publishing it in the deployment transaction record). Check nothing published leaks RPC hosts.

### 6.5 Post-deployment verification (the auditor should perform or script this)

For each chain, against the real RPC at a recorded block:

- [ ] `eth_getCode(address)` non-empty; `extcodehash` equals `keccak256(runtimeCode)` from the reproducible build.
- [ ] `funder()`, `wrappedNative()`, `chainId()`, `root()`, `claimCount()`, `listHash()` equal the manifest.
- [ ] `verify(index, account, token, amount, proof)` returns true for ≥ 100 random rows and the last row; false for one altered row — on the **real node**, which also exercises the Cancun opcodes on that chain's EVM (§3.6).
- [ ] `available(token)` before approvals is 0; after approvals equals each approval amount (RUNBOOK §6).
- [ ] Canary: since claims are permissionless, the team can pay the smallest row of each asset itself as the first live transaction on each chain, then watch `Claimed` events and balances before announcing. Recommend this and define the abort criterion (any `TransferFailed`/mismatch → revoke batch).

### 6.6 Secrets and hygiene

- [ ] `bridge/local/` is gitignored (confirmed); `cache/`, `out/`, `broadcast/`, `runs/` ignored. `cache/Deploy.s.sol/` holds the RPC URL and must be deleted after each run. `scripts/check-public-package.py` rejects the archival host in committed files — run it.

---

## 7. Phase E — funding the Safe: escrow release (prerequisite; `tools/release_plan.py`, `test/EscrowRelease.t.sol`, `docs/RELEASE.md`)

This step moves 100% of the collateral and is the highest-value transaction set in the process. It
is described here as a prerequisite to the claim setup; if it has not had its own review, recommend
one. Minimum checks for this audit:

### 7.0 Live state first

- [ ] Snapshot, per chain at a pinned block: custody Safe owners/threshold/nonce/native balance/each approval token balance; each escrow's locked balance; each escrow's receive version, trusted remotes (local path and Harmony path 116), ULN config and executor for the local path; `getInboundNonce` for the local path. Compare with `runs/…/release/RELEASE.md` and the earlier `release-built-20261022T0751Z/RELEASE.md`. Determine exactly which release files have executed (transaction hashes), which are pending, and whether any escrow is half-finished (the planner refuses to plan over one). The current `release/` directory lists only `0-one-reserve/`; the Safe already holds ~19.2 ETH and ~163.6 BNB and shows 4/2 owners on all chains — reconcile.

### 7.1 Mechanism

- [ ] Per escrow, the 10 (or 8) calls: owner sets itself as sole verifier and executor for the local path, pins the receive library, trusts `Safe ++ escrow` on the local path, `verify`s and `commitVerification`s one packet (nonce 1, payload credits the entire locked balance to the Safe), then restores trusted remote, receive version, executor, and ULN config. Independently decode one escrow's calls from the Safe batch and from an EOA file; confirm the payload format per escrow type (`oft_v1_packet_type` vs legacy for `0xab89…bcab`), the amount equals the live locked balance at build time, and the receiver is the custody Safe.
- [ ] Restoration: steps 7–10 write back exactly the pre-state; the Harmony path (116) is untouched; the endpoint records nonce 1 on the local path (no replay). `EscrowRelease.t.sol` asserts these on a fork — run it for every chain yourself and read its assertions critically (what is **not** asserted?).
- [ ] No reliance on LayerZero off-chain services: confirm the chosen receive library (`ReceiveUln301`) and endpoint have no pause and that defaults cannot override an app-level config within the same transaction (RELEASE.md's argument). Check the library's `verify`/`commitVerification` access control actually accepts the owner as DVN and executor given the config set in calls 1–2.
- [ ] `commitVerification(packet, 300000)` destination gas: sufficient for `_creditTo` on a `NativeOFT` paying coin to a Safe proxy (fallback handler) and for `ProxyOFT` `safeTransfer` of hook tokens (FXS)? The fork test measures; confirm margins.
- [ ] Safe batches: atomic; hash at live nonce; largest ~9.1M gas vs EIP-7825 cap; EOA sequences: `send_calldata.py` simulates each, refuses wrong wallet/chain/pending tx, resumes from a log. Review the half-finished-escrow safety argument ("only the owner can attest or deliver on the path once call 1 ran").
- [ ] ONE funding: stage 1 = 62,179,080.2358 ONE from the Reserve Safe `0xf868…ded20` equals the ONE approval exactly; stage 2 deliberately withheld. Confirm the Reserve Safe batch (nonce 58 at build) and the recipient.

### 7.2 Handoff packages

- [ ] `handoff/safe-signers/` and `handoff/<key>/` READMEs: hashes of the files they describe match; instructions cannot lead a holder to sign the wrong chain's file (chain id is embedded in Tx Builder JSON; EOA sender checks chain). Check the zip contents equal the folder contents.

---

## 8. Phase F — the claim process (payee side)

### 8.1 What a payee needs and where it comes from

- [ ] Inputs: `(index, account, token, amount, proof)` from `claims.csv` + `tree.txt` (or `proof.py`). Confirm `proof.py` checks each proof against the manifest root and the leaf against `tree.txt` before printing; confirm `one1…` → `0x` conversion (`from_one1`) and lowercase matching against `claims.csv` (a checksummed input must still find rows).
- [ ] The `--cast` command: `claimMany((uint256,address,address,uint256,bytes32[])[])` tuple encoding; `--account <keystore>`; the sender pays gas only. Run it on a fork for a payee with several assets.
- [ ] No portal or front end exists yet for bridge claims (`harmony-claim-portal` has no reference). If one is built, it is a new attack surface (phishing, wrong contract address, wrong chain). Recommend: the payee flow needs **no approval and no signature other than the claim transaction**, and the portal must say so; publish contract addresses in the deployment record and have the portal pin them.

### 8.2 Failure modes a payee can hit (build the table with reproductions)

| Situation | Contract behaviour | Recovery |
|---|---|---|
| Row already paid by a relayer | `AlreadyClaimed` (single) / skipped (batch) | none needed; payee already has funds |
| Wrong amount/account/token/index | `InvalidProof` / `IndexOutOfRange` | fix inputs |
| Allowance exhausted or revoked | `TransferFailed` | wait for Safe to re-approve |
| Token blacklists payee or pauses | `TransferFailed` | row stays unpaid; cannot change account |
| Payee contract rejects coin | paid in WETH/WBNB, `PaidWrapped` | none |
| Fee-on-transfer (PAXG) | receives `amount − fee` | expected |
| Payee has no control of the address on this chain | pays anyway | upstream (§4.6) |
| Front-run by relayer | payee's own tx reverts, funds already received | use `claimMany` or check `isClaimed` first |

### 8.3 Verification without trusting the tooling

- [ ] Document how a payee independently checks their row: read `root()`/`listHash()` on-chain, hash the published `claims.csv`, locate the row, recompute leaf and proof with any Merkle library using sorted pairs + carry-up, call `verify()`. Confirm the leaf/tree scheme strings in the manifest are sufficient for this.

### 8.4 Monitoring and incident response

- [ ] Specify a monitor: per token, Σ `Claimed.amount` ≤ initial approval; `available()` trending; any `PaidWrapped`; any `Swept`; Safe allowance changes not initiated by the team. Define the trigger for the revoke batch and who can execute it within what time (2-of-4; the revoke `safe-revoke.json` must be rebuilt at the live nonce).
- [ ] Correction procedure (DESIGN "Corrections"): approve 0 for the asset on the old contract, build a new list for that asset excluding paid rows (`claimedBitmap`), deploy, approve. Rehearse on a fork for one small asset and confirm no payee can be paid twice across the two contracts (the new list must exclude claimed indexes **by account+token**, since indexes differ between lists).

---

## 9. Severity rubric and report format

| Severity | Definition for this system |
|---|---|
| Critical | Loss of funds beyond a token's approval, payment to a non-row address, or any row payable twice. |
| High | A payee permanently loses a row through contract/tooling behaviour; a signed Safe batch does something other than its sheet says; wrong token/amount/spender in an approval; non-reproducible bytecode/address. |
| Medium | Claims for an asset or chain cannot proceed without redeploying (e.g. opcode unsupported, approval batch cannot execute); process gap that could lead to signing stale hashes. |
| Low | Griefing costing only the submitter gas; misleading sheet text; recoverable stuck funds via `sweep`. |
| Informational | Documentation, code clarity, test gaps, design decisions worth recording. |

Each finding: ID, severity, invariant(s) affected, location (file:line), description, PoC or
evidence (test path under `/tmp`, RPC read with block number), recommendation, team response.
End the report with: the I1–I11 verdict table; a per-chain go/no-go for (a) signing approvals,
(b) deploying, (c) opening claims; and the exact artifact hashes (manifests, `claims.csv`,
`tree.txt`, Tx Builder JSONs, bytecode) the verdict applies to.

### Design decisions the auditor should confirm the reasoning of, but not report as findings unless the reasoning fails

- No deadline / expiry: approvals are perpetual until the Safe revokes; unclaimed assets stay in the Safe.
- Permissionless `claim`/`sweep` (relayer model; tokens always to the row's account / funder).
- One contract per chain for all assets (exposure bounded per token by its approval).
- Fee-on-transfer rows deliver less than the row; blacklisted payees stay unpaid; contracts that reject coin receive the wrapped token.
- Approval signed before deployment (CREATE2 commitment).
- Revert data from tokens is not bubbled up.

---

## 10. Leads from the planning read (unverified — confirm or dismiss)

1. **Stale signing sheet**: Ethereum `APPROVALS.md` nonce 6 vs live nonce 7 in the later release snapshot; the "funded" checks read "not yet" while the Safe now holds coin. Process already says to rebuild before signing; confirm it is enforced and that signers use only a sheet produced at the live nonce.
2. **Self-referential owner check**: `approve_claims.py` takes expected owners from the Ethereum Safe when config has none. Recommend pinning in config (`custody_requirements.owners`).
3. **Fork tests ≠ real EVM**: Cancun opcode support on Linea/BSC/Arbitrum/Base is only established by the earlier `eth_call` probes; make `verify()` on the deployed contract via the real RPC a gate before approvals are *executed* (approvals can be signed earlier).
4. **Full-list proof verification** is not a release gate (builder samples ~2,000 rows). Cheap to add; do it in the audit.
5. **No explorer source verification step** in the runbook.
6. **`claimMany` skips claimed indexes without proof checking** — benign, document.
7. **Zero-margin assets**: for DAI, FRAX, FXS, SUSHI, and xSUSHI the escrow balance equals the approval exactly, and the ONE stage-1 transfer equals the ONE approval exactly, so the Safe will hold precisely what the rows pay. Confirm no fee, hook, or rounding can leave the last row 1 wei short.
8. **Accounts without control on the destination chain** (Harmony contracts, token/escrow/Safe addresses as payees) — upstream, but quantify.
9. **Manifest authenticity**: `listHash` ties `claims.csv` to the contract and `tree_sha256` ties `tree.txt` to the manifest, but nothing on-chain ties the manifest itself; recommend a signed or on-chain-recorded manifest hash.
10. **Hardware-wallet signing of MultiSend delegatecalls**: confirm signers can see spender/amount per inner call or rely on the sheet's `safeTxHash` comparison only.

---

## 11. Command cheat sheet (scratch work in `/tmp`; never regenerate `runs/` in place)

```sh
cd harmony-migration/bridge-claims
forge build && forge test -vv                    # unit, fuzz, fixture
forge coverage --report summary
python3 tools/test_tools.py
slither src/BridgeClaims.sol --solc-remaps "forge-std/=../airdrop/lib/forge-std/src/"

set -a; . ../bridge/local/rpc.env; set +a        # never echo these
# independent rebuild of one chain into /tmp (compare SHA-256 with runs/…/<label>/claims.csv)
python3 tools/build_claims.py --allocations-dir ../artifacts/bridge-allocations-20261018/allocations \
  --output-dir /tmp/bridge-claims-audit/rebuild --chains base
# fork tests, one chain at a time; delete cache/ afterwards
CLAIMS_DIR=runs/bridge-allocations-20261018/ethereum FORK_RPC_URL="$ETHEREUM_RPC" forge test --match-contract Fork -vv
CLAIMS_DIR=runs/bridge-allocations-20261018/ethereum RELEASE_DIR=runs/bridge-allocations-20261018/release \
  FORK_RPC_URL="$ETHEREUM_RPC" forge test --match-contract EscrowRelease -vv
MANIFEST=runs/bridge-allocations-20261018/ethereum/manifest.json forge script script/Deploy.s.sol --rpc-url "$ETHEREUM_RPC"
rm -rf cache/Deploy.s.sol
# payee lookup
python3 tools/proof.py runs/bridge-allocations-20261018/base <payee> --cast
```
