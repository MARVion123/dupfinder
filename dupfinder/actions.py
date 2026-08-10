"""Deletion, quarantine and restore.

Every removal is logged with its source and destination, so "delete" in
quarantine mode is a move you can undo from the UI.
"""

from __future__ import annotations

import os
import shutil
import time

from .hashing import files_identical
from .safety import check_allowed, UnsafePath

TRASH_DIRNAME = ".dupfinder-trash"

# Marks a log entry as a rehearsal. Nothing with this prefix ever moved a file,
# so nothing with this prefix may be offered for restore.
SIMULATED_PREFIX = "simulate:"


def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(1, 10000):
        candidate = "%s.%d%s" % (base, i, ext)
        if not os.path.exists(candidate):
            return candidate
    return "%s.%d%s" % (base, int(time.time()), ext)


def _share_root(path: str, roots: list[str]) -> str | None:
    """The DSM share a path belongs to, e.g. /volume1/photo."""
    for root in roots:
        root = os.path.normpath(root)
        if path.startswith(root + os.sep):
            rest = path[len(root) + 1:].split(os.sep)
            if rest:
                return os.path.join(root, rest[0])
    return None


def _quarantine_target(path: str, scan_root: str, scan_id: int) -> str:
    trash = os.path.join(scan_root, TRASH_DIRNAME, "scan-%d" % scan_id)
    try:
        rel = os.path.relpath(path, scan_root)
    except ValueError:
        rel = os.path.basename(path)
    if rel.startswith(".."):
        rel = os.path.basename(path)
    return os.path.join(trash, rel)


def _recycle_target(path: str, roots: list[str]) -> str | None:
    share = _share_root(path, roots)
    if not share:
        return None
    recycle = os.path.join(share, "#recycle")
    if not os.path.isdir(recycle):
        return None
    rel = os.path.relpath(path, share)
    return os.path.join(recycle, rel)


class ActionRunner:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    # -- deletion --------------------------------------------------------
    def delete_files(self, file_ids: list[int], mode: str | None = None,
                     scan_id: int | None = None, dry_run: bool = False) -> dict:
        """Remove the given files, or work out what would happen (`dry_run`).

        A dry run walks exactly the same path - the same allowlist check, the
        same last-copy protection, the same destination arithmetic - and stops
        just short of the filesystem call. It is the rehearsal, not a separate
        code path, so what it reports is what a real run would do.
        """
        mode = mode or self.config["delete_mode"]
        if mode not in ("quarantine", "recycle", "permanent", "link"):
            raise ValueError("Unknown delete mode: %s" % mode)
        roots = self.config["roots_allowlist"]
        if mode == "link":
            return self.link_files(file_ids, dry_run=dry_run)

        results = {"deleted": [], "skipped": [], "failed": [],
                   "freed_bytes": 0, "mode": mode, "dry_run": bool(dry_run)}
        if not file_ids:
            return results

        marks = ",".join("?" * len(file_ids))
        rows = self.db.query(
            "SELECT id,scan_id,path,size,status FROM files WHERE id IN (%s)" % marks,
            tuple(file_ids),
        )
        wanted = set(file_ids)
        found = {r["id"] for r in rows}
        for missing in wanted - found:
            results["failed"].append({"file_id": missing, "message": "Unknown file id"})

        survivors = self._survivor_check(rows) if self.config["protect_last_copy"] else {}

        for row in rows:
            fid, path, size = row["id"], row["path"], row["size"]
            if row["status"] != "present":
                results["skipped"].append(
                    {"file_id": fid, "path": path,
                     "message": "Already %s" % row["status"]})
                continue
            if fid in survivors:
                results["skipped"].append(
                    {"file_id": fid, "path": path, "message": survivors[fid]})
                continue
            try:
                resolved = check_allowed(path, roots)
            except UnsafePath as exc:
                results["failed"].append(
                    {"file_id": fid, "path": path, "message": str(exc)})
                continue
            if not os.path.isfile(resolved):
                self.db.execute("UPDATE files SET status='missing' WHERE id=?", (fid,))
                results["skipped"].append(
                    {"file_id": fid, "path": path, "message": "File no longer exists"})
                continue

            try:
                dst = self._target(resolved, mode, row, roots)
                if not dry_run:
                    dst = self._move(resolved, mode, dst)
            except Exception as exc:  # noqa: BLE001
                self._log(row, mode, resolved, None, False, str(exc), dry_run)
                results["failed"].append(
                    {"file_id": fid, "path": path, "message": str(exc)})
                continue

            if dry_run:
                self._log(row, mode, resolved, dst, True,
                          "Simulated - nothing was moved", True)
            else:
                status = "deleted" if mode == "permanent" else "quarantined"
                self.db.execute("UPDATE files SET status=? WHERE id=?", (status, fid))
                self.db.cache_invalidate(resolved)
                self._log(row, mode, resolved, dst, True, None)
            results["deleted"].append({"file_id": fid, "path": path, "moved_to": dst})
            results["freed_bytes"] += size or 0

        return results

    # -- cross-reference instead of deletion -----------------------------
    def link_files(self, file_ids: list[int], dry_run: bool = False) -> dict:
        """Replace each selected file with a hard link to an identical twin.

        The bytes exist once on disk and remain reachable under every path, so
        the space is reclaimed without anything disappearing from any folder.

        Hard links rather than symbolic ones: a symbolic link breaks the moment
        its target is moved or renamed, and SMB clients treat it inconsistently.
        A hard link is the same file under two names - neither is "the
        original", and deleting one name leaves the other untouched.

        Not reflinks (copy-on-write clones), even though DSM's Btrfs volumes
        support them: a reflinked pair stays two independent inodes, so the very
        next scan would find them as duplicates again and offer to reclaim space
        that is already shared. A hard link raises the link count, and the
        indexing pass already collapses those - the pair simply stops being
        reported, which is the truth.
        """
        results = {"deleted": [], "skipped": [], "failed": [],
                   "freed_bytes": 0, "mode": "link", "dry_run": bool(dry_run)}
        if not file_ids:
            return results
        roots = self.config["roots_allowlist"]
        requested = set(file_ids)

        marks = ",".join("?" * len(file_ids))
        rows = self.db.query(
            "SELECT id,scan_id,path,size,mtime,status FROM files WHERE id IN (%s)" % marks,
            tuple(file_ids),
        )
        for missing in requested - {r["id"] for r in rows}:
            results["failed"].append({"file_id": missing, "message": "Unknown file id"})

        for row in rows:
            fid, path, size = row["id"], row["path"], row["size"]
            if row["status"] != "present":
                results["skipped"].append(
                    {"file_id": fid, "path": path, "message": "Already %s" % row["status"]})
                continue

            twin = self._twin(row, requested)
            if twin is None:
                results["skipped"].append(
                    {"file_id": fid, "path": path,
                     "message": "No surviving copy to point at - keep at least one "
                                "file in the group unselected"})
                continue

            try:
                dst = check_allowed(path, roots)
                src = check_allowed(twin["path"], roots)
                message = self._link_check(src, dst, row, twin)
            except UnsafePath as exc:
                results["failed"].append({"file_id": fid, "path": path, "message": str(exc)})
                continue
            if message:
                results["skipped"].append({"file_id": fid, "path": path, "message": message})
                continue

            if dry_run:
                self._log(row, "link", dst, src, True,
                          "Simulated - nothing was linked", True)
            else:
                try:
                    self._hardlink(src, dst)
                except Exception as exc:  # noqa: BLE001
                    self._log(row, "link", dst, src, False, str(exc))
                    results["failed"].append(
                        {"file_id": fid, "path": path, "message": str(exc)})
                    continue
                self.db.execute("UPDATE files SET status='linked' WHERE id=?", (fid,))
                self.db.cache_invalidate(dst)
                self._log(row, "link", dst, src, True,
                          "Now a hard link to %s" % src)
            results["deleted"].append({"file_id": fid, "path": path, "moved_to": src})
            results["freed_bytes"] += size or 0

        return results

    def _twin(self, row, requested):
        """A file in one of this file's groups that will still be there after."""
        rows = self.db.query(
            """SELECT f.id, f.path, f.size, f.mtime
               FROM group_members m
               JOIN group_members o ON o.group_id = m.group_id
               JOIN files f ON f.id = o.file_id
               WHERE m.file_id = ? AND f.id != ? AND f.status = 'present'
               ORDER BY f.id""",
            (row["id"], row["id"]),
        )
        for candidate in rows:
            if candidate["id"] not in requested:
                return candidate
        return None

    def _link_check(self, src, dst, row, twin) -> str | None:
        """Everything that must hold before one file replaces another.
        Returns a reason to skip, or None when it is safe."""
        try:
            s_stat, d_stat = os.stat(src), os.stat(dst)
        except OSError as exc:
            return str(exc)
        if s_stat.st_dev != d_stat.st_dev:
            return ("The two copies live on different volumes - a hard link "
                    "cannot span them")
        if s_stat.st_ino == d_stat.st_ino:
            return "Already the same file on disk"
        if s_stat.st_size != d_stat.st_size:
            return "Sizes no longer match - the files changed since the scan"
        # The whole point is that nothing is lost, so identity is proven here
        # and not taken from the group. The cache answers instantly when the
        # verification pass already compared this exact pair.
        cached = self.db.verify_get(src, s_stat.st_size, s_stat.st_mtime,
                                    dst, d_stat.st_size, d_stat.st_mtime)
        if cached is None:
            cached = files_identical(src, dst)
            self.db.verify_put(src, s_stat.st_size, s_stat.st_mtime,
                               dst, d_stat.st_size, d_stat.st_mtime, cached)
        if not cached:
            return "The two files are not byte-identical - refusing to link them"
        return None

    @staticmethod
    def _hardlink(src, dst):
        """Point dst at src's data without dst ever ceasing to exist.

        The link is made under a temporary name in the same directory and then
        renamed over the target, so a failure at any point leaves the original
        file exactly where it was. Deleting the target first would open a window
        in which the file is simply gone.
        """
        tmp = os.path.join(os.path.dirname(dst),
                           ".dupfinder-link-%d.tmp" % os.getpid())
        if os.path.exists(tmp):
            os.remove(tmp)
        os.link(src, tmp)
        try:
            os.replace(tmp, dst)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _target(self, path, mode, row, roots) -> str | None:
        """Where this file would go. Touches nothing."""
        if mode == "permanent":
            return None
        if mode == "recycle":
            dst = _recycle_target(path, roots)
            if not dst:
                raise RuntimeError(
                    "No #recycle folder for this share - enable the DSM recycle bin "
                    "or use quarantine mode")
            return dst
        scan = self.db.one("SELECT root FROM scans WHERE id=?", (row["scan_id"],))
        scan_root = scan["root"] if scan else os.path.dirname(path)
        return _quarantine_target(path, scan_root, row["scan_id"] or 0)

    def _move(self, path, mode, dst) -> str | None:
        if mode == "permanent":
            os.remove(path)
            return None
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        dst = _unique(dst)
        shutil.move(path, dst)
        return dst

    def _survivor_check(self, rows) -> dict:
        """Refuse any deletion that would empty a group of its last copy."""
        blocked = {}
        requested = {r["id"] for r in rows}
        group_ids = set()
        for row in rows:
            for g in self.db.query(
                "SELECT group_id FROM group_members WHERE file_id=?", (row["id"],)
            ):
                group_ids.add(g["group_id"])
        for gid in group_ids:
            members = self.db.query(
                """SELECT f.id FROM group_members m JOIN files f ON f.id=m.file_id
                   WHERE m.group_id=? AND f.status='present'""",
                (gid,),
            )
            present = [m["id"] for m in members]
            if not present:
                continue
            remaining = [i for i in present if i not in requested]
            if remaining:
                continue
            # Everything in this group was selected - keep the first one.
            keeper = present[0]
            blocked[keeper] = (
                "Kept: deleting it would remove the last copy in group #%d" % gid)
        return blocked

    def _log(self, row, action, src, dst, ok, message, dry_run=False):
        # Simulated entries carry a prefix so they can never be mistaken for
        # something that happened - not by restore(), not by the log in the UI.
        if dry_run:
            action = SIMULATED_PREFIX + action
        self.db.execute(
            """INSERT INTO actions(scan_id,file_id,action,src_path,dst_path,size,
                   ok,message,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (row["scan_id"], row["id"], action, src, dst, row["size"],
             1 if ok else 0, message, time.time()),
        )

    # -- restore ---------------------------------------------------------
    def restore(self, action_ids: list[int]) -> dict:
        results = {"restored": [], "failed": []}
        if not action_ids:
            return results
        marks = ",".join("?" * len(action_ids))
        rows = self.db.query(
            "SELECT * FROM actions WHERE id IN (%s) AND ok=1 AND dst_path IS NOT NULL "
            "AND action NOT LIKE '%s%%'" % (marks, SIMULATED_PREFIX),
            tuple(action_ids),
        )
        for row in rows:
            src, dst = row["dst_path"], row["src_path"]
            try:
                if not os.path.isfile(src):
                    raise RuntimeError("Quarantined file is gone")
                if os.path.exists(dst):
                    raise RuntimeError("Original path is occupied again")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            except Exception as exc:  # noqa: BLE001
                results["failed"].append({"action_id": row["id"], "message": str(exc)})
                continue
            if row["file_id"]:
                self.db.execute(
                    "UPDATE files SET status='present' WHERE id=?", (row["file_id"],))
            self.db.execute(
                """INSERT INTO actions(scan_id,file_id,action,src_path,dst_path,size,
                       ok,message,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (row["scan_id"], row["file_id"], "restore", src, dst, row["size"],
                 1, "Restored from %s" % row["action"], time.time()),
            )
            results["restored"].append({"action_id": row["id"], "path": dst})
        return results

    # -- housekeeping ----------------------------------------------------
    def empty_quarantine(self, scan_id: int) -> dict:
        scan = self.db.one("SELECT root FROM scans WHERE id=?", (scan_id,))
        if not scan:
            raise ValueError("Unknown scan")
        trash = os.path.join(scan["root"], TRASH_DIRNAME, "scan-%d" % scan_id)
        trash = check_allowed(trash, self.config["roots_allowlist"])
        if not os.path.isdir(trash):
            return {"removed": 0, "path": trash}
        count = sum(len(files) for _r, _d, files in os.walk(trash))
        shutil.rmtree(trash)
        self.db.execute(
            "UPDATE files SET status='deleted' WHERE scan_id=? AND status='quarantined'",
            (scan_id,),
        )
        return {"removed": count, "path": trash}
