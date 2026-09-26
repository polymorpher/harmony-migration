# Random-sample verification

This tool picks a random, reproducible set of claim rows from a frozen evidence
bundle and checks each one in detail. It also checks the whole bundle: file
hashes, the cutoff, row counts, and totals.

There are two ways to run it. Both give the same answers:

- **Command line:** `toolkit/scripts/random-sample-verify.py`, for technical
  reviewers and CI. This is the authoritative tool.
- **Web page:** `toolkit/verifier/random-sample-verifier.html`, a single file
  that works offline in a browser, for reviewers who do not use a terminal.

The tool adds to the existing full-bundle verifiers
(`verify-wone-allocation.py`, `verify-migration-stage-policy.py`,
`materialize-initial-stage.py`, and the private `cutoff-final-verifier`). It
does not replace or change them.

## Quick start

### Command line

```sh
python3 toolkit/scripts/random-sample-verify.py \
  --bundle artifacts/released-migration-bundle \
  --sample-size 25 \
  --seed 20260925 \
  --expected-manifest-sha256 <published hash> \
  --output sample-report.json
```

The terminal shows the seed, every sampled row with its result, each problem
with a plain explanation, and three final verdicts. The JSON report holds the
same information in full.

### Web page

1. Open `toolkit/verifier/random-sample-verifier.html` from disk in a current
   browser. No server or internet connection is needed.
2. Choose or drop the bundle folder. If your browser cannot open folders,
   choose all the bundle files instead, including `bundle-manifest.json`.
3. Enter a sample size and a seed. To repeat someone else's check, use their
   seed and sample size. If you have the published manifest hash, paste it too.
4. Select **Verify sample**. Select any sampled row to see every check on it.
5. Select **Download JSON report** to save the result. It has the same format
   as the command-line report.

The page never sends data anywhere. Its security policy blocks all network
connections.

## Reading the result

### The three verdicts

| Verdict | PASS means |
| --- | --- |
| Sampled rows passed | Every sampled row passed every check. |
| Bundle metadata passed | The bundle is exactly the frozen bundle: hashes, cutoff, row counts, and totals all match. |
| Historical chain truth independently verified | Archived chain data from at least two sources, at least one of them outside the RPC and explorer infrastructure, matches the cutoff blocks and every sampled balance. |

The third verdict stays `NOT VERIFIED` unless the bundle contains that
archived chain data. A passing bundle is then consistent with itself and with
the published cutoff, but it has not been checked against the chain.

### Check statuses

| Status | Meaning |
| --- | --- |
| `PASS` | The check ran and the values agree exactly. |
| `FAIL` | The check ran and found a problem. The message says what was found and what was expected; the explanation says why it matters. |
| `NOT VERIFIED` | The file the check needs is not in the bundle. This is never a pass. |
| `WARNING` | An RPC or explorer source disagrees with the bundle or the cutoff. This is reported as an infrastructure problem and never counts as verification. |

### Exit status (command line)

| Code | Meaning |
| --- | --- |
| `0` | Every requested check passed. |
| `1` | A check failed: a wrong amount, a mismatch between files, a changed or missing file, or malformed data. |
| `2` | The bundle or the arguments could not be used, for example an empty bundle, an invalid seed, or a sample larger than the number of distinct rows. |
| `3` | Incomplete: a required check was `NOT VERIFIED`, or `--require-chain-truth` was requested and not met. |

## What a pass proves

- Every sampled row has exact arithmetic and follows the WONE, 1,000 ONE
  threshold, stage, routing, and allocation rules.
- Every sampled row agrees across every file in the bundle.
- The bundle has not changed since it was frozen, and it uses the published
  cutoff blocks.
- When an airdrop list is included, it matches its Merkle root and pays only
  ready wallet destinations, each its exact amount.
- When independent archived chain data is included, the cutoff blocks and the
  sampled balances match it.

## What a pass does not prove

- **Rows outside the sample** are covered only by the bundle-wide totals and
  format checks. A passing sample makes widespread errors unlikely, but it
  cannot rule out a single wrong row. Use the full-bundle verifiers for that.
- **Chain state** is not checked unless independent archived chain data is in
  the bundle.
- **Archived chain data** is compared as recorded. The tool does not decode
  block headers, check validator signatures, or verify state proofs.
- **Policy decisions** such as contract classifications, non-issuance lists,
  exchange inventories, and destinations are taken as given. The tool checks
  that they were applied consistently, not that they are right.
- **Activity dates** in the stage policy are checked against the six-month
  window and the cutoff, but not recomputed from transactions.
- **Free-text columns** (`destination_id`, `reason`, `evidence`) in the wallet
  and vault-share allocations are shown but not checked.
- **WONE holder rows for addresses that have no claim** are covered only by the
  holder total, because the sample is drawn from the claim ledger.

## Why the tool works offline

Harmony's historical RPC and explorer services are stale and inconsistent. For
the cutoff blocks they can return pruned, lagging, or wrong data. A tool that
queried them would report false passes and false failures. So the tool reads
only a frozen bundle of files:

- every file's SHA-256 is recorded in the bundle manifest;
- the cutoff blocks are compared with the public
  [`manifests/snapshot-2026-09-10.json`](../manifests/snapshot-2026-09-10.json);
- saved RPC or explorer responses may be included, but only as supporting
  evidence. A disagreeing response is a `WARNING`. A bundle that marks an RPC
  or explorer source as authoritative fails.

## Reproducing a sample

The same bundle, seed, sample size, and `--sample-by` setting always select the
same rows, on any computer and in either tool. The rule is:

```text
score(id) = SHA-256 of the text "harmony-migration-sample/v1|<seed>|<id>",
            read as a big-endian number
sample    = the <sample size> identifiers with the lowest (score, id)
```

- Identifiers are the lowercase secure keys (or addresses, with
  `--sample-by address`) from the WONE overlay claim ledger.
- Row order does not matter, and every set of rows of the requested size is
  equally likely.
- The sample is never made smaller. If you ask for more rows than exist, the
  tool stops with exit status `2`.
- The seed is used exactly as typed. It cannot be empty or start or end with a
  space. If you leave it out, the tool makes one up and prints it; record it to
  repeat the check.
- The JSON report's `sample.reproduce` field contains the exact command.

The same rule in standard-library Python:

```python
import hashlib

def sample(seed, identifiers, size):
    def score(identifier):
        text = f"harmony-migration-sample/v1|{seed}|{identifier.lower()}"
        return int.from_bytes(hashlib.sha256(text.encode()).digest(), "big")
    return sorted({i.lower() for i in identifiers}, key=lambda i: (score(i), i))[:size]
```

Pick a seed nobody could choose after seeing the data, for example the hash of
an Ethereum block whose height was announced in advance.

## Building a bundle

After running the pipeline in [`reproduce.md`](reproduce.md), copy its outputs
into a new bundle. The output folder must not exist yet, and the builder
refuses malformed rows.

```sh
python3 toolkit/scripts/build-evidence-bundle.py \
  --output artifacts/released-migration-bundle \
  --bundle-id released-migration-bundle-2026-09-25 \
  --created-utc 2026-09-25T00:00:00Z \
  --sources artifacts/released-migration-bundle-sources.json \
  --default-source pipeline \
  --native-claims artifacts/cutoff-20260910/claims/all-address-native-claims-cutoff.csv \
  --wone-overlay artifacts/cutoff-20260910/claims/all-address-migration-claims-cutoff.csv \
  --wone-overlay-summary artifacts/cutoff-20260910/claims/all-address-migration-claims-cutoff-summary.json \
  --eligibility automatic=artifacts/cutoff-20260910/claims/migration-claims-automatic.csv \
  --eligibility contract_review=artifacts/cutoff-20260910/claims/migration-claims-genuine-contract-review.csv \
  --eligibility excluded_address=artifacts/cutoff-20260910/claims/migration-claims-excluded.csv \
  --eligibility-summary artifacts/cutoff-20260910/claims/migration-claims-policy-summary.json \
  --stage-policy artifacts/migration-policy-20260917/migration-stage-policy.csv \
  --stage-summary artifacts/migration-policy-20260917/migration-stage-summary.json \
  --routing-exceptions routing/local/generated/routing-exceptions.csv \
  --routing-summary routing/local/generated/routing-summary.json \
  --wallet-allocations routing/local/generated/initial-stage/wallet-allocations.csv \
  --vault-shares routing/local/generated/initial-stage/vault-shares.csv \
  --vault-delegations artifacts/cutoff-20260910/state/staked-to-vault-by-delegation.csv \
  --wone-holders artifacts/wone-holder-accounting-20260917/wone-holders-cutoff-excluding-layerzero.csv \
  --airdrop-run airdrop/runs/mainnet-initial --airdrop-scope complete \
  --historical-evidence artifacts/evidence/cutoff-headers-archive-db.json
```

- Change the paths to match your run.
- `--sources` is a JSON list of source descriptions (see the manifest format
  below). `--source-for ROLE=SOURCE_ID` sets the source of a single file type.
- Include `--wone-overlay-summary`. It lists the addresses whose WONE is
  intentionally excluded. Without it, the WONE holder comparison is
  `NOT VERIFIED`.
- The command prints the SHA-256 of `bundle-manifest.json`. Publish that hash
  so reviewers can check they have the right bundle.
- A bundle built from embargoed outputs is embargoed too; see
  [`numerical-embargo.md`](numerical-embargo.md).

## Reference

### Command-line options

| Option | Meaning |
| --- | --- |
| `--bundle DIR` | The bundle folder. |
| `--sample-size N` | How many rows to check. |
| `--seed S` | The sampling seed. Generated and printed when omitted. |
| `--sample-by secure_key\|address` | What to sample by. The default is `secure_key`. |
| `--output PATH` | Where to write the JSON report. Add `--replace` to overwrite a file. |
| `--list-files` | List the bundle files with their type, row count, and hash status, then stop. |
| `--show-row ID` | Check one row, given its secure key or address, and show every record joined to it. |
| `--verify-all-metadata` | Also check that every row's address hashes to its secure key. Slower. Allows a run with no sample. |
| `--expected-manifest-sha256 HASH` | Check the bundle manifest against the published hash. |
| `--require-chain-truth` | Fail unless archived chain data confirms the cutoff and the sample. |

`node toolkit/verifier/node-run.js` runs the web page's engine from the command
line with the same options. It exists for testing; use the Python command for
real verification.

### Bundle layout

```text
released-migration-bundle/
  bundle-manifest.json
  claims/native-claims.csv              native_claims
  claims/wone-overlay-claims.csv        wone_overlay (rows are sampled from this file)
  eligibility/automatic.csv             eligibility, category automatic
  eligibility/contract_review.csv       eligibility, category contract_review
  eligibility/excluded_address.csv      eligibility, category excluded_address
  policy/migration-stage-policy.csv     stage_policy
  routing/routing-exceptions.csv        routing_exceptions
  allocation/wallet-allocations.csv     wallet_allocations (initial stage)
  allocation/vault-shares.csv           vault_shares (initial stage)
  ledgers/vault-delegations.csv         vault_delegations (optional)
  ledgers/wone-holders.csv              wone_holders (optional)
  ledgers/exchange-wallets.csv          exchange_wallets (optional)
  summaries/wone-overlay-summary.json   wone_overlay_summary (optional)
  summaries/*.json                      eligibility, stage, and routing summaries (optional)
  airdrop/manifest.json                 airdrop_manifest (optional)
  airdrop/distribution.csv              airdrop_distribution (optional)
  airdrop/batches/batch-NNNNN.json      airdrop_batch (optional)
  evidence/NNN-*.json                   historical_evidence (optional)
```

The CSV files are pipeline outputs, copied unchanged:

| File type | Pipeline output | Joined to a claim by |
| --- | --- | --- |
| `native_claims` | `all-address-native-claims-cutoff.csv` | `secure_key` |
| `wone_overlay` | `all-address-migration-claims-cutoff.csv` from `apply-wone-qualification.py` | `secure_key` |
| `eligibility` | the category outputs of `apply-eligibility-policy.py` | `secure_key` |
| `stage_policy` | `migration-stage-policy.csv` | `secure_key` |
| `routing_exceptions` | `routing/local/generated/routing-exceptions.csv` | `source_secure_key` |
| `wallet_allocations` | `generated/initial-stage/wallet-allocations.csv` | `source_secure_key` |
| `vault_shares` | `generated/initial-stage/vault-shares.csv` | `source_secure_key` |
| `vault_delegations` | `staked-to-vault-by-delegation.csv` | `delegator_secure_key` |
| `wone_holders` | `wone-holders-cutoff.csv` | `address` |
| `exchange_wallets` | the normalized exchange inventory | `address_hex` |
| `airdrop_distribution` | the airdrop run's `distribution.csv` | `address` |

- The first seven file types are required. A missing required file fails the
  bundle.
- A missing optional file makes the checks that need it `NOT VERIFIED`, without
  failing the run.
- `wone_overlay_summary` is the summary written by `apply-wone-qualification.py`.
  Its `excluded_addresses_requested` list must include the WONE contract and
  both LayerZero NativeOFT contracts, and its `output_sha256` must match the
  WONE overlay file.

### File format rules

- CSV files are read strictly. An unclosed quote, text after a closing quote, a
  NUL byte, or invalid UTF-8 fails the file, and none of its rows are used.
- Numbers in the manifest and evidence files (sizes, row counts, chain IDs,
  block numbers, timestamps, shards) must be whole JSON numbers from 0 to
  2^53-1. Values such as `1.0` or `true` fail.
- File paths must be relative and plain: no leading `/`, and no `./`, `..`, or
  empty parts such as `a//b`.
- Every file in the bundle folder must be listed in the manifest. Hidden files
  such as `.DS_Store` are ignored.

### Manifest format (`bundle-manifest.json`)

```json
{
  "format": "harmony-migration-evidence-bundle/v1",
  "bundle_id": "released-migration-bundle-2026-09-25",
  "created_utc": "2026-09-25T00:00:00Z",
  "network": "harmony-mainnet",
  "chain_ids": {"shard0": 1666600000, "shard1": 1666600001},
  "cutoff": { "...": "copied from manifests/snapshot-2026-09-10.json" },
  "threshold_atto": "1000000000000000000000",
  "initial_window_since_utc": "2026-03-10T14:00:00Z",
  "airdrop_scope": "complete",
  "sources": [
    {"id": "pipeline", "kind": "pipeline", "description": "...",
     "authoritative": true, "retrieved_utc": "2026-09-20T00:00:00Z"}
  ],
  "files": [
    {"role": "native_claims", "path": "claims/native-claims.csv",
     "sha256": "<64 hex>", "bytes": 0, "rows": 0, "source": "pipeline"},
    {"role": "eligibility", "category": "automatic", "...": "..."}
  ],
  "declared_totals": {"wone_overlay.total_claim_atto": "<integer string>"}
}
```

- `sources[].kind` is one of `archival_database`, `state_proof`,
  `archived_block_header`, `archival_rpc`, `explorer`, or `pipeline`. Only the
  first three count as independent chain evidence. `archival_rpc` and
  `explorer` sources must set `authoritative` to `false`.
- Each `files[]` entry gives the file type (`role`), path, SHA-256, size in
  bytes, source, and, for CSV files, the number of data rows. Eligibility files
  also give their category.
- `declared_totals` lists every total named in `toolkit/verifier/rules.json`:
  row counts, component totals, wallet, vault, and total-claim amounts, WONE
  amounts, issued, not-issued, redistributed, manual, held, and deferred
  amounts, stage totals, and the airdrop total. The tool recomputes each one.
- `airdrop_scope` is `complete` when the airdrop list must pay every ready
  wallet destination, or `partial` for one batch. In a partial run, an unpaid
  destination is `NOT VERIFIED` rather than `FAIL`.

### Archived chain evidence format

```json
{
  "format": "harmony-historical-evidence/v1",
  "source_id": "archive-db",
  "retrieved_utc": "2026-09-11T00:00:00Z",
  "block_headers": [
    {"shard": 0, "number": 93623067, "hash": "0x...", "parent_hash": "0x...",
     "state_root": "0x...", "transactions_root": "0x...", "timestamp": 1789048800}
  ],
  "accounts": [
    {"shard": 0, "block": 93623067, "address": "0x...", "balance_atto": "...",
     "active_staked_or_delegated_atto": "...", "pending_undelegation_atto": "...",
     "unclaimed_staking_reward_atto": "...", "pending_cross_shard_atto": "...",
     "wone_balance_atto": "..."}
  ],
  "raw": "optional saved response, kept for the record"
}
```

`source_id` must match a source in the manifest. That source's `kind` decides
whether the file counts as independent evidence.

### What each sampled row is checked for

All amounts are whole numbers of atto-ONE (10^-18 ONE). Decimal ONE and USD
columns are recomputed from those whole numbers and compared as text; they are
never used in calculations.

**Arithmetic**

- `liquid_total = liquid_shard0 + liquid_shard1`
- `liquid_shard0 + liquid_shard1 + pending_undelegation + unclaimed_staking_reward + pending_cross_shard`
  equals `wallet_airdrop_atto` in the native claims and
  `native_wallet_airdrop_atto` in the WONE overlay
- `staked_to_vault = active_staked_or_delegated`
- `total_claim = wallet_airdrop + staked_to_vault`
- `native_total_claim = native_wallet_airdrop + staked_to_vault`
- `qualification_total = native_total_claim + wone_balance`
- `wallet_airdrop = native_wallet_airdrop + wone_airdrop`
- no amount is negative, and every decimal display value matches its whole
  number

**WONE and threshold**

- The threshold is exactly 1,000 ONE (`1000000000000000000000` atto-ONE),
  inclusive, applied to `qualification_total_atto`. The same value must appear
  in the rules, the published snapshot, the bundle manifest, and the
  eligibility summary.
- `wone_airdrop` equals the full WONE balance when the row meets the threshold
  or is a confirmed exchange wallet, and is zero otherwise.
- Excluded WONE holders have no WONE balance or airdrop in their claim. Other
  holders' WONE balance matches the holder list.

**Agreement between files**

- The same secure key, address, and amounts in the native claims, WONE overlay,
  eligibility output, and stage policy.
- The row is in the eligibility output and stage policy exactly when it meets
  the threshold, with a category that matches its stage classification.
- Stage deductions add up, come out of the wallet part before the vault part,
  and follow the stage rules: initial wallets were active in the six months
  before the cutoff and still meet the threshold after incident deductions;
  reviewed contracts are next stage or not issued; activity is not after the
  cutoff.
- Routing rows have a treatment, destination status, and stage that fit
  together; not-issued and redistributed routes equal the stage deductions;
  exchange wallets' manual routes cover their whole claim; validator wallets
  have explicit routes.
- Wallet and vault-share allocations equal the stage's wallet and vault parts
  for initial issued rows and are empty otherwise. Only ordinary wallets are
  paid at their own address without an explicit route, and every ready
  allocation names a destination.
- The airdrop pays each destination exactly its ready wallet allocation.

**Whole bundle**

- Manifest format, file hashes, sizes, and row counts; required files present;
  no unlisted files.
- Cutoff blocks, hashes, state roots, timestamps, and chain IDs match the
  published snapshot.
- Every row of every file is well formed, secure keys are in order, and there
  are no duplicates.
- Every native claim appears in the WONE overlay.
- Every declared total matches a recount, and the totals add up to each other.
- The pipeline summaries point at the bundled files by hash.
- The airdrop Merkle root, list hash, recipient count, total, and batch files
  recompute.
- All archived chain evidence is well formed and is compared source by source.

### How the command line and the web page stay identical

- Every formula, field list, and total is written once, as data, in
  `toolkit/verifier/rules.json`. The Python engine (`sample_core.py`) and the
  JavaScript engine (`sample_core.js`) both read that file, so neither contains
  its own copy of the claim arithmetic.
- Tests check that the field lists in `rules.json` match the pipeline's own
  scripts, and that the constants match the pipeline and the published
  snapshot.
- The Python engine reuses the pipeline's own number formatting, address check,
  and airdrop verification code.
- A test runs both engines on the same correct and deliberately broken bundles
  and requires every result to match.
- The web page is generated from its sources. `make test-python` fails if it is
  out of date; rebuild it with `make sample-verifier-html`.

### Performance

- The command line reads each file once, in order, and keeps only the sampled
  rows and a few lookup tables in memory.
- `--verify-all-metadata` hashes every address, which is much slower.
- The web page reads large files in 4 MiB pieces and shows its progress.
