# Bridge collateral allocations

This document describes how the Harmony LayerZero bridge collateral is moved
to custody and divided among the holders of the bridged tokens. The claim
contracts and claim interface are a later stage; this stage produces the
reviewed ledger, the per-chain claim lists they will consume, and the custody
plan. Exact amounts and addresses stay under the numerical embargo in the
ignored output directory.

Every chain fact comes from archive JSON-RPC at a pinned block: Harmony from a
Harmony archival node, other chains from an archive RPC provider. Block
explorers, explorer APIs, and indexer holder lists are not data sources.

## Pools

[`bridge/routes.json`](../bridge/routes.json) lists every bridge pool: one
remote bridge contract and one remote asset, with the Harmony contract or
contracts on the other side. Each pool has one of two kinds.

- `remote_escrow`: the asset lives on the remote chain. The remote bridge
  contract holds it in escrow and Harmony holds a wrapped token. Holders of
  the wrapped token on Harmony at the Harmony cutoff claim the escrowed asset
  on the remote chain. A pool can have more than one Harmony token (Linea USDC
  has a legacy and a current wrapper); their balances are added.
- `harmony_origin`: the asset lives on Harmony. A Harmony contract locks it and
  the remote chain holds a bridged token. ONE is the only supported asset of
  this kind: the two NativeOFT contracts lock native ONE (and any WONE they
  hold) for bridged ONE on BNB Chain and Ethereum. Because Harmony is retired,
  that collateral is issued as Ethereum ERC-20 ONE from the supply reserve
  Safe instead of being moved. Other Harmony-origin assets have no claim asset
  off Harmony and are reported as unsupported.

`fetch-bridge-state.py` checks the registry against the chain: code, endpoint,
`token()`, decimals, and which side holds the collateral.

Bridge contracts were redeployed and re-paired over time, so the registry also
records every earlier pairing. Two checks require it to be complete:

- `fetch-bridge-state.py` reads each remote adapter's `SetTrustedRemote` and
  `SetTrustedRemoteAddress` history from its deployment. Every Harmony peer it
  ever trusted must be a registered path of the pool (`historical_peer` paths
  for earlier proxies of the same wrapper) or an `ignored_peers` entry with a
  reason (a peer that wraps another token and exchanged no messages).
- `check-harmony-peers.py` reads the same events emitted by every registered
  Harmony proxy up to the Harmony cutoff. Every remote adapter one ever trusted
  must be the pool's adapter, a `previous_remote_adapters` entry, or an
  `ignored_remote_peers` entry with a reason.

Without the earlier paths, messages delivered through an old proxy look
undelivered, and an old escrow that still holds the asset would be left out of
collateral and custody.

## Destinations of bridged ONE

The NativeOFT contracts can only reach chains the Harmony LayerZero endpoint
has sent to. `fetch-bridge-state.py` reads the endpoint's outbound nonce from
each Harmony-origin contract to every LayerZero chain id up to
`--discovery-max-chain-id` at the Harmony cutoff. A nonzero nonce for a chain
without a registered pool fails the state check, so every chain that ever
received bridged ONE has a pool.

## Snapshot blocks and the rollback

Harmony shard 0 was rolled back to block `92,730,034` (`2026-08-11T23:25:37Z`).
Everything Harmony did after it on the abandoned branch was undone, but
deliveries that LayerZero completed on other chains during that time were
not. The rollback block on another chain is the last block at or before the
same timestamp; the cutoff block is the last block at or before the migration
cutoff, `2026-09-10T14:00:00Z`. `resolve-snapshot-blocks.py` resolves both
labels on every chain and verifies the pinned Harmony blocks.

A remote chain received rollback-orphaned deliveries when it accepted more
messages from a Harmony proxy than canonical Harmony ever sent:

```text
sum over the proxy's remote paths of inbound counters at the state block
  > the proxy's Harmony outbound counter at the rollback block
```

The Harmony endpoint's outbound counter counts every message a proxy sent, to
any remote adapter, while each remote inbound counter counts only the messages
delivered on its own path, so the remote side is summed over the current and
previous adapters. The rollback block is the last block both Harmony histories
share, so any delivery beyond its outbound count came from the abandoned
branch. The same rule, reversed, compares each remote adapter's outbound
counter with the sum of Harmony inbound counters over every proxy. This uses
only endpoint counters and does not depend on an event index.

Holder snapshots:

- `remote_escrow` pools use Harmony holders at the Harmony cutoff block.
- ONE pools use remote holders at the remote cutoff block, unless the remote
  chain received rollback-orphaned ONE deliveries, in which case they use the
  remote rollback block. This keeps the forged ONE delivered during the
  incident, and everything traded from it, out of the holder balances.
- `snapshot_policy.remote_holder_overrides` in the configuration can force a
  label for a chain; the plan records the basis either way.

`build-snapshot-plan.py` also flags a pool for review when the counters are
not comparable (a remote counter ahead at the rollback time) or when canonical
Harmony sends after the rollback reuse nonces already consumed by orphaned
deliveries.

## Message adjustments

A holder snapshot alone misstates any message that crossed the snapshot
block or never completed. `scan-bridge-messages.py` reads the nonce ranges
from the plan, finds the matching `SendToChain`, `ReceiveFromChain`, and
`MessageFailed` events, and assigns each event its nonce from the endpoint
counter before and after its block. Each message becomes a signed adjustment
for one address:

| Pool kind | Category | Adjustment |
|---|---|---|
| remote escrow | `orphaned_payout` (escrow paid a burn the rollback restored) | recipient debited, or kept as pool loss with `orphaned_payouts: pool_loss` |
| remote escrow | `undelivered_burn` (canonical Harmony burn never paid) | Harmony sender credited |
| remote escrow | `undelivered_deposit` (remote deposit never minted on canonical Harmony) | remote sender credited |
| ONE | `orphaned_mint` (remote mint for a lock the rollback undid) | none when after the snapshot block; debited otherwise |
| ONE | `canonical_mint_after_snapshot` (canonical lock minted after the snapshot block) | recipient credited |
| ONE | `undelivered_lock` (canonical lock never minted) | Harmony sender credited |
| ONE | `undelivered_return` (remote burn at or before the snapshot block never released) | remote sender credited |
| ONE | `canonical_release_after_snapshot` (remote burn after the snapshot block that canonical Harmony released) | remote sender debited |

A failed delivery consumed a nonce without moving tokens: in a canonical range
its recipient is credited, in an orphaned range nothing changes. Every pool
must account for every nonce in its ranges, or the scan is incomplete and the
build refuses to run.

## Holder snapshots

`token-holders` replays every `Transfer` event from the token's deployment to
the snapshot block. Tokens on the same chain and block share one replay: each
`eth_getLogs` request filters on all of them. The result is accepted only
when every replayed balance equals `balanceOf()` at the snapshot block and the
balances sum to `totalSupply()`.

A provider can silently omit a log from a range response; a missed transfer
between two holders leaves the total intact and is visible only per account.
For every account whose replay differs from `balanceOf()`, `token-holders`
queries that account's own transfers and applies any log the range scan did
not see; if none is missing there, it binary-searches the first block where
the account's replay and `balanceOf()` diverge and re-reads that block. Every
repaired log is listed in the snapshot summary, and the check repeats until
all balances match.

## Holder classification

`classify-holders.py` reads code at the snapshot block. Accounts without code,
EIP-7702 delegated accounts, and Harmony validator wrappers are key-controlled
and claim at the same address. Safes and MultiSigWallets claim at the same
address only when they hold on the claim chain itself; elsewhere they are held
until an approved destination exists. 1wallets, SmartVaults, and other
contracts are held for review. On Harmony the cutoff contract review adds its
1wallet, multisig, SmartVault, and validator classifications.

Bridge contracts, tokens, endpoints, Horizon bridge managers and multisigs,
the bridge deployer, custody multisigs, the reserve Safe, the zero address,
and the burn sink are system addresses on every chain and are excluded. So is
any address no key can control: addresses at or below `0xffff` (precompiles
and BNB Chain system contracts) and placeholder addresses that repeat one hex
digit.

`build-migration-exclusions.py` carries the migration's non-issuance decisions
into the bridge: an address is excluded from every pool when the migration
issues none of its claim (non-issuance inventory rows with no remaining
original-address allocation, reported wallet-theft perpetrators and
report-linked theft recipients, burn and inaccessible addresses). Addresses
the migration only partly deducts keep their bridge allocations; its summary
lists them, with the part of each intended deduction that the cutoff claim
could not cover, for a separate decision.
Optional exclusion and override files apply reviewed decisions:

- exclusions: `chain,address,pool_id,reason` (blank `pool_id` means every pool);
- overrides: `pool_id,address,action,destination,reason`, where `action` is
  `claim`, `redirect` (pay `destination`), `hold`, or `exclude`.

## Allocation

For each account in a pool:

```text
gross = max(0, snapshot holding + message adjustments)
```

Claimable and held accounts are eligible. When their gross total exceeds the
pool collateral, every eligible allocation is

```text
floor(gross * collateral / eligible)
```

otherwise it equals gross. Excluded balances, surplus collateral, rounding
dust, and amounts below `minimum_claim_raw` stay in custody, so each pool
satisfies

```text
collateral = claimable + held + retained
```

Held allocations are reserved, not paid; resolving one with an override moves
it into the claim list without changing anyone else's allocation. Debits
larger than the holding are recorded as `unrecovered_debit_raw`.

Collateral is the escrow balance, plus any balance of the same asset left in a
previous escrow, at the pinned remote state block for `remote_escrow` pools,
and the NativeOFT native ONE plus WONE balance at the Harmony cutoff for ONE
pools. With `--contract-review-policy`, the ONE
collateral must equal the NativeOFT amounts in the migration ledger, which
routes them to the `layerzero-nativeoft-custody` destination.

## Custody

Each remote chain names a custody multisig in the configuration. By default
it is the bridge admin Safe that already owns the escrows on that chain;
`custody_requirements` (owner count and threshold) must hold for it to be
ready. A chain without a qualifying multisig is reported as missing. The
custody plan lists, per chain, each escrow's balance to move, its current
owner, and whether that owner is the custody Safe. The escrows have no
withdrawal: each escrow's owner releases it on its own chain by delivering one
LayerZero packet it attests itself, with no cross-chain message and no
LayerZero service or fee. See
[`bridge-claims/docs/RELEASE.md`](../bridge-claims/docs/RELEASE.md).

The ONE funding entry sends the total ONE collateral from the reserve Safe to
the Ethereum custody multisig and includes the ERC-20 `transfer` calldata for
a Safe transaction. Its destination is the proposed
`layerzero-nativeoft-custody` routing destination.

## Outputs and verification

`build-bridge-allocations.py` writes, in the output directory:

- `pools.csv` — collateral, eligible total, scale, claimable, held, retained,
  surplus, and shortfall per pool;
- `allocations.csv` — one row per pool and account with the holding,
  adjustment, class, status, payee, and allocation;
- `claims/<chain>.csv` — claimable allocations summed per payee and claim
  token, the input for the claim contracts in
  [`bridge-claims/`](../bridge-claims/README.md);
- `custody-plan.json` — escrow transfers, custody readiness, and ONE funding;
- `summary.json` and `BRIDGE_ALLOCATIONS.md`.

`verify-bridge-allocations.py` recomputes holdings from the holder files and
adjustments from the message file, re-derives every allocation from the
pool scale, checks conservation, requires the claim lists to equal the
claimable allocations exactly, checks the custody and funding amounts and
calldata, and checks every recorded output hash.

`build-summary-report.py` then reads the verified outputs and writes
`BRIDGE_ALLOCATION_SUMMARY.md`: per-chain rollups, per-asset collateral
shares (claimable, held, retained, shortfall, scale), bridged-ONE detail
and claim-size buckets, same-symbol totals across chains, holder classes,
the largest held positions, claim-size quantiles, and review items such as
payouts to reserved low addresses.

With a price snapshot from `fetch-prices.py`, the summary also values every
pool, chain, and account in USD and lists the price used for each token.
USD stablecoins named in `pricing.stablecoins` are valued at par. Every other
asset is priced by the CoinMarketCap listing that lists the escrowed token's
contract on that chain, so a symbol alone never selects a price; a configured
id covers renamed or migrated tokens and records why. Bridged ONE is priced as
Harmony ONE because it is paid in ERC-20 ONE. Assets without a quote stay in
units and are left out of USD totals. Prices are a point-in-time valuation for
reporting; allocations are always in token units and never depend on them.

## Known limits

- Failed deliveries are detected only inside the scanned nonce ranges. A
  failure stored before the rollback and never retried needs a separate
  full-history `MessageFailed` scan.
- Horizon-era routes mint through the Horizon multisig on Harmony. Deposits
  whose mint transaction was submitted but never executed there are not yet
  detected.
- LP positions and other contract holders are held, not looked through.
- The custody plan lists what each escrow holds; the release transactions
  themselves come from `bridge-claims/tools/release_plan.py`.

## Claims

Each chain's claim list becomes one immutable `BridgeClaims` contract that
pays every asset of that chain from the custody multisig's exact approvals;
held positions are not in the lists and stay in custody. See
[`bridge-claims/docs/DESIGN.md`](../bridge-claims/docs/DESIGN.md) and
[`bridge-claims/docs/RUNBOOK.md`](../bridge-claims/docs/RUNBOOK.md).

## Running it

See [`bridge/README.md`](../bridge/README.md).
