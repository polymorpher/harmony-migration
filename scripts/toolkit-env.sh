#!/usr/bin/env bash

if [[ -n "${ZSH_VERSION:-}" ]]; then
  SCRIPT_PATH="${(%):-%x}"
else
  SCRIPT_PATH="${BASH_SOURCE[0]}"
fi
ROOT=$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)
THIRD_PARTY="$ROOT/third_party"

export CGO_CFLAGS="-I$THIRD_PARTY/bls/include -I$THIRD_PARTY/mcl/include"
export CGO_LDFLAGS="-L$THIRD_PARTY/bls/lib"
export LD_LIBRARY_PATH="$THIRD_PARTY/bls/lib:$THIRD_PARTY/mcl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export DYLD_LIBRARY_PATH="$THIRD_PARTY/bls/lib:$THIRD_PARTY/mcl/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
