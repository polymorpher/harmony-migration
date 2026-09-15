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
`total_claim_atto`: liquid balances, active delegation or validator
stake, pending undelegation, unclaimed staking rewards, and supported pending
cross-shard receipts.

Total claim is not the same as direct wallet airdrop. Active stake/delegation
is moved to the corresponding validator's ERC-4626 vault and represented by
vault shares. The direct wallet airdrop excludes that staked amount.

The inclusive result is split into ordinary EOAs, verified validator-wrapper
accounts that are also key-controlled, genuine contracts requiring
class-specific recovery, and policy-routed accounts. Extra-mint recipients are
capped by their exact unreturned extra mint, so some claim rows must be
split between terminal non-issuance and the ordinary destination. The gross
claim ledger stays unchanged, but not-issued amounts are excluded from final
token and vault-share creation.

Calculated totals, component breakdowns, result population counts, and output
hashes are intentionally withheld pending independent reproduction and the
first public results article. See
[`docs/numerical-embargo.md`](docs/numerical-embargo.md).

## Distribution status

The public source package is ready for independent review. It is **not itself a
distribution file**. Non-issuance, treasury, contract recovery, lost-wallet,
frozen-wallet, and validator-governor decisions live in ignored routing files.

The ignored routing workspace under `routing/local/` contains reviewed sparse
exceptions, a generated unresolved work queue, and a conservation summary.
Ordinary code-less EOA same-address delivery is implicit; verified
validator-wrapper accounts are explicit code-bearing same-address exceptions.
These routing artifacts are not a complete distribution file. Build and verify
the final wallet/Merkle input separately, and only after the routing summary
says `status: ready`.

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
- `repro/as-run/` — ignored exact operator scripts retained locally
- `repro/source-snapshots/` — source snapshots for earlier accounting phases
- `user-faq.md` — end-user migration FAQ

The ignored `repro/as-run` scripts contain machine-specific paths and service
controls. Public reviewers should follow `docs/reproduce.md` instead.

## Trust boundary

Consensus state, canonical blocks, signed transactions, staking wrappers, and
cross-shard receipt records are primary sources. Explorer balances are not
used.

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
