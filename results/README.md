# Results

Large CSVs are not stored in Git. They should be attached to a versioned
release or placed in immutable object storage together with:

- byte size;
- row count;
- SHA-256;
- snapshot manifest;
- threshold comparison;
- verification output.

## Local ignored artifacts

On the accounting workstation, all CSVs and generated outputs are available
under the Git-ignored `artifacts/` directory:

- `artifacts/cutoff-20260910/` — final cutoff state, claims, differences, and
  verification;
- `artifacts/migration-claims-20260909/` — earlier fully resolved claim set;
- `artifacts/remote-evacuation-20260910/` — non-database run outputs copied
  from the original machines;
- `artifacts/original-harmony-local/` — complete mirror of the original
  Harmony `local/` output tree;
- `artifacts/source-workspace-top-level/` — flat top-level investigation
  outputs and reports.

The mirrors use filesystem hard links where possible, so they do not duplicate
the large files' physical storage. They passed recursive checksum comparisons
against their sources.

## Pre-publication hold

The complete generated result set is intentionally excluded from Git until:

1. Harmony reviewers independently run the public reproduction;
2. their result is recorded without access to the local expected result; and
3. the first public results article is published.

The withheld set includes:

- `all-address-migration-claims-cutoff.csv`;
- direct wallet-airdrop and per-validator vault-share ledgers;
- inclusive and strict threshold CSVs;
- preliminary code-bearing review output;
- final automatic, genuine-contract recovery, and policy-routed outputs;
- contract-account classifications and destination evidence;
- explicit non-issuance/manual routes, compiled sparse wallet/vault
  exceptions, validator-governor exceptions, and the unresolved work queue;
- historical treasury-calculation and current non-issuance outputs;
- result summary JSON;
- row counts, component totals, aggregate totals, and output hashes.

Reviewers should record their independently generated filenames, byte sizes,
row counts, totals, and SHA-256 values before asking for the held-back
comparison data.

Pre-routing category and `base-*` files are entitlement inputs. The generated
routing files under `routing/local/generated/` contain exceptions only and are
usable in a later deployment build only when their summary reports
`status: ready`. A complete wallet distribution or Merkle input must be
materialized and verified separately.

The public cutoff blocks, hashes, and state roots remain in
`manifests/snapshot-2026-09-10.json`. The selected policy remains documented in
`docs/eligibility-policy.md`.

## Verification

`make verify` checks the public source package without requiring or revealing
the ignored results. Local maintainers with the private dated result set can
run:

```sh
make verify-private
```

See `docs/numerical-embargo.md` for the release conditions and
`embargoed/REDACTION-NOTES.md` for the local restoration map.
