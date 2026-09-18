# Local artifacts

This directory is Git-ignored except for this file.

It contains:

- `claim-accounting-<date>/` — generated claim-delivery reports;
- `contract-review-<date>/` — contract classifications and destination files;
- cutoff state and staking exports;
- complete total-claim, direct wallet-airdrop, staked-to-vault, and
  difference CSVs;
- archival WONE holder, combined-threshold, redistribution, and retained
  reserve evidence;
- exchange normalization provenance, per-exchange memos, address audits,
  Gate airdropped/not-airdropped lists, and generated policy/route inputs;
- address-preimage recovery evidence;
- contract-classification facts, reports, and category CSVs;
- historical treasury-routing and current non-issuance calculations;
- copied remote run outputs and source-workspace evidence.

Do not publish these files during the numerical embargo. Do not place private
keys, credentials, production configuration, writable Harmony databases, or
other secrets here.

Compact private findings and result identities are organized separately under
`docs/findings/`, `results/2026-09-11/`, and the ignored result manifests.
