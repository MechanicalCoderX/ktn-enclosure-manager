# Sourced by docker-entrypoint.sh (POSIX sh). Not executable on its own.
#
# ktn_bool VALUE - print "true" or "false" for KTN_TLS_ENABLED's spellings;
# return 1 (printing nothing) for anything else, so the caller can refuse to
# start rather than guess.
#
# This must accept exactly what the app accepts: TLS_BOOL_TRUE/TLS_BOOL_FALSE
# in backend/ktnmgr/config.py. tests/test_tls_config.py runs both over the
# same table. If they drifted, the entrypoint could start uvicorn without TLS
# while the app reported it on, or refuse a value the app would take. Case is
# matched letter by letter rather than with tr, so nothing (a trailing
# newline, a locale) is normalised away that the Python side would keep.
ktn_bool() {
    case "$1" in
        1 | [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Oo][Nn]) echo true ;;
        0 | [Ff][Aa][Ll][Ss][Ee] | [Nn][Oo] | [Oo][Ff][Ff] | '') echo false ;;
        *) return 1 ;;
    esac
}
