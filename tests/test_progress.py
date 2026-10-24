"""Does the near-duplicate pass say what it is doing while it does it?

    python3 tests/test_progress.py

The pass hands a batch of 256 files to a process pool and drains the result
with `list(pool.map(...))`, which does not return until the last one is done.
Everything the screen shows was updated around that call: the filename once,
before the batch; the counter once, after it.

On images nobody notices - a batch is seconds. On video each file costs an
ffprobe and three ffmpeg seeks, so a batch is half an hour, and for that half
hour the screen showed one filename and a motionless bar. That is
indistinguishable from a scan that has hung, and the only way to find out
which it was, was to wait.

So this file pins down two things that have no other way of being checked:
the filename moves more than once per batch, and the counter reaches the
number of candidates exactly - not twice, which is the obvious way to get a
moving bar wrong, and not short of it, which is what happens when a helper
machine's answers are forgotten.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dupfinder import scanner as scanner_mod             # noqa: E402
from dupfinder.config import Config                      # noqa: E402
from dupfinder.db import Database                        # noqa: E402
from dupfinder.scanner import ScanEngine                 # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label,
                           "" if ok else "  <- " + detail))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="progress-")
    try:
        tree = os.path.join(tmp, "tree")
        os.makedirs(tree)
        files = 12
        for i in range(files):
            with open(os.path.join(tree, "f%02d.bin" % i), "wb") as fh:
                fh.write(os.urandom(300_000))   # distinct, so each wants CTPH

        cfg = Config(os.path.join(tmp, "data"))
        cfg.update({"roots_allowlist": [tmp]})
        db = Database(cfg.db_path)
        eng = ScanEngine(db, cfg)
        scan_id = eng.start(tree)
        while eng.status()["state"] == "running":
            time.sleep(0.05)

        # Run the pass again from nothing, so every candidate has to be
        # hashed rather than read back out of the cache.
        db.execute("DELETE FROM hash_cache")
        db.execute("UPDATE files SET fuzzy=NULL WHERE scan_id=?", (scan_id,))

        # A chunk size small enough that _open_pool actually opens one: it
        # refuses below FUZZY_CHUNK items, so a large chunk silently tests
        # the single-process branch instead - which it did, and the mutation
        # that put the whole batch back in one call sailed through.
        #
        # 4 per batch over 12 files also separates the two behaviours: the
        # old code named one file per batch, so three in all.
        saved_chunk = scanner_mod.FUZZY_CHUNK
        scanner_mod.FUZZY_CHUNK = 4

        seen_paths: list[str] = []
        used_pool: list[bool] = []
        real_set = eng._set
        real_run = eng._run_locally

        def recording_run(pool, jobs, progress=None):
            # The note the pool logs is overwritten later in the pass, so
            # reading it back afterwards says nothing. Record the argument.
            used_pool.append(pool is not None)
            return real_run(pool, jobs, progress)

        def recording_set(**kw):
            # The pass also clears the field between stages, and an empty
            # string is not a file being worked on.
            if kw.get("current"):
                seen_paths.append(kw["current"])
            return real_set(**kw)

        eng._set = recording_set
        eng._run_locally = recording_run
        try:
            eng._fuzzy_pass(scan_id, [], {"near_threshold": 70,
                                          "image_similarity": False})
        finally:
            eng._set = real_set
            eng._run_locally = real_run
            scanner_mod.FUZZY_CHUNK = saved_chunk

        print("")
        pooled = any(used_pool)
        print("hashed in %s" % ("worker processes" if pooled
                                else "one process (too few cores for a pool)"))
        check("the worker-pool path is the one under test", pooled,
              "no pool opened - this machine has too few cores, and the "
              "single-process branch was tested instead")

        print("")
        print("the filename moves while a batch is being hashed")
        distinct = len(set(seen_paths))
        check("more than one file is named during one batch", distinct > 1,
              "%d distinct name(s) from %d update(s)" % (distinct, len(seen_paths)))
        check("every candidate gets its turn", distinct == files,
              "%d of %d" % (distinct, files))

        print("")
        print("and the counter lands exactly on the work done")
        done = eng.status()["done"]
        check("counted once each, not twice", done == files,
              "%d counted, %d candidates" % (done, files))

        print("")
        print("a cached candidate is counted too, without being hashed")
        # Second run over the same tree: everything is in the cache now, so
        # nothing is hashed - and the bar still has to reach the end.
        eng._status["done"] = 0
        eng._fuzzy_pass(scan_id, [], {"near_threshold": 70,
                                      "image_similarity": False})
        done = eng.status()["done"]
        check("a pass that hashes nothing still finishes its count",
              done == files, "%d counted, %d candidates" % (done, files))
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
