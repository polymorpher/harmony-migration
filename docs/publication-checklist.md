# Publication checklist

Before publishing the migration allocation:

1. Confirm the cutoff manifest:
   - shard-0 block, hash, and root;
   - shard-1 block, hash, and root.
2. State the selected inclusive `>= 1,000 ONE` comparison and confirm that all
   exact-threshold rows are included using `qualification_total_atto`
   (`native_total_claim_atto + wone_balance_atto`).
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
4. Publish the burn-aware non-issuance ledger. Confirm that extra-mint
   recipient amounts are capped by unreturned extra mint, partial rows preserve
   the existing ordinary-destination remainder, and no token or vault share is
   created for a `not_issuing` row. A not-issued staked row must also reduce
   the corresponding validator-vault deposit by the same amount.
   Confirm that wallet-theft additions distinguish explicitly reported
   perpetrators from transaction-linked recipients and that reported victim
   wallets are not routed to non-issuance.
5. Apply every reviewed route input under `routing/local/`. Regenerate, but
   never manually edit, the sparse exception and unresolved outputs. Require
   the routing summary to report `status: ready`, with no inactive route,
   unresolved wallet amount, vault-share amount, or validator governor.
   Confirm that expanded claims equal issuable plus not-issued plus
   redistributed source amounts exactly.
   Confirm that `contract-recovery-custody` maps to a specific Safe or multisig
   holding address, separate from general treasury, and that verified claimants
   are paid only from its existing balance.
   Confirm that every qualifying non-Gate exchange wallet is absent from the
   implicit automatic category, every positive current non-Gate exchange claim
   has exactly one manual route, and blank exchange destinations remain holds.
   Confirm separately that Gate has no exchange reroute, that only its ordinary
   qualifying automatic rows receive same-address delivery, and that the
   airdropped/not-airdropped address lists and residual-value total close to
   its complete normalized inventory.
6. State explicitly whether identified rollback-exploit proceeds are honored
   as state claims or redirected. Do not imply that the existing treasury
   inventory covers that incident.
7. Reconcile the WONE holder ledger to `totalSupply()` and the native reserve.
   Confirm that the qualified-holder amount is added once to wallet rows and
   offset once as `redistributed`, while the remainder is `not_issuing` and
   retained in the Year 2025 Supply Reserve.
8. Separately reconcile each LayerZero NativeOFT reserve against remote supply
   and messages in flight at pinned blocks before approving its custody route.
9. State that retired-shard receipts are excluded unless additional proof is
   obtained.
10. Run the strict claim verifier and exact cutoff verifier.
11. Record:
   - filename;
   - byte size;
   - row count;
   - direct wallet-airdrop atto-ONE;
   - WONE airdrop and redistributed-source atto-ONE;
   - staked-to-vault atto-ONE;
   - total claim atto-ONE;
   - SHA-256.
12. Materialize the complete deployment allocation from the base entitlements
    and approved exceptions, verify it independently, and publish it as an
    immutable release asset.
13. Publish the snapshot and source-code manifests.
14. Have a second developer reproduce the selected threshold output from the
    full verified cutoff ledger.

Do not publish:

- validator keys or passphrase files;
- private node configuration;
- machine logs;
- writable database snapshots;
- explorer-derived balances;
- raw or normalized exchange ownership lists, signatures, operational
  destinations, address-level memos, or their hashes before authorized
  disclosure;
- locally calculated totals, component breakdowns, population counts, or
  expected output hashes before independent reviewers record their results and
  the first public results article is published.
