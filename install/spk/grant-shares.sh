#!/bin/sh
# Give the package's service user access to shared folders, on DSM 7.
#
#   sudo sh grant-shares.sh --list                     # what can it reach today
#   sudo sh grant-shares.sh photo video                # show what would change
#   sudo sh grant-shares.sh --apply photo video        # actually change it
#   sudo sh grant-shares.sh --apply --ro archive       # read-only
#   sudo sh grant-shares.sh --apply --revoke photo     # take it back
#   sudo sh grant-shares.sh --check /volume1/photo/2019
#
# Share names with spaces work: quote them.
#   sudo sh grant-shares.sh --apply "Nino Arbeit" "Extern-Eigene Dateien"
#
# Why this exists: DSM 7 only lets Synology-signed packages run as root, so
# this one runs as its own user and starts with access to nothing. Clicking
# through Control Panel once per share is the documented way; this is the same
# thing without the clicking, and with a check afterwards.
#
# It always adds to the existing access list and never replaces it.
# `synoshare --setuser` also accepts `=`, which would drop every other user
# from the share. That operator is deliberately not used anywhere below.

set -e

USER_NAME=${USER_NAME:-dupfinder}
AUTH=RW
OPERATOR="+"
REVOKE=0
APPLY=0
LIST_ONLY=0
# Newline-separated, never space-separated: real share names contain spaces
# ("Extern-Eigene Dateien"), and the first version of this script split those
# into two shares that did not exist.
SHARES=""
CHECK_PATHS=""
NL="
"

die() { echo "error: $*" >&2; exit 1; }

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit ${1:-0}
}

while [ $# -gt 0 ]; do
    case "$1" in
        --user)   USER_NAME=$2; shift 2 ;;
        --ro)     AUTH=RO; shift ;;
        --revoke) REVOKE=1; OPERATOR="-"; shift ;;
        --apply)  APPLY=1; shift ;;
        --list)   LIST_ONLY=1; shift ;;
        --check)  CHECK_PATHS="${CHECK_PATHS}$2${NL}"; shift 2 ;;
        -h|--help) usage 0 ;;
        -*)       die "unknown option: $1  (try --help)" ;;
        *)        SHARES="${SHARES}$1${NL}"; shift ;;
    esac
done

[ "$(id -u)" = "0" ] || die "run this with sudo"
command -v synoshare >/dev/null 2>&1 || die "synoshare not found - is this a DSM 7 NAS?"

id "${USER_NAME}" >/dev/null 2>&1 || die \
    "there is no user called '${USER_NAME}'. Install the package first, or pass --user NAME."

# --- can the service user actually get at a path? ---------------------------
# The only answer that means anything. Share permissions, folder ACLs and the
# traverse rights on every parent all have to line up, and when they do not,
# every one of them fails the same way. So ask the user itself.
as_user() {
    sudo -u "${USER_NAME}" "$@" 2>/dev/null
}

reachable() {
    path=$1
    [ -e "${path}" ] || { echo "missing"; return; }
    if ! as_user test -x "${path}"; then echo "no access"; return; fi
    if ! as_user ls "${path}" >/dev/null; then echo "no access"; return; fi
    if as_user test -w "${path}"; then echo "read/write"; else echo "read only"; fi
}

share_path() {
    # synoshare knows where a share really lives; do not assume /volume1.
    synoshare --get-real-path "$1" 2>/dev/null | tr -d '\r' | tail -n 1
}

# `synoshare --enum ALL` prints two header lines and then one bare share name
# per line, not indented:
#
#   Share Enum Arguments: [0x3FF0F]  ALL ENC DEC ...
#   37 Listed:
#   Apps
#   Extern-Eigene Dateien
#
# The first version of this expected the names to be indented and so found
# none at all - the list came out empty with no error, which is the worst way
# for a parser to be wrong.
all_shares() {
    synoshare --enum ALL 2>/dev/null \
        | tr -d '\r' \
        | sed -e '/^Share Enum Arguments:/d' \
              -e '/^[0-9][0-9]* Listed:/d' \
              -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        | grep -v '^$' || true
}

# Reads share names from stdin, one per line.
report() {
    printf "  %-30s %-40s %s\n" "SHARE" "PATH" "${USER_NAME} can"
    while IFS= read -r share; do
        [ -n "${share}" ] || continue
        path=$(share_path "${share}")
        [ -n "${path}" ] || path="(unknown)"
        printf "  %-30s %-40s %s\n" "${share}" "${path}" "$(reachable "${path}")"
    done
}

# --- --list -----------------------------------------------------------------
if [ "${LIST_ONLY}" = "1" ]; then
    found=$(all_shares)
    if [ -z "${found}" ]; then
        die "synoshare listed no shares. Check the output of:  synoshare --enum ALL"
    fi
    echo "Shares on this NAS, and what ${USER_NAME} can do with them:"
    echo
    printf '%s\n' "${found}" | report
    echo
    echo "Grant one with:  sudo sh $0 --apply <share>"
    echo "Names with spaces need quotes:  sudo sh $0 --apply \"Nino Arbeit\""
    exit 0
fi

if [ -n "${CHECK_PATHS}" ] && [ -z "${SHARES}" ]; then
    echo "Checking paths as ${USER_NAME}:"
    printf '%s' "${CHECK_PATHS}" | while IFS= read -r path; do
        [ -n "${path}" ] || continue
        printf "  %-52s %s\n" "${path}" "$(reachable "${path}")"
    done
    exit 0
fi

[ -n "${SHARES}" ] || usage 1

# --- refuse to work on shares that do not exist -----------------------------
printf '%s' "${SHARES}" | while IFS= read -r share; do
    [ -n "${share}" ] || continue
    synoshare --get "${share}" >/dev/null 2>&1 || die \
        "no shared folder called '${share}'. See:  sudo sh $0 --list"
done

echo "Before:"
printf '%s' "${SHARES}" | report
echo

# Revoking means taking the user off both access lists, which drops it back to
# the share's default of nothing. Not `NA +`, and emphatically not `NA -`:
# synoshare keeps one list per access level, so removing a name from the
# no-access list is not the opposite of granting it - it is a different edit
# that can leave the access it was meant to take away.
if [ "${REVOKE}" = "1" ]; then
    PLAN="RW -${NL}RO -"
else
    PLAN="${AUTH} ${OPERATOR}"
fi

if [ "${APPLY}" != "1" ]; then
    echo "Would run:"
    printf '%s' "${SHARES}" | while IFS= read -r share; do
        [ -n "${share}" ] || continue
        printf '%s\n' "${PLAN}" | while read -r auth op; do
            echo "  synoshare --setuser '${share}' ${auth} ${op} ${USER_NAME}"
        done
    done
    echo
    echo "Nothing was changed. Add --apply to do it."
    exit 0
fi

echo "Applying:"
printf '%s' "${SHARES}" | while IFS= read -r share; do
    [ -n "${share}" ] || continue
    printf '%s\n' "${PLAN}" | while read -r auth op; do
        echo "  synoshare --setuser '${share}' ${auth} ${op} ${USER_NAME}"
        synoshare --setuser "${share}" "${auth}" "${op}" "${USER_NAME}" \
            || die "synoshare refused to change '${share}'"
    done
done
echo

echo "After:"
printf '%s' "${SHARES}" | report

if [ -n "${CHECK_PATHS}" ]; then
    printf '%s' "${CHECK_PATHS}" | while IFS= read -r path; do
        [ -n "${path}" ] || continue
        printf "  %-52s %s\n" "${path}" "$(reachable "${path}")"
    done
fi

echo
cat <<'NOTE'
If a share still says "no access", the share permission is not the thing
stopping it. The usual causes, in order of how often they are the answer:

  * You are scanning a subfolder and a folder above it denies traversal.
    File Station -> right-click the folder -> Properties -> Permissions,
    add the user with "Traverse folders / List folders" on "This folder",
    for every level down to the one you scan.
  * The share carries its own ACL that overrides the simple Read/Write flag.
    `synoshare --list_acl <share>` shows it.
  * The share is encrypted and not mounted.
NOTE
