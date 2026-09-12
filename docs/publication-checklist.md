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
   - validator-wrapper accounts are independently proved key-controlled and
     moved to the automatic output;
   - genuine contracts remain in class-specific recovery;
   - every contract destination has documented evidence and no category is
     silently sent to its old Harmony contract address.
4. Publish the burn-aware treasury-routing ledger. Confirm that extra-mint
   recipient amounts are capped by unreturned extra mint, partial rows are
   split correctly across wallet and vault delivery, and treasury routing does
   not change the total claim.
5. State that retired-shard receipts are excluded unless additional proof is
   obtained.
6. Run the strict claim verifier and exact cutoff verifier.
7. Record:
   - filename;
   - byte size;
   - row count;
   - direct wallet-airdrop atto-ONE;
   - staked-to-vault atto-ONE;
   - total claim atto-ONE;
   - SHA-256.
8. Publish the CSV as an immutable release asset.
9. Publish the snapshot and source-code manifests.
10. Have a second developer reproduce the selected threshold output from the
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
