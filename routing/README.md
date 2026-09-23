# Explicit claim routing

Claim amounts and destinations are separate concerns:

```text
total_claim = wallet_airdrop + staked_to_vault
```

Routes never change the expanded claim input. They redirect wallet tokens and
vault shares, hold them, mark an exact amount `not_issuing`, or mark a WONE
source amount `redistributed`. Not-issued and redistributed source amounts are
excluded from final token and vault-share creation; redistribution is paired
with WONE already added to holder rows.

Same-address delivery is implicit only for an ordinary code-less EOA. Generated
routing files contain exceptions, not one row per ordinary wallet. Verified
validator wrappers are explicit same-address exceptions because they are
code-bearing; genuine contracts, policy-routed accounts, and explicitly named
deferred claims are also exceptions.

## Public and private files

- `routes.example.csv` — public schema example with dummy addresses;
- `destinations.example.csv` — public symbolic-destination example;
- `governors.example.csv` — public validator-governor example;
- `policy-decisions.example.csv` — public release-gate example;
- `local/` — ignored authoritative routing inputs, destinations, decisions,
  and generated exception outputs.

Place manual additions in separate files under `routing/local/`, for example:

- `not-issuing.csv`
- `contract-policy.csv`
- `multisigs.csv`
- `lost-wallets.csv`
- `frozen-wallets.csv`
- `bridge-reserves.base.csv` — immutable initializer output
- `bridge-reserves.csv` — generated WONE-aware routes
- `exchanges.csv` — generated private exchange manual-delivery routes
- `validator-governors.csv`
- `destinations.csv`
- `exchange-destinations.csv` — generated private exchange destinations
  (aggregate, and wallet/staking pairs for split exchanges)
- `policy-decisions.csv`
- `artifacts/migration-policy-20260917/migration-stage-policy.csv` — generated
  classification-independent stage input
- `generated/routing-exceptions.csv` — sparse wallet and vault-share exceptions
- `generated/validator-governor-exceptions.csv` — sparse vault-governor
  exceptions, kept separate from validator wallet delivery
- `generated/validator-vault-stages.csv` — complete validator-vault assets
  partitioned into initial, exchange-aggregate, next-stage, deferred,
  not-issued, and post-policy totals
- `generated/initial-stage/` — materialized initial wallet destinations,
  vault-share beneficiaries, validator assets/governors, unresolved gates, and
  verification summary
- `artifacts/exchange-accounting-20260917/exchange-manual-deliveries.csv`,
  `exchange-wallet-deliveries.csv`, `exchange-delegation-withdrawals.csv`, and
  `exchange-validator-vault-adjustments.csv` — the private manual delivery
  worksheet (one transfer per destination, funded from the 2050 supply
  reserve) and the delegated principal released from each validator vault
- `generated/unresolved-routing.csv` — generated hold queue; never edit it
- `generated/routing-summary.json` — conservation and release-gate summary

Initialize these held-by-default files once:

```sh
python3 toolkit/scripts/routing/init-local-routing.py
```

The initializer refuses to overwrite existing routing decisions.

`merge-wallet-theft-inventory.py` preserves the historical exact amounts,
validates reviewed wallet-theft additions against the global cutoff ledger,
and writes `non-issuance-inventory.csv`. Reported victim wallets remain a
separate review population and are not routed to non-issuance.

`build-non-issuance-routes.py` combines the existing reviewed inventory and
the historical retained-cap inventory. It converts each positive
`not_issued_atto` or `retained_cap_atto` amount into `not-issuing.csv`,
rejecting overlap. Existing partial-row remainders continue under the separate
stage and destination policies.

`build-wone-routes.py` reads the immutable `bridge-reserves.base.csv` and the
verified WONE qualification summary, then writes `bridge-reserves.csv` with
two additional exact source routes:
the amount paired with ordinary-threshold and exchange manual-delivery WONE
is `redistributed`, while the remainder is `not_issuing` and retained in the
2050 premint reserve. Their sum must equal the WONE contract's shard-0 native
reserve. Same-address shard-1 ONE is not backing and falls through to
reviewed-contract non-issuance.

`build-migration-stage-policy.py` generates the address-level stage policy
after applying exact deductions without retesting the snapshot threshold.
It distinguishes initial wallets, deferred wallets, next-stage reviewed
contracts, and terminal reviewed-contract non-issuance while recording
`migration_stage` separately from `issuance_treatment`.

`build-contract-policy-routes.py` consumes that stage policy. It writes:

- `ALL` next-stage holds for reviewed multisigs and 1wallet allocations;
- `ALL` terminal non-issuance routes for SmartVault and all other reviewed
  genuine contracts;
- no duplicate LayerZero route, because the two dedicated next-stage
  `SHARD0_LIQUID` holds already live in `bridge-reserves.csv`.

Generated multisig holds intentionally have no common destination ID. Add a
separate per-address row to `multisigs.csv`, with priority below `500`, only
after its replacement Safe owner set and threshold are verified. Filling one
symbolic destination must never redirect all 96 multisigs.

Higher-priority incident and WONE routes apply first. The contract policy then
consumes every remaining direct-wallet and staked-vault component. No generic
contract-recovery-custody destination remains.

`build-exchange-accounting.py` writes `exchanges.csv` with a manual-delivery
route for each positive native, WONE, or delegated claim in a confirmed
exchange inventory and writes `exchange-destinations.csv` separately. Every
route carries `reason = exchange_manual_reserve_delivery` and
`status = exchange_manual` (or `hold` while a destination is missing). The
route shape follows the exchange's destination mode: one `ALL`,
`wallet_first_pro_rata_vault` route to the aggregate destination; one exact
`wallet_only` route for the wallet components plus one `ALL` route for the
staking components when the exchange split its destinations (priority `300`
then `301`); one `ALL` route whose `destination_address` is the source wallet
itself for same-address exchanges; and, for a tiered exchange, a same-address
route when the source's ordinary stage is `initial` and an aggregate route
otherwise. The ordinary threshold does not limit exchange entitlement; it only
identifies overlap to remove from the implicit automatic category. Reviewed
incident/non-issuance routes still apply first, while later reserve or generic
contract handling cannot silently take an exchange-routed remainder. Confirmed
routes compile into the `exchange_manual` stage with
`issuance_treatment = manual_from_reserve`; they are excluded from the airdrop
and from every `issuable_*` total, and their delegated principal is released
from the validator vaults.

The routing command accepts both `--routes` and `--destinations` repeatedly.
Files are merged by `priority`, then `route_id`; file order is irrelevant, and
destination identifiers must remain globally unique. Use `--replace` when
regenerating the exception outputs after an approved input change.
`--migration-stages` is required and must cover the complete inclusive
threshold set exactly. Explicit non-exchange below-threshold claims remain
`manual_review`; confirmed exchange claims use the separate `exchange_manual`
stage and never enter the ordinary initial stage.

## Route input columns

- `route_id` — globally unique stable identifier;
- `priority` — smaller integer is applied first;
- `source_address` — Harmony claim owner;
- `destination_id` — symbolic destination from any supplied destination CSV;
- `destination_address` — optional direct Ethereum address; takes precedence
  over `destination_id`;
- `amount_atto` — exact amount, `ALL` for everything still unassigned, or
  `SHARD0_LIQUID` for the source's shard-0 liquid component;
- `allocation_method`:
  - `wallet_first_pro_rata_vault`
  - `wallet_only`
  - `vault_only_pro_rata`
- `reason` — human-readable policy reason;
- `evidence` — report, ticket, governance decision, or transaction evidence;
- `notes` — operator notes.

For a partial route, `wallet_first_pro_rata_vault` consumes direct wallet tokens
first. Any remainder is taken proportionally from all of the source's validator
positions, using exact integer largest-remainder allocation.

LayerZero reserve-contract routes use `SHARD0_LIQUID` so same-address value on
another shard is not mislabeled as contract backing. WONE uses two exact
amounts generated from its verified holder summary. Any source remainder
proceeds to the next applicable route. Under the reviewed policy, the WONE
address's non-backing shard-1 remainder is terminal non-issuance.

## Generated output contracts

`routing-exceptions.csv` is the compiled sparse exception ledger. Its rows
contain:

- `component` — `wallet_airdrop` or `vault_shares`;
- source and optional validator address/secure-key fields;
- `source_category` and `source_code_bearing`;
- `migration_stage` — `initial`, `exchange_manual`, `next_stage`, `deferred`,
  or the explicit below-threshold state `manual_review`; terminal rows may
  retain an associated stage but never invent one;
- `issuance_treatment` — `issue`, `manual_from_reserve`, `not_issued`, or
  `redistributed`;
- `amount_atto` and `exception_type`;
- route priority, destination, status, reason, and evidence.

`exception_type` distinguishes manual `explicit_route` rows, verified
`validator_wrapper_same_address` rows, and generated holds for contract,
excluded, or explicitly routed deferred claims. An ordinary code-less EOA with
no explicit route is absent.

Generated reviewed-contract policy rows also carry the cutoff block number,
block hash, and state root used to classify the contract. Routing rejects a
missing, mixed, or mismatched cutoff identity and requires it to match the
validator-classification CSV from the same contract-review run.

`validator-governor-exceptions.csv` contains only explicit governor overrides
and generated governor holds. The default governor for an otherwise untouched
validator vault is implicitly the validator's key-controlled address.

`unresolved-routing.csv` is derived from the two exception sets. It contains
every held wallet, vault-share, or governor exception, including its amount and
intended destination. A `not_issuing` or `redistributed` row is terminal and
does not appear in this work queue. The file is a release gate, not an input or
an additional policy decision.

`routing-summary.json` proves that explicit routes plus implicit defaults
preserve the complete wallet and staked-to-vault totals. It records hashes for
the generated CSVs, unresolved totals, not-issued, redistributed, and
remaining all-stage totals, per-stage totals and readiness, the WONE reserve
split, inactive routes, pending stage review, and pending policy decisions.
`stage_readiness` scopes held amounts, governor holds, and policy gates to each
stage. `exchange_manual` is independently ready when every configured exchange
destination is confirmed; it is executed by hand from the 2050 supply reserve
rather than by the airdrop deployment. The global status is conservative;
an unresolved next-stage destination does not by itself change
`initial_stage_status` or exchange readiness.

`materialize-initial-stage.py` expands implicit code-less wallet delivery,
applies explicit initial destinations, subtracts terminal wallet/vault
non-issuance, and filters out exchange-aggregate, next-stage, deferred, and
manual-review rows. It also builds the initial validator-vault asset and share
plans and verifies them against `stage_readiness.initial`. Its output remains
held while any initial destination, governor, or stage-scoped policy gate is
unresolved.

## Destination input columns

- `destination_id` — symbolic identifier such as `treasury` or
  `not-issuing`;
- `destination_address` — Ethereum address, blank while unresolved;
- `status` — `ready`, `hold`, `exchange_manual` (delivered by hand from the
  2050 supply reserve; only exchange routes may use it), terminal
  `not_issuing`, or terminal `redistributed`;
- `notes` — operator notes.

A blank or held exception destination never falls back to the original source
address. It remains in `unresolved-routing.csv`.

`not-issuing` is the only destination allowed to have `not_issuing` status. It
must have no address. The routed amount is complete and resolved, but no token
may be created for it. A not-issued staked row also reduces the corresponding
validator vault's deployed assets and shares by that exact amount.

`wone-holder-redistribution` is the only destination allowed to have
`redistributed` status. It also has no address. It is a source offset paired
exactly with WONE already included in ordinary-threshold or exchange
manual-delivery wallet rows, not a second recipient.

## Validator-governor input columns

- `validator_address` — Harmony validator represented by the vault;
- `destination_id` or `destination_address` — Ethereum governor;
- `status` — `ready` or `hold`;
- `reason`, `evidence`, `notes` — policy record.

The validator's own wallet and vault-share delivery appears in
`routing-exceptions.csv` as a verified code-bearing same-address exception.
Vault governance is a separate concern: an untouched vault governor defaults
to the validator's key-controlled address, while a validator address with an
explicit claim route defaults to governor hold until `validator-governors.csv`
supplies an approved destination.

`validator-vault-stages.csv` starts from each base vault's full active
principal. It records represented initial, exchange-aggregate, next-stage,
qualified-deferred, manual-review, and not-issued assets plus the uncompiled
below-threshold deferred remainder. `post_policy_assets_atto` equals base assets
minus not-issued assets. Initial and exchange-aggregate assets have separate
readiness; next-stage or deferred shares cannot be released merely because
their validator vault exists.

## Policy-decision gate

`policy-decisions.csv` records non-address decisions that can still make a
numerically correct route unsafe. Each row has:

- `decision_id`;
- `status` (`pending` or `resolved`);
- `decision`;
- `evidence`;
- `notes`.

A resolved row must state the decision. Any pending row keeps the generated
routing summary on hold.

The file must include `rollback-exploit-proceeds`,
`initial-wallet-activity-stage`, `reviewed-contract-migration-policy`,
`exchange-manual-reserve-delivery`, `wone-holder-redistribution`, and
`layerzero-nativeoft-reconciliation`, as created by
`init-local-routing.py`. Missing required decisions reject the input,
including an empty or header-only file. Additional decisions are allowed and
also keep routing on hold while pending.

The WONE policy is resolved: add WONE to ordinary inclusive-threshold rows and
to confirmed exchange rows, offset the matching source reserve as
`redistributed`, and mark the remaining reserve `not_issuing`.
`build-wone-routes.py` derives both exact amounts from the verified holder
overlay; no destination address is used.

LayerZero NativeOFT contracts are separate because they directly hold native
ONE. Their next-stage eligibility is resolved. The remaining pending gate is
destination readiness: remote supply and messages in flight must be reconciled
before a destination is approved.

## Safety

- Non-issuance, treasury, burn, inaccessible, and perpetrator routes take
  precedence over same-address delivery.
- Exchange wallets overlapping the ordinary threshold may not remain implicit
  automatic deliveries; every positive native, WONE, or delegated claim in a
  received exchange inventory must have exchange manual-delivery routes
  regardless of that threshold, and every blank exchange destination is held.
- No exchange wallet is airdropped. The whole exchange entitlement is delivered
  manually from the 2050 supply reserve, and exchange delegated principal is
  released from the validator vaults instead of being issued as shares.
- Ordinary code-less EOAs alone use an implicit same-address default.
- Every verified validator wrapper is recorded as a code-bearing same-address
  exception with the validator classification file as evidence.
- Genuine contracts never enter an activity-derived initial-stage row.
- After higher-priority exact incident or reserve routes, excluded reviewed
  contracts send every remainder to terminal non-issuance. Reviewed multisig
  and 1wallet remainders stay held for the next stage, while LayerZero uses
  its dedicated next-stage hold.
- Any unconsumed remainder for an ordinary EOA is implicit; any unconsumed
  validator remainder remains an explicit validator exception.
- Routing requests may not exceed the source's remaining total claim.
- Issuable amounts plus not-issued amounts plus redistributed source amounts
  must sum back to the expanded wallet and vault totals.

The authoritative routing policy consists of the reviewed sparse inputs and
their generated exception outputs under `routing/local/`. The conservative
all-stage build requires zero inactive routes, unresolved amounts, missing
governors, and pending policy decisions. A scoped deployment may proceed when
its own `stage_readiness` entry and independent materializer are ready; this is
how `exchange_manual` remains deliverable while unrelated ordinary-stage
policy gates are pending.
