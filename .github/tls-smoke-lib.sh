# shellcheck shell=bash
# Sourced by the tls-smoke job in .github/workflows/verify.yml, so the
# variables below are used there (SC2034), and the commas are tmpfs mount
# options, not array separators (SC2054).
# shellcheck disable=SC2034,SC2054
set -euo pipefail

IMAGE=ktn-enclosure-manager:smoke

# The same hardening TrueNAS's compose applies (docker-compose.yml).
HARDENING=(--cap-drop ALL --cap-add SETUID --cap-add SETGID
           --security-opt no-new-privileges
           --tmpfs /run/ktn:mode=2770,uid=0,gid=1000)

# Verifies the served certificate against the throwaway CA, by name.
HTTPS=(curl -sS --max-time 10 --cacert smoke/gen/first.crt
       --resolve ktn-smoke.test:18443:127.0.0.1)

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

wait_healthy() {
    local _
    for _ in $(seq 1 90); do
        if [ "$(docker inspect -f '{{.State.Health.Status}}' "$1")" = healthy ]; then
            return 0
        fi
        sleep 1
    done
    fail "$1 did not become healthy within 90 s"
}

fingerprint() {
    openssl x509 -noout -fingerprint -sha256
}
