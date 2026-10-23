"""Pausing a scan, and carrying it on - including across a restart.

    python3 tests/test_pause_resume.py

Stopping a scan used to mean giving it up. The hours were not lost - every
hash is in `hash_cache` and the index in `files`, both on disk - but nothing
offered to use them, and an interrupted scan was recorded as *cancelled*,
which was true about the process and wrong about the data.

A resume does not re-enter the phase it stopped in. It does not need to: the
chain runs again and walks past everything already recorded. What it costs is
one more pass over the directory tree and the arithmetic of grouping.

The trap, and the reason this file exists: groups are INSERTed, not upserted.
Running the chain twice over one scan would report every duplicate twice, and
nothing would complain - the counts would simply be wrong. So a resumed scan
is checked against a plain one over the same tree, and they have to agree.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dupfinder.config import Config                       # noqa: E402
from dupfinder.db import Database                         # noqa: E402
from dupfinder.scanner import ScanEngine                  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label,
                           "" if ok else "  <- " + detail))
    if not ok:
        FAILURES.append(label)


def make_tree(root: str, pairs: int = 400):
    """`pairs` duplicated files, plus one that has no twin.

    Many small files rather than a few large ones. The point is a scan that
    reliably lasts long enough to be paused in the middle, and what makes a
    scan long is the per-file work - a stat, a cache lookup, a row - not the
    bytes.
    """
    a, b = os.path.join(root, "a"), os.path.join(root, "b")
    os.makedirs(a)
    os.makedirs(b)
    for i in range(pairs):
        blob = os.urandom(8_000)
        for folder in (a, b):
            with open(os.path.join(folder, "f%03d.bin" % i), "wb") as fh:
                fh.write(blob)
    with open(os.path.join(a, "alone.bin"), "wb") as fh:
        fh.write(os.urandom(20_000))


def engine_for(tmp: str, name: str):
    cfg = Config(os.path.join(tmp, name))
    cfg.update({"roots_allowlist": [tmp]})
    db = Database(cfg.db_path)
    return cfg, db, ScanEngine(db, cfg)


def wait_idle(engine, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine.status()["state"] != "running":
            return engine.status()
        time.sleep(0.05)
    return engine.status()


def groups_of(db, scan_id):
    return db.one("SELECT COUNT(*) AS n FROM groups WHERE scan_id=?",
                  (scan_id,))["n"]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pause-test-")
    try:
        tree = os.path.join(tmp, "tree")
        make_tree(tree)

        # ---- the reference: one uninterrupted scan ------------------------
        _, ref_db, ref = engine_for(tmp, "ref")
        ref_id = ref.start(tree)
        wait_idle(ref)
        expected = groups_of(ref_db, ref_id)
        print("\na plain scan to compare against")
        check("it found the duplicates", expected > 0, str(expected))

        # ---- pause ------------------------------------------------------
        print("\na paused scan is parked, not written off")
        cfg, db, eng = engine_for(tmp, "run")
        scan_id = eng.start(tree)
        # Wait until groups have actually been written, which is the moment a
        # pause becomes dangerous rather than merely interesting: groups are
        # INSERTed, so a resume that did not clear them first would report
        # every duplicate twice. Pausing earlier passes this file for the
        # wrong reason - there is nothing yet to duplicate.
        for _ in range(4000):
            st = eng.status()
            if st["state"] != "running" or st["groups_found"] > 0:
                break
            time.sleep(0.01)
        check("groups existed before we paused",
              eng.status()["groups_found"] > 0,
              "phase %s, %s groups" % (eng.status()["phase"],
                                       eng.status()["groups_found"]))
        check("pause is accepted while running", eng.pause())
        status = wait_idle(eng)
        check("the thread stopped", status["state"] != "running", status["state"])

        row = db.one("SELECT state FROM scans WHERE id=?", (scan_id,))
        check("the scan is recorded as paused", row["state"] == "paused",
              row["state"])
        check("and it is offered for resuming",
              eng.resumable() is not None)
        check("the file index survived",
              db.one("SELECT COUNT(*) AS n FROM files WHERE scan_id=?",
                     (scan_id,))["n"] > 0)

        # ---- resume -----------------------------------------------------
        print("\ncarrying it on finishes the job")
        again = eng.resume()
        check("it resumes the same scan, not a new one", again == scan_id,
              "%s vs %s" % (again, scan_id))
        status = wait_idle(eng)
        check("and it finished", status["state"] == "done",
              "%s: %s" % (status["state"], status.get("error")))
        check("nothing is offered for resuming any more",
              eng.resumable() is None)

        print("\nand it counts the duplicates once, not twice")
        got = groups_of(db, scan_id)
        check("same group count as an uninterrupted scan", got == expected,
              "%d resumed vs %d plain" % (got, expected))
        check("every group still has at least two members",
              not db.one(
                  """SELECT g.id FROM groups g
                     WHERE g.scan_id=? AND (
                       SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id
                     ) < 2 LIMIT 1""", (scan_id,)))
        check("suggestions were written",
              db.one("SELECT COUNT(*) AS n FROM suggestions WHERE scan_id=?",
                     (scan_id,))["n"] == got)

        # ---- pause twice ------------------------------------------------
        print("\nit can be paused again, and carried on again")
        second = eng.start(tree)
        for _ in range(4000):
            st = eng.status()
            if st["state"] != "running" or st["groups_found"] > 0:
                break
            time.sleep(0.01)
        eng.pause()
        wait_idle(eng)
        eng.resume()
        status = wait_idle(eng)
        check("the second round also finished", status["state"] == "done",
              status["state"])
        check("and still counts once", groups_of(db, second) == expected,
              "%d vs %d" % (groups_of(db, second), expected))

        # ---- a restart --------------------------------------------------
        # The App constructor is what runs when the service comes back up.
        print("\na scan cut short by a restart is resumable, not lost")
        from dupfinder.server import App
        db.execute("UPDATE scans SET state='running', finished_at=NULL WHERE id=?",
                   (second,))
        # Automatic resuming is the next section's subject; here the parking
        # itself is what is being checked, so it is switched off.
        cfg.update({"resume_on_start": False})
        app = App(cfg)
        row = app.db.one("SELECT state, error FROM scans WHERE id=?", (second,))
        check("it comes back as paused, not cancelled", row["state"] == "paused",
              row["state"])
        check("and says why", "restart" in (row["error"] or "").lower(),
              str(row["error"]))
        check("the status offers it", app.api_status()["resumable"] is not None)

        print("\na paused scan is still the one the results show")
        # It used to fall out of this query, so pausing made the table quietly
        # show an older scan while the work sat there untouched.
        check("the latest-scan lookup finds it",
              app._latest_scan_id({}) == second,
              "%s vs %s" % (app._latest_scan_id({}), second))

        print("\ndiscarding withdraws the offer and keeps the data")
        before = db.one("SELECT COUNT(*) AS n FROM files WHERE scan_id=?",
                        (second,))["n"]
        app.api_scan_discard({"scan_id": second})
        check("no longer offered", app.scanner.resumable(second) is None)
        check("the files are still there",
              db.one("SELECT COUNT(*) AS n FROM files WHERE scan_id=?",
                     (second,))["n"] == before)
        check("and so is the hash cache",
              db.one("SELECT COUNT(*) AS n FROM hash_cache")["n"] > 0)

        # ---- what a re-walk must notice --------------------------------
        print("\na resumed scan re-reads what changed and drops what is gone")
        _, db3, eng3 = engine_for(tmp, "changed")
        tree3 = os.path.join(tmp, "tree3")
        make_tree(tree3, pairs=6)
        s3 = eng3.start(tree3)
        wait_idle(eng3)

        edited = os.path.join(tree3, "a", "f000.bin")
        removed = os.path.join(tree3, "b", "f001.bin")
        added = os.path.join(tree3, "a", "brandnew.bin")
        before = db3.one("SELECT md5, size FROM files WHERE scan_id=? AND path=?",
                         (s3, edited))

        with open(edited, "wb") as fh:          # same path, different content
            fh.write(os.urandom(200_000))
        os.utime(edited, (time.time(), time.time()))
        os.remove(removed)
        with open(added, "wb") as fh:
            fh.write(os.urandom(30_000))

        db3.execute("UPDATE scans SET state='paused' WHERE id=?", (s3,))
        eng3.resume(s3)
        wait_idle(eng3)

        after = db3.one("SELECT md5, size, mtime FROM files WHERE scan_id=? AND path=?",
                        (s3, edited))
        # The edited file now has a size nothing else shares, so it is no
        # longer a duplicate candidate and never gets hashed again. What must
        # not happen is the row keeping the hash of the bytes that are gone.
        check("the hash of the old content is dropped",
              after["md5"] is None,
              "%s -> %s" % (before["md5"], after["md5"]))
        check("and its recorded size is the current one",
              after["size"] == os.path.getsize(edited),
              "%s vs %s" % (after["size"], os.path.getsize(edited)))

        gone = db3.one("SELECT status FROM files WHERE scan_id=? AND path=?",
                       (s3, removed))
        check("a file that has gone is marked missing",
              gone and gone["status"] == "missing", str(gone and gone["status"]))

        fresh = db3.one("SELECT id FROM files WHERE scan_id=? AND path=?",
                        (s3, added))
        check("a file that appeared is picked up", fresh is not None)

        # ---- the weekly reboot ------------------------------------------
        print("\nan interrupted scan carries on by itself when asked to")
        cfg3 = Config(os.path.join(tmp, "changed"))
        cfg3.update({"roots_allowlist": [tmp], "resume_on_start": True})
        db3.execute("UPDATE scans SET state='running', finished_at=NULL, error=NULL "
                    "WHERE id=?", (s3,))
        app3 = App(cfg3)
        st3 = wait_idle(app3.scanner)
        check("it started again without being asked",
              st3["scan_id"] == s3 and st3["state"] == "done",
              "%s / %s" % (st3["scan_id"], st3["state"]))

        print("\nbut a deliberate pause is left alone")
        db3.execute("UPDATE scans SET state='paused', finished_at=NULL, error=NULL "
                    "WHERE id=?", (s3,))
        app4 = App(cfg3)
        check("nothing started", app4.scanner.status()["state"] != "running",
              app4.scanner.status()["state"])
        check("and it is still on offer", app4.scanner.resumable(s3) is not None)

        print("\nand the switch turns it off")
        cfg3.update({"resume_on_start": False})
        db3.execute("UPDATE scans SET state='running', finished_at=NULL, error=NULL "
                    "WHERE id=?", (s3,))
        app5 = App(cfg3)
        check("parked, not restarted",
              app5.scanner.status()["state"] != "running"
              and app5.scanner.resumable(s3) is not None)

        # ---- a pause keeps what it already paid for ---------------------
        print("")
        print("a pause keeps the hashes already computed")
        # The flush used to sit after the try/finally, so the batch in
        # progress was discarded on the way out - up to 399 signatures that
        # had already been computed. For video that is three ffmpeg seeks
        # each, recomputed from scratch on the resume.
        from dupfinder import scanner as scanner_mod
        from dupfinder.scanner import Cancelled

        _, db4, eng4 = engine_for(tmp, "partial")
        tree4 = os.path.join(tmp, "tree4")
        os.makedirs(tree4)
        for i in range(12):
            with open(os.path.join(tree4, "f%02d.bin" % i), "wb") as fh:
                fh.write(os.urandom(300_000))   # distinct, so each wants CTPH
        s4 = eng4.start(tree4)
        wait_idle(eng4)

        # Start the pass from nothing, so every candidate has to be hashed.
        db4.execute("DELETE FROM hash_cache")
        db4.execute("UPDATE files SET fuzzy=NULL WHERE scan_id=?", (s4,))

        # Small chunks and a cancel on the second one: exactly one chunk is
        # computed, and it is far short of BATCH, so nothing flushes inside
        # the loop and only the final flush can save it.
        saved_chunk = scanner_mod.FUZZY_CHUNK
        saved_abort = eng4._abort_if_cancelled
        calls = {"n": 0}

        def abort_on_second():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise Cancelled()

        scanner_mod.FUZZY_CHUNK = 4
        eng4._abort_if_cancelled = abort_on_second
        try:
            eng4._fuzzy_pass(s4, [], {"near_threshold": 70,
                                      "image_similarity": False})
            kept = -1
        except Cancelled:
            kept = db4.one("SELECT COUNT(*) AS n FROM hash_cache "
                           "WHERE fuzzy IS NOT NULL")["n"]
        finally:
            scanner_mod.FUZZY_CHUNK = saved_chunk
            eng4._abort_if_cancelled = saved_abort

        check("the pass was actually interrupted", kept >= 0,
              "it ran to the end instead")
        check("and the part-finished batch reached the cache", kept > 0,
              "%d signature(s) survived" % kept)
        on_files = db4.one("SELECT COUNT(*) AS n FROM files "
                           "WHERE scan_id=? AND fuzzy IS NOT NULL", (s4,))["n"]
        check("the files carry the same signatures", on_files == kept,
              "%d on files, %d in cache" % (on_files, kept))

        print("\nand the obvious refusals")
        check("resuming when there is nothing parked fails clearly",
              not_ok(lambda: app.scanner.resume(), ValueError))
        check("pausing when nothing runs is simply False",
              app.scanner.pause() is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print("%d check(s) failed: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


def not_ok(fn, exc_type):
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
