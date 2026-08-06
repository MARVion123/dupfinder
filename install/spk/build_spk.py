#!/usr/bin/env python3
"""Build a Synology DSM 7 package (.spk) for Duplicate File Finder.

    python3 install/spk/build_spk.py

Produces install/spk/dist/dupfinder-<version>.spk

A .spk is an uncompressed tar containing INFO (first), package.tgz (the payload
extracted to /var/packages/dupfinder/target), the control scripts, the conf
directory and two PNG icons. Everything is assembled here rather than with tar
so the executable bits on the scripts survive being built on Windows.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

PKG_NAME = "dupfinder"
BUILD_NUMBER = "0008"

# The single source of truth for the port. Substituted into INFO (so Package
# Center's "Open" button points at it), into conf/dupfinder.sc (so DSM opens
# it in the firewall) and into the start script (passed to `serve --port`).
# Change it here if 8777 clashes with something else on the NAS, then rebuild.
PORT = "8777"

# --- INFO ------------------------------------------------------------------

def read_version() -> str:
    ns: dict = {}
    with open(os.path.join(REPO, PKG_NAME, "__init__.py"), "r", encoding="utf-8") as fh:
        exec(fh.read(), ns)          # noqa: S102 - trivial, our own file
    return ns["__version__"]


def build_info(version: str) -> str:
    fields = [
        ("package", PKG_NAME),
        ("version", "%s-%s" % (version, BUILD_NUMBER)),
        ("os_min_ver", "7.0-40000"),
        ("arch", "noarch"),
        ("displayname", "Duplicate File Finder for Synology NAS"),
        ("description",
         "Find duplicate and near-duplicate files anywhere under a directory "
         "you choose, see how similar they are, and remove the copies you tick. "
         "Deletions are quarantined and reversible by default."),
        ("maintainer", "dupfinder"),
        ("thirdparty", "yes"),
        # Synology's docs call `startable` deprecated since 6.1-14907 (use
        # `ctl_stop`) and say both default to "yes". Empirically that is wrong
        # for DSM 7.2: builds 0002/0003 carried this field and installed;
        # dropping it in 0004 made every install fail with error 276 "failed to
        # acquire postinst worker" and `synopkg start` answer 272 "Package is
        # not startable". DSM evidently uses its presence to decide the package
        # has a service at all. Do not remove it again.
        ("startable", "yes"),
        ("silent_install", "no"),
        ("silent_upgrade", "no"),
        ("silent_uninstall", "no"),
        ("support_center", "no"),
        ("beta", "no"),
        # Gives Package Center an "Open" button pointing at the web UI.
        ("adminprotocol", "http"),
        ("adminport", PORT),
        ("adminurl", "/"),
    ]
    return "".join('%s="%s"\n' % (key, value) for key, value in fields)


# --- payload ---------------------------------------------------------------

def build_payload(tmp: str) -> str:
    """package.tgz - extracted to /var/packages/dupfinder/target on install."""
    path = os.path.join(tmp, "package.tgz")
    source = os.path.join(REPO, PKG_NAME)

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        base = os.path.basename(info.name)
        if base == "__pycache__" or base.endswith((".pyc", ".pyo")):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mode = 0o755 if info.isdir() else 0o644
        return info

    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
        tar.add(source, arcname=PKG_NAME, filter=keep)
    return path


# --- icons -----------------------------------------------------------------
# Rendered at 4x and box-downsampled, which is enough anti-aliasing for an icon
# and avoids a Pillow dependency just to build a package.

def _blend(dst: bytearray, index: int, colour: tuple[int, int, int], alpha: float) -> None:
    for channel in range(3):
        dst[index + channel] = int(
            dst[index + channel] * (1.0 - alpha) + colour[channel] * alpha)
    dst[index + 3] = int(dst[index + 3] * (1.0 - alpha) + 255 * alpha)


def _rounded_rect(buf: bytearray, width: int, box, radius: float,
                  colour: tuple[int, int, int], alpha: float = 1.0) -> None:
    x0, y0, x1, y1 = box
    for y in range(max(0, int(y0)), min(width, int(y1) + 1)):
        for x in range(max(0, int(x0)), min(width, int(x1) + 1)):
            # Distance test only matters near the four corners.
            cx = x0 + radius if x < x0 + radius else (x1 - radius if x > x1 - radius else x)
            cy = y0 + radius if y < y0 + radius else (y1 - radius if y > y1 - radius else y)
            if (x - cx) ** 2 + (y - cy) ** 2 > radius * radius:
                continue
            _blend(buf, (y * width + x) * 4, colour, alpha)


def _render(size: int) -> bytes:
    scale = 4
    big = size * scale
    buf = bytearray(big * big * 4)

    unit = big / 100.0
    # Background tile.
    _rounded_rect(buf, big, (0, 0, big - 1, big - 1), 22 * unit, (37, 71, 122))
    # Back sheet, then front sheet offset toward the top-left.
    _rounded_rect(buf, big, (40 * unit, 30 * unit, 78 * unit, 82 * unit),
                  4 * unit, (150, 178, 220))
    _rounded_rect(buf, big, (22 * unit, 18 * unit, 60 * unit, 70 * unit),
                  4 * unit, (247, 250, 255))
    # Text lines on the front sheet.
    for row in range(4):
        top = (27 + row * 9) * unit
        right = (52 if row % 2 == 0 else 45) * unit
        _rounded_rect(buf, big, (29 * unit, top, right, top + 3.5 * unit),
                      1.7 * unit, (120, 140, 170))
    # Accent bar marking the "duplicate" pair.
    _rounded_rect(buf, big, (40 * unit, 74 * unit, 78 * unit, 82 * unit),
                  4 * unit, (240, 160, 60))

    # Box downsample.
    out = bytearray(size * size * 4)
    area = scale * scale
    for y in range(size):
        for x in range(size):
            totals = [0, 0, 0, 0]
            for sy in range(scale):
                row = ((y * scale + sy) * big + x * scale) * 4
                for sx in range(scale):
                    px = row + sx * 4
                    for channel in range(4):
                        totals[channel] += buf[px + channel]
            index = (y * size + x) * 4
            for channel in range(4):
                out[index + channel] = totals[channel] // area
    return bytes(out)


def _png(size: int, pixels: bytes) -> bytes:
    raw = b"".join(b"\x00" + pixels[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def build_icon(size: int) -> bytes:
    return _png(size, _render(size))


# --- assembly --------------------------------------------------------------

def main() -> int:
    version = read_version()
    dist = os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)
    spk_path = os.path.join(dist, "%s-%s-%s.spk" % (PKG_NAME, version, BUILD_NUMBER))

    tmp = tempfile.mkdtemp(prefix="spk-")
    try:
        payload = build_payload(tmp)
        info = build_info(version)

        # Validate the JSON conf files before shipping them - a malformed
        # privilege or resource file fails the install with a useless error.
        for name in ("privilege", "resource"):
            with open(os.path.join(HERE, "conf", name), "r", encoding="utf-8") as fh:
                json.load(fh)

        with tarfile.open(spk_path, "w", format=tarfile.USTAR_FORMAT) as spk:

            def add_bytes(name: str, data: bytes, mode: int = 0o644) -> None:
                info_obj = tarfile.TarInfo(name)
                info_obj.size = len(data)
                info_obj.mode = mode
                info_obj.uid = info_obj.gid = 0
                info_obj.uname = info_obj.gname = "root"
                spk.addfile(info_obj, io.BytesIO(data))

            def add_file(name: str, path: str, mode: int = 0o644) -> None:
                with open(path, "rb") as fh:
                    data = fh.read().replace(b"\r\n", b"\n")
                add_bytes(name, data.replace(b"@PORT@", PORT.encode()), mode)

            # INFO must come first so DSM can read the metadata without
            # unpacking the whole archive.
            add_bytes("INFO", info.encode("utf-8"))
            add_bytes("PACKAGE_ICON.PNG", build_icon(72))
            add_bytes("PACKAGE_ICON_256.PNG", build_icon(256))
            add_file("package.tgz", payload)

            # The replace/upgrade scripts are no-ops but must be present: DSM
            # takes the replace path whenever any previous install of this
            # package exists, and a missing script there fails the whole
            # install with "failed to acquire postinst worker".
            for name in ("preinst", "postinst", "preuninst", "postuninst",
                         "preupgrade", "postupgrade", "prereplace", "postreplace",
                         "start-stop-status"):
                add_file("scripts/" + name,
                         os.path.join(HERE, "scripts", name), mode=0o755)

            for name in ("privilege", "resource", "dupfinder.sc"):
                add_file("conf/" + name, os.path.join(HERE, "conf", name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("built %s (%.1f KiB)" % (spk_path, os.path.getsize(spk_path) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
