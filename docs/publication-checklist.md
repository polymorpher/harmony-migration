# Publication checklist

Before publishing the migration allocation:

1. Confirm the cutoff manifest:
   - shard-0 block, hash, and root;
   - shard-1 block, hash, and root.
2. State the selected inclusive `>= 1,000 ONE` comparison and confirm that all
   exact-threshold rows are included using `total_claim_atto`.
   Confirm that active stake/delegation is excluded from direct wallet airdrop
   and represented in the validator-vault share ledger.
3. Confirm the code-bearing review:
   - the all-address metadata companion resolves every blank shard-0 code field
     at the cutoff, including below-threshold claims;
   - validator-wrapper accounts are independently proved key-controlled and
     recorded as explicit code-bearing same-address exceptions;
   - genuine contracts remain in class-specific recovery;
   - every contract destination has documented evidence and no category is
     silently sent to its old Harmony contract address.
4. Publish the burn-aware treasury-routing ledger. Confirm that extra-mint
   recipient amounts are capped by unreturned extra mint, partial rows are
   split correctly across wallet and vault delivery, and treasury routing does
   not change the total claim.
5. Apply every reviewed route input under `routing/local/`. Regenerate, but
   never manually edit, the sparse exception and unresolved outputs. Require
   the routing summary to report `status: ready`, with no inactive route,
   unresolved wallet amount, vault-share amount, or validator governor.
6. State explicitly whether identified rollback-exploit proceeds are honored
   as state claims or redirected. Do not imply that the existing treasury
   inventory covers that incident.
7. Reconcile WONE and bridge-lock backing against external-chain holder claims
   so no locked ONE is issued twice.
8. State that retired-shard receipts are excluded unless additional proof is
   obtained.
9. Run the strict claim verifier and exact cutoff verifier.
10. Record:
   - filename;
   - byte size;
   - row count;
   - direct wallet-airdrop atto-ONE;
   - staked-to-vault atto-ONE;
   - total claim atto-ONE;
   - SHA-256.
11. Materialize the complete deployment allocation from the base entitlements
    and approved exceptions, verify it independently, and publish it as an
    immutable release asset.
12. Publish the snapshot and source-code manifests.
13. Have a second developer reproduce the selected threshold output from the
    full verified cutoff ledger.

Do not publish:

- validator keys or passphrase files;
- private node configuration;
- machine logs;
- writable database snapshots;
- explorer-derived balances;
- locally calculated totals, component breakdowns, population counts, or
  expected output hashes before independent reviewers record their results and
  the first public results article is published.
