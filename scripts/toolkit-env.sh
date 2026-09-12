#!/usr/bin/env bash

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
THIRD_PARTY="$ROOT/third_party"

export CGO_CFLAGS="-I$THIRD_PARTY/bls/include -I$THIRD_PARTY/mcl/include"
export CGO_LDFLAGS="-L$THIRD_PARTY/bls/lib"
export LD_LIBRARY_PATH="$THIRD_PARTY/bls/lib:$THIRD_PARTY/mcl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
