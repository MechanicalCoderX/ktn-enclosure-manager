#!/usr/bin/env bash
# Fails if backend/requirements.txt is not exactly what its inputs compile to.
#
# The lock is recompiled from requirements.in with the lock's own pins as
# constraints, so every version stays put and only the body is re-derived:
# the hashes (from the index, not copied from the old file), the environment
# markers, and which packages are in it at all. The two bodies are compared
# with comments stripped, so a different uv's header or "# via" layout does
# not count. A hand-edited pin, a dropped or altered hash, a missing package
# (say uvloop) or a body compiled for another Python or platform under this
# header all fail. This is consistency, not freshness: nothing is
# upgraded. It also fails if PyPI later adds or yanks a file of a pinned
# version; recompile then (CONTRIBUTING.md, "Dependency lock").
#
# The options below are the lock header's; tests/test_image_build_inputs.py
# fails if they differ. CI runs this with a pinned uv: UV="pipx run --spec
# uv==X.Y.Z uv". Locally, `scripts/check-lock.sh` with uv on PATH.
set -euo pipefail

read -ra uv <<< "${UV:-uv}"
cd "$(dirname "$0")/../backend"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

sed -n 's/^\([A-Za-z0-9][^\\]*\) \\$/\1/p' requirements.txt > "$tmp/pins.txt"
[ -s "$tmp/pins.txt" ] || { echo "no pins read from requirements.txt" >&2; exit 1; }

"${uv[@]}" pip compile --universal --python-version 3.15 --generate-hashes \
    --no-header --no-annotate --quiet \
    requirements.in -c "$tmp/pins.txt" -o "$tmp/requirements.txt"

if ! grep -v '^ *#' requirements.txt | diff -u - "$tmp/requirements.txt"; then
    echo "backend/requirements.txt does not match what requirements.in compiles to" \
         "(diff above: - committed, + recompiled). Recompile it as CONTRIBUTING.md," \
         "\"Dependency lock\", describes." >&2
    exit 1
fi
echo "backend/requirements.txt matches its inputs ($(wc -l < "$tmp/pins.txt") pins)."
