#!/bin/sh
# Install Duplicate Finder on Synology DSM 7.4.
#
#   sudo sh install-dsm.sh            # install + start
#   sudo sh install-dsm.sh uninstall
#
# Run it over SSH from the directory that contains the dupfinder/ package.

set -e

APP_DIR=${APP_DIR:-/volume1/apps/dupfinder}
DATA_DIR=${DATA_DIR:-/var/services/dupfinder}
SERVICE=dupfinder
UNIT=/etc/systemd/system/${SERVICE}.service
PORT=${PORT:-8777}

die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run this with sudo"

if [ "$1" = "uninstall" ]; then
    systemctl stop  ${SERVICE} 2>/dev/null || true
    systemctl disable ${SERVICE} 2>/dev/null || true
    rm -f "${UNIT}"
    systemctl daemon-reload
    echo "Service removed. Application files in ${APP_DIR} and data in ${DATA_DIR} were kept."
    echo "Delete them by hand if you want them gone."
    exit 0
fi

command -v python3 >/dev/null 2>&1 || die \
    "python3 not found. Install 'Python 3' from Package Center first."

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "Found Python ${PYVER}"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || die \
    "Python 3.8 or newer is required (found ${PYVER})"

SRC=$(cd "$(dirname "$0")/.." && pwd)
[ -d "${SRC}/dupfinder" ] || die "cannot find the dupfinder/ package next to this script"

echo "Installing ${SRC}/dupfinder -> ${APP_DIR}"
mkdir -p "${APP_DIR}" "${DATA_DIR}"
rm -rf "${APP_DIR}/dupfinder"
cp -r "${SRC}/dupfinder" "${APP_DIR}/dupfinder"
chmod -R a+rX "${APP_DIR}"

# Optional extras. The core scanner is stdlib-only and works without these.
if python3 -m pip --version >/dev/null 2>&1; then
    echo "Installing optional extras (anthropic for AI suggestions, Pillow for image matching)..."
    python3 -m pip install --upgrade --quiet anthropic Pillow || \
        echo "  ...optional install failed; the app still runs, just without AI/image features."
else
    echo "pip is unavailable - skipping optional extras."
    echo "  AI suggestions need: python3 -m pip install anthropic"
fi

echo "Writing ${UNIT}"
sed -e "s#/volume1/apps/dupfinder#${APP_DIR}#" \
    -e "s#/var/services/dupfinder#${DATA_DIR}#" \
    "${SRC}/install/dupfinder.service" > "${UNIT}"

systemctl daemon-reload
systemctl enable ${SERVICE}
systemctl restart ${SERVICE}
sleep 2

if systemctl is-active --quiet ${SERVICE}; then
    IP=$(hostname -i 2>/dev/null | awk '{print $1}')
    echo
    echo "Duplicate Finder is running."
    echo "  URL      : http://${IP:-<nas-ip>}:${PORT}"
    echo "  Data dir : ${DATA_DIR}"
    echo "  Logs     : journalctl -u ${SERVICE} -f"
    echo
    echo "Set an access token before exposing this beyond your LAN:"
    echo "  ${APP_DIR} \$ python3 -m dupfinder serve --token 'choose-something-long'"
    echo "or edit ${DATA_DIR}/config.json and restart the service."
else
    echo "Service failed to start. Check: journalctl -u ${SERVICE} -n 50"
    exit 1
fi
