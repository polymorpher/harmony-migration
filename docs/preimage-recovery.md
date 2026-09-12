# Address preimage recovery

Harmony's secure state trie stores:

```text
secure_key = keccak256(20-byte address)
```

The mapping from secure key back to address is auxiliary database data. A state
root can therefore contain a valid account balance even when one database has
no plaintext address for that key.

Keccak-256 is not reversible. Recovery means locating candidate addresses in
other canonical data and verifying them.

## Acceptance rule

An address may be attached only when:

```text
keccak256(address) == secure_key
```

This rule is enforced by every recovery and merge command.

## Recovery order

1. Scan `secure-key-` records in other Harmony full/archive databases.
2. Scan canonical regular transaction senders and recipients.
3. Scan staking transaction fields.
4. Scan cross-shard receipt senders and recipients.
5. Derive top-level contract creation addresses.
6. Inspect canonical execution traces for internal calls, `CREATE`,
   `CREATE2`, and self-destruct beneficiaries.
7. Locate the account's historical state transition and trace only that block.
8. For a positive-nonce EOA, locate its monotonic nonce transition and recover
   the signed sender address.

Explorer balances are not a recovery source.

## Direct database preimages

```sh
bin/account-preimage-resolve \
  -input unresolved.csv \
  -output resolved.csv \
  -preimage-db /path/to/first/harmony_db_0 \
  -preimage-db /path/to/second/harmony_db_0
```

The input must be sorted by `secure_key` and contain `secure_key,address`.

## Canonical body scan

```sh
bin/canonical-address-resolve \
  -db /path/to/archive/harmony_db_0 \
  -targets unresolved.csv \
  -matches canonical-matches.csv \
  -checkpoint checkpoint.json \
  -summary summary.json \
  -start-block 0 \
  -end-block 93623067
```

This command imports Harmony block types. Build it with the pinned Harmony,
MCL, and BLS dependencies using `scripts/build-toolkit.sh`.

## Targeted state transition

When a full body scan is too expensive:

```sh
bin/account-transition-locate \
  -db /path/to/archive/harmony_db_0 \
  -targets unresolved-claims.csv \
  -output account-transitions.csv \
  -summary transition-summary.json \
  -current-block 93623067 \
  -shard-id 0
```

Then trace only the returned blocks:

```sh
bin/transition-trace-resolve \
  -rpc https://your-own-archive-rpc.example \
  -targets unresolved-claims.csv \
  -transitions account-transitions.csv \
  -output trace-matches.csv \
  -summary trace-summary.json
```

Use an RPC you operate or independently trust as canonical. Balance values still
come from the state database, not the RPC trace.

## Applying matches

```sh
bin/address-match-apply \
  -input unresolved.csv \
  -match direct-preimage-matches.csv \
  -match trace-matches.csv \
  -output fully-resolved.csv \
  -summary apply-summary.json
```

The final verifier must report zero unresolved addresses.
