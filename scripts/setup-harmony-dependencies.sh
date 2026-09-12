#!/usr/bin/env bash

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
THIRD_PARTY="$ROOT/third_party"

HARMONY_URL=${HARMONY_URL:-https://github.com/polymorpher/harmony.git}
HARMONY_REV=${HARMONY_REV:-8292e786db513186faff5b397fdf864c0c189ac1}
MCL_URL=${MCL_URL:-https://github.com/harmony-one/mcl.git}
MCL_REV=${MCL_REV:-ac6b73317f5321b33fc877a4dd7218e1815693d8}
BLS_URL=${BLS_URL:-https://github.com/harmony-one/bls.git}
BLS_REV=${BLS_REV:-2b7e49894c0f15f5c40cf74046505b7f74946e52}

clone_at() {
  local url=$1
  local revision=$2
  local destination=$3

  if [[ ! -d "$destination/.git" ]]; then
    git clone --filter=blob:none --no-checkout "$url" "$destination"
  fi
  if [[ -n "$(git -C "$destination" status --short --untracked-files=no)" ]]; then
    echo "refusing to change dirty dependency: $destination" >&2
    exit 1
  fi
  git -C "$destination" fetch --depth=1 origin "$revision"
  git -C "$destination" checkout --detach "$revision"
  test "$(git -C "$destination" rev-parse HEAD)" = "$revision"
}

mkdir -p "$THIRD_PARTY"
clone_at "$HARMONY_URL" "$HARMONY_REV" "$THIRD_PARTY/harmony"
clone_at "$MCL_URL" "$MCL_REV" "$THIRD_PARTY/mcl"
clone_at "$BLS_URL" "$BLS_REV" "$THIRD_PARTY/bls"

echo "Pinned Harmony, MCL, and BLS dependencies are ready under $THIRD_PARTY"
