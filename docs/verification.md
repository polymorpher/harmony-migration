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
5. assemble and strictly verify total-claim, wallet-airdrop, and
   staked-to-vault fields;
6. apply the inclusive threshold;
7. resolve blank code metadata for the complete claim population at the
   cutoff, classify every prioritized code-bearing row, and prove validator
   overrides;
8. apply and verify the final destination split;
9. build validator-vault deposits plus priority and deferred share ledgers; and
10. apply explicit treasury and manual routes across wallet and vault delivery;
11. verify the sparse routing and governor exception sets, requiring every
    unresolved destination to appear in the generated hold queue; and
12. record deterministic counts, totals, and hashes before receiving the
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
- `wallet_airdrop = liquid + pending undelegation + unclaimed reward + pending
  cross-shard`;
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
- eligibility and routing consume deterministic
  `contract-review-policy.csv` and `validator-policy-accounts.csv`; mutable
  activity context is excluded from release-comparison inputs;
- genuine contracts remain in manual review;
- selected inaccessible/dead rows are handled by policy;
- category wallet, vault, total-claim amounts and output hashes match the
  policy summary;
- per-validator vault deposits equal all active delegation principal;
- priority and deferred shares are disjoint and complete;
- explicit route amounts close exactly across wallet and vault components;
- ordinary code-less EOA same-address delivery is implicit and absent from the
  exception output;
- every verified validator account appears as an explicit code-bearing
  same-address exception for each nonzero delivery component;
- validator wallet exceptions and validator-vault governor exceptions remain
  separate;
- no held destination silently falls back to the original address.

The later treasury-routing overlay must additionally verify:

- recipient reclaim is
  `min(total claim, max(exact extra mint - verified burn, 0))`;
- partially reclaimed rows preserve the remainder for the original address;
- burn/inaccessible and report-identified perpetrator rows use the selected
  full-claim policy;
- treasury and ordinary-destination outputs sum back to unchanged wallet and
  vault entitlements.

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
```

The exact artifact-bundle verifier embeds expected result identities and is
therefore held with the private result package. It is exercised by local
`make verify-private` workflows and will be published with the results.

Explorer balances are not part of any verification path.
