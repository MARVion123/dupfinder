"""SQLite storage.

One connection per thread (the HTTP server is threaded, the scanner runs in
its own worker). WAL mode lets the UI read results while a scan is writing.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root          TEXT NOT NULL,
    state         TEXT NOT NULL,          -- running|cancelled|done|error
    phase         TEXT,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    files_seen    INTEGER DEFAULT 0,
    bytes_seen    INTEGER DEFAULT 0,
    files_hashed  INTEGER DEFAULT 0,
    bytes_hashed  INTEGER DEFAULT 0,
    groups_found  INTEGER DEFAULT 0,
    wasted_bytes  INTEGER DEFAULT 0,
    options       TEXT,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id   INTEGER NOT NULL,
    path      TEXT NOT NULL,
    parent    TEXT NOT NULL,
    name      TEXT NOT NULL,
    ext       TEXT,
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    dev       INTEGER,
    inode     INTEGER,
    quick     TEXT,
    md5       TEXT,
    fuzzy     TEXT,
    dhash     TEXT,
    -- 'linked' means the file is still there and still readable, but its
    -- bytes are now shared with an identical twin instead of duplicated.
    status    TEXT DEFAULT 'present',     -- present|deleted|quarantined|missing|linked
    UNIQUE(scan_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_scan_size ON files(scan_id, size);
CREATE INDEX IF NOT EXISTS idx_files_scan_md5  ON files(scan_id, md5);
CREATE INDEX IF NOT EXISTS idx_files_scan_qk   ON files(scan_id, quick);

CREATE TABLE IF NOT EXISTS groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id      INTEGER NOT NULL,
    kind         TEXT NOT NULL,           -- exact|near
    signature    TEXT,
    similarity   REAL NOT NULL,
    verified     INTEGER DEFAULT 0,
    file_count   INTEGER NOT NULL,
    total_bytes  INTEGER NOT NULL,
    wasted_bytes INTEGER NOT NULL,
    max_size     INTEGER NOT NULL,
    folder_span  INTEGER DEFAULT 1,
    label        TEXT
);
CREATE INDEX IF NOT EXISTS idx_groups_scan ON groups(scan_id, wasted_bytes DESC);

CREATE TABLE IF NOT EXISTS group_members (
    group_id   INTEGER NOT NULL,
    file_id    INTEGER NOT NULL,
    similarity REAL NOT NULL,
    PRIMARY KEY (group_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_members_file ON group_members(file_id);

CREATE TABLE IF NOT EXISTS suggestions (
    group_id     INTEGER PRIMARY KEY,
    scan_id      INTEGER NOT NULL,
    source       TEXT NOT NULL,           -- ai|heuristic
    keep_file_id INTEGER,
    confidence   INTEGER,
    summary      TEXT,
    merge_plan   TEXT,
    verdicts     TEXT,                    -- JSON: {file_id: {action, reason}}
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sugg_scan ON suggestions(scan_id);

CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id      INTEGER,
    file_id      INTEGER,
    action       TEXT NOT NULL,           -- quarantine|recycle|permanent|restore
    src_path     TEXT NOT NULL,
    dst_path     TEXT,
    size         INTEGER,
    ok           INTEGER NOT NULL,
    message      TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_scan ON actions(scan_id, id DESC);

-- Survives across scans so a re-run does not re-hash unchanged files.
CREATE TABLE IF NOT EXISTS hash_cache (
    path   TEXT PRIMARY KEY,
    size   INTEGER NOT NULL,
    mtime  REAL NOT NULL,
    quick  TEXT,
    md5    TEXT,
    fuzzy  TEXT,
    dhash  TEXT,
    seen_at REAL NOT NULL
);

-- Directory mtimes from the last completed scan, for the quick rescan. A
-- directory's mtime changes whenever an entry is added, removed or renamed -
-- but NOT when a file's contents change, which is exactly the trade the quick
-- rescan makes and why it is opt-in.
CREATE TABLE IF NOT EXISTS dir_cache (
    path    TEXT PRIMARY KEY,
    mtime   REAL NOT NULL,
    scan_id INTEGER NOT NULL,
    seen_at REAL NOT NULL
);

-- The byte-for-byte comparison is the most expensive thing a repeat scan does,
-- and it was the only pass without a cache: on an unchanged tree it re-read
-- every duplicate pair in full, every time. A pair is remembered only together
-- with both files' size and mtime, so any edit to either side invalidates the
-- entry - the same assumption hash_cache already makes.
CREATE TABLE IF NOT EXISTS verify_cache (
    a_path  TEXT NOT NULL,
    a_size  INTEGER NOT NULL,
    a_mtime REAL NOT NULL,
    b_path  TEXT NOT NULL,
    b_size  INTEGER NOT NULL,
    b_mtime REAL NOT NULL,
    same    INTEGER NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (a_path, b_path)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self.write_lock = threading.Lock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=60.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=60000")
            self._local.conn = conn
        return conn

    # -- helpers -------------------------------------------------------
    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        return self.connect().execute(sql, params).fetchone()

    def execute(self, sql: str, params=()):
        conn = self.connect()
        with self.write_lock:
            cur = conn.execute(sql, params)
            conn.commit()
        return cur

    def executemany(self, sql: str, seq):
        conn = self.connect()
        with self.write_lock:
            conn.executemany(sql, seq)
            conn.commit()

    # -- hash cache ----------------------------------------------------
    def cache_get(self, path: str, size: int, mtime: float):
        row = self.one(
            "SELECT quick, md5, fuzzy, dhash FROM hash_cache "
            "WHERE path=? AND size=? AND abs(mtime-?) < 0.001",
            (path, size, mtime),
        )
        return row

    def cache_put(self, path, size, mtime, quick=None, md5=None, fuzzy=None, dhash=None):
        conn = self.connect()
        with self.write_lock:
            conn.execute(
                """INSERT INTO hash_cache(path,size,mtime,quick,md5,fuzzy,dhash,seen_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     size=excluded.size, mtime=excluded.mtime,
                     quick=COALESCE(excluded.quick, hash_cache.quick),
                     md5=COALESCE(excluded.md5, hash_cache.md5),
                     fuzzy=COALESCE(excluded.fuzzy, hash_cache.fuzzy),
                     dhash=COALESCE(excluded.dhash, hash_cache.dhash),
                     seen_at=excluded.seen_at""",
                (path, size, mtime, quick, md5, fuzzy, dhash, time.time()),
            )
            conn.commit()

    def cache_put_many(self, rows):
        """Store many cache entries in one transaction.

        rows: (path, size, mtime, quick, md5, fuzzy, dhash)

        cache_put commits once per file. On an SSD that is 16,600 files a
        second and invisible; the cost is one fsync per file, and on a NAS with
        rotating disks an fsync is orders of magnitude dearer than the hash it
        is recording. Measured on 20,000 rows: 1.21 s one at a time against
        0.03 s batched, 37x.
        """
        if not rows:
            return
        conn = self.connect()
        now = time.time()
        with self.write_lock:
            conn.executemany(
                """INSERT INTO hash_cache(path,size,mtime,quick,md5,fuzzy,dhash,seen_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     size=excluded.size, mtime=excluded.mtime,
                     quick=COALESCE(excluded.quick, hash_cache.quick),
                     md5=COALESCE(excluded.md5, hash_cache.md5),
                     fuzzy=COALESCE(excluded.fuzzy, hash_cache.fuzzy),
                     dhash=COALESCE(excluded.dhash, hash_cache.dhash),
                     seen_at=excluded.seen_at""",
                [tuple(row) + (now,) for row in rows],
            )
            conn.commit()

    def cache_invalidate(self, path: str):
        self.execute("DELETE FROM hash_cache WHERE path=?", (path,))
        self.execute("DELETE FROM verify_cache WHERE a_path=? OR b_path=?",
                     (path, path))

    # -- housekeeping --------------------------------------------------
    def usage(self) -> dict:
        """Row counts per table, plus the size of the database on disk.

        A scan writes one row per file, so a tree of a million files leaves a
        million rows behind - and the next scan of the same tree writes a
        million more. Nothing prunes itself; this is what makes that visible.
        """
        tables = ["scans", "files", "groups", "group_members", "suggestions",
                  "actions", "hash_cache", "verify_cache", "dir_cache"]
        counts = {}
        for table in tables:
            row = self.one("SELECT COUNT(*) c FROM %s" % table)
            counts[table] = row["c"] if row else 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        per_scan = self.query(
            """SELECT s.id, s.root, s.state, s.started_at,
                      (SELECT COUNT(*) FROM files f WHERE f.scan_id=s.id) AS files,
                      (SELECT COUNT(*) FROM groups g WHERE g.scan_id=s.id) AS groups
               FROM scans s ORDER BY s.id DESC""")
        return {"counts": counts, "bytes_on_disk": total,
                "scans": [dict(r) for r in per_scan]}

    def prune(self, keep_scans: int = 3, drop_stale_cache: bool = True) -> dict:
        """Drop old scans and cache entries for files that are gone.

        The caches are deliberately *not* cleared wholesale: they are what makes
        a repeat scan cheap, and they stay valid across scans. Only entries
        whose file no longer exists on disk are worth removing - those can never
        match anything again.
        """
        removed = {"scans": 0, "files": 0, "groups": 0, "cache": 0}
        keep = [r["id"] for r in self.query(
            "SELECT id FROM scans ORDER BY id DESC LIMIT ?", (max(0, keep_scans),))]
        doomed = [r["id"] for r in self.query("SELECT id FROM scans")
                  if r["id"] not in keep]
        for scan_id in doomed:
            self.execute(
                "DELETE FROM group_members WHERE group_id IN "
                "(SELECT id FROM groups WHERE scan_id=?)", (scan_id,))
            cur = self.execute("DELETE FROM groups WHERE scan_id=?", (scan_id,))
            removed["groups"] += cur.rowcount or 0
            self.execute("DELETE FROM suggestions WHERE scan_id=?", (scan_id,))
            cur = self.execute("DELETE FROM files WHERE scan_id=?", (scan_id,))
            removed["files"] += cur.rowcount or 0
            self.execute("DELETE FROM dir_cache WHERE scan_id=?", (scan_id,))
            self.execute("DELETE FROM scans WHERE id=?", (scan_id,))
            removed["scans"] += 1

        if drop_stale_cache:
            gone = [r["path"] for r in self.query("SELECT path FROM hash_cache")
                    if not os.path.exists(r["path"])]
            for i in range(0, len(gone), 400):
                chunk = gone[i:i + 400]
                marks = ",".join("?" * len(chunk))
                self.execute("DELETE FROM hash_cache WHERE path IN (%s)" % marks,
                             tuple(chunk))
                self.execute(
                    "DELETE FROM verify_cache WHERE a_path IN (%s) OR b_path IN (%s)"
                    % (marks, marks), tuple(chunk) * 2)
            removed["cache"] = len(gone)
        return removed

    def vacuum(self) -> None:
        """Hand the freed pages back to the filesystem.

        Deleting rows only marks pages reusable inside the file; without this
        the database never actually shrinks. It rewrites the whole file, so it
        wants roughly the current size free on the volume, and it must not run
        while a scan is writing.
        """
        conn = self.connect()
        with self.write_lock:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.isolation_level = None
            conn.execute("VACUUM")
            conn.isolation_level = ""

    # -- directory cache (quick rescan) --------------------------------
    def dir_mtimes(self, scan_id: int) -> dict:
        """path -> mtime for every directory that scan visited."""
        return {r["path"]: r["mtime"] for r in self.query(
            "SELECT path, mtime FROM dir_cache WHERE scan_id=?", (scan_id,))}

    def dir_put_many(self, rows):
        if not rows:
            return
        conn = self.connect()
        with self.write_lock:
            conn.executemany(
                """INSERT INTO dir_cache(path,mtime,scan_id,seen_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     mtime=excluded.mtime, scan_id=excluded.scan_id,
                     seen_at=excluded.seen_at""",
                rows,
            )
            conn.commit()

    # -- byte-comparison cache -----------------------------------------
    # Pairs are stored with the lexicographically smaller path first, so
    # (a, b) and (b, a) are the same row and one lookup answers both.
    @staticmethod
    def _pair(a_path, a_size, a_mtime, b_path, b_size, b_mtime):
        if a_path <= b_path:
            return (a_path, a_size, a_mtime, b_path, b_size, b_mtime)
        return (b_path, b_size, b_mtime, a_path, a_size, a_mtime)

    def verify_get(self, a_path, a_size, a_mtime, b_path, b_size, b_mtime):
        """True/False if this exact pair was compared before, else None."""
        key = self._pair(a_path, a_size, a_mtime, b_path, b_size, b_mtime)
        row = self.one(
            "SELECT same FROM verify_cache WHERE a_path=? AND a_size=? "
            "AND abs(a_mtime-?) < 0.001 AND b_path=? AND b_size=? "
            "AND abs(b_mtime-?) < 0.001",
            key,
        )
        return None if row is None else bool(row["same"])

    def verify_row(self, a_path, a_size, a_mtime, b_path, b_size, b_mtime, same):
        """One row for verify_put_many, ordered and ready to insert."""
        return self._pair(a_path, a_size, a_mtime, b_path, b_size, b_mtime) + (
            1 if same else 0, time.time())

    def verify_put_many(self, rows):
        """Insert a batch of comparison results in one transaction.

        Written in batches rather than one row at a time: a first scan produces
        one result per compared pair, and committing each of them separately
        cost more than the comparisons it was trying to save.
        """
        if not rows:
            return
        conn = self.connect()
        with self.write_lock:
            conn.executemany(
                """INSERT INTO verify_cache
                       (a_path,a_size,a_mtime,b_path,b_size,b_mtime,same,seen_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(a_path,b_path) DO UPDATE SET
                     a_size=excluded.a_size, a_mtime=excluded.a_mtime,
                     b_size=excluded.b_size, b_mtime=excluded.b_mtime,
                     same=excluded.same, seen_at=excluded.seen_at""",
                rows,
            )
            conn.commit()
