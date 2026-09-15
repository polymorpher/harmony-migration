# Numerical-results embargo

The source code, cutoff identifiers, claim schema, database requirements,
contract-classification method, routing rules, and reproduction procedure are
public for independent review.

The locally calculated numerical outputs are temporarily withheld. This
includes:

- total-claim, direct wallet-airdrop, staked-to-vault, and vault-share totals;
- result population and routing-category counts;
- per-address balances and claim mappings;
- contract-classification counts and monetary breakdowns;
- cutoff-relative account-activity counts and claim-value breakdowns;
- non-issuance, treasury-routing, and incident-recovery amounts;
- generated CSV hashes and expected verification outputs;
- detailed finding reports and machine-readable result files.

Harmony reviewers should run the code against independently obtained archive
databases and record their deterministic outputs before receiving the withheld
numbers. This avoids anchoring their work to the original calculation.

The numerical files may be published only after both conditions are met:

1. independent reviewers have recorded their results and completed source-code
   review; and
2. the accompanying public results article has been published.

Their absence is intentional and does not mean that the state scans, address
recovery, contract classification, or routing analysis were not completed.

## Local private layout

- `docs/findings/claim-accounting/` — unredacted cutoff and address-recovery
  findings;
- `docs/findings/destination-mapping/` — unredacted contract and routing
  findings;
- `results/2026-09-11/claim-accounting/` and
  `results/2026-09-11/destination-mapping/` — indexed compact
  machine-readable result package;
- `artifacts/` — complete generated CSVs, caches, reports, and verification
  outputs;
- `repro/as-run/` — exact machine-specific operator scripts retained as local
  evidence;
- `routing/local/` — real treasury, frozen-wallet, lost-wallet, multisig, and
  validator-governor destinations;
- `embargoed/` — restoration and provenance notes;
- `manifests/results.sha256` and the dated release manifest — identities of
  ignored private results;
- `toolkit/cmd/cutoff-final-verifier/` — the exact bundle verifier, held
  privately because it embeds expected result identities.

Local maintainers with those ignored files can run `make verify-private`.
Public clones use `make verify` and `make check-public`; neither command
requires or reveals the embargoed outputs.

## Restoration

After both release conditions are met:

1. review `embargoed/REDACTION-NOTES.md`;
2. publish the dated result and finding directories;
3. restore exact result sections in public summaries where useful;
4. publish the result and release manifests;
5. narrow or remove only the corresponding embargo rules;
6. regenerate the public source manifest; and
7. run both public and private verification targets before release.
