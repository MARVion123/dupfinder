"""The scan engine.

Five escalating passes, each one cheaper than the next is expensive:

  1. walk      - collect every file, skip excludes and hardlink twins
  2. size      - only sizes that occur more than once can be duplicates
  3. quick     - md5 of head+tail+size, 128 KiB per file
  4. md5       - full content hash of whatever survived
  5. verify    - byte-for-byte proof of every md5 match (optional, on by default)
  6. fuzzy     - CTPH + perceptual hashing to rate *near* duplicates

There is no time limit and no file-count limit. A scan can run for days; it
reports progress continuously, can be cancelled at any point, and can be
started again - the hash cache means a re-run only pays for what changed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import threading
import time
import traceback
from collections import defaultdict

from . import hashing
from .hashing import Cancelled
from .safety import check_allowed

BATCH = 400

# How many candidates are resolved against the cache before the misses among
# them are handed to the workers. Big enough that dispatch overhead disappears,
# small enough that progress keeps moving and a cancel is noticed promptly.
FUZZY_CHUNK = 256


def _cpu_count() -> int:
    """Cores this process may actually use, not what the box advertises."""
    try:
        return max(1, len(os.sched_getaffinity(0)))     # honours cgroup pinning
    except AttributeError:
        return max(1, os.cpu_count() or 1)


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _fuzzy_job(job):
    """Hash one candidate. Runs in a worker process.

    Lives at module level because that is what a worker can import. On Linux
    the pool forks and this is academic; on Windows each worker starts a fresh
    interpreter and re-imports the main module, which is why `python -m
    dupfinder` guards its entry point - without that guard every worker would
    start the whole program again.

    Plain tuples in and out, on purpose: this crosses a process boundary, so
    there is no database handle, no cancel event and no scanner state in here -
    none of that would pickle, and sharing it would be wrong even if it did.
    Cancellation is handled by the parent between chunks.
    """
    path, size, max_bytes, need_fz, need_dh = job
    fz = dh = None
    if need_fz:
        try:
            fz = hashing.fuzzy_hash(path, size, None, max_bytes)
        except OSError:
            fz = None
    if need_dh:
        ext = os.path.splitext(path)[1].lower()
        dh = (hashing.video_dhash(path) if ext in hashing.VIDEO_EXTS
              else hashing.image_dhash(path))
    return fz, dh


PHASES = [
    ("walk", "Indexing files"),
    ("size", "Grouping by size"),
    ("quick", "Partial hashing"),
    ("md5", "Full MD5 hashing"),
    ("verify", "Byte-for-byte verification"),
    ("fuzzy", "Near-duplicate analysis"),
    ("group", "Building result groups"),
]


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:      # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self):
        out = defaultdict(list)
        for node in list(self.parent):
            out[self.find(node)].append(node)
        return out


def name_similarity(a: str, b: str) -> float:
    """0..1 similarity of two file names, ignoring case and copy markers."""
    import difflib
    import re

    def clean(s):
        s = os.path.splitext(s)[0].lower()
        s = re.sub(r"[\s_\-]*\(?\b(copy|kopie|copia|\d{1,3})\)?$", "", s)
        return re.sub(r"[^a-z0-9]+", "", s)

    ca, cb = clean(a), clean(b)
    if not ca or not cb:
        return 0.0
    return difflib.SequenceMatcher(None, ca, cb).ratio()


# Folder names that mean the same thing. Two folders called "Video" and
# "Videos" holding the same files are almost always one folder that got split,
# and that is worth flagging louder than a coincidental overlap.
_FOLDER_SYNONYMS = [
    {"pic", "pics", "picture", "pictures", "photo", "photos", "img", "imgs",
     "image", "images", "bild", "bilder", "foto", "fotos"},
    {"video", "videos", "vid", "vids", "movie", "movies", "film", "filme",
     "clip", "clips"},
    {"doc", "docs", "document", "documents", "dokument", "dokumente"},
    {"backup", "backups", "sicherung", "sicherungen", "bak"},
    {"download", "downloads", "dl"},
    {"music", "musik", "audio", "songs", "song", "sound", "sounds"},
    {"archive", "archives", "archiv", "archive alt", "old", "alt"},
    {"new", "neu", "neue"},
    {"tmp", "temp", "temporary"},
]


def _normalise_folder(name: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "", name.lower())
    return re.sub(r"(e?s)$", "", lowered) or lowered


def folder_kinship(a: str, b: str) -> tuple[bool, str]:
    """Do these two folder names mean the same thing?

    Returns (related, why). Used to lift a pair like Video/Videos above a pair
    that merely happens to share files - the first is one folder that got
    split, the second may be a deliberate copy.
    """
    name_a, name_b = os.path.basename(a), os.path.basename(b)
    if not name_a or not name_b:
        return False, ""
    low_a, low_b = name_a.lower(), name_b.lower()
    if low_a == low_b:
        return True, "same name, different case or location"
    norm_a, norm_b = _normalise_folder(name_a), _normalise_folder(name_b)
    if norm_a and norm_a == norm_b:
        return True, "singular and plural of the same word"
    for group in _FOLDER_SYNONYMS:
        if low_a in group and low_b in group:
            return True, "two words for the same thing"
        if norm_a in group and norm_b in group:
            return True, "two words for the same thing"
    if norm_a and norm_b and (norm_a.startswith(norm_b) or norm_b.startswith(norm_a)):
        if abs(len(norm_a) - len(norm_b)) <= 4:
            return True, "one name is the other with a suffix"
    if name_similarity(name_a, name_b) >= 0.85:
        return True, "near-identical names"
    return False, ""


class ScanEngine:
    """Owns the single active scan. Thread-safe."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._status = {
            "state": "idle",
            "scan_id": None,
            "root": None,
            "phase": None,
            "phase_label": None,
            "phase_index": 0,
            "phase_total": len(PHASES),
            "current": "",
            "files_seen": 0,
            "bytes_seen": 0,
            "files_hashed": 0,
            "bytes_hashed": 0,
            "todo": 0,
            "done": 0,
            "groups_found": 0,
            "wasted_bytes": 0,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "cache_hits": 0,
            "reused_dirs": 0,
            "note": "",
        }

    # -- public API ----------------------------------------------------
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            st = dict(self._status)
        started = st.get("started_at")
        st["elapsed"] = (time.time() - started) if started and st["state"] == "running" else (
            (st.get("finished_at") or 0) - started if started else 0
        )
        todo, done = st.get("todo") or 0, st.get("done") or 0
        st["phase_progress"] = round(done * 100.0 / todo, 1) if todo else None
        return st

    def start(self, root: str, options: dict | None = None) -> int:
        if self.is_running():
            raise RuntimeError("A scan is already running")
        root = check_allowed(root, self.config["roots_allowlist"])
        if not os.path.isdir(root):
            raise ValueError("%s is not a directory" % root)

        opts = {
            "verify_bytes": self.config["verify_bytes"],
            "near_duplicates": self.config["near_duplicates"],
            "near_threshold": self.config["near_threshold"],
            "image_similarity": self.config["image_similarity"],
            "min_size": self.config["min_size"],
            "max_size": self.config["max_size"],
            "follow_symlinks": self.config["follow_symlinks"],
            "quick_rescan": self.config["quick_rescan"],
        }
        opts.update(options or {})

        cur = self.db.execute(
            "INSERT INTO scans(root,state,phase,started_at,options) VALUES(?,?,?,?,?)",
            (root, "running", "walk", time.time(), json.dumps(opts)),
        )
        scan_id = cur.lastrowid

        self._cancel.clear()
        with self._lock:
            self._status.update(
                state="running", scan_id=scan_id, root=root, phase="walk",
                phase_label=PHASES[0][1], phase_index=1, current="",
                files_seen=0, bytes_seen=0, files_hashed=0, bytes_hashed=0,
                todo=0, done=0, groups_found=0, wasted_bytes=0,
                started_at=time.time(), finished_at=None, error=None,
                cache_hits=0, reused_dirs=0,
            )
        self._thread = threading.Thread(
            target=self._run, args=(scan_id, root, opts), daemon=True,
            name="dupfinder-scan",
        )
        self._thread.start()
        return scan_id

    def cancel(self) -> bool:
        if not self.is_running():
            return False
        self._cancel.set()
        return True

    # -- internals -----------------------------------------------------
    def _set(self, **kw):
        with self._lock:
            self._status.update(kw)

    def _bump(self, key, amount=1):
        with self._lock:
            self._status[key] = (self._status.get(key) or 0) + amount

    def _phase(self, name, todo=0):
        idx = next((i for i, (n, _) in enumerate(PHASES) if n == name), 0)
        self._set(phase=name, phase_label=PHASES[idx][1], phase_index=idx + 1,
                  todo=todo, done=0, current="")
        self.db.execute("UPDATE scans SET phase=? WHERE id=?", (name, self._status["scan_id"]))

    def _abort_if_cancelled(self):
        if self._cancel.is_set():
            raise Cancelled()

    # -- parallel hashing ----------------------------------------------
    def _open_pool(self, work_items):
        """A process pool for the fuzzy pass, or None to stay on one core.

        Not worth starting for a handful of files: on Windows and on DSM each
        worker is a fresh interpreter, and that costs more than it saves until
        there is real work to spread. Returns None on any failure - a NAS that
        cannot fork should still finish the scan, just slower.
        """
        workers = int(self.config["fuzzy_workers"])
        if workers <= 0:
            # Leave a core for the rest of the NAS; this pass runs for hours
            # and the box is usually also serving files while it does.
            workers = max(1, _cpu_count() - 1)
        if workers < 2 or work_items < FUZZY_CHUNK:
            return None
        try:
            from concurrent.futures import ProcessPoolExecutor

            pool = ProcessPoolExecutor(max_workers=workers)
        except Exception as exc:                        # noqa: BLE001
            self._log_note("could not start workers (%s); hashing on one core" % exc)
            return None
        self._log_note("near-duplicate hashing across %d processes" % workers)
        return pool

    def _run_jobs(self, pool, jobs):
        """Hash a batch, in the pool when there is one, in-process otherwise."""
        if not jobs:
            return []
        if pool is None:
            return [_fuzzy_job(job) for job in jobs]
        try:
            return list(pool.map(_fuzzy_job, jobs, chunksize=4))
        except Exception as exc:                        # noqa: BLE001
            # A broken pool must not lose the scan. Finish this batch here and
            # let the caller carry on; the pool is shut down by the finally.
            self._log_note("workers failed (%s); continuing on one core" % exc)
            return [_fuzzy_job(job) for job in jobs]

    def _log_note(self, message):
        with self._lock:
            self._status["note"] = message

    def _run(self, scan_id, root, opts):
        try:
            self._walk(scan_id, root, opts)
            candidates = self._by_size(scan_id, opts)
            candidates = self._quick_pass(scan_id, candidates)
            md5_groups = self._md5_pass(scan_id, candidates)
            exact = self._verify_pass(scan_id, md5_groups, opts)
            # Publish the exact groups now rather than at the end. They are
            # already proven byte for byte, and the near-duplicate pass that
            # follows can run for minutes - during which the table would
            # otherwise sit empty even though the answer is largely known.
            # Written without switching the phase display, which still belongs
            # to whatever runs next.
            self._build_groups(scan_id, exact, [], phase=False)
            self._suggest(scan_id)

            near = []
            if opts.get("near_duplicates"):
                near = self._fuzzy_pass(scan_id, exact, opts)
                self._build_groups(scan_id, [], near)
                self._suggest(scan_id)
            self._finish(scan_id, "done")
        except Cancelled:
            self._finish(scan_id, "cancelled")
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            msg = "%s: %s" % (type(exc).__name__, exc)
            self.db.execute(
                "UPDATE scans SET error=? WHERE id=?",
                (msg + "\n" + traceback.format_exc(limit=6), scan_id),
            )
            self._set(error=msg)
            self._finish(scan_id, "error")

    def _suggest(self, scan_id):
        """Fill the suggestion column with the local rule engine.

        Without this a scan started from the web UI leaves every group
        unsuggested, which also makes the per-group "select suggested
        deletions" checkbox and the confidence sort inert. Idempotent: the
        insert upserts on group_id, so running it again after the
        near-duplicate groups arrive only adds the new ones.
        """
        try:
            from .ai import heuristic_suggestions

            heuristic_suggestions(self.db, scan_id)
        except Exception:  # noqa: BLE001 - a suggestion must never fail a scan
            pass

    def _finish(self, scan_id, state):
        st = self.status()
        self.db.execute(
            """UPDATE scans SET state=?, finished_at=?, files_seen=?, bytes_seen=?,
                   files_hashed=?, bytes_hashed=?, groups_found=?, wasted_bytes=?
               WHERE id=?""",
            (state, time.time(), st["files_seen"], st["bytes_seen"],
             st["files_hashed"], st["bytes_hashed"], st["groups_found"],
             st["wasted_bytes"], scan_id),
        )
        self._set(state=state, finished_at=time.time(), current="")

    # ---- phase 1: walk ----------------------------------------------
    def _walk(self, scan_id, root, opts):
        self._phase("walk")
        excludes = set(self.config["exclude_dirs"])
        # One compiled pattern instead of a loop of fnmatch calls. The loop ran
        # five times per file and cost more than reading the directory did.
        globs = [g for g in self.config["exclude_globs"] if g]
        excluded_name = re.compile(
            "|".join("(?:%s)" % fnmatch.translate(g) for g in globs)).match if globs else None
        min_size = int(opts.get("min_size") or 0)
        max_size = int(opts.get("max_size") or 0)
        follow = bool(opts.get("follow_symlinks"))

        # Quick rescan: a directory whose mtime is unchanged since the last
        # completed scan of this root had nothing added, removed or renamed, so
        # its file list is taken from that scan instead of being stat'ed again.
        # Off unless asked for, because an mtime says nothing about the
        # *contents* of the files inside - see _previous_scan.
        prev_scan = self._previous_scan(scan_id, root) if opts.get("quick_rescan") else None
        known = self.db.dir_mtimes(prev_scan) if prev_scan else {}
        reused_dirs = 0

        seen_inodes = set()
        batch = []
        dir_rows = []
        reuse: list[str] = []
        stack = [root]
        visited_dirs = set()

        while stack:
            self._abort_if_cancelled()
            current = stack.pop()
            try:
                # Loop protection by (device, inode) rather than realpath.
                # realpath resolves every component of every path and was the
                # second most expensive thing in this pass; a directory's
                # identity is one stat away.
                st = os.stat(current)
                key = (st.st_dev, st.st_ino)
                if key in visited_dirs:
                    continue
                visited_dirs.add(key)
                entries = list(os.scandir(current))
            except (OSError, PermissionError):
                continue

            dir_rows.append((current, st.st_mtime, scan_id, time.time()))
            if len(dir_rows) >= BATCH:
                self.db.dir_put_many(dir_rows)
                dir_rows = []

            unchanged = (prev_scan is not None
                         and abs(known.get(current, -1) - st.st_mtime) < 0.001)
            if unchanged:
                # Remember it and copy the files forward in bulk after the walk.
                # Doing it per directory meant two statements and a commit for
                # each one, which cost more than the stat calls it was saving.
                # The sub-directories are still walked either way: a change
                # three levels down does not touch this directory's mtime.
                reuse.append(current)
                reused_dirs += 1

            self._set(current=current)
            for entry in entries:
                self._abort_if_cancelled()
                try:
                    if entry.is_symlink() and not follow:
                        continue
                    if entry.is_dir(follow_symlinks=follow):
                        if entry.name in excludes:
                            continue
                        stack.append(entry.path)
                        continue
                    if unchanged:
                        continue          # already carried over, no stat needed
                    if not entry.is_file(follow_symlinks=follow):
                        continue
                    if excluded_name is not None and excluded_name(entry.name):
                        continue
                    stat = entry.stat(follow_symlinks=follow)
                except (OSError, PermissionError):
                    continue

                size = stat.st_size
                if size < min_size or (max_size and size > max_size):
                    continue
                # Hardlinks are the same bytes on disk - counting them as
                # duplicates would invite the user to "free" space that
                # does not exist.
                key = (stat.st_dev, stat.st_ino)
                if stat.st_nlink > 1:
                    if key in seen_inodes:
                        continue
                    seen_inodes.add(key)

                name = entry.name
                batch.append((
                    scan_id, entry.path, os.path.dirname(entry.path), name,
                    os.path.splitext(name)[1].lower(), size, stat.st_mtime,
                    stat.st_dev, stat.st_ino,
                ))
                self._bump("files_seen")
                self._bump("bytes_seen", size)

                if len(batch) >= BATCH:
                    self._flush_files(batch)
                    batch = []
        if batch:
            self._flush_files(batch)
        self.db.dir_put_many(dir_rows)
        if prev_scan is not None:
            self._carry_over(scan_id, prev_scan, reuse)
            self._set(reused_dirs=reused_dirs)

    def _previous_scan(self, scan_id, root):
        """The last scan of this exact root that ran to completion.

        Only 'done' counts: a cancelled scan holds a partial index, and
        carrying that forward would silently drop files.
        """
        row = self.db.one(
            "SELECT id FROM scans WHERE root=? AND state='done' AND id<? "
            "ORDER BY id DESC LIMIT 1", (root, scan_id))
        return row["id"] if row else None

    def _carry_over(self, scan_id, prev_scan, parents):
        """Copy the file rows of every unchanged directory in one go.

        Chunked only to stay under SQLite's limit on bound parameters; the
        point is that this is a handful of statements rather than two per
        directory.
        """
        if not parents:
            return
        for i in range(0, len(parents), 400):
            self._abort_if_cancelled()
            chunk = parents[i:i + 400]
            marks = ",".join("?" * len(chunk))
            self.db.execute(
                """INSERT OR IGNORE INTO files
                       (scan_id,path,parent,name,ext,size,mtime,dev,inode)
                   SELECT ?,path,parent,name,ext,size,mtime,dev,inode
                   FROM files
                   WHERE scan_id=? AND status='present' AND parent IN (%s)""" % marks,
                (scan_id, prev_scan, *chunk),
            )
        row = self.db.one(
            "SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM files WHERE scan_id=?",
            (scan_id,))
        if row:
            self._set(files_seen=row["c"], bytes_seen=row["b"])

    def _flush_files(self, batch):
        self.db.executemany(
            """INSERT OR IGNORE INTO files
               (scan_id,path,parent,name,ext,size,mtime,dev,inode)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            batch,
        )

    # ---- phase 2: size ----------------------------------------------
    def _by_size(self, scan_id, opts):
        self._phase("size")
        rows = self.db.query(
            """SELECT size FROM files WHERE scan_id=? AND size>0
               GROUP BY size HAVING COUNT(*)>1""",
            (scan_id,),
        )
        sizes = [r["size"] for r in rows]
        self._set(todo=len(sizes), done=len(sizes))
        if not sizes:
            return []
        ids = []
        for i in range(0, len(sizes), 400):
            self._abort_if_cancelled()
            chunk = sizes[i:i + 400]
            marks = ",".join("?" * len(chunk))
            ids.extend(
                r["id"] for r in self.db.query(
                    "SELECT id FROM files WHERE scan_id=? AND size IN (%s)" % marks,
                    (scan_id, *chunk),
                )
            )
        return ids

    # ---- phase 3: quick hash ----------------------------------------
    def _quick_pass(self, scan_id, file_ids):
        self._phase("quick", len(file_ids))
        if not file_ids:
            return []
        updates = []
        cached_rows = []
        for row in self._iter_files(file_ids, "id,path,size,mtime"):
            self._abort_if_cancelled()
            self._set(current=row["path"])
            cached = self.db.cache_get(row["path"], row["size"], row["mtime"])
            value = cached["quick"] if cached and cached["quick"] else None
            if value:
                self._bump("cache_hits")
            else:
                try:
                    value = hashing.quick_hash(row["path"], row["size"])
                except OSError:
                    value = None
                else:
                    cached_rows.append(
                        (row["path"], row["size"], row["mtime"], value, None, None, None))
            if value:
                updates.append((value, row["id"]))
            self._bump("done")
            if len(updates) >= BATCH:
                self.db.executemany("UPDATE files SET quick=? WHERE id=?", updates)
                self.db.cache_put_many(cached_rows)
                updates = []
                cached_rows = []
        if updates:
            self.db.executemany("UPDATE files SET quick=? WHERE id=?", updates)
        self.db.cache_put_many(cached_rows)

        rows = self.db.query(
            """SELECT id FROM files WHERE scan_id=? AND quick IS NOT NULL
               AND (size, quick) IN (
                   SELECT size, quick FROM files
                   WHERE scan_id=? AND quick IS NOT NULL
                   GROUP BY size, quick HAVING COUNT(*)>1)""",
            (scan_id, scan_id),
        )
        return [r["id"] for r in rows]

    # ---- phase 4: full md5 ------------------------------------------
    def _md5_pass(self, scan_id, file_ids):
        self._phase("md5", len(file_ids))
        if not file_ids:
            return {}
        updates = []
        cached_rows = []
        for row in self._iter_files(file_ids, "id,path,size,mtime"):
            self._abort_if_cancelled()
            self._set(current=row["path"])
            cached = self.db.cache_get(row["path"], row["size"], row["mtime"])
            value = cached["md5"] if cached and cached["md5"] else None
            if value:
                self._bump("cache_hits")
                self._bump("bytes_hashed", row["size"])
            else:
                try:
                    value = hashing.full_md5(
                        row["path"], self._cancel,
                        progress=lambda n: self._bump("bytes_hashed", n),
                    )
                except OSError:
                    value = None
                else:
                    cached_rows.append(
                        (row["path"], row["size"], row["mtime"], None, value, None, None))
            if value:
                updates.append((value, row["id"]))
                self._bump("files_hashed")
            self._bump("done")
            if len(updates) >= BATCH:
                self.db.executemany("UPDATE files SET md5=? WHERE id=?", updates)
                self.db.cache_put_many(cached_rows)
                updates = []
                cached_rows = []
        if updates:
            self.db.executemany("UPDATE files SET md5=? WHERE id=?", updates)
        self.db.cache_put_many(cached_rows)

        rows = self.db.query(
            """SELECT md5, size, GROUP_CONCAT(id) AS ids FROM files
               WHERE scan_id=? AND md5 IS NOT NULL
               GROUP BY md5, size HAVING COUNT(*)>1""",
            (scan_id,),
        )
        return {
            (r["md5"], r["size"]): [int(x) for x in r["ids"].split(",")]
            for r in rows
        }

    # ---- phase 5: byte verification ---------------------------------
    def _verify_pass(self, scan_id, md5_groups, opts):
        """Return a list of (signature, verified, [file_ids])."""
        total = sum(len(v) for v in md5_groups.values())
        verify = bool(opts.get("verify_bytes"))
        self._phase("verify", total if verify else 0)
        result = []
        if not verify:
            for (digest, size), ids in md5_groups.items():
                result.append((digest, False, ids))
            return result

        meta = self._verify_meta([i for ids in md5_groups.values() for i in ids])
        pending: list = []
        for (digest, size), ids in md5_groups.items():
            self._abort_if_cancelled()
            # Split into byte-identical clusters. Almost always one cluster.
            clusters: list[list[int]] = []
            for fid in ids:
                self._abort_if_cancelled()
                info = meta.get(fid)
                path = info["path"] if info else None
                self._set(current=path or "")
                self._bump("done")
                if not path or not os.path.exists(path):
                    continue
                placed = False
                for cluster in clusters:
                    ref = meta.get(cluster[0])
                    if not ref:
                        continue
                    if self._same_bytes(ref, info, pending):
                        cluster.append(fid)
                        placed = True
                        break
                if not placed:
                    clusters.append([fid])
                if len(pending) >= BATCH:
                    self.db.verify_put_many(pending)
                    pending = []
            for i, cluster in enumerate(clusters):
                if len(cluster) > 1:
                    sig = digest if i == 0 else "%s#%d" % (digest, i)
                    result.append((sig, True, cluster))
        self.db.verify_put_many(pending)
        return result

    def _verify_meta(self, ids):
        """path, size and mtime per file id - the key material for the cache."""
        out = {}
        for row in self._iter_files(ids, "id,path,size,mtime"):
            out[row["id"]] = {"path": row["path"], "size": row["size"],
                              "mtime": row["mtime"]}
        return out

    def _same_bytes(self, a, b, pending) -> bool:
        """Byte-for-byte comparison, remembered across scans.

        The proof itself is not weakened: a cached answer is only reused while
        both files still carry the size and mtime they had when it was taken.
        Any edit to either side changes one of those and the pair is compared
        again - exactly the assumption the hash cache already rests on.
        """
        cached = self.db.verify_get(a["path"], a["size"], a["mtime"],
                                    b["path"], b["size"], b["mtime"])
        if cached is not None:
            self._bump("cache_hits")
            return cached
        same = hashing.files_identical(a["path"], b["path"], self._cancel)
        pending.append(self.db.verify_row(
            a["path"], a["size"], a["mtime"],
            b["path"], b["size"], b["mtime"], same))
        return same

    # ---- phase 6: fuzzy / near duplicates ---------------------------
    def _pick_fuzzy_candidates(self, rows, redundant, want_images, unlimited,
                               max_size, max_bytes):
        """Decide, per file, whether to spend a read on it.

        Returns (row, is_image, wants_ctph) triples. Three rules, each of which
        exists because the pass was otherwise paying for nothing:

        1. An image with Pillow present gets the perceptual hash and *not* the
           CTPH one. Measured on the same picture saved at two JPEG qualities:
           CTPH scores 0%, the perceptual hash 100%, and CTPH costs 24x more.
        2. Extensions in fuzzy_skip_exts are left to the exact passes.
        3. With a byte budget in force every large file ends up with the same
           CTPH block size, so the block size no longer separates a 20 MB file
           from a 4 GB one. Bucket by size instead, and skip anything with no
           possible partner within a 4x size window.
        """
        skip_exts = {str(e).lower() for e in self.config["fuzzy_skip_exts"]}
        want_video = (bool(self.config["video_similarity"])
                      and hashing.ffmpeg_available())
        picked = []
        for row in rows:
            if row["id"] in redundant:
                continue
            ext = (row["ext"] or "").lower()
            is_image = want_images and ext in hashing.IMAGE_EXTS
            is_video = want_video and ext in hashing.VIDEO_EXTS
            # Video gets both signals, because they answer different questions:
            # CTPH recognises the same file remuxed, frame hashes recognise the
            # same film re-encoded, and neither sees what the other sees.
            wants_perceptual = is_image or is_video
            wants_ctph = (not is_image and ext not in skip_exts
                          and (unlimited or row["size"] <= max_size))
            if wants_perceptual or wants_ctph:
                picked.append((row, wants_perceptual, wants_ctph))

        if max_bytes <= 0:
            return picked

        bands: dict[int, int] = defaultdict(int)
        for row, _img, wants_ctph in picked:
            if wants_ctph:
                bands[int(row["size"]).bit_length()] += 1

        out = []
        for row, is_image, wants_ctph in picked:
            if wants_ctph:
                band = int(row["size"]).bit_length()
                # Counts include this file, so "< 2" means nothing else is
                # anywhere near its size.
                if bands[band] + bands[band - 1] + bands[band + 1] < 2:
                    wants_ctph = False
            if is_image or wants_ctph:
                out.append((row, is_image, wants_ctph))

        # Biggest first. This pass is the one people stop early - it is orders
        # of magnitude slower per byte than the others - and the order decides
        # what they are left holding. Working down from the largest files means
        # an hour of it has already found most of the reclaimable space.
        out.sort(key=lambda item: item[0]["size"], reverse=True)
        return out

    def _fuzzy_pass(self, scan_id, exact, opts):
        threshold = int(opts.get("near_threshold") or 70)
        min_size = max(int(self.config["fuzzy_min_size"]), hashing.MIN_BLOCKSIZE * 64)
        max_size = int(self.config["fuzzy_max_size"])
        max_bytes = int(self.config["fuzzy_max_bytes"])
        # With a byte budget in force a 6 GiB film costs exactly what a 16 MiB
        # one costs, so the "too big to fuzzy hash" cutoff loses its reason to
        # exist - and dropping it is the whole point for video collections.
        unlimited = max_bytes > 0
        want_images = bool(opts.get("image_similarity")) and hashing.pillow_available()

        # One representative per exact group is enough; the rest are identical.
        redundant = set()
        for _sig, _v, ids in exact:
            redundant.update(ids[1:])

        rows = self.db.query(
            "SELECT id,path,name,ext,size,mtime FROM files "
            "WHERE scan_id=? AND size>=? ORDER BY size",
            (scan_id, min_size),
        )
        candidates = self._pick_fuzzy_candidates(
            rows, redundant, want_images, unlimited, max_size, max_bytes)
        self._phase("fuzzy", len(candidates))
        if len(candidates) < 2:
            return []

        info = {}
        updates = []
        cached_rows = []
        # The CTPH loop is pure Python, so it holds the GIL and threads buy
        # nothing here - this is the one pass that needs separate processes.
        pool = self._open_pool(sum(1 for _r, _i, w in candidates if w))
        try:
            for chunk in _chunks(candidates, FUZZY_CHUNK):
                self._abort_if_cancelled()
                todo, done_now = [], []
                for row, is_image, wants_ctph in chunk:
                    cached = self.db.cache_get(row["path"], row["size"], row["mtime"])
                    fz = cached["fuzzy"] if cached else None
                    dh = cached["dhash"] if cached else None

                    # A signature cached under a larger byte budget carries a
                    # larger block size and would never bucket with freshly
                    # hashed files. Rehash rather than quietly compare across
                    # two regimes.
                    if fz and (hashing.fuzzy_blocksize(fz)
                               > hashing.fuzzy_blocksize_for(row["size"], max_bytes)):
                        fz = None
                    if not wants_ctph:
                        fz = None

                    need_fz = bool(wants_ctph and fz is None)
                    need_dh = bool(is_image and dh is None)
                    if need_fz or need_dh:
                        todo.append((row, fz, dh, need_fz, need_dh))
                    else:
                        done_now.append((row, fz, dh, None, None))

                if todo:
                    self._set(current=todo[0][0]["path"])
                results = self._run_jobs(
                    pool,
                    [(row["path"], row["size"], max_bytes, need_fz, need_dh)
                     for row, _fz, _dh, need_fz, need_dh in todo])

                computed = []
                for (row, fz, dh, _nf, _nd), (fresh_fz, fresh_dh) in zip(todo, results):
                    computed.append((row, fresh_fz if fresh_fz else fz,
                                     fresh_dh if fresh_dh else dh,
                                     fresh_fz, fresh_dh))

                for row, fz, dh, fresh_fz, fresh_dh in done_now + computed:
                    if fresh_fz or fresh_dh:
                        cached_rows.append((row["path"], row["size"], row["mtime"],
                                            None, None, fresh_fz, fresh_dh))
                    if fz or dh:
                        updates.append((fz, dh, row["id"]))
                    # No path: nothing downstream reads it, and it is the
                    # largest field. One of these exists per candidate for the
                    # whole pass - at 300,000 candidates the difference is
                    # hundreds of megabytes on a box that has a few gigabytes
                    # in total. `ext` is interned because a library has a
                    # handful of distinct extensions and no reason to hold
                    # 300,000 copies of ".mp4".
                    info[row["id"]] = {
                        "name": row["name"], "ext": sys.intern(row["ext"] or ""),
                        "size": row["size"], "fuzzy": fz, "dhash": dh,
                    }
                    self._bump("done")
                if len(updates) >= BATCH:
                    self.db.executemany(
                        "UPDATE files SET fuzzy=?, dhash=? WHERE id=?", updates)
                    self.db.cache_put_many(cached_rows)
                    updates = []
                    cached_rows = []
        finally:
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)   # cancel_futures is 3.9+
        if updates:
            self.db.executemany("UPDATE files SET fuzzy=?, dhash=? WHERE id=?", updates)
        self.db.cache_put_many(cached_rows)

        pairs = {}
        self._compare_fuzzy_buckets(info, threshold, pairs)
        if want_images:
            self._compare_image_buckets(info, threshold, pairs)
        if not pairs:
            return []

        uf = _UnionFind()
        for (a, b) in pairs:
            uf.union(a, b)
        groups = []
        exact_ids = {i for _s, _v, ids in exact for i in ids}
        for members in uf.groups().values():
            if len(members) < 2:
                continue
            # A cluster that is entirely inside one exact group adds nothing.
            if all(m in exact_ids for m in members):
                continue
            groups.append((members, pairs))
        return groups

    def _compare_fuzzy_buckets(self, info, threshold, pairs):
        """Score every pair that could possibly match, and no others.

        This used to compare everything in a block-size bucket against
        everything else. That is quadratic, so it was capped at
        `fuzzy_bucket_cap` files - and under a byte budget every file above the
        budget gets the *same* block size, which made "a bucket" the whole
        library. The cap then kept the 600 smallest files in it and silently
        dropped the rest, which on a film collection is precisely the files
        worth finding.

        The index replaces both. A comparison returns 0 unless the two
        signatures share a seven-character window, so indexing those windows
        yields exactly the pairs worth scoring - no cap, nothing dropped, and
        near-linear instead of quadratic.

        Windows shared by more than `fuzzy_gram_cap` files are dropped first.
        That part is not lossless: a window common to thousands of files
        carries no information about any particular pair, and keeping it would
        generate millions of pairs to score for nothing.
        """
        candidates = self._fuzzy_candidate_pairs(info)
        for a, b in candidates:
            self._abort_if_cancelled()
            key = (a, b) if a < b else (b, a)
            if key in pairs:
                continue
            score = self._pair_score(info[a], info[b])
            if score >= threshold:
                pairs[key] = score

    def _fuzzy_candidate_pairs(self, info):
        """Pairs sharing at least one signature window, via a temporary index.

        Kept in SQLite rather than a dictionary: a library of a million files
        produces tens of millions of windows, and a Python dict of those would
        cost more memory than a NAS has. The table is temporary, so it lives on
        the scan's connection and disappears with it.
        """
        conn = self.db.connect()
        with self.db.write_lock:
            conn.execute("DROP TABLE IF EXISTS fuzzy_gram")
            conn.execute(
                "CREATE TEMP TABLE fuzzy_gram(level INTEGER, gram INTEGER, fid INTEGER)")

            rows = []
            indexed = 0
            for fid, meta in info.items():
                if not meta["fuzzy"]:
                    continue
                for level, gram in hashing.fuzzy_grams(meta["fuzzy"]):
                    rows.append((level, gram, fid))
                if len(rows) >= 20000:
                    conn.executemany("INSERT INTO fuzzy_gram VALUES(?,?,?)", rows)
                    rows = []
                indexed += 1
            if rows:
                conn.executemany("INSERT INTO fuzzy_gram VALUES(?,?,?)", rows)
            if indexed < 2:
                conn.execute("DROP TABLE IF EXISTS fuzzy_gram")
                conn.commit()
                return

            cap = max(2, int(self.config["fuzzy_gram_cap"]))
            conn.execute(
                """DELETE FROM fuzzy_gram WHERE EXISTS (
                       SELECT 1 FROM (SELECT level, gram FROM fuzzy_gram
                                      GROUP BY level, gram HAVING COUNT(*) > ?) common
                       WHERE common.level = fuzzy_gram.level
                         AND common.gram  = fuzzy_gram.gram)""", (cap,))
            conn.execute("CREATE INDEX idx_fuzzy_gram ON fuzzy_gram(level, gram)")
            conn.commit()

        # Streamed rather than collected: a library can produce millions of
        # candidate pairs and a Python list of those is 120 bytes each. The
        # table is dropped once the cursor is exhausted, not before - dropping
        # a table out from under an open cursor is not something to try.
        cursor = conn.execute(
            """SELECT DISTINCT a.fid, b.fid FROM fuzzy_gram a
               JOIN fuzzy_gram b ON a.level = b.level AND a.gram = b.gram
                                AND a.fid < b.fid""")
        seen = 0
        try:
            while True:
                batch = cursor.fetchmany(10000)
                if not batch:
                    break
                seen += len(batch)
                for row in batch:
                    yield row[0], row[1]
        finally:
            cursor.close()
            with self.db.write_lock:
                conn.execute("DROP TABLE IF EXISTS fuzzy_gram")
                conn.commit()
            self._log_note("compared %s candidate pairs from %s signatures"
                           % ("{:,}".format(seen), "{:,}".format(indexed)))

    def _compare_image_buckets(self, info, threshold, pairs):
        images = [fid for fid, m in info.items() if m["dhash"]]
        cap = int(self.config["fuzzy_bucket_cap"]) * 4
        if len(images) > cap:
            images = images[:cap]
        for i in range(len(images)):
            self._abort_if_cancelled()
            a = images[i]
            for j in range(i + 1, len(images)):
                b = images[j]
                key = (min(a, b), max(a, b))
                if key in pairs:
                    continue
                sig_a, sig_b = info[a]["dhash"], info[b]["dhash"]
                if hashing.is_video_signature(sig_a) or hashing.is_video_signature(sig_b):
                    # Several frames averaged, so the bar can sit lower than for
                    # a single still without inviting coincidences.
                    score = hashing.video_compare(sig_a, sig_b)
                    floor = max(threshold, 80)
                else:
                    score = hashing.dhash_similarity(sig_a, sig_b)
                    floor = max(threshold, 88)
                if score >= floor:
                    pairs[key] = score

    def _pair_score(self, a, b) -> int:
        fz = hashing.fuzzy_compare(a["fuzzy"], b["fuzzy"])
        if fz <= 0:
            return 0
        big = max(a["size"], b["size"]) or 1
        size_ratio = min(a["size"], b["size"]) / big
        name = name_similarity(a["name"], b["name"])
        # Content dominates; name and size are tie-breakers that push an
        # already-similar pair over the line rather than creating a match.
        score = 0.80 * fz + 0.12 * (size_ratio * 100) + 0.08 * (name * 100)
        if a["ext"] != b["ext"]:
            score -= 5
        return int(min(98, max(0, round(score))))

    # ---- write groups -----------------------------------------------
    def _build_groups(self, scan_id, exact, near, phase=True):
        """Write groups to the database.

        Called twice per scan: once with the exact groups the moment they are
        proven, once with the near-duplicate ones afterwards. The counters
        accumulate through _bump rather than being assigned, so the second call
        cannot erase what the first reported.
        """
        if phase:
            self._phase("group", len(exact) + len(near))
        conn = self.db.connect()

        for sig, verified, ids in exact:
            self._abort_if_cancelled()
            gid = self._insert_group(
                conn, scan_id, "exact", sig, 100.0 if verified else 99.0,
                verified, ids, {i: (100.0 if verified else 99.0) for i in ids},
            )
            if gid:
                self._bump("groups_found")
                self._bump("wasted_bytes", self._group_waste(conn, gid))
            if phase:
                self._bump("done")

        for members, pairs in near:
            self._abort_if_cancelled()
            meta = self._meta(conn, members)
            if len(meta) < 2:
                if phase:
                    self._bump("done")
                continue
            # Representative = largest, then oldest: the copy most likely to be
            # the original rather than a re-export.
            rep = max(meta, key=lambda m: (m["size"], -m["mtime"]))["id"]
            sims = {rep: 100.0}
            for m in meta:
                if m["id"] == rep:
                    continue
                key = (min(rep, m["id"]), max(rep, m["id"]))
                sims[m["id"]] = float(pairs.get(key, 0) or self._indirect(pairs, rep, m["id"]))
            avg = sum(v for k, v in sims.items() if k != rep) / max(1, len(sims) - 1)
            gid = self._insert_group(
                conn, scan_id, "near", None, round(avg, 1), False,
                [m["id"] for m in meta], sims,
            )
            if gid:
                self._bump("groups_found")
                self._bump("wasted_bytes", self._group_waste(conn, gid))
            if phase:
                self._bump("done")

        # Commit here and not per group: the UI reads through WAL while this
        # runs, and one commit at the end of each call is what makes the whole
        # batch appear at once rather than half-built.
        conn.commit()

    @staticmethod
    def _indirect(pairs, a, b):
        """Best known score linking two members that were joined transitively."""
        best = 0
        for (x, y), score in pairs.items():
            if a in (x, y) or b in (x, y):
                best = max(best, score)
        return best * 0.9

    def _meta(self, conn, ids):
        marks = ",".join("?" * len(ids))
        return conn.execute(
            "SELECT id,path,parent,name,size,mtime FROM files WHERE id IN (%s)" % marks,
            tuple(ids),
        ).fetchall()

    def _insert_group(self, conn, scan_id, kind, signature, similarity, verified, ids, sims):
        rows = self._meta(conn, ids)
        if len(rows) < 2:
            return None
        sizes = [r["size"] for r in rows]
        total = sum(sizes)
        biggest = max(sizes)
        parents = {r["parent"] for r in rows}
        label = os.path.basename(rows[0]["name"])
        with self.db.write_lock:
            cur = conn.execute(
                """INSERT INTO groups(scan_id,kind,signature,similarity,verified,
                       file_count,total_bytes,wasted_bytes,max_size,folder_span,label)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (scan_id, kind, signature, similarity, 1 if verified else 0,
                 len(rows), total, total - biggest, biggest, len(parents), label),
            )
            gid = cur.lastrowid
            conn.executemany(
                "INSERT OR REPLACE INTO group_members(group_id,file_id,similarity) VALUES(?,?,?)",
                [(gid, r["id"], float(sims.get(r["id"], similarity))) for r in rows],
            )
        return gid

    @staticmethod
    def _group_waste(conn, gid):
        row = conn.execute("SELECT wasted_bytes FROM groups WHERE id=?", (gid,)).fetchone()
        return row["wasted_bytes"] if row else 0

    # ---- misc --------------------------------------------------------
    def _iter_files(self, ids, columns):
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.db.query(
                "SELECT %s FROM files WHERE id IN (%s)" % (columns, marks), tuple(chunk)
            ):
                yield row

