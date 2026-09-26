# Toolkit

## Core commands

- `account-snapshot` — enumerate every account leaf at a state root
- `account-preimage-resolve` — fill addresses from another preimage DB
- `actual-supply` — independently total liquid and staking state
- `staking-claims` — export aggregate staking components and optional
  per-validator active-delegation principal
- `cross-shard-supply` — classify receipts at explicit source/destination cutoffs
- `cx-lookup-snapshot` — build a small cutoff CX-lookup database
- `historical-state-diff` — compare two account tries
- `canonical-address-resolve` — recover addresses from canonical block data
- `account-transition-locate` — locate targeted state transitions
- `transition-trace-resolve` — recover addresses from traces
- `account-nonce-preimage-resolve` — recover a sender at its nonce transition
- `address-match-apply` — apply verified address mappings to a CSV
- `migration-claims-verify` — verify total-claim, wallet-airdrop, and
  staked-to-vault fields
- `cutoff-final-verifier` — ignored until result publication because it embeds
  exact expected artifact identities

Additional historical investigation commands are under `cmd/forensics`.

## Python scripts

- `scripts/census/` — sorted cross-shard liquid merge
- `scripts/claims/` — component merge, claim-delivery formatting, threshold
  filtering, and validator-vault share allocation
- `scripts/contract-review/` — distinguish validator wrappers from genuine
  contracts and collect recovery evidence
- `scripts/routing/` — apply treasury and manually maintained destination
  routes and emit sparse wallet/share and validator-governor exceptions
- `scripts/cutoff/` — interval, difference, receipt, RPC, and reconciliation checks
- `scripts/forensics/` — historical exploit and supply investigation

- `scripts/build-evidence-bundle.py` — freeze pipeline outputs into a
  hash-bound evidence bundle
- `scripts/random-sample-verify.py` — verify a reproducible random sample of a
  bundle (see `../docs/random-sample-verification.md`)
- `verifier/` — the shared sample-verification core: `rules.json` (formulas
  and schemas), `sample_core.py`, `sample_core.js`, and the generated offline
  `random-sample-verifier.html`

All Python pipeline scripts use the standard library.

## Build dependency

Three address-recovery commands use Harmony-specific block types. The toolkit
therefore pins a Harmony source checkout at:

```text
../third_party/harmony
```

Run:

```sh
../scripts/setup-harmony-dependencies.sh
../scripts/build-toolkit.sh
source ../scripts/toolkit-env.sh
```

The build helper also prepares the MCL and BLS native libraries needed by the
Harmony packages.

See `../docs/reproduce.md` for database and execution instructions.
