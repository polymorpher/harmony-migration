# Exchange update scope memo

Date: 2026-09-18 (policy boundary and chain revised 2026-09-22)

Read this before changing an exchange wallet list, signature proof, or
destination.

## Default scope

An exchange update is an **exchange-local data refresh**, not a migration-policy
or global-accounting change.

- Validate only the changed exchange files.
- Independently sum row-level values; never trust a submitted aggregate total.
- Join submitted wallet addresses to the already-built cutoff native-ONE
  accounting artifacts.
- Verify any submitted ownership signatures and the configured destination(s)
  for the exchange's `destination_mode`.
- Update exchange-normalized CSVs, exchange summaries/memos, and other compact
  exchange-only findings.
- Reuse unchanged normalized exchange outputs by source hash.

Target completion time for an ordinary update is under one minute. Do not
restart chain scans or multi-gigabyte global verification for this task.

## Asset and policy boundary

Every exchange wallet is excluded from the airdrop, and all exchange migration
is delivered manually, directly from the year 2050 supply reserve
(`delivery_policy: manual_from_reserve` for every exchange). The exchange's
complete cutoff entitlement — native ONE, WONE at the listed addresses, and
delegated principal released from the validator vaults — goes to the
destination(s) it confirmed under its `destination_mode` (`aggregate`,
`aggregate_split`, `same_address`, or `tiered`).

- Exchange-submitted totals are reconciliation evidence only; row-level cutoff
  accounting determines every amount.
- Do not invent a new qualification test or migration stage. Confirmed
  exchange rows compile into the existing `exchange_manual` stage; a Gate
  wallet's ordinary stage only selects its delivery tier.
- Do not alter the WONE reserve split by hand. Exchange WONE enters the overlay
  through the normalized inventory, so an inventory change re-pins the overlay
  (see the chain below); the reserve arithmetic is derived, never edited.
- Do not change another exchange's destination or mode while updating one
  exchange.

Threshold overlap is calculated only to remove exchange wallets from the
implicit automatic same-address output. It is not an exchange entitlement
statistic and should not headline the exchange memo.

## External agents' workspaces

`harmony-supply-audit/artifacts/historical-hacks-investigation-20260916/`,
`artifacts/x-article-20260917/`, and `artifacts/activity-composition-20260919/`
belong to agents working outside Cursor. Never write into them, never read
them as pipeline inputs, and never mirror them. The retained-address input the
stage policy needs is pinned in this repository at
`artifacts/supply-reconciliation-20260911/not-issued-retained-initial-addresses.csv`
(and in `harmony-supply-audit/artifacts/historical-retention-snapshot-20260916/`).

## Do not run by default

Unless the user explicitly requests a complete release rebuild, or the fast
path shows that the changed inventory adds or removes an address that holds
WONE or meets the ordinary threshold (which forces the chain below), do not
modify or regenerate:

- `apply-wone-qualification.py` outputs;
- WONE holder, redistribution, or retained-reserve artifacts;
- all-address native/migration claim ledgers;
- migration-stage policy;
- global routing exceptions or initial-stage materialization;
- the full WONE allocation verifier;
- unrelated contract, incident, non-issuance, or supply reports.

Do not create a new migration stage or a new issuance treatment for an
exchange. Any uncommitted change doing so is over-scoped and should be removed
before finalizing an exchange-only update.

## Fast path

1. Hash and inspect the changed raw workbook/document/destination.
2. Parse and normalize only the changed exchange.
3. Check duplicates and cross-exchange overlap against existing normalized
   outputs.
4. Independently sum its row-level submitted native balances, when supplied.
5. Join its addresses to existing cutoff native claim rows and calculate the
   requested exchange statistics.
6. Verify designated signatures and destination binding (one destination for
   `aggregate`/`tiered`, a wallet/staking pair for `aggregate_split`, none for
   `same_address`).
7. Refresh the exchange memo and compact exchange summary.
8. Run focused exchange tests and syntax checks.

Stop there unless the user explicitly asks to propagate the change into a
release candidate.

### When a new custodial wallet meets the ordinary threshold

`build-exchange-accounting.py` refuses to finish while a qualifying exchange
wallet is still in the automatic same-address category. That is the one case
where the eligibility split must be touched, and it is quick (minutes, no chain
scan):

1. bootstrap `build-exchange-accounting.py` without the category/stage
   arguments to regenerate `qualified-exchange-exclusions.csv`;
2. move the previous `policy-*.csv`/`policy-summary.json`/`policy-verify.json`
   aside and rerun `apply-eligibility-policy.py` plus
   `verify-eligibility-policy.py` with the new exclusion file;
3. `make migration-policy`;
4. rerun `build-exchange-accounting.py` with the category and stage arguments,
   then `make exchange-native-report` and `make private-manifest`.

When step 2 was needed, the downstream release artifacts are stale until the
chain below is rerun. If the user asked to "update routing" or "finish", run
it rather than stopping to ask; it takes roughly thirty minutes end to end
(two slow steps) and touches no chain data:

5. `build-vault-share-allocation.py` (move the previous `out/base-*` files
   aside first); only the automatic/excluded wallet base lists change unless
   the moved wallets had delegations;
6. `apply-routes.py` (≈4 minutes; the one slow step), then
   `verify-exchange-routing.py`;
7. re-pin the exchange normalization summary in the WONE overlay: rerun
   `apply-wone-qualification.py --replace` on both all-address ledgers with the
   arguments recorded in their `*-summary.json` (both 1.5 GB outputs reproduce
   byte-for-byte when the new exchange holds no WONE, so nothing downstream
   re-hashes), then `filter-claims-by-one.py` on both (move the old outputs
   aside), `build-wone-routes.py --replace`, and `make migration-policy` again
   so the stage summary pins the new migration summary. If the overlay stops
   with "WONE-only metadata does not equal the complete delivery set", a new
   exchange address holds WONE but has no native row: open the archival RPC
   tunnel and rerun `build-wone-new-holder-metadata.py --replace` with the
   pinned block/hash/state-root and the current normalization summary first
   (it only adds the missing rows). When the exchange holds WONE, the ledgers
   change and steps 4–6 must run **after** this step, not before;
8. `make initial-stage`, `verify-wone-allocation.py --replace` (≈15 minutes;
   the second slow step), and the report builders that read
   `routing-summary.json` (`build-entitlement-report.py`,
   `build-non-issuance-report.py`, `build-wone-report.py`);
9. `make private-manifest`, `make verify-private`, `make check-public`.

When the changed inventory holds no WONE this is a provenance re-pin, not a
WONE recalculation: the WONE holder census does not change, and the step must
prove that by reproducing the ledger hashes. When it does hold WONE, the
census is still unchanged but the redistributed/retained split moves by exactly
the exchange WONE added to wallet rows; record the before/after amounts.

## Escalation

A full downstream rebuild is justified only when the user explicitly asks for
release-ready allocations/routing or when an exchange address change must alter
an already approved distribution artifact. When either condition holds, say
that the routing compile is the slow step and run the chain above in the same
turn; do not end the turn to ask permission.
