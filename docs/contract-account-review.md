# Contract-account review

The eligibility pipeline first places code-bearing rows of the inclusive
`>= 1,000 ONE` ledger in a preliminary review file. This document describes
how those rows are classified. Exact per-address results, counts, and monetary
breakdowns are held under the numerical embargo in
`docs/findings/destination-mapping/contract-account-review.md`,
`results/2026-09-11/`, and `artifacts/contract-review-<date>/`.

The threshold uses each row's `qualification_total`: native total claim,
including active stake/delegation, plus cutoff WONE. Contract policy records
`wallet_airdrop`, including qualified WONE, and `staked_to_vault` separately;
active delegation is represented through
validator-vault shares rather than being sent as direct ERC-20 ONE.

The liquid state export does not carry code metadata for staking-only accounts.
Before classification, every claim row with a blank shard-0 code field is
resolved with historical `eth_getCode` at the exact cutoff block. This covers
the prioritized batch and future below-threshold claims.

## Validator-wrapper accounts are not EVM contracts

Harmony stores every validator's `ValidatorWrapper` RLP-encoded in the
account's **code** field (`core/state/statedb.go`, `UpdateValidatorWrapper`
calls `SetCode(addr, rlp, true)`). A state export therefore sees a non-empty
code hash for every validator, even though the account is an ordinary
ECDSA-controlled EOA that signs its own staking transactions.

Detection is exact and needs no heuristics:

1. `hmyv2_getValidatorInformation(address)` succeeds (a contract returns
   `rlp: expected input list for types.ValidatorWrapper`); and
2. the account code decodes as an RLP list whose first item is the account's
   own 20-byte address.

Rows that pass both tests belong in the key-controlled automatic category, not
the genuine-contract review category. The classifier writes the deterministic
override set to `validator-policy-accounts.csv` and contextual details to
`validator-accounts.csv`. It locates the `CreateValidator` transaction in the
canonical block identified by the wrapper's `creation-height`.

That validator classification does not defeat a higher-priority explicit
policy route. For example, if an exchange inventory ever contains a verified
validator account, the exchange exclusion suppresses its same-address
automatic override and the routing engine also holds its vault governor until
an approved replacement is provided.

## Classification of real contracts

Classification and enrichment facts are read from a Harmony shard-0 archival
node RPC (`eth_getCode`, `eth_getBalance`, `eth_getStorageAt`, `eth_call`,
`trace_block`, `hmyv2_getTransactionsHistory`,
`hmyv2_getValidatorInformation`, `hmyv2_getDelegationsByDelegator`). Cutoff
total-claim, wallet-airdrop, and staked-to-vault components come from the
independently constructed two-shard claim CSV.
Explorer and indexer balances are not used.

Every state fact that can affect classification or recovery—owners,
thresholds, recovery addresses, proxy slots and implementations, guardians,
interface probes, and holder enumeration—is read at the shard-0 cutoff block.
Each derived cache records that block's hash and state root. Classification
stops if any input used a different block, hash, or root. Latest balances,
delegations, and activity are context only.

| question | detector |
|---|---|
| Gnosis Safe multisig | `getOwners()` returns a non-empty address array, `getThreshold()` in `[1, owners]`, `VERSION()` returns a string, storage slot 0 holds the singleton. Works for the Harmony `multisig.harmony.one` v1.2.0 fork singleton and canonical v1.3.0 / v1.4.1 singletons. Output: owners, threshold, version, singleton, creation, first funding. |
| 1wallet (all versions) | `getInfo()` returns exactly eight words `(root, height, interval, t0, lifespan, maxOperationsPerInterval, lastResortAddress, dailyLimit)` with sane ranges; `getVersion()` gives major/minor for v2+. Recovery address is reported only when it differs from the 1wallet treasury addresses (`0x7534978F…`, old `0x02F2cF45…`), which the contract itself treats as "not set". `getForwardAddress()` is reported when set. |
| ERC-20 | `totalSupply()`, `balanceOf()`, `name()`/`symbol()` succeed and the dispatcher (own code or proxy implementation) contains `transfer/approve` and `allowance|transferFrom`. Synthetix-style staking pools that merely expose `balanceOf/totalSupply` are excluded. |
| NFT | ERC-165 answers for `0x80ac58cd` / `0xd9b67a26` when ERC-165 is implemented consistently; otherwise dispatcher must contain `ownerOf`, `safeTransferFrom`, `setApprovalForAll`, `transferFrom`. CryptoPunks-style markets are reported as "other NFT". Cutoff-state owners are enumerated through Multicall3. |
| well-known apps | registry of verified addresses (official address books and repositories), on-chain parent relationships (`factory()`, `POOL()`, `comptroller()`, `walletFactory()`, Safe singleton), function-signature fingerprints matched to the project's source, and (last) `name()`/`symbol()` patterns. The evidence rule that fired is recorded per row. |
| type-only identification | generic fingerprint rules (staking-precompile delegation pools, NFT marketplaces, presales, payment splitters, escrows, games) for contracts whose operator cannot be attributed. |

Creation block and transaction: binary search on `eth_getCode` followed by
`trace_block` (the `create` trace whose result address is the account). First
funding: balance binary search between creation and the earliest direct value
transfer, then `trace_block`; the earliest direct transfer from the node's
per-address index is used as a cross-check.

Known limitations:

- the per-address transaction index orders legacy entries by hash, so first
  transactions are recovered by resolving the hash-sorted prefix; the most
  recent transaction is read from the `DESC` page;
- interaction counts include direct transactions only;
- a contract drained to exactly zero and later refunded can make the balance
  binary search return a later boundary; the direct-transfer cross-check wins
  when earlier;
- ERC-1155 and non-sequential-id collections cannot have holders enumerated
  from state (`eth_getLogs` is limited to 1,024 blocks per query on public
  nodes).

## Migration policy applied after classification

Classification is evidence, not a destination or stage. The confirmed policy
uses address-based joins against `contract-review-policy.csv`:

- Safe multisigs, the two reviewed LayerZero collateral contracts, and
  reviewed 1wallet allocations are eligible in the next stage regardless of
  activity;
- 1wallet uses recovery-multisig handling, but no destination is ready without
  cutoff ownership/recovery evidence;
- SmartVault is a separate family and is not issued;
- every other reviewed genuine-contract allocation is not issued;
- validator wrappers remain wallet accounts and can enter the six-month
  initial wallet stage.

Non-issuance consumes both direct-wallet and staked-vault components. Contract
rows never enter the wallet activity table. The review does not classify the
below-threshold code-bearing population and does not infer abandonment from
code or inactivity.

## Reproduction

See `toolkit/scripts/contract-review/README.md`.
