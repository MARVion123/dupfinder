"""The two secrets this tool holds, and where they must not turn up.

    python3 tests/test_secrets.py

An Anthropic API key is a billable credential. The auth token is what stands
between the open port and every file on the volumes. Both live in clear text
in one JSON file next to the database, because there is nowhere better on a
DSM package - and that makes two questions worth settling in a test rather
than in a code review.

Who can read the file. `save()` used to write it with whatever the umask
said, which on DSM is 0644 - world-readable, on a machine that by design has
other packages, other accounts and shared folders on it.

And whether either value ever reaches the browser. The settings dialog has
to show that a key is present without showing the key, so `public()` replaces
it with the word "set". A refactor that returned the raw dict instead would
look harmless, work perfectly, and put the key in the page source.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dupfinder.config import Config                      # noqa: E402

FAILURES: list[str] = []
KEY = "sk-ant-test-0000000000000000000000000000"
TOKEN = "a-token-nobody-should-see"


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label,
                           "" if ok else "  <- " + detail))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="secrets-")
    try:
        cfg = Config(tmp)
        cfg.update({"anthropic_api_key": KEY, "auth_token": TOKEN})

        print("")
        print("the file holding them is readable by its owner only")
        # Windows has no permission bits, so the mode cannot be read back
        # there - and leaving the check to CI means the one thing this file
        # exists for is unverifiable on the machine it is written on. So the
        # call itself is checked everywhere, and the resulting mode where
        # the filesystem has one.
        asked = []
        real_chmod = os.chmod

        def recording_chmod(path, mode, *a, **kw):
            asked.append((os.path.basename(path), mode))
            return real_chmod(path, mode, *a, **kw)

        os.chmod = recording_chmod
        try:
            cfg.update({"ai_max_groups": 7})            # any change, to save
        finally:
            os.chmod = real_chmod

        check("saving asks for owner-only permissions",
              any(mode == 0o600 for _name, mode in asked), str(asked))
        check("and asks for it on the temporary file, before the move",
              any(name.endswith(".tmp") and mode == 0o600
                  for name, mode in asked), str(asked))

        if os.name == "nt":
            print("  ..   (Windows has no permission bits to read back)")
        else:
            mode = stat.S_IMODE(os.stat(cfg.path).st_mode)
            check("no group or world access", not (mode & 0o077),
                  "mode is %o" % mode)
            check("the owner can still read and write it",
                  mode & stat.S_IRUSR and mode & stat.S_IWUSR,
                  "mode is %o" % mode)

        # The temporary file is where this used to leak: written with the
        # umask's mode and only then moved into place.
        leftovers = [n for n in os.listdir(tmp) if n.endswith(".tmp")]
        check("no temporary copy is left behind", not leftovers, str(leftovers))

        print("")
        print("neither secret reaches the browser")
        public = cfg.public()
        as_json = json.dumps(public)
        check("the key is replaced by a marker",
              public["anthropic_api_key"] == "set",
              str(public["anthropic_api_key"]))
        check("and the key itself is nowhere in the payload",
              KEY not in as_json)
        check("the token is replaced too", public["auth_token"] == "set",
              str(public["auth_token"]))
        check("and the token itself is nowhere in the payload",
              TOKEN not in as_json)

        print("")
        print("but they are still there for the service itself")
        check("the key reads back", cfg.api_key() == KEY)
        check("and it survives a reload",
              Config(tmp).api_key() == KEY)

        print("")
        print("an empty key falls back to the environment")
        cfg.update({"anthropic_api_key": ""})
        saved = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-from-the-environment"
        try:
            check("the environment is used when the field is blank",
                  cfg.api_key() == "sk-ant-from-the-environment",
                  cfg.api_key())
            check("and public() still says a key is present",
                  cfg.public()["anthropic_api_key"] == "set")
        finally:
            if saved is None:
                del os.environ["ANTHROPIC_API_KEY"]
            else:
                os.environ["ANTHROPIC_API_KEY"] = saved

        check("with neither, it reports no key", not cfg.api_key(),
              repr(cfg.api_key()))

        print("")
        print("a broken SDK is reported as broken, not as ready")
        # An empty `anthropic` directory on the path imports fine as a
        # namespace package and carries none of the names. availability()
        # only tried the import, so it answered ok - and the first request
        # died with "module 'anthropic' has no attribute 'Anthropic'" after
        # the user had already gone and made an API key.
        import types
        from dupfinder.ai import AIEngine

        cfg.update({"anthropic_api_key": KEY, "ai_enabled": True})
        engine = AIEngine(None, cfg)

        hollow = types.ModuleType("anthropic")
        hollow.__file__ = "/somewhere/anthropic/__init__.py"
        real = sys.modules.get("anthropic")
        sys.modules["anthropic"] = hollow
        try:
            verdict = engine.availability()
            check("it is not called ready", verdict["ok"] is False,
                  str(verdict))
            check("and the reason says what is wrong",
                  "no Anthropic class" in verdict["reason"],
                  verdict["reason"])
            check("and names where the module was found",
                  "/somewhere/anthropic" in verdict["reason"],
                  verdict["reason"])
            check("a real path points at pip, not at permissions",
                  "pip install" in verdict["reason"]
                  and "chmod" not in verdict["reason"],
                  verdict["reason"])

            # The case that actually happened: the package is installed and
            # complete, but mode 700 root:root, so the service user cannot
            # read it. Python then imports the directory as a namespace
            # package - __file__ is None and nothing is in it. Telling
            # somebody to reinstall here is wrong and destructive: the
            # install is fine, the permissions are not.
            del hollow.__file__
            hollow.__path__ = ["/venv/lib/python3.11/site-packages/anthropic"]
            verdict = engine.availability()
            check("an unreadable package is not called ready",
                  verdict["ok"] is False, str(verdict))
            check("and is named a permission problem",
                  "permission" in verdict["reason"].lower(), verdict["reason"])
            check("with chmod, not a reinstall",
                  "chmod" in verdict["reason"]
                  and "pip install" not in verdict["reason"],
                  verdict["reason"])
            check("and it names the directory it could not read",
                  "site-packages/anthropic" in verdict["reason"],
                  verdict["reason"])
            hollow.__file__ = "/somewhere/anthropic/__init__.py"

            # And the opposite: a module that does carry the class passes.
            hollow.Anthropic = object
            verdict = engine.availability()
            check("a module with the class is accepted",
                  verdict["ok"] is True, str(verdict))
        finally:
            if real is None:
                sys.modules.pop("anthropic", None)
            else:
                sys.modules["anthropic"] = real
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
