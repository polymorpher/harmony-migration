# Process history

This document records how the accounting method evolved. It is not the
recommended command sequence; use `docs/reproduce.md` for that.

## 1. Initial liquid census

The first pass captured shard-0 and shard-1 state roots and exported every
positive secure account leaf. Per-shard CSVs were merged by secure key so one
address present on both shards appeared once.

An external USD price was initially used to inspect balances above $1. That
filter was exploratory and is not the final migration policy.

## 2. Complete claim components

Liquid balances alone were insufficient. The calculation was extended to
include:

- active staking and delegation;
- pending undelegation;
- unclaimed staking reward;
- supported pending cross-shard receipts.

The supply scanner and the per-delegator staking scanner were implemented
separately so their totals could be compared.

## 3. Supply-divergence investigation

Historical state roots, block reward accumulators, cross-shard receipts, and
staking boundaries were examined to explain why state-recognized claims differ
from Harmony's public supply formula.

That investigation identified separate mechanisms, including historical
unauthorized creation and formula/accounting differences. These findings do not
change the cutoff state roots.

Detailed migration reports are retained under the ignored `docs/findings/`
tree. Supply-specific findings are maintained in `harmony-supply-audit`.

## 4. Address preimage recovery

The initial compact databases lacked many auxiliary secure-key preimages.
Direct archive DB access recovered most mappings.

The remaining mappings were recovered from canonical evidence:

- block bodies and signed transactions;
- staking records;
- cross-shard receipts;
- targeted account-state transitions;
- execution traces;
- a final nonce-transition lookup.

Every accepted address was verified against its secure key. Explorer balances
were never used.

## 5. Final cutoff

The final policy time was mapped to:

- shard 0 block `93,623,067`;
- shard 1 block `95,882,100`.

The complete shard-0 trie was re-enumerated at the final root. The shard-1 final
root was identical to its earlier recovery root, and all intervening blocks
were independently audited.

Staking wrappers were discovered from the selected state root, not from
current-head metadata.

## 6. Difference outputs

The original and final claim ledgers were merged by secure key. Changed rows
were classified as created, deleted, amount-changed, or metadata-changed.

The final union classified created, removed, amount-changed, and metadata-only
rows. The observed category counts are withheld during independent
reproduction.

## 7. Independent verification

Verification included:

- a second full state traversal;
- direct trie differences;
- historical RPC checks for every changed liquid account;
- full canonical interval scans;
- execution traces for every newly created account;
- strict address and arithmetic verification;
- aggregate reconciliation across every component.

## 8. Public threshold

The final policy changed from the exploratory USD filter to a
ONE-denominated `1,000 ONE` threshold.

The selected policy is inclusive, so rows exactly at `1,000 ONE` are included.
The original split separated code-bearing rows for manual review and selected
inaccessible/dead rows from automatic distribution. Observed category counts
are withheld during independent reproduction.

The September 11 treasury review added a burn-aware destination overlay.
Extra-mint recipients are capped by their exact unreturned extra mint, while
burn/inaccessible and report-identified perpetrator balances use separate
rules. Some rows require a partial destination split. The total claim stayed
unchanged.

The September 15 policy update kept those exact audited amounts and partial
remainders, but changed their outcome from treasury delivery to terminal
`not_issuing`. The gross audit ledger remains unchanged; final issued supply is
reduced by the not-issued amount.

The September 16 wallet-theft review added four existing cutoff accounts to
the perpetrator-related inventory: two explicitly named alleged perpetrators
and two direct theft recipients. Twenty reported victim wallets were recorded
separately and were not routed to non-issuance. No balance was added to the
global claim ledger.

## 9. Contract-account correction

The original policy treated every non-empty code hash as a contract-review
account. Contract analysis established that Harmony validator accounts also
carry code because their RLP validator wrapper is stored in that field.

The maintained policy now uses code presence only to create a preliminary
review set. Independently verified validator-wrapper accounts return to
key-controlled automatic routing; genuine contracts remain in class-specific
recovery.

Observed contract populations and balances remain under the numerical embargo.

## 10. Wallet versus vault correction

The `total_claim` calculation was correct, but it was initially presented as if
the entire amount were the direct wallet allocation. That conflated total
claim with one of its delivery paths.

The maintained output now separates:

- `total_claim`, which includes active stake/delegation and is used for the
  threshold;
- `wallet_airdrop`, which excludes it; and
- `staked_to_vault`, which is assigned per validator and represented by
  ERC-4626 shares.

The delivery split does not change the total claim.

## 11. WONE holder qualification

The September 17 update enumerated every positive WONE holder at the cutoff
from the shard-0 archival node and reconciled the result exactly to both WONE
`totalSupply()` and the WONE contract's native reserve.

The current inclusive threshold now uses native total claim plus WONE balance.
Qualified rows receive their WONE amount in the direct wallet component. The
matching WONE source reserve is classified as `redistributed`, while the
below-threshold/excluded remainder is `not_issuing` and retained in the Year
2025 Supply Reserve.

The WONE contract's self-held WONE is excluded from recipient delivery.
LayerZero's NativeOFT contracts remain a separate native-ONE reconciliation;
they do not hold WONE in this ledger.

## 12. Exchange delivery policy

The September 17 exchange update normalized the private inventories received
from Binance.US, Gate, MEXC, and OKX, while retaining Binance and KuCoin as
explicitly incomplete inputs. Address conversion, duplicate/overlap checks,
exact submitted-balance reconciliation, and supplied signature verification
were performed before any delivery policy was generated.

Gate did not request aggregate rerouting and remains under the ordinary
inclusive threshold. Qualifying wallets reported by other exchanges were
removed from implicit same-address delivery; their positive current migration
claims were converted to manual aggregate routes. Missing destinations remain
holds. Per-exchange memos and a complete Gate airdropped/not-airdropped audit
were generated from existing cutoff and activity artifacts rather than a new
chain scan.

## 13. Preservation

All irreplaceable generated outputs, source snapshots, manifests, and runtime
evidence were copied off the original machines and checksum-verified before
remote access was released.

Raw chain databases were not copied. They are infrastructure inputs, and public
reproduction requires independently obtained archival/full databases.
