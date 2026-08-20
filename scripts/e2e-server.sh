#!/usr/bin/env bash
# Boot the backend against the captured sysfs fixture, with a clean data dir so
# every E2E run starts from the first-run bootstrap screen.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${KTN_E2E_DATA:-/tmp/ktn-e2e-data}"

rm -rf "$DATA"
mkdir -p "$DATA"

# A writable copy: the IDENT tests actually write the locate attribute.
SYSFS="$DATA/sys"
cp -r "$ROOT/tests/fixtures/sysfs_root" "$SYSFS"

# The fixture carries two synthetic entries the E2E suite depends on: an empty
# bay at SES slot 99 (proves discovery keys on the 'slot' attribute) and a
# decoy non-slot directory. The real KTN-STL3 has exactly 15 bays, so anything
# user-facing shot against the fixture must prune them or it depicts hardware
# that does not exist - which is exactly how a phantom "Bay 100" ended up in
# the README screenshots. Screenshots set this; the E2E suite must not.
if [ "${KTN_E2E_SYNTHETIC_SLOTS:-1}" = "0" ]; then
    rm -rf "$SYSFS"/class/enclosure/*/99 "$SYSFS"/class/enclosure/*/decoy
fi

export KTN_SYSFS_ROOT="$SYSFS"
export KTN_DEV_ROOT="$DATA/dev"
export KTN_DATA_DIR="$DATA"
export KTN_TRUENAS_URL=""
export KTN_POLL_SLOTS_SECONDS=0.5
# The chassis poll defaults to 30s, which is longer than Playwright's per-test
# timeout - a test waiting for telemetry could never see it arrive.
export KTN_POLL_SES_SECONDS=1
# Synthetic sysfs tree, no SES device - see tests/test_api.py.
export KTN_IDENT_METHOD=sysfs
# Replay the captured (sanitised) SES pages so the chassis view has real
# content without a shelf attached. Without this it renders only its
# "sg_ses is not installed" state.
export KTN_SG_SES_BINARY="$ROOT/tests/fixtures/fake-sg_ses"
export PYTHONPATH="$ROOT/backend"

exec "$ROOT/.venv/bin/python" -m uvicorn ktnmgr.main:app \
  --host 127.0.0.1 --port "${KTN_E2E_PORT:-8421}" --log-level warning
