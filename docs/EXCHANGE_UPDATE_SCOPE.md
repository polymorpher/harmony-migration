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

## Escalation

A full downstream rebuild is justified only when the user explicitly asks for
release-ready allocations/routing or when an exchange address change must alter
an already approved distribution artifact. State that this is a slow global
operation before starting it.
