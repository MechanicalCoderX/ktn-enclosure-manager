#!/bin/sh
# Start the privileged IDENT helper (if configured), then drop privileges and
# run the web application as the unprivileged 'ktn' user.
#
# When KTN_IDENT_HELPER_SOCKET is set, the web process never writes sysfs
# itself: it sends identify_on/identify_off over the socket and the helper -
# the only privileged component - performs and re-validates the write (§31).
set -eu

DATA_DIR="${KTN_DATA_DIR:-/data}"
PORT="${KTN_PORT:-8420}"
HOST="${KTN_HOST:-0.0.0.0}"

mkdir -p "$DATA_DIR"

# Best-effort: succeeds only if the deployment granted CAP_CHOWN, which the
# hardened compose file deliberately does not.
chown -R ktn:ktn "$DATA_DIR" 2>/dev/null || true

# The data directory holds accounts, the session key, IDENT timers and the
# audit log. If uid 1000 cannot write it the app "works" but silently loses
# every account on restart and cannot audit, so say so loudly rather than
# discovering it later.
if ! su -s /bin/sh -c "test -w '$DATA_DIR'" ktn 2>/dev/null; then
    echo "ERROR: $DATA_DIR is not writable by uid 1000 (the app user)." >&2
    echo "       Fix it on the host, then restart:" >&2
    echo "         chown -R 1000:1000 <your KTN_DATA_PATH>" >&2
    echo "       Refusing to start: accounts and the audit log would not persist." >&2
    exit 1
fi

if [ -n "${KTN_IDENT_HELPER_SOCKET:-}" ]; then
    SOCK_DIR="$(dirname "$KTN_IDENT_HELPER_SOCKET")"
    mkdir -p "$SOCK_DIR"
    echo "starting privileged IDENT helper on ${KTN_IDENT_HELPER_SOCKET}"
    python /app/helper/ktn_ident_helper.py \
        --socket "$KTN_IDENT_HELPER_SOCKET" \
        --socket-group 1000 \
        --sysfs-root "${KTN_SYSFS_ROOT:-/sys}" \
        --allow "${KTN_ENCLOSURE_ALLOWLIST:-}" &
    HELPER_PID=$!

    # Wait briefly for the socket so the first request cannot race startup.
    i=0
    while [ ! -S "$KTN_IDENT_HELPER_SOCKET" ] && [ "$i" -lt 50 ]; do
        sleep 0.1
        i=$((i + 1))
    done
    [ -S "$KTN_IDENT_HELPER_SOCKET" ] || echo "warning: IDENT helper socket did not appear"

    trap 'kill "$HELPER_PID" 2>/dev/null || true' TERM INT
fi

if [ "$(id -u)" = "0" ]; then
    echo "dropping privileges to ktn (uid 1000) for the web process"
    exec setpriv --reuid=1000 --regid=1000 --clear-groups \
        python -m uvicorn ktnmgr.main:app --host "$HOST" --port "$PORT" \
        --log-level "${KTN_LOG_LEVEL:-info}"
fi

exec python -m uvicorn ktnmgr.main:app --host "$HOST" --port "$PORT" \
    --log-level "${KTN_LOG_LEVEL:-info}"
