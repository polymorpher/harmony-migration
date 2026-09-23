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
`not_issuing`. The gross audit ledger and fixed premint remain unchanged; the
migration allocation is reduced by the not-issued amount.

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
key-controlled wallet classification; genuine contracts follow a separate
reviewed stage/non-issuance policy.

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
September 18 exchange correction also includes WONE held by normalized
exchange wallets regardless of that ordinary threshold; the September 22
exchange policy extends that to every confirmed exchange inventory. The
matching WONE source reserve is classified as `redistributed`, while the
remaining backing is `not_issuing` and retained in the 2050 premint reserve.

The WONE contract's self-held WONE is excluded from recipient delivery.
LayerZero's NativeOFT contracts remain a separate native-ONE reconciliation;
they do not hold WONE in this ledger.

## 12. Exchange delivery policy

The September 17 exchange update normalized the private inventories received
from Binance.US, Gate, MEXC, and OKX, while retaining Binance and KuCoin as
explicitly incomplete inputs. The September 18 update added KuCoin's two-shard
inventory, separate signature proof, and aggregate destination, as well as the
Binance.US aggregate destination. The September 22 update added DigitalX's
69-wallet inventory and the ERC-20 destination declared inside its workbook.
Binance remains the only missing wallet inventory and the only exchange with
an unresolved aggregate destination.

Address conversion, duplicate/overlap checks, exact submitted-balance
reconciliation, and supplied signature verification are performed before any
delivery policy is generated. KuCoin's importer additionally merges duplicate
addresses across its shard sheets, reproduces the workbook's exact per-shard
and combined totals, checks its pinned cutoff identity, and requires its
signature worksheet and DOCX proof to agree before EIP-191 recovery.
DigitalX's importer permits spreadsheet formulas only in explorer-link and
summary-total cells, reproduces each display-rounded total from row data,
binds the declared destination to the configured destination file, and keeps
the exchange's separately reported rollback-invalidated deposits (three
transactions) outside wallet accounting as a claim awaiting an explicit policy
decision. Because two DigitalX custodial wallets met the ordinary threshold,
the eligibility split, migration-stage policy, base vault-share allocation,
compiled routing, exchange routing verification, and initial-stage
materialization were regenerated so those wallets left the automatic
same-address population and joined the exchange stage. The same update moved
the historical-retention non-issuance input to this repository's own
`artifacts/supply-reconciliation-20260911/` copy after the supply-audit
forensic workspace quarantined the original export path.

The September 22 exchange policy added Binance, Bybit, and HTX inventories and
replaced the aggregate-exchange stage with manual delivery from the year 2050
supply reserve for every exchange. Every wallet in a confirmed exchange
inventory is excluded from the airdrop; its complete cutoff entitlement
(native ONE, WONE, and delegated principal) is delivered by hand to the
destination(s) the exchange confirmed, and exchange delegated principal is
released from the validator vaults instead of being issued as shares.
Destination policies are recorded per exchange: one aggregate address, a
wallet/staking address pair, the source wallets themselves, or Gate's tiering
in which wallets meeting the initial-distribution criteria are delivered at
their own addresses and all others are aggregated to Gate's confirmed
destination. Gate's two addresses supplied outside its workbook were merged
into its inventory, and its reported total was reconciled exactly from the
workbook's shard-0 balances plus those addresses. Because the exchange set now
includes Gate wallets holding only WONE, the WONE-only metadata was refreshed
from the archival node at the pinned cutoff block before the overlay,
threshold, eligibility, stage, vault, routing, and materialization artifacts
were regenerated. The threshold subset was unchanged; the initial airdrop
shrank by the exchange wallets it previously contained. Missing destinations
remain holds. Per-exchange memos, Gate delivery-tier lists, and the manual
delivery worksheet were generated from existing cutoff and activity artifacts
rather than a new chain scan.

## 13. Migration-stage and reviewed-contract policy

The September 17 policy fixed threshold membership before deductions and
limited the initial stage to positive eligible wallets with six-month indexed
activity. The September 18 exchange correction separated confirmed exchange
delivery into its own stage, and the September 22 policy made that stage
`exchange_manual` with `manual_from_reserve` treatment for every exchange:
individual source threshold, activity, and ordinary stage do not gate that
delivery, nothing in it is airdropped, and a Gate wallet's ordinary stage only
selects its delivery tier.

Reviewed multisig, LayerZero collateral, and 1wallet allocations moved to the
next stage regardless of activity. The 1wallet group uses recovery-multisig
handling without an unverified address. SmartVault and all other reviewed
genuine contracts became terminal non-issuance across wallet and vault-share
components.

The former blanket contract-recovery-custody route was retired. A generated
address-level policy now records stage separately from `not_issued` and
`redistributed` treatment. Stage-scoped readiness prevents unresolved
next-stage destinations from being misreported as initial-stage blockers, and
an independent materializer expands only initial issued wallet/vault rows.

## 14. Preservation

All irreplaceable generated outputs, source snapshots, manifests, and runtime
evidence were copied off the original machines and checksum-verified before
remote access was released.

Raw chain databases were not copied. They are infrastructure inputs, and public
reproduction requires independently obtained archival/full databases.
