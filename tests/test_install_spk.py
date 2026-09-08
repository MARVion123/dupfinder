"""Checks on the DSM installer script.

    python3 tests/test_install_spk.py

Two things are worth guarding here, and neither is caught by `sh -n`.

The first is the refusal to install while a scan is running. Installing
uninstalls first, and uninstalling kills the scan mid-phase; on a library
this size that can throw away days. The guard reads the scan state over
HTTP, so it is tested against a real server that answers the way the real
one does - including the case that matters most, a server that does not
answer at all. A first install on a NAS with nothing running must not be
mistaken for "a scan is running".

The second is that the state is parsed out of JSON with sed. That is fine
for one stable field and awful to get wrong: an earlier parser in this
project guessed a format from a screenshot and quietly matched nothing,
and the stub it was tested against encoded the same wrong guess, so the
test proved nothing. Hence a real server here, not a fixture string.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT = os.path.join(REPO, "install", "spk", "install-spk.sh")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label,
                           "" if ok else "  <- " + detail))
    if not ok:
        FAILURES.append(label)


# --------------------------------------------------------------------------
# a server that answers like the real one

class _Handler(BaseHTTPRequestHandler):
    state = {"scan": "running"}
    cancel_delay = 0.0

    def log_message(self, *args):        # noqa: D102 - silence the access log
        pass

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                    # noqa: N802 - http.server's spelling
        # The real payload, key order included: "scan_id" sits right after
        # "state" and must not be mistaken for the "scan" object itself.
        self._json({
            "version": "1.0.0",
            "uptime": 244621.0,
            "scan": {"state": self.state["scan"], "scan_id": 13,
                     "root": "/volume1/OnlyFans", "phase": "fuzzy"},
            "ai": {"state": "idle"},
        })

    def do_POST(self):                   # noqa: N802
        if self.path != "/api/scan/cancel":
            self.send_response(404)
            self.end_headers()
            return
        # The workers finish the file they are on, so the state flips a
        # moment later. A guard that only looked once would race past this.
        threading.Timer(self.cancel_delay,
                        lambda: self.state.__setitem__("scan", "cancelled")
                        ).start()
        self._json({"cancelled": True})


class Server:
    def __init__(self, scan_state: str = "running", cancel_delay: float = 0.0):
        _Handler.state = {"scan": scan_state}
        _Handler.cancel_delay = cancel_delay
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self) -> "Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------
# a copy of the script that stops before it would touch DSM

def harness(port: int, tmp: str) -> str:
    """The real script, cut off where it would hand the file to synopkg.

    Everything above that line is what this file tests: argument handling,
    the download, the checksum, the scan guard. Below it lies `synopkg`,
    which exists only on a NAS.
    """
    with open(SCRIPT, encoding="utf-8", newline="") as fh:
        text = fh.read()

    text = text.replace('[ "$(id -u)" = "0" ] || die "run this with sudo"',
                        "# root check removed for the test")
    text = text.replace("PORT=8777", "PORT=%d" % port)
    text = text.replace(
        'LATEST=0\nCANCEL=0\nSPK=""',
        'report() { echo "STUB report"; }\n'
        'clean()  { echo "STUB clean"; }\n\n'
        'LATEST=0\nCANCEL=0\nSPK=""')

    cut = text.index('say "checking the package before handing it to DSM"')
    text = text[:cut] + 'echo "REACHED-INSTALL ${SPK}"\nexit 0\n'

    path = os.path.join(tmp, "harness.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def run(script: str, *args: str, env: dict | None = None):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["sh", script, *args], capture_output=True,
                          text=True, env=merged, timeout=300)


# --------------------------------------------------------------------------

def main() -> int:
    if not os.path.exists(SCRIPT):
        print("no install-spk.sh at %s" % SCRIPT)
        return 1

    print("\nthe script parses as a POSIX shell script")
    syntax = subprocess.run(["sh", "-n", SCRIPT], capture_output=True,
                            text=True)
    check("sh -n", syntax.returncode == 0, syntax.stderr.strip())

    # A stray control byte here is not theoretical: a backreference in a sed
    # expression was once written into this file as 0x01, which `sh -n`
    # accepts happily and which silently turns every match into an empty
    # string.
    with open(SCRIPT, "rb") as fh:
        raw = fh.read()
    stray = sorted({b for b in raw if b < 9 or b in (11, 12, 13)})
    check("no stray control bytes", not stray,
          "found " + ", ".join(hex(b) for b in stray))

    tmp = tempfile.mkdtemp(prefix="install-spk-test-")
    spk = os.path.join(tmp, "some.spk")
    with open(spk, "wb") as fh:
        fh.write(b"not a real package, and never read by these paths")

    try:
        # ---- arguments -------------------------------------------------
        print("\nit refuses what it cannot act on")
        with Server(scan_state="idle") as srv:
            h = harness(srv.port, tmp)

            r = run(h)
            check("no arguments", r.returncode == 1, r.stderr.strip())

            r = run(h, "--bogus")
            check("an unknown option", r.returncode == 1
                  and "unknown option" in r.stderr, r.stderr.strip())

            r = run(h, "--latest", spk)
            check("--latest together with a file", r.returncode == 1
                  and "not both" in r.stderr, r.stderr.strip())

            r = run(h, os.path.join(tmp, "absent.spk"))
            check("a file that is not there", r.returncode == 1
                  and "no such file" in r.stderr, r.stderr.strip())

            r = run(h, spk)
            check("an existing file gets through to the install",
                  r.returncode == 0 and "REACHED-INSTALL" in r.stdout,
                  r.stdout.strip() + r.stderr.strip())

        # ---- the guard -------------------------------------------------
        print("\nit will not install on top of a running scan")
        with Server(scan_state="running") as srv:
            h = harness(srv.port, tmp)
            r = run(h, spk)
            check("a running scan stops the install", r.returncode == 1
                  and "a scan is running" in r.stderr, r.stderr.strip())
            check("and it says how to proceed",
                  "--force" in r.stderr, r.stderr.strip())

        print("\n--force cancels it first, and waits for it to stop")
        with Server(scan_state="running", cancel_delay=2.0) as srv:
            h = harness(srv.port, tmp)
            started = time.time()
            r = run(h, "--force", spk)
            waited = time.time() - started
            check("the install proceeds", r.returncode == 0
                  and "REACHED-INSTALL" in r.stdout,
                  r.stdout.strip() + r.stderr.strip())
            check("it waited for the scan to actually stop", waited >= 2.0,
                  "returned after %.1fs" % waited)

        # This is the one that matters on a first install: no server at all
        # must read as "nothing is running", never as "a scan is running".
        print("\na NAS with nothing listening is not a running scan")
        free = HTTPServer(("127.0.0.1", 0), _Handler)
        port = free.server_address[1]
        free.server_close()
        h = harness(port, tmp)
        r = run(h, spk)
        check("no server means the install proceeds", r.returncode == 0
              and "REACHED-INSTALL" in r.stdout,
              r.stdout.strip() + r.stderr.strip())

    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print("%d check(s) failed: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
