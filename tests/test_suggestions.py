"""Checks on the local rule engine that fills the suggestion column.

    python3 tests/test_suggestions.py

Two things are tested, and the second is the reason this file exists.

The verdicts: which copy is kept, which is offered for deletion, and which is
only flagged for review. A wrong verdict here is a file the tool offers to
delete when it should not.

And the cost. The first shape of this ran one SELECT and one committed INSERT
per group. That is invisible on the few hundred groups a test scan makes and
hopeless on a real library: a run over 45,000 groups became 45,000 round-trips
and 45,000 separate transactions, inside the HTTP request the button fires.
Nothing crashed, nothing errored - the button simply looked dead for minutes,
which is the hardest kind of failure to report. So the number of database
round-trips is asserted to be flat, not proportional to the number of groups.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dupfinder.db import Database                        # noqa: E402
from dupfinder.ai import heuristic_suggestions           # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label,
                           "" if ok else "  <- " + detail))
    if not ok:
        FAILURES.append(label)


class Counting:
    """The real Database, with every round-trip counted."""

    def __init__(self, db):
        self._db = db
        self.queries = 0
        self.writes = 0

    def query(self, sql, params=()):
        self.queries += 1
        return self._db.query(sql, params)

    def one(self, sql, params=()):
        self.queries += 1
        return self._db.one(sql, params)

    def execute(self, sql, params=()):
        self.writes += 1
        return self._db.execute(sql, params)

    def executemany(self, sql, seq):
        self.writes += 1
        return self._db.executemany(sql, seq)


def make_scan(db, groups, *, kind="exact", similarity=100.0):
    """One scan. `groups` is a list of lists of (path, size, mtime[, taken_at])."""
    scan_id = db.execute(
        "INSERT INTO scans(root,state,started_at) VALUES(?,?,?)",
        ("/volume1/test", "done", time.time()),
    ).lastrowid
    for members in groups:
        gid = db.execute(
            """INSERT INTO groups(scan_id,kind,similarity,file_count,
                   total_bytes,wasted_bytes,max_size)
               VALUES(?,?,?,?,?,?,?)""",
            (scan_id, kind, similarity, len(members),
             sum(m[1] for m in members), 0, max(m[1] for m in members)),
        ).lastrowid
        for member in members:
            path, size, mtime = member[0], member[1], member[2]
            taken = member[3] if len(member) > 3 else None
            fid = db.execute(
                """INSERT INTO files(scan_id,path,parent,name,ext,size,mtime,taken_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (scan_id, path, os.path.dirname(path), os.path.basename(path),
                 os.path.splitext(path)[1], size, mtime, taken),
            ).lastrowid
            db.execute(
                "INSERT INTO group_members(group_id,file_id,similarity) VALUES(?,?,?)",
                (gid, fid, similarity),
            )
    return scan_id


def verdicts_of(db, scan_id):
    """{path: action} across every suggestion of this scan.

    Keyed by full path, not basename: duplicates routinely share a name, and
    keying by name made one verdict silently overwrite the other.
    """
    out = {}
    names = {r["id"]: r["path"]
             for r in db.query("SELECT id, path FROM files WHERE scan_id=?", (scan_id,))}
    for row in db.query("SELECT verdicts FROM suggestions WHERE scan_id=?", (scan_id,)):
        for fid, verdict in json.loads(row["verdicts"]).items():
            out[names[int(fid)]] = verdict["action"]
    return out


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sugg-test-")
    try:
        db = Database(os.path.join(tmp, "t.db"))
        now = time.time()

        # ---- the verdicts ------------------------------------------------
        print("\nit keeps the better copy and offers the other")
        scan = make_scan(db, [[
            ("/volume1/photo/holiday.jpg", 1000, now),
            ("/volume1/downloads/holiday (1).jpg", 1000, now),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("the curated path is kept", v.get("/volume1/photo/holiday.jpg") == "keep", str(v))
        check("the download with (1) is offered for deletion",
              v.get("/volume1/downloads/holiday (1).jpg") == "delete", str(v))

        print("\na backup copy is never offered for deletion")
        scan = make_scan(db, [[
            ("/volume1/photo/holiday.jpg", 1000, now),
            ("/volume1/backup/2024/holiday.jpg", 1000, now),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("the original is kept", v.get("/volume1/photo/holiday.jpg") == "keep", str(v))
        check("the backup is flagged for review, not deletion",
              "delete" not in v.values(), str(v))

        # The comment inside score_keep says a backup must not win "keep",
        # because promoting it would delete the curated original and leave
        # only the redundant copy. The code never implemented that rule.
        print("\na backup must not win even when the original sits somewhere ugly")
        scan = make_scan(db, [[
            ("/volume1/downloads/report.pdf", 2000, now),
            ("/volume1/backup/report.pdf", 2000, now),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("the only real copy is kept, not the backup",
              v.get("/volume1/downloads/report.pdf") == "keep", str(v))
        check("and the original is never the one offered for deletion",
              v.get("/volume1/downloads/report.pdf") != "delete", str(v))

        print("\nanything short of identical is only flagged")
        scan = make_scan(db, [[
            ("/volume1/video/clip.mp4", 5000, now),
            ("/volume1/video/clip copy.mp4", 5000, now),
        ]], kind="near", similarity=90.0)
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("a 90% match is review, never delete",
              "delete" not in v.values(), str(v))

        print("\na group with one surviving copy gets no suggestion")
        scan = make_scan(db, [[("/volume1/photo/only.jpg", 10, now)]])
        written = heuristic_suggestions(db, scan)
        check("nothing written", written == 0, str(written))

        # ---- which copy is the original ----------------------------------
        JAHR = 365 * 86400

        print("\nby default it keeps the original, not the newest copy")
        scan = make_scan(db, [[
            ("/volume1/media/alt/clip.mp4", 5000, now - 3 * JAHR),
            ("/volume1/media/neu/clip.mp4", 5000, now),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("the three-year-old copy is kept",
              v.get("/volume1/media/alt/clip.mp4") == "keep", str(v))
        check("and the fresh one is offered",
              v.get("/volume1/media/neu/clip.mp4") == "delete", str(v))

        print("\nthe date inside the file beats the file's own timestamp")
        # The original was re-saved last week, so its mtime is the newer one.
        # Its EXIF still says 2019, and that is what a copy cannot fake.
        scan = make_scan(db, [[
            ("/volume1/media/a/img.jpg", 5000, now, now - 5 * JAHR),
            ("/volume1/media/b/img.jpg", 5000, now - 2 * JAHR, now - 2 * JAHR),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("the copy whose EXIF is oldest wins, despite a newer mtime",
              v.get("/volume1/media/a/img.jpg") == "keep", str(v))

        print("\nthe preference can be turned around")
        scan = make_scan(db, [[
            ("/volume1/media/alt/doc.pdf", 5000, now - 3 * JAHR),
            ("/volume1/media/neu/doc.pdf", 5000, now),
        ]])
        heuristic_suggestions(db, scan, prefer="newest")
        v = verdicts_of(db, scan)
        check("asking for the newest keeps the newest",
              v.get("/volume1/media/neu/doc.pdf") == "keep", str(v))

        print("\nan unknown preference falls back, it does not reverse")
        scan = make_scan(db, [[
            ("/volume1/media/alt/x.bin", 5000, now - 3 * JAHR),
            ("/volume1/media/neu/x.bin", 5000, now),
        ]])
        heuristic_suggestions(db, scan, prefer="whatever")
        v = verdicts_of(db, scan)
        check("garbage behaves like the default, not like newest",
              v.get("/volume1/media/alt/x.bin") == "keep", str(v))

        print("\nage is a strong signal, not an overriding one")
        # Documented ordering: age (12) beats a merely good folder name (10)
        # and loses to "(1)" in the filename (25) and a backup path (30).
        scan = make_scan(db, [[
            ("/volume1/neutral/a.jpg", 5000, now - 3 * JAHR),
            ("/volume1/photo/a.jpg", 5000, now),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("older beats a good-looking folder",
              v.get("/volume1/neutral/a.jpg") == "keep", str(v))

        scan = make_scan(db, [[
            ("/volume1/media/b (1).jpg", 5000, now - 3 * JAHR),
            ("/volume1/media/b.jpg", 5000, now),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("but a copy marker still loses, however old",
              v.get("/volume1/media/b.jpg") == "keep", str(v))

        scan = make_scan(db, [[
            ("/volume1/backup/c.jpg", 5000, now - 3 * JAHR),
            ("/volume1/media/c.jpg", 5000, now),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("and the oldest backup never wins either",
              v.get("/volume1/media/c.jpg") == "keep", str(v))

        print("\nthe verdict says why")
        scan = make_scan(db, [[
            ("/volume1/media/d1.jpg", 5000, now - 3 * JAHR, now - 3 * JAHR),
            ("/volume1/media/d2.jpg", 5000, now, now),
        ]])
        heuristic_suggestions(db, scan)
        row = db.one("SELECT verdicts FROM suggestions WHERE scan_id=?", (scan,))
        reasons = [x["reason"] for x in json.loads(row["verdicts"]).values()
                   if x["action"] == "keep"]
        check("the kept copy explains that it is the oldest",
              reasons and "oldest" in reasons[0], str(reasons))
        check("and names the date in the file as the source",
              reasons and "in the file itself" in reasons[0], str(reasons))

        print("\nidentical dates leave the decision to path and name")
        scan = make_scan(db, [[
            ("/volume1/downloads/e.jpg", 5000, now),
            ("/volume1/photo/e.jpg", 5000, now),
        ]])
        heuristic_suggestions(db, scan)
        v = verdicts_of(db, scan)
        check("the better location wins when the clock says nothing",
              v.get("/volume1/photo/e.jpg") == "keep", str(v))

        # ---- the cost ----------------------------------------------------
        print("\nthe work does not grow a round-trip per group")
        klein = make_scan(db, [[("/v/a%d/x.bin" % i, 100, now),
                                ("/v/b%d/x.bin" % i, 100, now)] for i in range(5)])
        gross = make_scan(db, [[("/v/a%d/y.bin" % i, 100, now),
                                ("/v/b%d/y.bin" % i, 100, now)] for i in range(200)])

        c1 = Counting(db)
        heuristic_suggestions(c1, klein)
        c2 = Counting(db)
        heuristic_suggestions(c2, gross)

        check("5 groups and 200 groups cost the same number of reads",
              c1.queries == c2.queries, "%d vs %d" % (c1.queries, c2.queries))
        check("and the same number of writes",
              c1.writes == c2.writes, "%d vs %d" % (c1.writes, c2.writes))
        check("which is a handful, not one per group",
              c2.queries + c2.writes <= 6,
              "%d reads, %d writes for 200 groups" % (c2.queries, c2.writes))
        check("and all 200 were written anyway",
              db.one("SELECT COUNT(*) AS n FROM suggestions WHERE scan_id=?",
                     (gross,))["n"] == 200)

        print("\nrunning it twice changes nothing")
        before = db.one("SELECT COUNT(*) AS n FROM suggestions WHERE scan_id=?",
                        (gross,))["n"]
        heuristic_suggestions(db, gross)
        after = db.one("SELECT COUNT(*) AS n FROM suggestions WHERE scan_id=?",
                       (gross,))["n"]
        check("the row count is unchanged", before == after,
              "%d -> %d" % (before, after))

        print("\nand it can be pointed at single groups")
        gid = db.one("SELECT id FROM groups WHERE scan_id=?", (gross,))["id"]
        n = heuristic_suggestions(db, gross, [gid])
        check("only that group is rewritten", n == 1, str(n))
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
