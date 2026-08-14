"""Checks on the DSM package.

    python3 tests/test_spk.py

Builds the .spk and takes it apart again. The point of this file is one bug:
for several builds the payload was run through a CRLF rewrite meant for the
shell scripts, which silently damaged the gzip stream. The package looked
right in every way you would think to check - correct members, correct size,
plausible INFO - and DSM's only response was "failed to acquire postinst
worker". It also came and went between builds, because whether a compressed
stream contains the byte pair 0x0D 0x0A is essentially a coin toss.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "install", "spk"))

import build_spk                                        # noqa: E402


FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("ok" if condition else "FAIL", name,
                           "" if condition else "   <- " + detail))
    if not condition:
        FAILURES.append(name)


def rejects(name: str, spk: str, checksum: str) -> None:
    """verify() must refuse this file."""
    try:
        build_spk.verify(spk, checksum)
    except Exception as exc:                            # noqa: BLE001 - any refusal counts
        check(name, True)
        print("       (%s: %s)" % (type(exc).__name__, str(exc)[:70]))
    else:
        check(name, False, "verify() accepted a broken package")


def rewrite_payload(src: str, dst: str, mangle) -> str:
    """Copy a .spk, mangling package.tgz, and return the new checksum.

    The checksum in INFO is left alone; the caller decides whether to pass the
    old or the new one, which is how the two independent guards get tested
    separately.
    """
    digest = ""
    with tarfile.open(src) as old, tarfile.open(dst, "w") as new:
        for member in old.getmembers():
            data = old.extractfile(member).read()
            if member.name == "package.tgz":
                data = mangle(data)
                digest = hashlib.md5(data).hexdigest()
            member.size = len(data)
            new.addfile(member, io.BytesIO(data))
    return digest


def gzip_containing_crlf() -> bytes:
    """A valid gzip stream that definitely contains the byte pair \\r\\n.

    Built rather than assumed: the payload of any given build may happen not to
    contain the pair, in which case the CRLF rewrite is a no-op and a test
    using it would pass while proving nothing. That very nearly happened here.
    """
    rng = random.Random(20260814)
    for _ in range(200):
        blob = gzip.compress(bytes(rng.randrange(256) for _ in range(4096)))
        if b"\r\n" in blob:
            return blob
    raise AssertionError("could not construct a gzip stream containing CRLF")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="spk-test-")
    try:
        print("building")
        result = subprocess.run(
            [sys.executable, os.path.join(REPO, "install", "spk", "build_spk.py")],
            capture_output=True, text=True)
        check("build succeeds", result.returncode == 0, result.stderr.strip()[:200])
        if result.returncode != 0:
            return 1

        spk = os.path.join(
            REPO, "install", "spk", "dist",
            "dupfinder-%s-%s.spk" % (build_spk.read_version(), build_spk.BUILD_NUMBER))
        check("package exists", os.path.isfile(spk), spk)

        print("\nstructure")
        with tarfile.open(spk) as archive:
            names = archive.getnames()
            info = archive.extractfile("INFO").read().decode("utf-8")
            payload = archive.extractfile("package.tgz").read()
        check("INFO is the first member", names[0] == "INFO", names[0])
        check("conf/ ships privilege and resource",
              {"conf/privilege", "conf/resource"} <= set(names))

        fields = dict(
            line.split("=", 1) for line in info.strip().splitlines() if "=" in line)
        fields = {k: v.strip('"') for k, v in fields.items()}
        check("INFO carries a checksum", bool(fields.get("checksum")))
        check("checksum matches the payload",
              fields.get("checksum") == hashlib.md5(payload).hexdigest())
        check("support_conf_folder is set", fields.get("support_conf_folder") == "yes",
              "DSM ignores conf/ without it")
        check("dsmuidir points at the payload", fields.get("dsmuidir") == "ui")
        check("dsmappname is set", bool(fields.get("dsmappname")))
        check("startable", fields.get("startable") == "yes")

        # A URL in `description` is dead text; these are the fields Package
        # Center actually turns into links.
        for field in ("maintainer_url", "distributor_url", "helpurl", "support_url"):
            value = fields.get(field, "")
            check("%s is a link" % field,
                  value.startswith("https://") and " " not in value, value or "(missing)")
        check("German description present", bool(fields.get("description_ger")))

        print("\npayload")
        plain = gzip.decompress(payload)
        with tarfile.open(fileobj=io.BytesIO(plain)) as inner:
            members = set(inner.getnames())
            shortcut = json.loads(inner.extractfile("ui/config").read().decode("utf-8"))
            firewall = inner.extractfile("ui/dupfinder.sc").read().decode("utf-8")
        check("application is present", "dupfinder/__main__.py" in members)
        check("static files are present", "dupfinder/static/app.js" in members)
        check("the licence travels with the software", "LICENSE" in members,
              "a .spk is a distribution; PolyForm obliges it to carry the terms")
        check("shortcut config is present", "ui/config" in members)
        for size in build_spk.UI_ICON_SIZES:
            check("icon %d px" % size, "ui/images/dupfinder-%d.png" % size in members)

        print("\ncross-file agreement")
        check("shortcut id matches dsmappname",
              fields["dsmappname"] in shortcut[".url"],
              "INFO says %s, ui/config defines %s"
              % (fields["dsmappname"], ", ".join(shortcut[".url"])))
        entry = shortcut[".url"].get(fields["dsmappname"], {})
        check("shortcut port matches adminport",
              entry["port"] == fields["adminport"],
              "%s vs %s" % (entry["port"], fields.get("adminport")))
        check("firewall rule matches the port",
              'dst.ports="%s/tcp"' % fields["adminport"] in firewall, firewall.strip())
        check("no placeholder survived", "@PORT@" not in plain.decode("latin-1"))

        print("\nverify() accepts a good package")
        try:
            build_spk.verify(spk, fields["checksum"])
            check("clean package passes", True)
        except Exception as exc:                        # noqa: BLE001
            check("clean package passes", False, str(exc))

        print("\nverify() refuses damaged packages")
        bad = os.path.join(tmp, "bad.spk")

        rewrite_payload(spk, bad, lambda d: d[:2000] + bytes([d[2000] ^ 0xFF]) + d[2001:])
        rejects("flipped byte, checksum in INFO unchanged", bad, fields["checksum"])

        digest = rewrite_payload(
            spk, bad, lambda d: d[:2000] + bytes([d[2000] ^ 0xFF]) + d[2001:])
        rejects("flipped byte, checksum recomputed to match", bad, digest)

        # The original bug, reproduced exactly: a payload that does contain
        # CRLF, put through the rewrite, with a checksum that agrees. Only the
        # decompression guard can catch this one.
        digest = rewrite_payload(
            spk, bad, lambda _: gzip_containing_crlf().replace(b"\r\n", b"\n"))
        rejects("CRLF rewrite of the payload", bad, digest)

        truncated = rewrite_payload(spk, bad, lambda d: d[:len(d) // 2])
        rejects("truncated payload", bad, truncated)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print("%d check(s) failed: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
