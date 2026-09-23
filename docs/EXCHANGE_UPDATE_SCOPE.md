# Exchange update scope memo

Date: 2026-09-18

Read this before changing an exchange wallet list, signature proof, or
destination.

## Default scope

An exchange update is an **exchange-local data refresh**, not a migration-policy
or global-accounting change.

- Validate only the changed exchange files.
- Independently sum row-level values; never trust a submitted aggregate total.
- Join submitted wallet addresses to the already-built cutoff native-ONE
  accounting artifacts.
- Verify any submitted ownership signatures and configured aggregate
  destination.
- Update exchange-normalized CSVs, exchange summaries/memos, and other compact
  exchange-only findings.
- Reuse unchanged normalized exchange outputs by source hash.

Target completion time for an ordinary update is under one minute. Do not
restart chain scans or multi-gigabyte global verification for this task.

## Asset and policy boundary

Exchange submissions in this workspace concern their submitted custodial
wallet inventory and native ONE aggregate delivery.

- Do **not** infer exchange ownership or authorization of WONE merely because a
  listed address appears in the independent WONE holder census.
- Incidental WONE at a listed address is not part of the exchange submission.
- Do not add exchange addresses to the WONE overlay or alter the WONE reserve
  split.
- Gate is the exception to aggregate delivery: it remains under the ordinary
  threshold, same-address, and wallet-stage policy.
- For other exchanges, use the established native exchange-accounting
  semantics and configured aggregate destination. Do not invent a new
  qualification test or migration stage.

Threshold overlap may be calculated internally only when an existing automatic
same-address output must exclude a non-Gate custodial address. It is not an
exchange entitlement statistic and should not headline the exchange memo.

## External agents' workspaces

`harmony-supply-audit/artifacts/historical-hacks-investigation-20260916/`,
`artifacts/x-article-20260917/`, and `artifacts/activity-composition-20260919/`
belong to agents working outside Cursor. Never write into them, never read
them as pipeline inputs, and never mirror them. The retained-address input the
stage policy needs is pinned in this repository at
`artifacts/supply-reconciliation-20260911/not-issued-retained-initial-addresses.csv`
(and in `harmony-supply-audit/artifacts/historical-retention-snapshot-20260916/`).

## Do not run by default

Unless the user explicitly requests a complete release rebuild, do not modify
or regenerate:

- `apply-wone-qualification.py` outputs;
- WONE holder, redistribution, or retained-reserve artifacts;
- all-address native/migration claim ledgers;
- migration-stage policy;
- global routing exceptions or initial-stage materialization;
- the full WONE allocation verifier;
- unrelated contract, incident, non-issuance, or supply reports.

Do not create an `exchange_aggregate` migration stage or add WONE to exchange
entitlements. Any uncommitted change doing either is over-scoped and should be
removed before finalizing an exchange-only update.

## Fast path

1. Hash and inspect the changed raw workbook/document/destination.
2. Parse and normalize only the changed exchange.
3. Check duplicates and cross-exchange overlap against existing normalized
   outputs.
4. Independently sum its row-level submitted native balances, when supplied.
5. Join its addresses to existing cutoff native claim rows and calculate the
   requested exchange statistics.
6. Verify designated signatures and destination binding.
7. Refresh the exchange memo and compact exchange summary.
8. Run focused exchange tests and syntax checks.

Stop there unless the user explicitly asks to propagate the change into a
release candidate.

### When a new custodial wallet meets the ordinary threshold

`build-exchange-accounting.py` refuses to finish while a qualifying non-Gate
wallet is still in the automatic same-address category. That is the one case
where the eligibility split must be touched, and it is quick (minutes, no chain
scan):

1. bootstrap `build-exchange-accounting.py` without the category/stage
   arguments to regenerate `qualified-non-gate-exclusions.csv`;
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
   so the stage summary pins the new migration summary. Skip
   `build-wone-new-holder-metadata.py` (needs RPC) when every new exchange
   address already has a native row; its summary keeps the old pin;
8. `make initial-stage`, `verify-wone-allocation.py --replace` (≈15 minutes;
   the second slow step), and the report builders that read
   `routing-summary.json` (`build-entitlement-report.py`,
   `build-non-issuance-report.py`, `build-wone-report.py`);
9. `make private-manifest`, `make verify-private`, `make check-public`.

This is a provenance re-pin, not a WONE recalculation: the WONE holder census,
reserve split, and redistribution do not change with an exchange update, and
the step must prove that by reproducing the ledger hashes.

## Escalation

A full downstream rebuild is justified only when the user explicitly asks for
release-ready allocations/routing or when an exchange address change must alter
an already approved distribution artifact. When either condition holds, say
that the routing compile is the slow step and run the chain above in the same
turn; do not end the turn to ask permission.
