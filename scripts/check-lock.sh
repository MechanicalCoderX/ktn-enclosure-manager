#!/usr/bin/env bash
# Fails if backend/requirements.txt or backend/build-requirements.txt is not
# exactly what its inputs compile to.
#
# Each lock is recompiled from its .in file with the lock's own pins as
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

# Each lock against its own input: the runtime lock against requirements.in,
# the image's build backend against build-requirements.in. Both are checked
# before the script exits, so one run reports every drifted lock.
status=0
for name in requirements build-requirements; do
    sed -n 's/^\([A-Za-z0-9][^\\]*\) \\$/\1/p' "$name.txt" > "$tmp/pins.txt"
    [ -s "$tmp/pins.txt" ] || { echo "no pins read from $name.txt" >&2; exit 1; }

    "${uv[@]}" pip compile --universal --python-version 3.15 --generate-hashes \
        --no-header --no-annotate --quiet \
        "$name.in" -c "$tmp/pins.txt" -o "$tmp/$name.txt"

    if ! grep -v '^ *#' "$name.txt" | diff -u - "$tmp/$name.txt"; then
        echo "backend/$name.txt does not match what $name.in compiles to" \
             "(diff above: - committed, + recompiled). Recompile it as CONTRIBUTING.md," \
             "\"Dependency lock\", describes." >&2
        status=1
        continue
    fi
    echo "backend/$name.txt matches its inputs ($(wc -l < "$tmp/pins.txt") pins)."
done
exit "$status"
