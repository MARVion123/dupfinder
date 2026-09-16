"""Checks on the interpreter the DSM package picks.

    python3 tests/test_start_stop_status.py

A NAS has several Python interpreters, and which one the service runs decides
which optional features exist: Pillow gives perceptual image matching, the
Anthropic SDK gives AI suggestions. Neither can ship inside the package -
Pillow, pydantic-core and jiter are all compiled per platform, and the .spk is
arch="noarch" - so they are installed alongside, and the package picks the
interpreter that has them.

The bug this file exists for: the choice used to rank on Pillow alone. A venv
carrying the Anthropic SDK lost to any earlier candidate that happened to have
Pillow, so the SDK could be installed perfectly and still never be the one
that ran - and nothing anywhere said so.

The interpreters here are shell stubs that answer the import probes the script
actually sends. PATH is emptied for the run, because `find_python` also
considers whatever `command -v python3` finds, and on a developer machine or
in CI that is a real interpreter with real packages - which would decide the
test instead of the fixtures.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT = os.path.join(REPO, "install", "spk", "scripts", "start-stop-status")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label,
                           "" if ok else "  <- " + detail))
    if not ok:
        FAILURES.append(label)


def extract(text: str, name: str) -> str:
    """One shell function, from its opening line to the closing brace."""
    start = text.index("%s() {" % name)
    end = text.index("\n}", start) + 2
    return text[start:end]


def make_stub(path: str, *, usable: bool = True, pil: bool = False,
              anthropic: bool = False) -> None:
    """An interpreter that answers the three probes the script sends."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "#!/bin/sh\n"
            "case \"$2\" in\n"
            "  *version_info*) exit %d ;;\n"
            "  *'import PIL'*) exit %d ;;\n"
            "  *'import anthropic'*) exit %d ;;\n"
            "esac\n"
            "exit 1\n" % (0 if usable else 1, 0 if pil else 1,
                          0 if anthropic else 1))
    os.chmod(path, 0o755)


def choose(tmp: str, candidates: list[str]) -> str:
    """Run the real find_python over these candidates and return its pick."""
    with open(SCRIPT, encoding="utf-8", newline="") as fh:
        script = fh.read().replace("\r\n", "\n")

    harness = os.path.join(tmp, "harness.sh")
    with open(harness, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("CANDIDATES=\"%s\"\n\n" % "\n".join(candidates))
        fh.write(extract(script, "usable") + "\n\n")
        fh.write(extract(script, "extras_score") + "\n\n")
        fh.write(extract(script, "find_python") + "\n\n")
        fh.write("find_python\n")

    sh = shutil.which("sh") or "/bin/sh"
    leer = os.path.join(tmp, "leer")
    os.makedirs(leer, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = leer                      # no real python3 may join the race
    out = subprocess.run([sh, harness], capture_output=True, text=True,
                         env=env, timeout=60)
    return out.stdout.strip()


def main() -> int:
    if not os.path.exists(SCRIPT):
        print("no start-stop-status at %s" % SCRIPT)
        return 1

    tmp = tempfile.mkdtemp(prefix="sss-test-")
    try:
        nichts = os.path.join(tmp, "py-plain")
        nur_pil = os.path.join(tmp, "py-pil")
        beides = os.path.join(tmp, "py-both")
        nur_sdk = os.path.join(tmp, "py-sdk")
        alt = os.path.join(tmp, "py-ancient")
        make_stub(nichts)
        make_stub(nur_pil, pil=True)
        make_stub(beides, pil=True, anthropic=True)
        make_stub(nur_sdk, anthropic=True)
        make_stub(alt, usable=False, pil=True, anthropic=True)

        print("\nit takes the interpreter that can do the most")
        check("nothing installed: the first usable one",
              choose(tmp, [nichts, nur_pil]) in (nichts, nur_pil))
        check("Pillow beats bare", choose(tmp, [nichts, nur_pil]) == nur_pil,
              choose(tmp, [nichts, nur_pil]))
        check("the SDK alone also beats bare",
              choose(tmp, [nichts, nur_sdk]) == nur_sdk)
        check("both beat either",
              choose(tmp, [nur_pil, beides]) == beides)

        # The regression. Pillow-only came first and used to win outright.
        print("\nthe venv with the SDK is not passed over")
        check("a later interpreter with both wins over an earlier one with Pillow",
              choose(tmp, [nur_pil, nichts, beides]) == beides,
              choose(tmp, [nur_pil, nichts, beides]))
        check("and over an earlier one with the SDK only",
              choose(tmp, [nur_sdk, beides]) == beides)

        print("\nand the old guarantees still hold")
        check("too old is skipped even with everything installed",
              choose(tmp, [alt, nichts]) == nichts,
              choose(tmp, [alt, nichts]))
        check("nothing usable at all: no answer",
              choose(tmp, [os.path.join(tmp, "absent")]) == "")

        # A real tie between two equally equipped interpreters.
        zwilling = os.path.join(tmp, "py-twin")
        make_stub(zwilling, pil=True, anthropic=True)
        check("equal scores: the earlier candidate keeps its place",
              choose(tmp, [beides, zwilling]) == beides,
              choose(tmp, [beides, zwilling]))
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
