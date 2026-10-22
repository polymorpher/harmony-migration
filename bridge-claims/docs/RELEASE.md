# Releasing the bridge collateral to the custody Safe

The claim contracts pay from the custody Safe, so the collateral must first leave the bridge
escrows and the ONE Reserve Safe. This note explains why the escrow release is a LayerZero delivery
rather than an ERC-20 transfer, how each owner performs it on the escrow's own chain without any
LayerZero service, and how to run and check it.

## Why it is not a transfer

Every escrow is a LayerZero v1 OFT contract: a `ProxyOFT` that locks a real ERC-20 (USDC, LINK,
...), or a `NativeOFT` that locks the native coin (ETH, BNB). Their owner functions only configure
LayerZero (trusted remote, messaging libraries, minimum gas, ownership); none of them withdraws or
rescues the locked asset. Collateral leaves an escrow only through `_creditTo`, which runs when the
escrow's LayerZero endpoint delivers a message from a trusted remote. The endpoint is fixed in each
escrow and is the only caller `lzReceive` accepts.

## Who decides what the endpoint delivers

The endpoint delivers through the escrow's receive library, ReceiveUln301 on all five chains. That
library delivers a packet once the escrow's required verifiers have attested it, and only when the
escrow's executor submits it. The escrow's owner chooses all three per source chain, with
`setReceiveVersion` and `setConfig`. LayerZero's verifiers (DVNs) and executor are only the
defaults that apply when the owner has chosen nothing.

So each owner releases its own escrows on the escrow's chain: a Safe owner in one transaction, a
single-key owner in a short sequence. Each escrow gets the same 10 calls, in this order. Calls 3
and 8 appear only when the escrow follows LayerZero's default receive library, which every escrow
does today. `v` is the receive library version in effect; "own chain" is the escrow chain's
LayerZero id.

1. `escrow.setConfig(v, own chain, 2, UlnConfig)`: the owner becomes the only verifier for
   messages from the custody Safe on this chain. There are no optional verifiers, and 1
   confirmation, so no LayerZero default applies.
2. `escrow.setConfig(v, own chain, 1, owner)`: the owner becomes the only party allowed to deliver
   them.
3. `escrow.setReceiveVersion(v)`: pins the receive library in effect, so a later change of
   LayerZero's default library cannot reach the escrow. The endpoint's library versions are
   append-only.
4. `escrow.setTrustedRemote(own chain, Safe ++ escrow)`: the escrow accepts messages from the
   custody Safe on this chain.
5. `ReceiveUln301.verify(header, payloadHash, 1)`: the owner, as verifier, attests one packet. It
   is message 1 (the path nonce) from the Safe to this escrow, and its payload credits the whole
   locked balance to the Safe:
   - for the escrows of Harmony's OFT v1 fork (they expose `circulatingSupply()`),
     `abi.encode(uint16 0, bytes Safe, bytes Safe, uint256 amount)`;
   - for the legacy Ethereum ETH `NativeOFT` escrow (`0xab89…bcab`), `abi.encode(bytes Safe,
     uint256 amount)`.
6. `ReceiveUln301.commitVerification(packet, 300000)`: the owner, as executor, delivers it. The
   library checks the attestation, the endpoint calls the escrow, and the escrow pays the Safe.
   This is the only call that moves funds.
7. `escrow.setTrustedRemote(own chain, previous)`: puts back the previous trusted remote. It is
   empty on every escrow except the Ethereum LINK escrow, which keeps a 2022 entry.
8. `escrow.setReceiveVersion(previous)`: the escrow follows LayerZero's default library again.
9. `escrow.setConfig(v, own chain, 1, previous)`: puts back the previous executor (unset).
10. `escrow.setConfig(v, own chain, 2, previous)`: puts back the previous verifier config (unset).

Nothing is sent across chains and no fee is paid. Afterwards every escrow setting is
byte-for-byte what it was; only the balance changed. The endpoint keeps its own record that
message 1 on that path was delivered, which blocks any replay. The Harmony path (chain 116), its
trusted remote, and its nonce are not touched.

### Why the packet comes from the Safe on the escrow's own chain

The packet's source path is a label the owner chooses. It names exactly what happens: the custody
Safe on this chain receives, the owner attests, and it is the first message on a fresh path
(nonce 1). The alternatives would misdescribe the release:

- **The Harmony path** would save calls 4 and 7, since the escrows already trust the Harmony
  bridge contract: 8 calls per escrow instead of 10. But anyone reading the chain, LayerZero Scan
  and this repository's bridge-message scanner included, would see a withdrawal sent by the Harmony
  bridge contract at the next Harmony nonce. For the Ethereum USDC escrow that is nonce 852, the
  number of a real Harmony transfer that was never delivered (its sender is credited in the claim
  list). After a hack that used forged Harmony bridge messages, the release would look like more of
  them.
- **Another chain** (for example Arbitrum) would claim a send that never happened there.

### Why there is no cross-chain alternative

An earlier plan had the custody Safe on Arbitrum and Base send LayerZero messages to the escrows,
with a one-unit rehearsal first and LayerZero fees paid in ETH. It was removed rather than kept as
a fallback. It delivers through the same endpoint and ReceiveUln301 contracts as the local release
and, in addition, needs LayerZero's off-chain verifiers and executor to process every message. So
it cannot succeed in any situation where the local release fails.

### What LayerZero can and cannot do

The release reads nothing LayerZero controls off chain. No verifier or executor of theirs takes
part, and no message waits for their attestation. On chain, the only LayerZero code in the path is
the deployed endpoint and ReceiveUln301 contracts. In the Ethereum bytecode neither contract has a
pause function. Their owner functions that affect receiving only set defaults
(`setDefaultReceiveVersion`, `setDefaultUlnConfigs`, `setDefaultExecutors`) or append library
versions (`newVersion`), and the release overrides every default it relies on within the same
transaction.

What a reader of the chain sees, per escrow: the owner set itself as verifier and executor, the
owner attested a packet from the custody Safe, the escrow paid the Safe, and the owner restored
the previous settings. Nobody else is presented as having verified anything.

## Owners

Escrows owned by a Safe are released with one Transaction Builder batch per chain and owner. The
batch is atomic: if any call fails, nothing happens. About 350,000 gas per escrow keeps the
largest batch (23 Ethereum escrows) near 8.1 million gas, under the 16,777,216 per-transaction cap.

Escrows owned by a single key get raw calldata (`to`, `value`, `data`) to send in file order: 10
transactions per escrow, or 8 when its receive version is already pinned. Each is under 100,000
gas. The sequence is not atomic, but stopping midway is safe: once the first call has run, only
the owner can attest or deliver on the path. The verifier config is the first setting changed and
the last one restored, so `tools/release_plan.py` detects a half-finished escrow and refuses to
plan over it; finish it from the same file.

## Running it

```sh
set -a; . ../bridge/local/rpc.env; set +a
python3 tools/release_plan.py --allocations-dir ../artifacts/<run>/allocations \
  --output-dir runs/<run>/release
```

`runs/<run>/release/` then holds, in the order to run them:

| Step | Directory | Who |
|---|---|---|
| 0a | `0-safe-owners/` | initial owner on 1-of-1 chains (optional; adds the other owners, threshold 2) |
| 0b | `0-one-reserve/` | the ONE Reserve Safe on Ethereum |
| 1 | `1-release/` | each escrow owner (`*-safe.json` batches, `*-eoa.json` calldata) |

and `RELEASE.md`. That sheet says what each file does and who signs it, and lists the escrows,
locked balances, prior settings, Safe nonces, and Safe transaction hashes. Rebuild right before
signing: each packet carries the exact locked balance and path nonce read at build time, and each
Safe hash the Safe's nonce.

The two kinds of file:

- **`*-safe.json`** is a Safe Transaction Builder batch. Load it in the Safe app's Transaction
  Builder. Check that the Safe transaction hash matches `RELEASE.md`, then sign with the required
  owners and execute. It is one transaction at the planned nonce. If the Safe executes anything
  else first, that hash no longer applies; rebuild.
- **`*-eoa.json`** lists transactions for the key in `from`. Send each entry's `to`, `value` (0),
  and `data` as its own transaction, in file order. If one fails, resend it, and finish a started
  escrow from the same file instead of rebuilding.

Gas is paid by the account that executes each transaction. No Safe needs native coin for this
step.

## Verification

- `test/EscrowRelease.t.sol`, on a fork of each chain, replays every file the plan lists for that
  chain:
  - each Safe batch is rebuilt from its Transaction Builder file, must hash to the signing sheet's
    Safe transaction hash at the Safe's live nonce, and runs through the real Safe;
  - each EOA file is sent by its owner, one transaction at a time.

  Every escrow must end empty and the Safe must receive the locked total of each asset (less
  PAXG's transfer fee). The local path must advance by one nonce, every setting must read as
  before (both paths), every transaction must stay under the gas cap, and every approval must be
  funded. All 49 escrows on the five chains pass.
- `tools/test_tools.py`: the packet codec reproduces a real ReceiveUln301 delivery on Ethereum. The
  payloads reproduce a real Harmony delivery to the Ethereum USDC escrow and the legacy encoding
  for `0xab89…bcab`. Each escrow's calls write back exactly what they overwrite, and the planner
  refuses a half-finished release.

The fork runs the real endpoint and library code, and nothing happens off chain, so no part of the
release is left to rehearse on mainnet.
