"""Checks on comparing one folder tree against another.

    python3 tests/test_compare_trees.py

Grouping has always ignored folder structure - groups come from content, and
paths play no part in forming them. What was missing is the question people
actually have: *what under A also exists under B*. The search box matches one
member; this needs a group that reaches into both trees at once.

Two things here are easy to get wrong and silent when wrong:

A prefix must be anchored. "/volume1/photo" matching "/volume1/photos" would
quietly widen a comparison the user asked to narrow, and the extra rows look
exactly like real answers.

And the pattern must be escaped. A folder called "100%_raw" contains two LIKE
wildcards; unescaped it matches folders that have nothing to do with it.

The endpoints run against a real database here. A fake one would accept any
SQL and prove nothing about either.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dupfinder.config import Config                       # noqa: E402
from dupfinder.server import App, prefix_like, truncate_path   # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label,
                           "" if ok else "  <- " + detail))
    if not ok:
        FAILURES.append(label)


def add_group(db, scan_id, paths, size=1000):
    """One duplicate group holding these paths."""
    gid = db.execute(
        """INSERT INTO groups(scan_id,kind,similarity,file_count,
               total_bytes,wasted_bytes,max_size)
           VALUES(?,?,?,?,?,?,?)""",
        (scan_id, "exact", 100.0, len(paths), size * len(paths), size, size),
    ).lastrowid
    for path in paths:
        fid = db.execute(
            """INSERT INTO files(scan_id,path,parent,name,ext,size,mtime)
               VALUES(?,?,?,?,?,?,?)""",
            (scan_id, path, os.path.dirname(path), os.path.basename(path),
             os.path.splitext(path)[1], size, time.time()),
        ).lastrowid
        db.execute(
            "INSERT INTO group_members(group_id,file_id,similarity) VALUES(?,?,?)",
            (gid, fid, 100.0),
        )
    return gid


def paths_of(app, result):
    """Every path in every returned group, flattened."""
    out = []
    for group in result["groups"]:
        out.extend(f["path"] for f in group["files"])
    return sorted(out)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="compare-test-")
    try:
        app = App(Config(tmp))
        db = app.db
        scan = db.execute(
            "INSERT INTO scans(root,state,started_at) VALUES(?,?,?)",
            ("/volume1", "done", time.time()),
        ).lastrowid

        spanning = add_group(db, scan, ["/volume1/old/a/x.mp4", "/volume1/new/p/x.mp4"])
        add_group(db, scan, ["/volume1/old/a/y.mp4", "/volume1/old/b/y.mp4"])
        add_group(db, scan, ["/volume1/new/p/z.mp4", "/volume1/new/q/z.mp4"])

        # For the anchoring check: photo and photos are different folders.
        add_group(db, scan, ["/volume1/photo/p.jpg", "/volume1/zzz/p.jpg"])
        add_group(db, scan, ["/volume1/photos/q.jpg", "/volume1/zzz/q.jpg"])

        # For the escaping check: both of these contain LIKE wildcards.
        add_group(db, scan, ["/volume1/100%_raw/r.jpg", "/volume1/zzz/r.jpg"])
        add_group(db, scan, ["/volume1/100ZZZQraw/s.jpg", "/volume1/zzz/s.jpg"])

        # A single file lying under both A and B must not satisfy both halves.
        add_group(db, scan, ["/volume1/old/a/v.mp4", "/volume1/other/v.mp4"])

        def groups(**kw):
            q = {"scan_id": [str(scan)], "limit": ["200"]}
            q.update({k: [v] for k, v in kw.items()})
            return app.api_groups(q)

        print("\nit answers what under A also exists under B")
        r = groups(a="/volume1/old", b="/volume1/new")
        check("exactly the group that spans both", r["total"] == 1, str(r["total"]))
        check("and it is the right one",
              paths_of(app, r) == ["/volume1/new/p/x.mp4", "/volume1/old/a/x.mp4"],
              str(paths_of(app, r)))

        print("\nduplicates living inside one tree are not the question")
        ids = [g["id"] for g in r["groups"]]
        check("the A-only group is left out", spanning in ids and len(ids) == 1,
              str(ids))

        print("\na prefix is anchored, not a substring")
        r = groups(a="/volume1/photo", b="/volume1/zzz")
        check("photo does not drag in photos",
              paths_of(app, r) == ["/volume1/photo/p.jpg", "/volume1/zzz/p.jpg"],
              str(paths_of(app, r)))
        r = groups(a="/volume1/photos", b="/volume1/zzz")
        check("and photos finds only itself",
              paths_of(app, r) == ["/volume1/photos/q.jpg", "/volume1/zzz/q.jpg"],
              str(paths_of(app, r)))

        print("\nwildcards in a folder name stay literal")
        r = groups(a="/volume1/100%_raw", b="/volume1/zzz")
        check("the per-cent and underscore match themselves",
              paths_of(app, r) == ["/volume1/100%_raw/r.jpg", "/volume1/zzz/r.jpg"],
              str(paths_of(app, r)))
        check("and not the folder they would match as wildcards",
              "/volume1/100ZZZQraw/s.jpg" not in paths_of(app, r))

        print("\none file cannot be both sides of its own comparison")
        r = groups(a="/volume1/old", b="/volume1/old/a")
        check("a group whose only A-member is the B-member is not a match",
              all("/volume1/other/v.mp4" not in p for p in paths_of(app, r)),
              str(paths_of(app, r)))

        print("\na half comparison is ignored, not half applied")
        check("A alone returns everything", groups(a="/volume1/old")["total"] == 8,
              str(groups(a="/volume1/old")["total"]))
        check("B alone too", groups(b="/volume1/new")["total"] == 8)

        print("\nnothing in common means nothing is shown")
        check("an empty answer, not an error",
              groups(a="/volume1/old", b="/volume1/nowhere")["total"] == 0)

        # ---- folder pairs, rolled up ------------------------------------
        print("\nthe folder view can answer at tree level")
        leaf = app.api_folders({"scan_id": [str(scan)], "depth": ["0"]})
        rolled = app.api_folders({"scan_id": [str(scan)], "depth": ["2"]})

        leaf_pairs = {(p["a"], p["b"]) for p in leaf["pairs"]}
        check("at folder level old/a and new/p are one of several pairs",
              ("/volume1/new/p", "/volume1/old/a") in leaf_pairs
              or ("/volume1/old/a", "/volume1/new/p") in leaf_pairs,
              str(sorted(leaf_pairs)))

        rolled_pairs = {(p["a"], p["b"]) for p in rolled["pairs"]}
        check("rolled up, old against new is a single row",
              ("/volume1/new", "/volume1/old") in rolled_pairs,
              str(sorted(rolled_pairs)))
        check("and pairs inside one tree disappear",
              ("/volume1/old", "/volume1/old") not in rolled_pairs
              and ("/volume1/new", "/volume1/new") not in rolled_pairs,
              str(sorted(rolled_pairs)))
        check("rolling up never invents pairs",
              len(rolled_pairs) <= len(leaf_pairs),
              "%d rolled vs %d leaf" % (len(rolled_pairs), len(leaf_pairs)))

        # ---- the helpers on their own -----------------------------------
        print("\nand the two helpers behave")
        check("prefix is anchored with a separator",
              prefix_like("/volume1/photo") == "/volume1/photo/%",
              prefix_like("/volume1/photo"))
        check("a trailing slash changes nothing",
              prefix_like("/volume1/photo/") == prefix_like("/volume1/photo"))
        check("truncate keeps short paths whole",
              truncate_path("/volume1", 2) == "/volume1")
        check("truncate cuts long ones",
              truncate_path("/volume1/a/b/c", 2) == "/volume1/a")
        check("depth 0 is a no-op",
              truncate_path("/volume1/a/b/c", 0) == "/volume1/a/b/c")
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
