# Verification

## Fast verification without a database

Run:

```sh
make verify
make check-public
```

The first command verifies every public source file against
`manifests/source-code.sha256`. The second confirms that ignored findings,
results, private paths, and likely numerical outputs are not publishable under
the current repository rules.

Neither command requires a Harmony database or reveals embargoed results.

Local maintainers with the ignored private package can additionally run:

```sh
make verify-private
```

## Full independent verification

No single scanner is treated as sufficient evidence. The cutoff ledger was
checked through independent state, RPC, trace, CSV, and aggregate paths.

A full independent run should:

1. export both liquid states at the public cutoff roots;
2. export both aggregate staking claims and per-validator active delegations;
3. reconcile pending cross-shard receipts;
4. recover and cryptographically verify every address preimage;
5. assemble and strictly verify native total-claim, wallet-airdrop, and
   staked-to-vault fields;
6. normalize private exchange inventories so non-Gate aggregate-delivery
   membership is fixed before the WONE overlay;
7. enumerate cutoff WONE holders, reconcile them to `totalSupply()` and the
   native reserve, then apply ordinary-threshold and aggregate-exchange WONE;
8. apply the inclusive combined native-plus-WONE threshold to the ordinary
   wallet path;
9. resolve blank code metadata for the complete claim population at the
   cutoff, classify every prioritized code-bearing row, and prove validator
   overrides;
10. apply and verify the final destination split;
11. generate the non-Gate exclusion/manual-route overlay and stage-aware Gate
    split audit from the normalized exchange inputs, and verify it against
    existing cutoff data;
12. build validator-vault deposits plus priority and deferred share ledgers;
13. generate and independently reconcile the wallet-only initial stage,
    next-stage reviewed contracts, deferred wallets, and reviewed-contract
    non-issuance;
14. apply explicit redistribution, non-issuance, exchange, contract-policy,
    and other manual routes across wallet and vault delivery;
15. verify the sparse routing, governor exceptions, and complete validator-vault
    stage partition, requiring every
    unresolved destination to appear in the generated hold queue;
16. independently materialize initial-only wallet/vault plans and the separate
    non-Gate exchange wallet/share aggregate, matching each to its scoped stage
    readiness; and
17. record deterministic counts, totals, and hashes before receiving the
   original results.

## State checks

- A full shard-0 state traversal enumerates every account and every positive
  balance.
- An independent full supply traversal must reproduce the same liquid total.
- Independent staking discovery must reproduce the validator, delegation, and
  undelegation populations and component totals.
- Per-validator active delegation principal must sum exactly to the independently
  aggregated active-stake component. A historical archival-RPC export provides
  a second path independent of the database scanner.

Observed population counts and totals are withheld during independent
reproduction.

## Historical difference checks

The direct trie difference between the original and cutoff shard-0 roots
classifies created, changed, and deleted state accounts.

Official canonical RPC independently checks the old and final balances and
nonces of every changed account. Every newly created account must also be found
in a canonical execution trace at its creation transition.

Observed counts and comparison results are withheld during independent
reproduction.

## Interval checks

Both shard intervals are checked for contiguous canonical block coverage,
regular and staking transactions, cross-shard activity, gas use, and state-root
changes. Reviewers should record their independently observed counts before
requesting the held-back comparison data.

## CSV checks

The strict verifiers check:

- secure-key ordering;
- `keccak256(address) == secure_key`;
- zero unresolved addresses;
- non-negative integer components;
- shard liquid arithmetic;
- `native_wallet_airdrop = liquid + pending undelegation + unclaimed reward +
  pending cross-shard`;
- `qualification_total = native_total_claim + wone_balance`;
- `wone_airdrop = wone_balance` for the inclusive ordinary batch or a
  normalized non-Gate aggregate-exchange wallet;
- `wallet_airdrop = native_wallet_airdrop + wone_airdrop`;
- `staked_to_vault = active stake/delegation`;
- `total_claim = wallet_airdrop + staked_to_vault`;
- exact 18-decimal ONE rendering;
- exact USD rendering;
- cutoff and price metadata;
- threshold semantics;
- signed difference arithmetic and classifications.

The local final cross-component reconciliation passed. Its result count is
withheld during independent reproduction.

Cross-shard receipt lookup distinguishes an absent destination key from a
database read failure. Absence may represent a pending receipt; any `Has`,
`Get`, decode, or iterator error aborts before totals are published.

The prioritized activity enrichment additionally checks:

- every `>= 1,000 ONE` candidate has exactly one activity record;
- a hybrid activity file reports exact database-derived and RPC-derived row
  counts whose sum equals the candidate population;
- database-derived rows read the local explorer-node indexes in descending
  block/index order and verify selected blocks against the canonical chain
  database;
- RPC-derived rows request descending history from the archival node's
  built-in transaction index and compare each result with canonical block
  hashes;
- each selected block is at or before the corresponding cutoff, and stale fork
  entries from either index path are skipped rather than accepted as activity;
- the selected transaction block is at or before its shard cutoff and its
  timestamp is at or before `2026-09-10T14:00:00Z`;
- activity evidence is either complete or entirely blank; and
- cumulative calendar-month totals close against the exact candidate ledger.

The scanner does not use the retiring Explorer website or REST API. Activity
does not change snapshot threshold membership or prove control. Internal EVM
traces and validator consensus signatures are not scanned. The six-month
window selects only the initial wallet stage; genuine contracts never enter
those rows. Cross-shard recipient entries are not treated as proof that the
destination receipt was applied, and failed/reverted transactions remain
activity because they are present in the top-level index.

The selected inclusive eligibility split is independently checked to ensure:

- every resolved address matches its secure account key at merge, format,
  eligibility, classification, and routing boundaries;
- no eligibility row has unresolved code metadata;
- every threshold row appears exactly once;
- ordinary EOAs are in the automatic output;
- every code-bearing row in the automatic output appears in the independently
  verified validator-account override;
- every validator override is code-bearing, above threshold, and present
  exactly once, with both validator RPC evidence and a cutoff-code RLP address
  match;
- ownership, Safe thresholds, recovery settings, proxies, guardians, and
  holder evidence all share the recorded cutoff block hash and state root;
- classification, staging, and routing consume deterministic
  `contract-review-policy.csv` and `validator-policy-accounts.csv`; mutable
  latest-state context is excluded from release-comparison inputs;
- genuine contracts partition exactly into reviewed next-stage and not-issued
  sets, while verified validator wrappers remain wallets;
- `migration_stage` and `issuance_treatment` remain separate, and every issued
  wallet/vault component closes independently;
- selected inaccessible/dead and qualifying non-Gate exchange rows are handled
  by policy;
- category wallet, vault, total-claim amounts and output hashes match the
  policy summary;
- per-validator vault deposits equal all active delegation principal;
- priority and deferred shares are disjoint and complete;
- each validator vault partitions into initial, exchange-aggregate, next-stage,
  qualified-deferred, manual-review, uncompiled-deferred, and not-issued
  assets; post-policy assets equal base assets minus not-issued assets;
- explicit route amounts close exactly across wallet and vault components;
- WONE redistributed source equals WONE added to ordinary-threshold and
  aggregate-exchange rows, and redistributed plus retained not-issued WONE
  equals the native reserve;
- ordinary code-less EOA same-address delivery is implicit and absent from the
  exception output;
- every verified validator account appears as an explicit code-bearing
  same-address exception for each nonzero delivery component;
- validator wallet exceptions and validator-vault governor exceptions remain
  separate;
- no held destination silently falls back to the original address.

`verify-migration-stage-policy.py` independently re-sums the address-level
stage file and compiled routing. `materialize-initial-stage.py` then proves
that its outputs contain only the initial issued allocation, expands implicit
same-address delivery, and matches wallet, share, vault, hold, governor, and
policy-gate totals to `stage_readiness.initial`.

The private exchange overlay additionally verifies:

- each raw spreadsheet/CSV/DOCX is parsed without executing active content;
  spreadsheets have no hidden sheets, macros, malformed addresses, duplicates,
  or unadjudicated cross-exchange overlap, and no formulas outside the
  explicitly allowed DigitalX explorer-link and summary-total columns, whose
  cached values must be reproduced from row data;
- each standardized address has an immutable source-row and source-file hash;
- exact balance parsing is unit- and shard-scope-aware; KuCoin's merged
  shard totals reproduce its workbook summary and identify the selected cutoff
  blocks and times; DigitalX's scientific-notation balances convert exactly,
  its declared destination equals the configured destination file, and its
  rolled-back deposit transactions are carried as a separate non-inventory
  claim;
- MEXC signed rows recover to the submitted source address and configured
  destination, while KuCoin's signature-marked source rows, signature
  worksheet, and separate DOCX proof agree before the same signer/destination
  recovery check succeeds;
- every qualifying non-Gate source is in the policy-routed category and no
  Gate source is excluded merely because it appears in the Gate inventory;
- every positive native or WONE non-Gate claim has exactly one priority-300
  manual route regardless of the ordinary threshold, and all such routes use
  the release-authorized `exchange_aggregate` stage;
- materialized per-exchange ERC-20 and per-validator vault-share totals equal
  that stage exactly, with no held destination or governor;
- Gate initial-stage and outside-initial lists partition its complete
  inventory, and its initial allocation plus residual qualification value
  closes exactly;
- prior activity data is reused only where it was collected, while other rows
  are labeled `not_collected`; and
- missing inventories or destinations remain holds and cannot produce a silent
  same-address fallback.

The incident non-issuance overlay must additionally verify:

- each direct extra-mint recipient's not-issued amount is
  `min(total claim, max(exact extra mint - verified burn, 0))`;
- partially affected rows preserve the remainder for the original address;
- burn/inaccessible and report-identified perpetrator rows use the selected
  previously audited amount;
- new perpetrator-related rows already exist in the global claim ledger,
  preserve explicitly reported and transaction-linked roles separately, and
  do not overlap the reported victim population;
- reported victim wallets receive no automatic non-issuance route;
- every `not_issuing` row has no destination address and is absent from the
  unresolved work queue;
- no token is created for a not-issued wallet row, and every not-issued staked
  row reduces both its validator-vault deposit and share mint by the same
  amount; and
- issuable plus not-issued plus redistributed source amounts sum back to the
  expanded routing input.

## Expected primary result

The locally calculated row count, component totals, aggregate claim, category
breakdown, and output hashes are intentionally withheld pending independent
reproduction and the first public results article.

The public cutoff anchors and dependency metadata are in
`manifests/snapshot-2026-09-10.json`.

## Commands

Verify a regenerated all-claims CSV:

```sh
bin/migration-claims-verify \
  -input all-address-migration-claims-cutoff.csv \
  -require-all-addresses \
  -expected-shard0-block 93623067 \
  -expected-shard1-block 95882100 \
  -expected-price-reference-shard0-block 93448483 \
  -expected-price-usd-per-one 0.00074801

python3 toolkit/scripts/claims/verify-wone-allocation.py
```

The Python verifier independently recomputes WONE holder completeness, the
native-to-WONE claim overlay, the exact inclusive-threshold subset, the
non-Gate aggregate-exchange exception, the reserve split, separate next-stage
NativeOFT holds, and shard-1 WONE-source reviewed-contract non-issuance.
Its deterministic JSON output is retained with the private result package and
is exercised by local `make verify-private` workflows.

The older `cutoff-final-verifier` command embeds expected identities for the
immutable native cutoff and historical USD-difference bundle. It remains with
the private artifacts, but it is not the post-WONE verifier.

Explorer balances are not part of any verification path.
