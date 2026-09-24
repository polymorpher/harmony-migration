# Paying directly from the Safe, without the airdrop contract

This is the second way to pay a list. Instead of deploying `CommittedBatchAirdrop` and letting an
operator wallet send pre-committed batches, the supply-reserve Safe sends the tokens itself. Each
Safe transaction carries a batch of ordinary token transfers, and the Safe's signers check and
sign every batch.

Use it when the signers want to approve each payment batch themselves, for example for a first
distribution to a few thousand wallets. There is nothing to deploy and no operator wallet, but
every batch is one more Safe transaction to check and sign. For very long lists the contract is
less work for the signers; see `docs/DESIGN.md`.

## How it works

- A batch is a list of `transfer(recipient, amount)` calls on the token, sent by the Safe.
- The Safe web app bundles such a list into one Safe transaction using Safe's official
  **MultiSendCallOnly** contract (it can only make plain calls, never change the Safe). All
  transfers in the transaction happen, or none do.
- `tools/safe_batch.py build` prepares exactly the transaction the Safe web app will create from
  the list, and computes the three numbers a signer checks:
  - the **domain hash**, which identifies this Safe on this chain (it is the same for every
    transaction of the Safe);
  - the **message hash**, a fingerprint of this transaction's content: target, call data,
    operation and nonce;
  - the **Safe transaction hash**, which combines the two. This is what the owners' signatures
    cover. The Safe web app shows it; a hardware wallet shows the domain hash and message hash
    while signing.
- If any address, amount, the order of the transfers, or the nonce differs from the prepared
  file, all three numbers you compare will differ. Signers never have to read thousands of rows on
  a small screen; they compare three short values with the signing sheet, and anyone can
  recompute those values from the published list.

## Step 1. Choose the recipients of this batch

```bash
./airdrop.py select-batch --input <full list>.csv --out-dir runs/batch-1-selection \
    --budget <largest total in whole tokens> \
    --include 0xAddressThatMustBeInThisBatch \
    --hold <addresses to keep out for now>.txt \
    --paid runs/<an earlier batch>/recipients.csv
```

Every recipient is paid its full amount or not at all. The default strategy, `smallest-first`,
fills the budget with the smallest amounts first, so a batch reaches as many recipients as
possible; `largest-first` and `input` (file order) are the alternatives. `--include` puts an
address in the batch whatever its size, `--hold` keeps addresses out without dropping them from
the list, and `--paid` removes recipients an earlier batch already paid (the tool stops if a paid
amount differs from the list). The tool writes `selected.csv` (the batch), `remaining.csv`
(everyone else, including held addresses) and `selection.json` (inputs, their SHA-256 and the
totals).

### A random mix of sizes

`--strategy random` draws a batch that mixes wallet sizes instead of taking the smallest first:
a few large wallets, a few medium ones, and many small ones, with smaller wallets more likely to be
drawn.

```bash
./airdrop.py select-batch --input <full list>.csv --out-dir runs/batch-1-selection \
    --budget <largest total> --strategy random --seed <announced block hash> \
    --large-count 1 --medium-count 5 [--small-count N]
```

Wallets are grouped by amount: large from `--large-from` (default 10,000,000 tokens), medium from
`--medium-from` (default 100,000 tokens), small below that. `--large-to` sets an upper limit for
the draw: wallets at or above it are not drawn and stay for a later batch. Large and medium wallets are drawn with
equal chances. Small wallets are drawn with a chance proportional to 1/amount (`--small-weight`
changes this to `inverse-sqrt`, `inverse-square` or `uniform`), until `--small-count`, the budget
or `--max-recipients` is reached. A drawn wallet that would exceed the budget is skipped. Every
draw, selected or skipped, is written to `draws.csv`, and `selection.json` records the seed and the
tier settings.

The draw is a deterministic function of the seed and the list, so anyone can repeat it. That only
means something if nobody could pick the seed after seeing the list. Announce in advance which
future Ethereum block's hash will be the seed, and use that hash. A block two or three ahead of
the announcement is enough; wait a block or two after it appears before reading its hash. `--include` addresses are listed
separately in `selection.json` as required addresses and do not take part in the draw.

## Step 2. Prepare the Safe transactions

The tool needs to know which Safe pays and which token it sends; they come from `--safe` and
`--token`, or from `RESERVE_ADDRESS` and `TOKEN_ADDRESS` in `.env`. With an RPC endpoint
(`--rpc-url`, or `RPC_URL` in `.env`) it reads everything else from the Safe contract:

```bash
./airdrop.py safe-batch build --input runs/batch-1-selection/selected.csv --out-dir runs/safe-batch-1 \
    --safe 0xReserveSafe --token 0xToken --rpc-url <endpoint>
```

Why those values matter: the transfers themselves only need the token address, but the three hashes
the signers compare depend on the Safe. The domain hash covers the chain id and the Safe's address.
The message hash covers the target contract, which is the MultiSendCallOnly contract for the Safe's
version, and the nonce. If any of these differs from what the Safe web app uses, the hashes will not
match and the signers must not sign.

The version and chain id read from the chain are always right. The nonce read from the chain is
the Safe's next *executed* nonce. If other transactions are already waiting in the Safe web app's
queue, pass `--nonce` with the next free nonce shown there. Passing `--safe-version`, `--nonce` and
`--chain-id` together builds without any RPC; with an RPC they are checked against the Safe.
`--label` is only a name shown in the Transaction Builder and on the signing sheet.

Useful options: `--max-transfers N` (transfers per Safe transaction, default 300, at most 450),
`--first-transfers N` (make the first transaction small, as a pilot, before the large ones),
`--expect-total` and `--expect-count` (stop unless the list matches what you expect). The list
order is kept, so `--include` addresses from step 1 land in the first transaction.

Output:

```text
runs/safe-batch-1/SIGNING-SHEET.md             every value a signer compares, per transaction
runs/safe-batch-1/manifest.json                parameters, input hashes, per-transaction hashes
runs/safe-batch-1/recipients.csv               the whole batch, in payment order
runs/safe-batch-1/tx-01-nonce-42/transaction-builder.json   import into the Transaction Builder
runs/safe-batch-1/tx-01-nonce-42/safe-transaction.json      the exact Safe transaction and hashes
runs/safe-batch-1/tx-01-nonce-42/recipients.csv             who this transaction pays
runs/safe-batch-1/tx-02-nonce-43/...
```

## Step 3. A second person verifies the files

```bash
./airdrop.py safe-batch verify --out-dir runs/safe-batch-1                       # offline
./airdrop.py safe-batch verify --out-dir runs/safe-batch-1 --rpc-url <endpoint>  # plus the chain
```

Offline, it rebuilds every transaction and hash from `recipients.csv` alone and compares them with
every file, including the Transaction Builder files and their checksums. With `--rpc-url` it also
checks that the Safe reports the expected version, shows its owners, threshold and current nonce,
that the MultiSendCallOnly address holds Safe's official code, that the token has 18 decimals and
the Safe holds enough for the transactions still to send, and which recipients (if any) are
contracts on this chain.

Independently, check that the batch total and count are what the accounting pipeline expects.

## Step 4. Propose each transaction in the Safe web app

For each `tx-NN-nonce-M` folder, in nonce order:

1. Open the Safe, then Apps, then **Transaction Builder**.
2. Drag `transaction-builder.json` into it (or use "choose a file"). It should list the expected
   number of `transfer` calls and show no checksum warning.
3. Create the batch and send it. In the review screen, set the nonce to the one on the signing
   sheet if the app proposes a different one.
4. Before signing, compare with the signing sheet: the target (`to`) is the MultiSendCallOnly
   address, the operation is a delegate call, the nonce, and the hashes shown under the
   transaction details.
5. Sign. The hardware wallet shows a domain hash and a message hash; both must equal the sheet.

A transaction with a single transfer is created by the Safe web app as a direct call to the token
instead of a MultiSendCallOnly batch; the sheet already shows the matching values.

## Step 5. The other signers confirm and sign

Each signer opens the queued transaction and compares its nonce and Safe transaction hash with the
signing sheet, and the hashes on the hardware wallet screen before approving. A signer who wants
to check the content without trusting the prepared files can copy the transaction's raw data from
the Safe web app into a file and run:

```bash
./airdrop.py safe-batch hash --safe 0xReserveSafe --chain-id 1 --nonce <nonce> \
    --to <MultiSendCallOnly address> --operation 1 --data-file data.hex --list-out decoded.csv
```

This recomputes the three hashes from the raw data, decodes every transfer (token, recipient,
amount), prints the count and total, and writes them to `decoded.csv` for comparison with the
published `recipients.csv` of that transaction.

**Do not sign if any value differs from the signing sheet.** The usual causes are a different
nonce (other transactions were queued first: rebuild with the right `--nonce`), or, in an offline
build, a `--safe-version` that does not match the Safe (the target address will differ: rebuild).

## Step 6. Execute, then confirm

Execute the transactions in nonce order. Each transfer to a new holder costs about 27,100 gas, so a
transaction of 300 transfers uses about 8.2 million gas and one of 450 about 12.2 million, below
Ethereum's per-transaction limit of 16,777,216.

Afterwards:

```bash
./airdrop.py safe-batch verify --out-dir runs/safe-batch-1 --rpc-url <endpoint> --balances
```

reports how many recipients now hold at least their amount. For the next batch, pass this batch's
`recipients.csv` to `select-batch --paid`, so nobody is paid twice and the tool confirms the amounts
still agree with the list.

## Official MultiSendCallOnly contracts

From [safe-global/safe-deployments](https://github.com/safe-global/safe-deployments); `verify
--rpc-url` compares the code at the address with the code hash below.

| Safe version | MultiSendCallOnly address | code hash |
|---|---|---|
| 1.3.0 | `0x40A2aCCbd92BCA938b02010E17A5b8929b49130D` (also `0xA1dabEF33b3B82c7814B6D82A79e50F4AC44102B` on some chains) | `0xa9865ac2d9c7a1591619b188c4d88167b50df6cc0c5327fcbd1c8c75f7c066ad` |
| 1.4.1 | `0x9641d764fc13c8B624c04430C7356C1C7C8102e2` | `0xecd5bd14a08c5d2122379900b2f272bdf107a7e92423c10dd5fe3254386c9939` |
| 1.5.0 | `0xA83c336B20401Af773B6219BA5027174338D1836` | `0xcdbdcec38d2f1c7d961b0029ff8416b7e86e9974d6f0e9c9580c7d17fcfb6663` |

## Exact rules, for anyone reimplementing the random draw

- Tiers: not drawn if amount >= large_to (when set), otherwise large if amount >= large_from,
  medium if amount >= medium_from, small otherwise (amounts in the smallest unit). Each tier's
  pool is every eligible wallet in it (not paid, held or required), sorted by lower-case address.
- Weights: 1 in the large and medium tiers. In the small tier, `10**80 // f(amount)` with f equal
  to 1, `isqrt(amount)`, `amount` or `amount**2` for `uniform`, `inverse-sqrt`, `inverse` and
  `inverse-square`.
- Tiers are drawn in the order large, medium, small. Draw k of a tier (k = 0, 1, 2, ...) computes
  r = the SHA-256 of the text `<seed>|<tier>|<k>` read as a big-endian integer, modulo the sum of
  the pool's current weights. The pick is the first pool entry whose running weight total is
  greater than r. It leaves the pool; it is selected if it fits the remaining budget and skipped
  otherwise.

## Exact rules, for anyone reimplementing the check

- Each transfer's call data is `transfer(address,uint256)`: selector `0xa9059cbb`, then the
  recipient and the amount as 32-byte words.
- MultiSendCallOnly's list packs each call as: operation (1 byte, 0 = call), target (20 bytes, the
  token), value (32 bytes, zero), data length (32 bytes), data. The Safe transaction's data is
  `multiSend(bytes)` (selector `0x8d80ff0a`) over that packed list.
- The Safe transaction is: to = MultiSendCallOnly, value 0, operation 1 (delegate call),
  safeTxGas 0, baseGas 0, gasPrice 0, gasToken and refundReceiver the zero address, and the nonce.
- Domain hash = keccak256(typeHash("EIP712Domain(uint256 chainId,address verifyingContract)"),
  chainId, Safe address). Message hash = keccak256 over the SafeTx type hash and the fields above,
  with the data replaced by its keccak256. Safe transaction hash = keccak256(0x19, 0x01, domain
  hash, message hash). These are the Safe contract's own rules (`getTransactionHash`) for versions
  1.3.0 and later.
