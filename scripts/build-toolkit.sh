#!/usr/bin/env bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
THIRD_PARTY="$ROOT/third_party"
OUTPUT=${OUTPUT:-"$ROOT/bin"}

"$ROOT/scripts/setup-harmony-dependencies.sh"

make -C "$THIRD_PARTY/mcl" -j"${JOBS:-4}"
make -C "$THIRD_PARTY/bls" -j"${JOBS:-4}" BLS_SWAP_G=1

export CGO_ENABLED=1
source "$ROOT/scripts/toolkit-env.sh"

mkdir -p "$OUTPUT"
cd "$ROOT/toolkit"
go mod download
go test ./...

for command in \
  account-snapshot \
  account-activity \
  account-preimage-resolve \
  actual-supply \
  staking-claims \
  cross-shard-supply \
  cx-lookup-snapshot \
  historical-state-diff \
  canonical-address-resolve \
  account-transition-locate \
  transition-trace-resolve \
  account-nonce-preimage-resolve \
  address-match-apply \
  migration-claims-verify; do
  go build -trimpath -o "$OUTPUT/$command" "./cmd/$command"
done

if [[ -d "$ROOT/toolkit/cmd/cutoff-final-verifier" ]]; then
  go build -trimpath -o "$OUTPUT/cutoff-final-verifier" \
    "./cmd/cutoff-final-verifier"
fi

echo "Toolkit binaries are in $OUTPUT"
echo "Before running Harmony-dependent binaries: source scripts/toolkit-env.sh"
