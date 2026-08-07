"""SQLite storage.

One connection per thread (the HTTP server is threaded, the scanner runs in
its own worker). WAL mode lets the UI read results while a scan is writing.
"""

from __future__ import annotations

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
    status    TEXT DEFAULT 'present',     -- present|deleted|quarantined|missing
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

    def cache_invalidate(self, path: str):
        self.execute("DELETE FROM hash_cache WHERE path=?", (path,))
        self.execute("DELETE FROM verify_cache WHERE a_path=? OR b_path=?",
                     (path, path))

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
