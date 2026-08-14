#!/bin/sh
# Install the Duplicate File Finder package on DSM 7, over SSH, as root.
#
#   sudo sh install-spk.sh dupfinder-1.0.0-0010.spk
#   sudo sh install-spk.sh --clean            # only remove the leftovers
#   sudo sh install-spk.sh --status           # only report what DSM thinks
#
# Package Center's "Manual Install" does the same job, but silently: when it
# fails it says "failed to install" and nothing else. This prints what DSM
# actually reports at every step.
#
# The --clean pass exists because a half-failed install leaves DSM in a state
# where it contradicts itself: `synopkg list` shows the package while
# `synopkg status` answers "non_installed / failed to locate given package".
# From then on every further install fails too, because DSM believes the
# package is already there. Nothing but removing the directories fixes it.

set -e

PKG=dupfinder
PORT=8777

die() { echo "error: $*" >&2; exit 1; }
say() { echo; echo "== $*"; }

[ "$(id -u)" = "0" ] || die "run this with sudo"

report() {
    say "what DSM currently thinks"
    echo "-- synopkg list"
    synopkg list 2>&1 | grep -i "${PKG}" || echo "   (not listed)"
    echo "-- synopkg status"
    synopkg status "${PKG}" 2>&1 || true
    echo "-- directories"
    for dir in "/var/packages/${PKG}" /volume*/@appstore/"${PKG}" \
               /volume*/@appdata/"${PKG}" /volume*/@appconf/"${PKG}" \
               /volume*/@apphome/"${PKG}" /volume*/@apptemp/"${PKG}"; do
        [ -e "${dir}" ] && echo "   present: ${dir}"
    done
    echo "-- systemd unit"
    systemctl is-active "pkgctl-${PKG}" 2>&1 || true
    echo "-- port ${PORT}"
    netstat -tlnp 2>/dev/null | grep ":${PORT} " || echo "   free"
}

clean() {
    say "removing every trace of previous attempts"

    # The systemd service from the non-package install binds the same port and
    # would fight the package for it.
    if systemctl is-enabled "${PKG}" >/dev/null 2>&1 || \
       systemctl is-active  "${PKG}" >/dev/null 2>&1; then
        echo "-- stopping the systemd service (the non-package install)"
        systemctl stop "${PKG}" 2>/dev/null || true
        systemctl disable "${PKG}" 2>/dev/null || true
        echo "   stopped. It is not deleted - re-enable it with:"
        echo "   systemctl enable ${PKG} && systemctl start ${PKG}"
    fi

    echo "-- synopkg uninstall (may well fail; that is the point)"
    synopkg stop "${PKG}"      >/dev/null 2>&1 || true
    synopkg uninstall "${PKG}" 2>&1 || echo "   uninstall refused, removing by hand"

    echo "-- directories"
    # @appdata holds the database. Keep it: reinstalling should not cost a scan
    # that took seventeen hours. Everything else is DSM's own bookkeeping and
    # is rebuilt on install.
    for dir in "/var/packages/${PKG}" /volume*/@appstore/"${PKG}" \
               /volume*/@appconf/"${PKG}" /volume*/@apphome/"${PKG}" \
               /volume*/@apptemp/"${PKG}"; do
        if [ -e "${dir}" ]; then
            rm -rf "${dir}" && echo "   removed ${dir}"
        fi
    done
    for dir in /volume*/@appdata/"${PKG}"; do
        [ -e "${dir}" ] && echo "   kept    ${dir}  (your database)"
    done

    echo "-- DSM UI link"
    rm -rf "/usr/syno/synoman/webman/3rdparty/${PKG}" 2>/dev/null || true

    echo "-- done. DSM should now consider the package absent:"
    synopkg status "${PKG}" 2>&1 || true
}

case "$1" in
    --status) report; exit 0 ;;
    --clean)  clean;  exit 0 ;;
    "")       die "give me the .spk file, or --clean / --status" ;;
esac

SPK=$1
[ -f "${SPK}" ] || die "no such file: ${SPK}"

say "checking the package before handing it to DSM"
# A corrupted payload is the failure mode that cost the most time here: DSM
# reports "failed to acquire postinst worker", which says nothing about the
# archive. Check it now, where the message can be plain.
TMP=$(mktemp -d)
trap 'rm -rf "${TMP}"' EXIT
tar xf "${SPK}" -C "${TMP}" INFO package.tgz 2>/dev/null \
    || die "${SPK} is not a readable .spk (INFO and package.tgz missing)"
gzip -t "${TMP}/package.tgz" 2>/dev/null \
    || die "package.tgz inside ${SPK} is corrupt. Rebuild the package; do not install this file."

WANT=$(sed -n 's/^checksum="\(.*\)"$/\1/p' "${TMP}/INFO")
GOT=$(md5sum "${TMP}/package.tgz" | cut -d' ' -f1)
if [ -n "${WANT}" ] && [ "${WANT}" != "${GOT}" ]; then
    die "checksum in INFO (${WANT}) does not match the payload (${GOT})"
fi
echo "   payload decompresses, checksum matches"
sed -n 's/^\(package\|version\|arch\|os_min_ver\|dsmuidir\|dsmappname\)=/   &/p' "${TMP}/INFO"

clean

say "installing"
synopkg install "${SPK}" 2>&1 || die "install failed - see /var/log/synopkg.log"

say "starting"
synopkg start "${PKG}" 2>&1 || die "start failed - see /var/packages/${PKG}/var/dupfinder.log"

say "result"
synopkg status "${PKG}" 2>&1 || true
sleep 3
if netstat -tln 2>/dev/null | grep -q ":${PORT} "; then
    echo "   listening on ${PORT}"
else
    echo "   NOT listening on ${PORT} yet - check /var/packages/${PKG}/var/dupfinder.log"
fi

echo
echo "Package Center should now show Duplicate File Finder with Stop and Open,"
echo "and the DSM main menu should have an icon you can drag onto the desktop."
echo "If the icon is missing, sign out of DSM and back in - the menu is cached."
