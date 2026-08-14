#!/usr/bin/env python3
"""Build a Synology DSM 7 package (.spk) for Duplicate File Finder.

    python3 install/spk/build_spk.py

Produces install/spk/dist/dupfinder-<version>-<build>.spk

A .spk is an uncompressed tar containing INFO, package.tgz (the payload
extracted to /var/packages/dupfinder/target), the control scripts, the conf
directory and two PNG icons. Everything is assembled here rather than with tar
so the executable bits on the scripts survive being built on Windows.

Layout produced:

    INFO                        metadata, including the md5 of package.tgz
    PACKAGE_ICON.PNG            64x64, shown in Package Center
    PACKAGE_ICON_256.PNG        256x256
    package.tgz
        dupfinder/              the application itself
        ui/config               the DSM main-menu and desktop shortcut
        ui/dupfinder.sc         firewall port description
        ui/images/*.png         shortcut icons, seven sizes
    scripts/*                   install and start-stop-status scripts
    conf/privilege              which user the service runs as
    conf/resource               points DSM at ui/dupfinder.sc

The shape of this package follows SynoCommunity's spksrc, which is the only
DSM 7 packaging that is demonstrably in the field. Five things were wrong for
long enough to be worth naming, since none of them produces a usable error
message:

  * package.tgz was written through the same helper as the shell scripts, so
    it had its CRLF pairs rewritten - inside a gzip stream. The payload was
    corrupt and DSM could not unpack it. This is the one that mattered, and it
    appeared and vanished between builds, because whether compressed output
    happens to contain 0x0D 0x0A is close to a coin toss. tests/test_spk.py
    exists for this.
  * INFO had no `checksum`. DSM validates package.tgz against it.
  * INFO had no `support_conf_folder="yes"` while the package shipped conf/.
  * conf/resource pointed `protocol-file` at "conf/dupfinder.sc". The path is
    resolved inside the installed package, not inside the .spk, so the file has
    to live in the payload.
  * PACKAGE_ICON.PNG was 72x72. That is the DSM 6 size; DSM 7 wants 64x64.
"""

from __future__ import annotations

import gzip
import hashlib
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
BUILD_NUMBER = "0012"

# Where Package Center's Help and publisher links point.
HOMEPAGE = "https://marvion123.github.io/dupfinder/"

# Names the entry in ui/config. DSM uses it for the main-menu shortcut and for
# Package Center's "Open" button, so INFO and ui/config have to agree on it.
DSM_APP_NAME = "com.marvion.dupfinder"

# The single source of truth for the port. Substituted into INFO, into
# ui/config (the shortcut), into ui/dupfinder.sc (the firewall rule) and into
# the start script, so the port DSM advertises and the port the daemon binds
# cannot drift apart. Change it here if 8777 clashes, then rebuild.
PORT = "8777"

# Sizes DSM asks for when drawing the shortcut: menu, desktop, search results.
UI_ICON_SIZES = (16, 24, 32, 48, 64, 72, 256)


# --- INFO ------------------------------------------------------------------

def read_version() -> str:
    ns: dict = {}
    with open(os.path.join(REPO, PKG_NAME, "__init__.py"), "r", encoding="utf-8") as fh:
        exec(fh.read(), ns)          # noqa: S102 - trivial, our own file
    return ns["__version__"]


def build_info(version: str, checksum: str) -> str:
    fields = [
        ("package", PKG_NAME),
        ("version", "%s-%s" % (version, BUILD_NUMBER)),
        ("os_min_ver", "7.0-40000"),
        ("arch", "noarch"),
        ("displayname", "Duplicate File Finder"),
        ("description",
         "Find duplicate and near-duplicate files anywhere under a directory "
         "you choose, see how similar they are, and remove the copies you tick. "
         "Deletions are quarantined and reversible by default."),
        # Package Center picks the description matching the user's DSM
        # language. `ger` is Synology's code for German, not `de`.
        ("description_ger",
         "Findet doppelte und ähnliche Dateien unterhalb eines Ordners deiner "
         "Wahl, zeigt an, wie ähnlich sie sind, und entfernt die Kopien, die du "
         "ankreuzt. Löschungen wandern standardmäßig in Quarantäne und lassen "
         "sich zurückholen."),
        ("maintainer", "MARVion123"),
        # These four are the only way a package gets a clickable link in
        # Package Center - `description` is plain text and a URL in it stays
        # dead. maintainer_url shows immediately; helpurl, support_url and
        # distributor_url appear once the package is installed.
        ("maintainer_url", "https://github.com/MARVion123"),
        ("distributor", "MARVion123"),
        ("distributor_url", HOMEPAGE),
        ("helpurl", HOMEPAGE),
        ("support_url", "https://github.com/MARVion123/dupfinder/issues"),
        ("thirdparty", "yes"),
        # Synology's docs call `startable` deprecated since 6.1-14907 (use
        # `ctl_stop`) and say both default to "yes". Empirically that is wrong
        # for DSM 7.2: builds 0002/0003 carried this field and installed;
        # dropping it in 0004 made every install fail with error 276 "failed to
        # acquire postinst worker" and `synopkg start` answer 272 "Package is
        # not startable". DSM evidently uses its presence to decide the package
        # has a service at all. Do not remove it again.
        ("startable", "yes"),
        # The main-menu / desktop shortcut, and what "Open" launches.
        ("dsmuidir", "ui"),
        ("dsmappname", DSM_APP_NAME),
        # Redundant with dsmappname for the Open button, but harmless and every
        # shipping package carries them.
        ("adminprotocol", "http"),
        ("adminport", PORT),
        ("adminurl", "/"),
        # Required whenever the package ships a conf/ directory. Without it DSM
        # silently ignores privilege and resource.
        ("support_conf_folder", "yes"),
        ("silent_install", "no"),
        ("silent_upgrade", "no"),
        ("silent_uninstall", "no"),
        ("support_center", "no"),
        ("beta", "no"),
        ("checksum", checksum),
    ]
    return "".join('%s="%s"\n' % (key, value) for key, value in fields)


# --- payload ---------------------------------------------------------------

def _substitute(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"@PORT@", PORT.encode())


def build_payload(tmp: str) -> tuple[str, str]:
    """package.tgz - extracted to /var/packages/dupfinder/target on install.

    Returns (path, md5) - INFO has to carry the checksum of the exact bytes.
    """
    stage = os.path.join(tmp, "stage")
    os.makedirs(stage)

    shutil.copytree(
        os.path.join(REPO, PKG_NAME), os.path.join(stage, PKG_NAME),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))

    # The licence has to travel with the software. PolyForm's Notices clause
    # obliges anyone handing on any part of it to hand on the terms - or their
    # URL - and the `Required Notice:` lines as well, and a .spk handed to a
    # stranger is exactly that. Builds up to 0011 shipped without either.
    shutil.copyfile(os.path.join(REPO, "LICENSE"), os.path.join(stage, "LICENSE"))

    ui = os.path.join(stage, "ui")
    os.makedirs(os.path.join(ui, "images"))
    for name in ("config", "%s.sc" % PKG_NAME):
        with open(os.path.join(HERE, "ui", name), "rb") as fh:
            body = _substitute(fh.read())
        with open(os.path.join(ui, name), "wb") as fh:
            fh.write(body)
    # Fail the build rather than shipping a config DSM will choke on.
    with open(os.path.join(ui, "config"), "r", encoding="utf-8") as fh:
        shortcut = json.load(fh)
    assert DSM_APP_NAME in shortcut[".url"], "ui/config and DSM_APP_NAME disagree"

    for size in UI_ICON_SIZES:
        with open(os.path.join(ui, "images", "%s-%d.png" % (PKG_NAME, size)), "wb") as fh:
            fh.write(build_icon(size))

    path = os.path.join(tmp, "package.tgz")

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mode = 0o755 if info.isdir() else 0o644
        return info

    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
        for entry in sorted(os.listdir(stage)):
            tar.add(os.path.join(stage, entry), arcname=entry, filter=keep)

    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return path, digest.hexdigest()


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
    # 16px is too small for the supersampled detail to survive; keep the scale
    # down there so the shape stays legible instead of turning to mush.
    scale = 4 if size >= 32 else 8
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


_ICON_CACHE: dict[int, bytes] = {}


def build_icon(size: int) -> bytes:
    # Nine icons come out of this and three of the sizes are asked for twice.
    if size not in _ICON_CACHE:
        _ICON_CACHE[size] = _png(size, _render(size))
    return _ICON_CACHE[size]


# --- verification ----------------------------------------------------------

def verify(spk_path: str, checksum: str) -> None:
    """Open the finished file the way DSM will and check it holds together.

    Worth the twenty lines: the bug this replaces produced a .spk that looked
    perfectly normal - right size, right members, right INFO - and whose
    payload simply would not decompress. Nothing in the build complained, and
    DSM's only response was "failed to acquire postinst worker".
    """
    required = {"INFO", "PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG", "package.tgz",
                "conf/privilege", "conf/resource", "scripts/start-stop-status",
                "LICENSE"}
    with tarfile.open(spk_path, "r") as spk:
        names = spk.getnames()
        missing = required - set(names)
        if missing:
            raise AssertionError("missing from the package: %s" % ", ".join(sorted(missing)))
        if names[0] != "INFO":
            raise AssertionError("INFO must be the first member, found %r" % names[0])

        payload = spk.extractfile("package.tgz").read()
        if hashlib.md5(payload).hexdigest() != checksum:
            raise AssertionError("package.tgz does not match the checksum in INFO")

        # Decompress the whole stream in one go rather than handing the bytes
        # to tarfile with mode="r:gz". tarfile stops at the end-of-archive
        # marker and never reads the gzip trailer, so it happily lists a
        # damaged archive; gzip.decompress checks CRC32 and length and does
        # not. Tested against a deliberately corrupted build, which tarfile
        # accepted without complaint.
        plain = gzip.decompress(payload)
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as inner:
            members = inner.getnames()
        for wanted in ("dupfinder/__main__.py", "ui/config", "ui/%s.sc" % PKG_NAME,
                       "ui/images/%s-256.png" % PKG_NAME, "LICENSE"):
            if wanted not in members:
                raise AssertionError("missing from the payload: %s" % wanted)

        shipped = spk.extractfile("LICENSE").read().decode("utf-8")
        if "Required Notice:" not in shipped.splitlines()[0]:
            raise AssertionError("the shipped LICENSE does not start with the Required Notice")

        # A stray carriage return in a shell script makes DSM's shell fail with
        # "\r: not found", which reads like the script is missing.
        for name in names:
            if name.startswith(("scripts/", "conf/")):
                if b"\r" in spk.extractfile(name).read():
                    raise AssertionError("%s still has CRLF line endings" % name)


# --- assembly --------------------------------------------------------------

def main() -> int:
    version = read_version()
    dist = os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)
    spk_path = os.path.join(
        dist, "%s-%s-%s.spk" % (PKG_NAME, version, BUILD_NUMBER))

    tmp = tempfile.mkdtemp(prefix="spk-")
    try:
        payload, checksum = build_payload(tmp)
        info = build_info(version, checksum)

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
                """Text files only - it rewrites line endings and @PORT@."""
                with open(path, "rb") as fh:
                    add_bytes(name, _substitute(fh.read()), mode)

            # INFO first so DSM can read the metadata without unpacking the
            # whole archive.
            add_bytes("INFO", info.encode("utf-8"))
            add_bytes("PACKAGE_ICON.PNG", build_icon(64))
            add_bytes("PACKAGE_ICON_256.PNG", build_icon(256))
            # Verbatim, and emphatically not through add_file. Until this build
            # the payload went through the same CRLF rewrite as the shell
            # scripts, so every 0x0D 0x0A pair inside the gzip stream was
            # silently turned into a single 0x0A. A compressed archive of this
            # size contains that pair with near certainty, which corrupted
            # package.tgz - differently on every build, since it depends on the
            # compressed bytes. That is the "failed to acquire postinst worker"
            # that came and went for no visible reason.
            with open(payload, "rb") as fh:
                add_bytes("package.tgz", fh.read())

            # The replace/upgrade scripts are no-ops but must be present: DSM
            # takes the replace path whenever any previous install of this
            # package exists, and a missing script there fails the whole
            # install with "failed to acquire postinst worker".
            for name in ("preinst", "postinst", "preuninst", "postuninst",
                         "preupgrade", "postupgrade", "prereplace", "postreplace",
                         "start-stop-status"):
                add_file("scripts/" + name,
                         os.path.join(HERE, "scripts", name), mode=0o755)

            for name in ("privilege", "resource"):
                add_file("conf/" + name, os.path.join(HERE, "conf", name))

            # DSM shows a LICENSE at the root of the .spk during installation,
            # so the terms are in front of the person installing it rather than
            # buried in the payload they may never look at.
            add_file("LICENSE", os.path.join(REPO, "LICENSE"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    verify(spk_path, checksum)

    print("built %s (%.1f KiB)" % (spk_path, os.path.getsize(spk_path) / 1024.0))
    print("  payload md5 : %s  (verified, decompresses cleanly)" % checksum)
    print("  port        : %s" % PORT)
    print("  shortcut    : %s" % DSM_APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
