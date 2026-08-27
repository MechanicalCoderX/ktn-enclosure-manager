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

# Which proxy addresses uvicorn believes X-Forwarded-* from. This is what
# makes the session cookie's Secure flag work behind an external TLS proxy:
# the flag derives from the request scheme, and uvicorn only rewrites the
# scheme from X-Forwarded-Proto when the header arrives from an address
# listed here. The default is uvicorn's own (127.0.0.1), so a deployment
# that sets nothing behaves exactly as before.
FORWARDED_ALLOW_IPS="${KTN_FORWARDED_ALLOW_IPS:-127.0.0.1}"

mkdir -p "$DATA_DIR"

# The enclosure lock lives here, not in the data dataset - see access.py. The
# directory is created unconditionally so the path is valid even when no
# privileged helper is configured and nothing else creates /run/ktn.
KTN_ENCLOSURE_LOCK="${KTN_ENCLOSURE_LOCK:-/run/ktn/enclosure.lock}"
export KTN_ENCLOSURE_LOCK
mkdir -p "$(dirname "$KTN_ENCLOSURE_LOCK")"

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

    # Deliberately NOT chmod'ing this directory.
    #
    # A previous version ran `chmod 2770` here to guarantee the setgid bit.
    # It did the opposite: the container has no CAP_FSETID, and when a process
    # without it chmods a directory whose group it is not a member of, Linux
    # strips S_ISGID from the mode. The tmpfs is mounted 2770 with gid 1000
    # and root is not in group 1000, so the call "succeeded", returned 0, and
    # silently turned 2770 into 0770 - after which the helper's socket was
    # created root:root and the web process could not connect to it at all.
    #
    # The helper now sets the socket's group itself when it binds, so nothing
    # here depends on the directory's setgid bit.

    echo "starting privileged IDENT helper on ${KTN_IDENT_HELPER_SOCKET}"
    python /app/helper/ktn_ident_helper.py \
        --socket "$KTN_IDENT_HELPER_SOCKET" \
        --socket-group 1000 \
        --sysfs-root "${KTN_SYSFS_ROOT:-/sys}" \
        --allow "${KTN_ENCLOSURE_ALLOWLIST:-}" \
        --ident-method "${KTN_IDENT_METHOD:-auto}" \
        --enclosure-lock "$KTN_ENCLOSURE_LOCK" &
    HELPER_PID=$!

    # Wait briefly for the socket so the first request cannot race startup.
    i=0
    while [ ! -S "$KTN_IDENT_HELPER_SOCKET" ] && [ "$i" -lt 50 ]; do
        sleep 0.1
        i=$((i + 1))
    done
    [ -S "$KTN_IDENT_HELPER_SOCKET" ] || echo "warning: IDENT helper socket did not appear"

    # No trap for the helper here: a previous version installed one, but the
    # web process is started with exec, which replaces this shell - so the
    # trap could never fire. It was dead code that looked like cleanup.
    # Container teardown kills the whole cgroup, helper included; and while
    # the container runs, /healthz probes the helper socket, so a helper that
    # dies marks the container unhealthy instead of silently losing IDENT and
    # all SES telemetry.
    : "$HELPER_PID"
fi

if [ "$(id -u)" = "0" ]; then
    echo "dropping privileges to ktn (uid 1000) for the web process"
    exec setpriv --reuid=1000 --regid=1000 --clear-groups \
        python -m uvicorn ktnmgr.main:app --host "$HOST" --port "$PORT" \
        --forwarded-allow-ips "$FORWARDED_ALLOW_IPS" \
        --log-level "${KTN_LOG_LEVEL:-info}"
fi

exec python -m uvicorn ktnmgr.main:app --host "$HOST" --port "$PORT" \
    --forwarded-allow-ips "$FORWARDED_ALLOW_IPS" \
    --log-level "${KTN_LOG_LEVEL:-info}"
