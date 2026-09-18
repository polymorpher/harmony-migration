# Harmony migration accounting

This repository contains the source code and methodology used to produce the
Harmony ONE migration ledger at the September 10, 2026 cutoff.

The repository is designed for independent review. A developer with their own
Harmony archival/full databases can rebuild the state exports, staking claims,
cross-shard adjustments, address mappings, final claim ledger, and verification
reports without access to the original operators' machines.

## Cutoff

- Requested time: `2026-09-10T14:00:00Z`
- Shard 0: block `93,623,067`
- Shard 1: block `95,882,100`, the last block at or before the cutoff

Exact block hashes and state roots are in
[`manifests/snapshot-2026-09-10.json`](manifests/snapshot-2026-09-10.json).

## Public eligibility threshold

The selected threshold is `>= 1,000 ONE`, applied to
`qualification_total_atto`: the native total claim plus cutoff WONE balance.
The native claim includes liquid balances, active delegation or validator
stake, pending undelegation, unclaimed staking rewards, and supported pending
cross-shard receipts.

Total claim is not the same as direct wallet airdrop. Active stake/delegation
is moved to the corresponding validator's ERC-4626 vault and represented by
vault shares. The direct wallet airdrop excludes that staked amount and
includes WONE for rows in the current qualifying batch.

The inclusive result is split into ordinary EOAs, verified validator-wrapper
accounts that are also key-controlled, reviewed genuine contracts, and
policy-routed accounts. Reviewed multisig, LayerZero collateral, and 1wallet
allocations are next stage; SmartVault and other reviewed contracts are not
issued. Extra-mint recipients are capped by their exact unreturned extra mint,
so some claim rows must be
split between terminal non-issuance and the ordinary destination. The gross
claim ledger stays unchanged, but not-issued amounts are excluded from final
token and vault-share creation.

Exchange-provided wallet inventories add a separate delivery overlay. Gate
remains under the ordinary inclusive threshold and same-address rules because
it did not request rerouting. Qualifying wallets reported by other exchanges
are removed from the implicit automatic category, and their positive current
migration claims—including threshold-deferred native claims—are routed
manually to a confirmed aggregate exchange destination. A missing destination
is a hold and never falls back to the source wallet.

WONE uses a separate conservation overlay. The source reserve paired with
qualified-holder WONE is terminal `redistributed`; the remaining
below-threshold/excluded reserve is `not_issuing` and retained in the 2050
premint reserve. This prevents the WONE contract and its holders from both
receiving the same backing.

The initial stage contains eligible wallets with indexed activity in the six
calendar months before the cutoff. Reviewed multisig, LayerZero collateral,
and 1wallet allocations are held for the next stage regardless of activity.
SmartVault and all other reviewed genuine-contract allocations are not issued.
Migration stage, account classification, destination, and readiness are
recorded separately.

Wallet-theft evidence keeps explicitly reported perpetrators,
transaction-linked theft recipients, and reported victim wallets as separate
roles. Victim wallets are not automatically routed to non-issuance.

Calculated totals, component breakdowns, result population counts, and output
hashes are intentionally withheld pending independent reproduction and the
first public results article. See
[`docs/numerical-embargo.md`](docs/numerical-embargo.md).

## Distribution status

The public source package is ready for independent review. It is **not itself a
distribution file**. Non-issuance, reviewed-contract, exchange,
lost-wallet, frozen-wallet, and validator-governor decisions live in ignored
routing files.

The ignored routing workspace under `routing/local/` contains reviewed sparse
exceptions, a generated unresolved work queue, and a conservation summary.
Ordinary code-less EOA same-address delivery is implicit; verified
validator-wrapper accounts are explicit code-bearing same-address exceptions.
These routing artifacts are not a complete distribution file. Build and verify
the stage-specific wallet/Merkle and validator-vault inputs separately.
Release a stage only when its `stage_readiness` entry is ready; the global
status remains a conservative all-stage gate.

The toolkit also records the strict comparison for audit. See
[`docs/eligibility-policy.md`](docs/eligibility-policy.md).

The earlier USD-based `$1` filter was exploratory and time-dependent. It is not
the public migration eligibility rule and is not part of the required
reproduction.

## Start here

1. Start with [`docs/README.md`](docs/README.md), then read
   [`docs/methodology.md`](docs/methodology.md).
2. Prepare your own archival databases using
   [`docs/reproduce.md`](docs/reproduce.md).
3. Install the pinned Harmony dependencies:

   ```sh
   ./scripts/setup-harmony-dependencies.sh
   ```

4. Build and test the toolkit:

   ```sh
   ./scripts/build-toolkit.sh
   source ./scripts/toolkit-env.sh
   ```

5. Run the state export, claim merge, threshold filter, and independent
   verification steps in [`docs/reproduce.md`](docs/reproduce.md).
6. Review claim destinations in [`docs/claim-routing.md`](docs/claim-routing.md).
7. Complete [`docs/publication-checklist.md`](docs/publication-checklist.md)
   before releasing an allocation.

## Repository layout

- `toolkit/cmd/` — Go state scanners, preimage recovery, and verifiers
- `toolkit/scripts/` — deterministic CSV pipelines and RPC audits
- `scripts/` — dependency and build helpers
- `docs/` — public methodology and reproduction instructions
- `docs/findings/` — ignored unredacted migration findings during the embargo
- `manifests/` — public source identities and ignored private result identities
- `results/2026-09-11/` — ignored compact result package during the embargo
- `artifacts/` — ignored claim-accounting, contract-review, and evidence files
- `routing/` — public schema/examples and ignored real destination routes
- `exchanges/` — ignored private exchange policy, raw submissions, normalized
  inventories, destinations, and operator notes
- `repro/as-run/` — ignored exact operator scripts retained locally
- `repro/source-snapshots/` — source snapshots for earlier accounting phases
- `user-faq.md` — end-user migration FAQ

The ignored `repro/as-run` scripts contain machine-specific paths and service
controls. Public reviewers should follow `docs/reproduce.md` instead.

## Trust boundary

Consensus state, canonical blocks, signed transactions, staking wrappers, and
cross-shard receipt records are primary sources. Explorer balances are not
used.

Exchange submissions are authoritative only for the exchange's source-wallet
inventory and requested destination. Their stated balances are compared
against, but never substituted for, cutoff consensus state.

Every recovered address must satisfy:

```text
keccak256(address) == secure_account_key
```

Large generated CSVs are intentionally not committed to Git. Publish them as
release assets and verify them against the hashes in the release manifest.

`manifests/source-code.sha256` inventories the public source and documentation
files. Regenerate it with `make manifest` whenever reviewed source changes.

## License

The toolkit is distributed under the GNU Lesser General Public License v3.0.
See [`LICENSE`](LICENSE). Harmony is a linked dependency and retains its own
license and copyright notices.
