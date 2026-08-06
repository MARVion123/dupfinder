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
import threading
import time
import traceback
from collections import defaultdict

from . import hashing
from .hashing import Cancelled
from .safety import check_allowed

BATCH = 400

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
                cache_hits=0,
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

    def _run(self, scan_id, root, opts):
        try:
            self._walk(scan_id, root, opts)
            candidates = self._by_size(scan_id, opts)
            candidates = self._quick_pass(scan_id, candidates)
            md5_groups = self._md5_pass(scan_id, candidates)
            exact = self._verify_pass(scan_id, md5_groups, opts)
            near = []
            if opts.get("near_duplicates"):
                near = self._fuzzy_pass(scan_id, exact, opts)
            self._build_groups(scan_id, exact, near)
            # Fill the suggestion column with the local rule engine, the same
            # way the CLI does after a headless scan. Without this a scan
            # started from the web UI leaves every group unsuggested, which
            # also makes the per-group "select suggested deletions" checkbox
            # and the confidence sort inert until the user happens to press
            # "AI suggestions". Idempotent: the insert upserts on group_id.
            try:
                from .ai import heuristic_suggestions

                heuristic_suggestions(self.db, scan_id)
            except Exception:  # noqa: BLE001 - a suggestion must never fail a scan
                pass
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
        globs = list(self.config["exclude_globs"])
        min_size = int(opts.get("min_size") or 0)
        max_size = int(opts.get("max_size") or 0)
        follow = bool(opts.get("follow_symlinks"))

        seen_inodes = set()
        batch = []
        stack = [root]
        visited_dirs = set()

        while stack:
            self._abort_if_cancelled()
            current = stack.pop()
            try:
                real = os.path.realpath(current)
                if real in visited_dirs:
                    continue
                visited_dirs.add(real)
                entries = list(os.scandir(current))
            except (OSError, PermissionError):
                continue

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
                    if not entry.is_file(follow_symlinks=follow):
                        continue
                    if any(fnmatch.fnmatch(entry.name, g) for g in globs):
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
                    self.db.cache_put(row["path"], row["size"], row["mtime"], quick=value)
            if value:
                updates.append((value, row["id"]))
            self._bump("done")
            if len(updates) >= BATCH:
                self.db.executemany("UPDATE files SET quick=? WHERE id=?", updates)
                updates = []
        if updates:
            self.db.executemany("UPDATE files SET quick=? WHERE id=?", updates)

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
                    self.db.cache_put(row["path"], row["size"], row["mtime"], md5=value)
            if value:
                updates.append((value, row["id"]))
                self._bump("files_hashed")
            self._bump("done")
            if len(updates) >= BATCH:
                self.db.executemany("UPDATE files SET md5=? WHERE id=?", updates)
                updates = []
        if updates:
            self.db.executemany("UPDATE files SET md5=? WHERE id=?", updates)

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

        paths = self._path_map([i for ids in md5_groups.values() for i in ids])
        for (digest, size), ids in md5_groups.items():
            self._abort_if_cancelled()
            # Split into byte-identical clusters. Almost always one cluster.
            clusters: list[list[int]] = []
            for fid in ids:
                self._abort_if_cancelled()
                path = paths.get(fid)
                self._set(current=path or "")
                self._bump("done")
                if not path or not os.path.exists(path):
                    continue
                placed = False
                for cluster in clusters:
                    ref = paths.get(cluster[0])
                    if ref and hashing.files_identical(ref, path, self._cancel):
                        cluster.append(fid)
                        placed = True
                        break
                if not placed:
                    clusters.append([fid])
            for i, cluster in enumerate(clusters):
                if len(cluster) > 1:
                    sig = digest if i == 0 else "%s#%d" % (digest, i)
                    result.append((sig, True, cluster))
        return result

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
        picked = []
        for row in rows:
            if row["id"] in redundant:
                continue
            ext = (row["ext"] or "").lower()
            is_image = want_images and ext in hashing.IMAGE_EXTS
            wants_ctph = (not is_image and ext not in skip_exts
                          and (unlimited or row["size"] <= max_size))
            if is_image or wants_ctph:
                picked.append((row, is_image, wants_ctph))

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
        for row, is_image, wants_ctph in candidates:
            self._abort_if_cancelled()
            self._set(current=row["path"])
            cached = self.db.cache_get(row["path"], row["size"], row["mtime"])
            fz = cached["fuzzy"] if cached else None
            dh = cached["dhash"] if cached else None

            # A signature cached under a larger byte budget carries a larger
            # block size and would never bucket with freshly hashed files.
            # Rehash rather than quietly compare across two regimes.
            if fz and (hashing.fuzzy_blocksize(fz)
                       > hashing.fuzzy_blocksize_for(row["size"], max_bytes)):
                fz = None

            if wants_ctph and fz is None:
                try:
                    fz = hashing.fuzzy_hash(row["path"], row["size"],
                                            self._cancel, max_bytes)
                except OSError:
                    fz = None
            elif not wants_ctph:
                fz = None
            if is_image and dh is None:
                dh = hashing.image_dhash(row["path"])
            if fz or dh:
                self.db.cache_put(row["path"], row["size"], row["mtime"],
                                  fuzzy=fz, dhash=dh)
                updates.append((fz, dh, row["id"]))
            info[row["id"]] = {
                "path": row["path"], "name": row["name"], "ext": row["ext"],
                "size": row["size"], "fuzzy": fz, "dhash": dh,
            }
            self._bump("done")
            if len(updates) >= BATCH:
                self.db.executemany(
                    "UPDATE files SET fuzzy=?, dhash=? WHERE id=?", updates)
                updates = []
        if updates:
            self.db.executemany("UPDATE files SET fuzzy=?, dhash=? WHERE id=?", updates)

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
        buckets = defaultdict(list)
        for fid, meta in info.items():
            bs = hashing.fuzzy_blocksize(meta["fuzzy"])
            if bs:
                buckets[bs].append(fid)
        cap = int(self.config["fuzzy_bucket_cap"])
        for bs, ids in buckets.items():
            self._abort_if_cancelled()
            # ssdeep signatures only compare across bs and 2*bs.
            neighbours = ids + buckets.get(bs * 2, [])
            if len(neighbours) > cap:
                neighbours = sorted(neighbours, key=lambda i: info[i]["size"])[:cap]
            for i in range(len(neighbours)):
                self._abort_if_cancelled()
                a = neighbours[i]
                for j in range(i + 1, len(neighbours)):
                    b = neighbours[j]
                    if (min(a, b), max(a, b)) in pairs:
                        continue
                    score = self._pair_score(info[a], info[b])
                    if score >= threshold:
                        pairs[(min(a, b), max(a, b))] = score

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
                score = hashing.dhash_similarity(info[a]["dhash"], info[b]["dhash"])
                if score >= max(threshold, 88):
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
    def _build_groups(self, scan_id, exact, near):
        self._phase("group", len(exact) + len(near))
        conn = self.db.connect()
        total_wasted = 0
        groups_found = 0

        for sig, verified, ids in exact:
            self._abort_if_cancelled()
            gid = self._insert_group(
                conn, scan_id, "exact", sig, 100.0 if verified else 99.0,
                verified, ids, {i: (100.0 if verified else 99.0) for i in ids},
            )
            if gid:
                groups_found += 1
                total_wasted += self._group_waste(conn, gid)
            self._bump("done")

        for members, pairs in near:
            self._abort_if_cancelled()
            meta = self._meta(conn, members)
            if len(meta) < 2:
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
                groups_found += 1
                total_wasted += self._group_waste(conn, gid)
            self._bump("done")

        conn.commit()
        self._set(groups_found=groups_found, wasted_bytes=total_wasted)

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

    def _path_map(self, ids):
        out = {}
        for row in self._iter_files(list(ids), "id,path"):
            out[row["id"]] = row["path"]
        return out
