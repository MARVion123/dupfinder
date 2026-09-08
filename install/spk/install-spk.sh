#!/bin/sh
# Install the Duplicate File Finder package on DSM 7, over SSH, as root.
#
#   sudo sh install-spk.sh --latest           # fetch the newest release and install it
#   sudo sh install-spk.sh --latest --force   # ... cancelling a running scan first
#   sudo sh install-spk.sh dupfinder-1.0.0-0015.spk
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
RELEASE_URL=${RELEASE_URL:-https://github.com/MARVion123/dupfinder/releases/latest/download}

die() { echo "error: $*" >&2; exit 1; }
say() { echo; echo "== $*"; }

# Every mktemp -d gets appended here. A single trap removes them all: two
# separate `trap ... EXIT` lines would mean the second silently replaces the
# first, and the download would be left behind on every run.
TMPDIRS=""
trap 'for d in ${TMPDIRS}; do rm -rf "${d}"; done' EXIT

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

# The scan state, or nothing at all when the server does not answer - which is
# the normal case on a first install and must not be read as "a scan is running".
scan_state() {
    curl -s -m 5 "http://127.0.0.1:${PORT}/api/status" 2>/dev/null \
        | tr ',' '\n' \
        | grep '"scan"' \
        | sed -n 's/.*"state": "\([a-z]*\)".*/\1/p'
}

fetch_latest() {
    say "fetching the latest release"
    command -v curl >/dev/null 2>&1 || die "curl is not available on this NAS"

    DL=$(mktemp -d)
    TMPDIRS="${TMPDIRS} ${DL}"

    curl -fLsS -o "${DL}/dupfinder.spk" "${RELEASE_URL}/dupfinder.spk" \
        || die "could not download ${RELEASE_URL}/dupfinder.spk"

    # The published checksum catches a truncated or intercepted download here,
    # where the message can say so. Without it the next failure would be
    # "package.tgz is corrupt", which blames the build rather than the transfer.
    if curl -fLsS -o "${DL}/SHA256SUMS" "${RELEASE_URL}/SHA256SUMS" 2>/dev/null; then
        want=$(sed -n 's/^\([0-9a-f]*\)  dupfinder.spk$/\1/p' "${DL}/SHA256SUMS")
        got=$(sha256sum "${DL}/dupfinder.spk" | cut -d' ' -f1)
        if [ -z "${want}" ]; then
            echo "   SHA256SUMS has no line for dupfinder.spk - skipping that check"
        elif [ "${want}" != "${got}" ]; then
            die "the download does not match the published checksum
   published: ${want}
   received : ${got}
   Do not install this file. Try again; if it persists, say so."
        else
            echo "   sha256 matches the published checksum"
        fi
    else
        echo "   no SHA256SUMS published - relying on the checksum inside the package"
    fi

    SPK="${DL}/dupfinder.spk"
    if tar xf "${SPK}" -C "${DL}" INFO 2>/dev/null; then
        echo "   downloaded $(sed -n 's/^version="\(.*\)"$/\1/p' "${DL}/INFO")"
    fi
}

LATEST=0
CANCEL=0
SPK=""
while [ $# -gt 0 ]; do
    case "$1" in
        --status) report; exit 0 ;;
        --clean)  clean;  exit 0 ;;
        --latest) LATEST=1 ;;
        --force)  CANCEL=1 ;;
        -*)       die "unknown option: $1" ;;
        *)        SPK=$1 ;;
    esac
    shift
done

if [ "${LATEST}" = "1" ]; then
    [ -z "${SPK}" ] || die "give me either --latest or a file, not both"
    fetch_latest
elif [ -z "${SPK}" ]; then
    die "give me the .spk file or --latest, or --clean / --status"
fi
[ -f "${SPK}" ] || die "no such file: ${SPK}"

# Installing uninstalls first, and uninstalling kills a running scan mid-phase:
# its progress would never reach the database. Refuse by default; --force
# cancels it properly, which does write the progress out.
state=$(scan_state)
if [ "${state}" = "running" ]; then
    if [ "${CANCEL}" = "1" ]; then
        say "a scan is running - cancelling it before touching the package"
        curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' \
            "http://127.0.0.1:${PORT}/api/scan/cancel" >/dev/null \
            || die "could not reach the cancel endpoint"
        i=0
        while [ ${i} -lt 180 ]; do
            if [ "$(scan_state)" != "running" ]; then
                break
            fi
            sleep 1
            i=$((i + 1))
        done
        if [ "$(scan_state)" = "running" ]; then
            die "the scan did not stop within three minutes - stop it in the web interface"
        fi
        echo "   stopped. Its progress and hash cache are in the database."
    else
        die "a scan is running.
   Installing would kill it mid-phase and lose the progress since the last write.
   Stop it in the web interface, or re-run with --force to cancel it cleanly first."
    fi
fi

say "checking the package before handing it to DSM"
# A corrupted payload is the failure mode that cost the most time here: DSM
# reports "failed to acquire postinst worker", which says nothing about the
# archive. Check it now, where the message can be plain.
TMP=$(mktemp -d)
TMPDIRS="${TMPDIRS} ${TMP}"
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

say "what is now installed"
sed -n 's/^version=/   version=/p' "/var/packages/${PKG}/INFO" 2>/dev/null \
    || echo "   could not read /var/packages/${PKG}/INFO"

echo
echo "Package Center should now show Duplicate File Finder with Stop and Open,"
echo "and the DSM main menu should have an icon you can drag onto the desktop."
echo "If the icon is missing, sign out of DSM and back in - the menu is cached."
