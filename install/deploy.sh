#!/bin/sh
# Pull the latest Duplicate Finder from git and roll it out on a Synology NAS.
#
#   sh deploy.sh            # deploy if the remote moved, otherwise do nothing
#   sh deploy.sh --force    # deploy even if nothing changed
#   sh deploy.sh --check    # report what would happen, change nothing
#
# Safe to run from a timer: it exits without touching the service when the
# remote has not moved, and it rolls back to the previous copy if the new one
# fails to come up.

set -e

REPO_URL=${REPO_URL:-https://github.com/MARVion123/dupfinder.git}
BRANCH=${BRANCH:-main}
SRC=${SRC:-/volume1/apps/src/dupfinder}     # the git working copy
APP=${APP:-/volume1/apps}                   # holds the importable dupfinder/ package
SERVICE=${SERVICE:-dupfinder}
UNIT=/etc/systemd/system/${SERVICE}.service

FORCE=0
CHECK=0
case "$1" in
    --force) FORCE=1 ;;
    --check) CHECK=1 ;;
    "") ;;
    *) echo "usage: $0 [--force|--check]" >&2; exit 2 ;;
esac

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
die() { log "error: $*"; exit 1; }

[ "$(id -u)" = "0" ] || die "run this as root"
command -v git >/dev/null 2>&1 || die \
    "git is not installed. Package Center -> Git Server, or install it via Entware."

# Reuse whatever interpreter and port the running unit was configured with,
# so this script keeps working after a switch to a virtualenv or another port.
PYTHON=$(awk -F= '/^ExecStart=/{print $2}' "${UNIT}" 2>/dev/null | awk '{print $1}')
[ -n "${PYTHON}" ] && [ -x "${PYTHON}" ] || PYTHON=/usr/bin/python3
PORT=$(awk -F'--port ' '/^ExecStart=/{print $2}' "${UNIT}" 2>/dev/null | awk '{print $1}')
[ -n "${PORT}" ] || PORT=8777

# ---- fetch -----------------------------------------------------------------
if [ ! -d "${SRC}/.git" ]; then
    log "first run: cloning ${REPO_URL}"
    [ "${CHECK}" = "1" ] && { log "--check: would clone, stopping here"; exit 0; }
    mkdir -p "$(dirname "${SRC}")"
    git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${SRC}"
    FORCE=1
else
    git -C "${SRC}" remote set-url origin "${REPO_URL}"
    git -C "${SRC}" fetch --quiet origin "${BRANCH}"
fi

LOCAL=$(git -C "${SRC}" rev-parse HEAD)
REMOTE=$(git -C "${SRC}" rev-parse "origin/${BRANCH}")

if [ "${LOCAL}" = "${REMOTE}" ] && [ "${FORCE}" = "0" ]; then
    log "already at $(git -C "${SRC}" rev-parse --short HEAD) - nothing to do"
    exit 0
fi

if [ "${CHECK}" = "1" ]; then
    log "--check: would move from ${LOCAL} to ${REMOTE}"
    git -C "${SRC}" --no-pager log --oneline "HEAD..origin/${BRANCH}" | sed 's/^/    /'
    exit 0
fi

# Discard local edits to the working copy on purpose: this directory is a
# deployment target, not a place to develop. Anything changed here by hand is
# meant to be overwritten by what is in the repository.
git -C "${SRC}" reset --quiet --hard "origin/${BRANCH}"
log "checked out $(git -C "${SRC}" rev-parse --short HEAD): $(git -C "${SRC}" log -1 --format=%s)"

# ---- verify before touching the running service ----------------------------
# A syntax error must fail here, not after the service has already been
# stopped. compileall walks the whole package and returns non-zero on the
# first file that will not parse.
"${PYTHON}" -m compileall -q "${SRC}/dupfinder" >/dev/null 2>&1 || \
    die "the new checkout does not compile - the running service was left alone"

# ---- swap in, keeping the old copy for rollback ----------------------------
BACKUP="${APP}/.dupfinder-previous"
rm -rf "${BACKUP}"
[ -d "${APP}/dupfinder" ] && cp -a "${APP}/dupfinder" "${BACKUP}"

rm -rf "${APP}/dupfinder.new"
cp -a "${SRC}/dupfinder" "${APP}/dupfinder.new"
find "${APP}/dupfinder.new" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

systemctl stop "${SERVICE}" || true
rm -rf "${APP}/dupfinder"
mv "${APP}/dupfinder.new" "${APP}/dupfinder"
chmod -R a+rX "${APP}/dupfinder"
systemctl start "${SERVICE}"

# ---- health check, with rollback -------------------------------------------
ok=0
i=0
while [ $i -lt 15 ]; do
    # 401 counts as healthy: it means the server is up and enforcing the access
    # token. Treating it as a failure would roll back every deployment on an
    # installation that has auth_token set.
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/status" || true)
    if [ "${code}" = "200" ] || [ "${code}" = "401" ]; then
        ok=1
        break
    fi
    sleep 1
    i=$((i + 1))
done

if [ "${ok}" = "1" ]; then
    log "deployed and answering on port ${PORT}"
    rm -rf "${BACKUP}"
    exit 0
fi

log "the new version did not answer within 15s - rolling back"
if [ -d "${BACKUP}" ]; then
    systemctl stop "${SERVICE}" || true
    rm -rf "${APP}/dupfinder"
    mv "${BACKUP}" "${APP}/dupfinder"
    systemctl start "${SERVICE}"
    log "rolled back to the previous copy"
else
    log "no previous copy to roll back to"
fi
die "deployment failed - check: journalctl -u ${SERVICE} -n 50"
