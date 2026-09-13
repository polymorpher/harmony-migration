# Contract-account review pipeline

Classifies every metadata-complete code-bearing row of the `>= 1,000 ONE`
cutoff ledger (`migration-claims-code-bearing-complete.csv`) using only a
Harmony shard-0 archival node RPC. Standard library Python only.

Do not classify directly from the liquid-export `code_hash` columns: they are
blank for staking-only accounts. First run
`toolkit/scripts/claims/enrich-claim-metadata-rpc.py` on the complete claim set
at the cutoff block, then apply the threshold to that metadata-complete file.

Stages (run in order; every stage is resumable and caches its output):

```sh
RPC=https://a.api.s0.t.hmny.io            # any Harmony shard-0 archival node with trace + tx-history APIs
IN=artifacts/cutoff-20260910/claims/migration-claims-code-bearing-complete.csv
OUT=artifacts/contract-review-20260911

python3 toolkit/scripts/contract-review/fetch-contract-facts.py   --input $IN --rpc $RPC --output $OUT/facts.json
python3 toolkit/scripts/contract-review/selector-census.py        --facts $OUT/facts.json --rpc $RPC --output $OUT/selectors.json
python3 toolkit/scripts/contract-review/enrich-contract-facts.py  --facts $OUT/facts.json --selectors $OUT/selectors.json --rpc $RPC --output $OUT/extra.json
python3 toolkit/scripts/contract-review/classify-contracts.py     --facts $OUT/facts.json --claims $IN --extra $OUT/extra.json --selectors $OUT/selectors.json --output-dir $OUT/out
python3 toolkit/scripts/contract-review/build-report.py           --output-dir $OUT/out --claims $IN --report $OUT/CONTRACT_ACCOUNT_REVIEW.md
```

## What each stage does

| script | facts collected |
|---|---|
| `fetch-contract-facts.py` | runtime code at cutoff and latest; latest balance, nonce, storage slot 0 and EIP-1967 slots; validator detection (`hmyv2_getValidatorInformation` + RLP address check of the code field); per-address direct-tx counts and first/last direct transactions (with the legacy hash-sorted index prefix resolved); creation block by binary search on `eth_getCode` and creation tx from `trace_block`; first inbound value transfer by balance binary search + `trace_block`; 133 `eth_call` ABI probes; current delegations |
| `selector-census.py` | 4-byte selectors from the dispatcher of each contract and of the implementation behind proxies (EIP-1967, EIP-1167 clones, Safe `masterCopy`, `implementation()`), resolved to signatures with a built-in dictionary plus the openchain.xyz signature database (labels only) |
| `enrich-contract-facts.py` | AMM pair token symbols, Safe singleton metadata, SmartVault owner/guardians, NFT current-owner census through Multicall3 `aggregate3` |
| `classify-contracts.py` | detectors for validator accounts, Gnosis Safe (any version), 1wallet (all versions, via `getInfo()` shape), SmartVault, ERC-20, ERC-721/1155, well-known apps (registry `known-apps.json`: verified addresses, on-chain parent rules, signature fingerprints, name patterns) and generic fingerprint patterns; writes per-category CSVs and `summary.json` |
| `build-report.py` | Markdown statistics report generated from the CSVs |

The preliminary review file intentionally includes validator-wrapper accounts,
whose RLP state is stored in the account code field. After classification, pass
`$OUT/out/validator-accounts.csv` to the final eligibility-policy command as
`--automatic-code-addresses`; genuine contracts remain in manual review.

Classification outputs retain `total_claim`, `wallet_airdrop`, and
`staked_to_vault` as distinct fields. The staked amount is mapped through
validator vault shares, not added to direct wallet distribution.

## Registry

`known-apps.json` holds the application registry. Every exact address in it
was checked on-chain (non-empty code, matching `name()/symbol()`, parents
derived from `factory()`, `POOL()`, `comptroller()`, `walletFactory()`,
`Endpoint.defaultSendLibrary()`, etc.). Sources are cited per app
(`docs`, `source`, `address_book`). Add new apps by extending `addresses`,
`signature_rules` (all-of function signatures) or `pattern_rules`.

## Trust boundary

- Classification code, latest balances, storage, traces, transactions, and
  validator wrappers: Harmony archival node RPC only.
- Total-claim, wallet-airdrop, and staked-to-vault components: the
  independently constructed two-shard claim CSV.
- Labels: official project repositories / address books (cloned under
  `third_party/reference/`), function signatures from the contract bytecode,
  and the openchain.xyz signature name database. No explorer API is used.

## Reference material used

- `third_party/reference/one-wallet` (1wallet contract versions, treasury addresses)
- `third_party/reference/safe-deployments` (canonical Safe singletons on chain 1666600000)
- `third_party/reference/harmony-totp` (SmartVault `otp_wallet.sol`)
- `third_party/reference/davinci-sdk`, `davinci_nft_marketplace` (daVinci mainnet config)
- `third_party/reference/known-apps/AaveV3Harmony.sol` (Aave address book)
