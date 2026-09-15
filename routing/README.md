# Explicit claim routing

Claim amounts and destinations are separate concerns:

```text
total_claim = wallet_airdrop + staked_to_vault
```

Routes never change the gross `total_claim` audit ledger. They redirect wallet
tokens and vault shares, hold them, or mark an exact amount `not_issuing`.
Not-issued amounts are excluded from final token and vault-share creation.

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
- `treasury.csv`
- `contracts-to-treasury.csv`
- `multisigs.csv`
- `lost-wallets.csv`
- `frozen-wallets.csv`
- `bridge-reserves.csv`
- `validator-governors.csv`
- `destinations.csv`
- `policy-decisions.csv`
- `generated/routing-exceptions.csv` — sparse wallet and vault-share exceptions
- `generated/validator-governor-exceptions.csv` — sparse vault-governor
  exceptions, kept separate from validator wallet delivery
- `generated/unresolved-routing.csv` — generated hold queue; never edit it
- `generated/routing-summary.json` — conservation and release-gate summary

Initialize these held-by-default files once:

```sh
python3 toolkit/scripts/routing/init-local-routing.py
```

The initializer refuses to overwrite existing routing decisions.

`build-non-issuance-routes.py` converts the exact audited amounts previously
assigned to treasury into `not-issuing.csv`. It reads the legacy
`treasury_reclaim_atto` inventory field without increasing it. Existing
partial-row remainders continue to the ordinary destination.

`build-contract-treasury-routes.py` writes every reviewed non-multisig
contract as an `ALL` route to `contract-recovery-custody`. That destination is
one Ethereum Safe or multisig that holds the funds until a verified claimant
is paid. It is separate from the general treasury. Operators may sign for the
holding address, but they may not spend that ONE as ordinary treasury money.
The builder always uses this destination; it cannot send those rows to
`treasury`. A later edit that points a generated row at `treasury` is rejected
by `apply-routes.py`. Reviewed multisigs are omitted on purpose: add a row to
`multisigs.csv` only after the replacement Ethereum Safe address has been
supplied and verified. Higher-priority entries in `bridge-reserves.csv` take
reserve-backed contract claims before these holding-address rows are used.

The routing command accepts `--routes` repeatedly. Files are merged by
`priority`, then `route_id`; file order is irrelevant. Use `--replace` when
regenerating the exception outputs after an approved input change.

## Route input columns

- `route_id` — globally unique stable identifier;
- `priority` — smaller integer is applied first;
- `source_address` — Harmony claim owner;
- `destination_id` — symbolic destination from `destinations.csv`;
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

Reserve-contract routes use `SHARD0_LIQUID` so same-address value on another
shard is not mislabeled as contract backing. Any remainder proceeds to the next
applicable route, normally generic contract-recovery custody.

## Generated output contracts

`routing-exceptions.csv` is the compiled sparse exception ledger. Its rows
contain:

- `component` — `wallet_airdrop` or `vault_shares`;
- source and optional validator address/secure-key fields;
- `source_category` and `source_code_bearing`;
- `amount_atto` and `exception_type`;
- route priority, destination, status, reason, and evidence.

`exception_type` distinguishes manual `explicit_route` rows, verified
`validator_wrapper_same_address` rows, and generated holds for contract,
excluded, or explicitly routed deferred claims. An ordinary code-less EOA with
no explicit route is absent.

Generated contract-recovery route rows also carry the cutoff block number,
block hash, and state root used to classify the contract. Routing rejects a
missing, mixed, or mismatched cutoff identity and requires it to match the
validator-classification CSV from the same contract-review run.

`validator-governor-exceptions.csv` contains only explicit governor overrides
and generated governor holds. The default governor for an otherwise untouched
validator vault is implicitly the validator's key-controlled address.

`unresolved-routing.csv` is derived from the two exception sets. It contains
every held wallet, vault-share, or governor exception, including its amount and
intended destination. A `not_issuing` row is terminal and does not appear in
this work queue. The file is a release gate, not an input or an additional
policy decision.

`routing-summary.json` proves that explicit routes plus implicit defaults
preserve the complete wallet and staked-to-vault totals. It records hashes for
the three generated CSVs, unresolved totals, not-issued and remaining issuable
totals, inactive routes, and pending policy decisions.

These files do not constitute a complete deployment allocation. A later
deployment build must materialize and verify the final wallet/Merkle input from
the base entitlements plus the approved sparse exceptions.

## Destination input columns

- `destination_id` — symbolic identifier such as `treasury` or
  `not-issuing`;
- `destination_address` — Ethereum address, blank while unresolved;
- `status` — `ready`, `hold`, or terminal `not_issuing`;
- `notes` — operator notes.

A blank or held exception destination never falls back to the original source
address. It remains in `unresolved-routing.csv`.

`not-issuing` is the only destination allowed to have `not_issuing` status. It
must have no address. The routed amount is complete and resolved, but no token
may be created for it. A not-issued staked row also reduces the corresponding
validator vault's deployed assets and shares by that exact amount.

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
`contract-recovery-custody`, `wone-reserve-custody`, and
`layerzero-nativeoft-reconciliation`, as created by
`init-local-routing.py`. Missing required decisions reject the input, including
an empty or header-only file. Additional decisions are allowed and also keep
routing on hold while pending.

The WONE policy is resolved: migrate its native reserve once to a dedicated
custody multisig and pay eligible WONE or bridged-WONE claims only by transfers
from that finite reserve. No additional ONE is issued for those claims. The
custody destination remains held until its approved multisig address is
supplied.

LayerZero NativeOFT contracts are separate because they directly hold native
ONE. Their decision remains pending until remote supply and messages in flight
are reconciled and a custody destination is approved.

## Safety

- Non-issuance, treasury, burn, inaccessible, and perpetrator routes take
  precedence over same-address delivery.
- Ordinary code-less EOAs alone use an implicit same-address default.
- Every verified validator wrapper is recorded as a code-bearing same-address
  exception with the validator classification file as evidence.
- Genuine contracts default to hold unless an explicit recovery route exists.
- After any higher-priority incident or reserve route, ordinary
  non-multisig contracts go to the contract holding address. The builder
  cannot send those generated rows to the general treasury. Later claimant
  payments come from that existing balance; they are not a second issuance.
- Any unconsumed remainder for an ordinary EOA is implicit; any unconsumed
  validator remainder remains an explicit validator exception.
- Routing requests may not exceed the source's remaining total claim.
- Issuable amounts plus not-issued amounts must sum back to the original wallet
  and vault totals.

The authoritative routing policy consists of the reviewed sparse inputs and
their generated exception outputs under `routing/local/`. It may feed a later
deployment build only when the summary reports zero inactive routes, zero
unresolved wallet or vault-share amounts, no missing validator governor, and no
pending policy decision.
