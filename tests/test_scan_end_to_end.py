"""A real scan over a real tree, start to finish.

    python3 tests/test_scan_end_to_end.py

Every other Python test here drives one piece with the rest stubbed. That
leaves the seams untested, and the seams are where this broke: adding a
`taken_at` column widened one INSERT, two other passes kept writing the old
shorter row, and the scan died with "Incorrect number of bindings supplied".
Nothing in the unit tests noticed. The only thing that caught it was running
a scan - which until now meant node, Playwright and a browser, so it did not
run in CI at all.

So this file scans a handful of real files on disk and asserts the whole
chain: files indexed, duplicates grouped, suggestions written, and the date
inside a photo carried from the file through the cache into the verdict.
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


def write_photo(path: str, taken: str | None):
    """A tiny JPEG, optionally carrying an EXIF date. False if Pillow is absent."""
    try:
        from PIL import Image
    except ImportError:
        return False
    # Large enough to behave like a photograph. A thumbnail-sized image gives
    # the perceptual hash almost nothing to work with, and a test built on one
    # measures the fixture rather than the tool.
    im = Image.new("RGB", (640, 480), (90, 140, 200))
    for x in range(0, 640, 3):          # structure, so the hash is not flat
        for y in range(0, 480, 7):
            im.putpixel((x, y), (x % 256, y % 256, 200))
    if taken:
        exif = Image.Exif()
        exif[0x0132] = taken            # TIFF DateTime: "YYYY:MM:DD HH:MM:SS"
        im.save(path, "JPEG", quality=92, exif=exif)
    else:
        im.save(path, "JPEG", quality=92)
    return True


def run_scan(engine, root, timeout=180):
    scan_id = engine.start(root)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine.status()["state"] != "running":
            break
        time.sleep(0.1)
    return scan_id, engine.status()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="scan-e2e-")
    try:
        tree = os.path.join(tmp, "tree")
        left, right = os.path.join(tree, "left"), os.path.join(tree, "right")
        os.makedirs(left)
        os.makedirs(right)

        blob = os.urandom(400_000)
        for folder in (left, right):
            with open(os.path.join(folder, "same.bin"), "wb") as fh:
                fh.write(blob)
        with open(os.path.join(left, "alone.bin"), "wb") as fh:
            fh.write(os.urandom(50_000))

        # The same picture, saved twice with different EXIF dates. They must
        # differ: two byte-identical copies carry identical embedded dates, so
        # for an exact duplicate the content date cannot separate them and only
        # mtime can. The date in the file earns its keep on *re-saved* copies,
        # which is what this pair is - same pixels, different bytes, matched by
        # the perceptual hash.
        #
        # The file dates then say the opposite of the EXIF dates, so a pass
        # that quietly fell back to mtime would pick the wrong copy and this
        # test would notice.
        photos = write_photo(os.path.join(left, "pic.jpg"), "2019:07:14 18:22:05")
        if photos:
            write_photo(os.path.join(right, "pic.jpg"), "2024:11:02 09:10:11")
            now_ts = time.time()
            os.utime(os.path.join(left, "pic.jpg"), (now_ts, now_ts))
            older = now_ts - 400 * 86400
            os.utime(os.path.join(right, "pic.jpg"), (older, older))

        cfg = Config(os.path.join(tmp, "data"))
        cfg.update({"roots_allowlist": [tmp]})
        db = Database(cfg.db_path)
        engine = ScanEngine(db, cfg)

        print("\na scan of a real tree runs to the end")
        scan_id, status = run_scan(engine, tree)
        check("it finished", status["state"] == "done",
              "%s: %s" % (status["state"], status.get("error")))
        check("no error recorded", not status.get("error"), str(status.get("error")))

        expected_files = 5 if photos else 3
        seen = db.one("SELECT COUNT(*) AS n FROM files WHERE scan_id=?",
                      (scan_id,))["n"]
        check("every file was indexed", seen == expected_files,
              "%d of %d" % (seen, expected_files))

        print("\nit found the duplicates and nothing else")
        groups = db.query(
            "SELECT id, kind FROM groups WHERE scan_id=?", (scan_id,))
        expected_groups = 2 if photos else 1
        check("one group per duplicated content", len(groups) == expected_groups,
              "%d groups" % len(groups))
        check("the file with no twin is not in one",
              not db.one(
                  """SELECT 1 AS x FROM group_members m JOIN files f ON f.id=m.file_id
                     WHERE f.scan_id=? AND f.name='alone.bin'""", (scan_id,)))

        print("\nevery group got a suggestion, without being asked")
        written = db.one("SELECT COUNT(*) AS n FROM suggestions WHERE scan_id=?",
                         (scan_id,))["n"]
        check("suggestions written by the scan itself",
              written == len(groups), "%d for %d groups" % (written, len(groups)))

        if not photos:
            print("\n  (Pillow absent - the EXIF part of this test is skipped)")
        else:
            print("\nthe date inside the photo reached the database")
            row = db.one(
                "SELECT taken_at FROM files WHERE scan_id=? AND path=?",
                (scan_id, os.path.join(left, "pic.jpg")))
            check("taken_at is set from EXIF", row and row["taken_at"],
                  str(row and row["taken_at"]))

            print("\nand decided which copy to keep, against the file dates")
            import json
            gid = db.one(
                """SELECT m.group_id AS g FROM group_members m
                   JOIN files f ON f.id = m.file_id
                   WHERE f.scan_id=? AND f.name='pic.jpg' LIMIT 1""",
                (scan_id,))["g"]
            sugg = db.one("SELECT verdicts FROM suggestions WHERE group_id=?", (gid,))
            verdicts = json.loads(sugg["verdicts"])
            names = {r["id"]: r["path"] for r in db.query(
                "SELECT id, path FROM files WHERE scan_id=?", (scan_id,))}
            kept = [names[int(fid)] for fid, v in verdicts.items()
                    if v["action"] == "keep"]
            check("exactly one copy is kept", len(kept) == 1, str(kept))
            check("and it is the one whose EXIF is older, although its mtime "
                  "is the newer of the two",
                  kept and kept[0] == os.path.join(left, "pic.jpg"), str(kept))

            print("\na second scan keeps the date through the cache")
            scan2, status2 = run_scan(engine, tree)
            check("the rescan also finished", status2["state"] == "done",
                  "%s: %s" % (status2["state"], status2.get("error")))
            row2 = db.one(
                "SELECT taken_at FROM files WHERE scan_id=? AND path=?",
                (scan2, os.path.join(left, "pic.jpg")))
            check("taken_at survived a cache hit", row2 and row2["taken_at"],
                  "a cached rescan must not lose it: " + str(row2 and row2["taken_at"]))
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
