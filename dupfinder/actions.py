"""Deletion, quarantine and restore.

Every removal is logged with its source and destination, so "delete" in
quarantine mode is a move you can undo from the UI.
"""

from __future__ import annotations

import os
import shutil
import time

from .safety import check_allowed, UnsafePath

TRASH_DIRNAME = ".dupfinder-trash"


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
                     scan_id: int | None = None) -> dict:
        mode = mode or self.config["delete_mode"]
        if mode not in ("quarantine", "recycle", "permanent"):
            raise ValueError("Unknown delete mode: %s" % mode)
        roots = self.config["roots_allowlist"]

        results = {"deleted": [], "skipped": [], "failed": [],
                   "freed_bytes": 0, "mode": mode}
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
                dst = self._perform(resolved, mode, row, roots)
            except Exception as exc:  # noqa: BLE001
                self._log(row, mode, resolved, None, False, str(exc))
                results["failed"].append(
                    {"file_id": fid, "path": path, "message": str(exc)})
                continue

            status = "deleted" if mode == "permanent" else "quarantined"
            self.db.execute("UPDATE files SET status=? WHERE id=?", (status, fid))
            self.db.cache_invalidate(resolved)
            self._log(row, mode, resolved, dst, True, None)
            results["deleted"].append({"file_id": fid, "path": path, "moved_to": dst})
            results["freed_bytes"] += size or 0

        return results

    def _perform(self, path, mode, row, roots) -> str | None:
        if mode == "permanent":
            os.remove(path)
            return None
        if mode == "recycle":
            dst = _recycle_target(path, roots)
            if not dst:
                raise RuntimeError(
                    "No #recycle folder for this share - enable the DSM recycle bin "
                    "or use quarantine mode")
        else:
            scan = self.db.one("SELECT root FROM scans WHERE id=?", (row["scan_id"],))
            scan_root = scan["root"] if scan else os.path.dirname(path)
            dst = _quarantine_target(path, scan_root, row["scan_id"] or 0)
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

    def _log(self, row, action, src, dst, ok, message):
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
            "SELECT * FROM actions WHERE id IN (%s) AND ok=1 AND dst_path IS NOT NULL"
            % marks,
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
