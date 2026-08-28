"""HTTP API + static UI, on top of the stdlib http.server.

No web framework: DSM ships a bare Python 3 and installing wheels on a NAS is
a chore. ThreadingHTTPServer is more than enough for a single-admin tool.
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, hashing
from .actions import ActionRunner
from .ai import AIEngine, heuristic_suggestions
from .db import Database
from .safety import UnsafePath, available_roots, check_allowed
from .scanner import ScanEngine, folder_kinship

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SORT_COLUMNS = {
    "similarity": "g.similarity",
    "wasted": "g.wasted_bytes",
    "size": "g.total_bytes",
    "largest": "g.max_size",
    "count": "g.file_count",
    "folders": "g.folder_span",
    "name": "g.label",
    "id": "g.id",
    "confidence": "s.confidence",
}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
        self.message = message


class Raw:
    """A response that is not JSON - currently only image previews.

    Endpoints normally return a dict that the handler encodes; returning one of
    these instead hands the bytes and content type straight through.
    """

    __slots__ = ("body", "ctype", "headers")

    def __init__(self, body: bytes, ctype: str, headers: dict | None = None):
        self.body = body
        self.ctype = ctype
        self.headers = headers or {}


class App:
    """Holds the shared state; the handler class is a thin shell around it."""

    def __init__(self, config):
        self.config = config
        self.db = Database(config.db_path)
        self.scanner = ScanEngine(self.db, config)
        self.ai = AIEngine(self.db, config)
        self.actions = ActionRunner(self.db, config)
        self.started_at = time.time()
        # A scan cannot survive a restart, so anything still marked "running"
        # in the database was interrupted by a reboot or a crash.
        self.db.execute(
            "UPDATE scans SET state='cancelled', finished_at=COALESCE(finished_at,?), "
            "error=COALESCE(error,'Interrupted by a service restart') "
            "WHERE state='running'",
            (time.time(),),
        )

    # -- routing --------------------------------------------------------
    def handle(self, method, path, query, body):
        route = (method, path)
        if method == "GET":
            if path == "/api/status":
                return self.api_status()
            if path == "/api/config":
                return self.config.public()
            if path == "/api/roots":
                return {"roots": available_roots(self.config["roots_allowlist"])}
            if path == "/api/browse":
                return self.api_browse(query)
            if path == "/api/scan/status":
                return self.scanner.status()
            if path == "/api/scans":
                return self.api_scans()
            if path == "/api/groups":
                return self.api_groups(query)
            if path == "/api/suggest/status":
                return self.ai.status()
            if path == "/api/actions":
                return self.api_actions(query)
            if path == "/api/thumb":
                return self.api_thumb(query)
            if path == "/api/database/usage":
                return self.api_database_usage()
            if path == "/api/folders":
                return self.api_folders(query)
            m = re.fullmatch(r"/api/group/(\d+)", path)
            if m:
                return self.api_group(int(m.group(1)))
        elif method == "POST":
            if path == "/api/config":
                return {"config": self.config.update(body or {})}
            if path == "/api/scan":
                return self.api_scan_start(body or {})
            if path == "/api/scan/cancel":
                return {"cancelled": self.scanner.cancel()}
            if path == "/api/suggest":
                return self.api_suggest(body or {})
            if path == "/api/suggest/cancel":
                return {"cancelled": self.ai.cancel()}
            if path == "/api/delete":
                return self.api_delete(body or {})
            if path == "/api/restore":
                return self.actions.restore(list(body.get("action_ids") or []))
            if path == "/api/quarantine/empty":
                return self.actions.empty_quarantine(int(body["scan_id"]))
            if path == "/api/database/clear":
                return self.api_database_clear(bool(body.get("keep_cache")))
        raise ApiError("No route for %s %s" % route, 404)

    # -- endpoints ------------------------------------------------------
    def api_status(self):
        return {
            "version": __version__,
            "uptime": round(time.time() - self.started_at, 1),
            "scan": self.scanner.status(),
            "ai": self.ai.status(),
            "pillow": hashing.pillow_available(),
            "roots": available_roots(self.config["roots_allowlist"]),
        }

    def api_database_usage(self):
        """What the database is holding, so the dialog can say it in numbers."""
        info = self.db.usage()
        return {
            "bytes_on_disk": info["bytes_on_disk"],
            "counts": info["counts"],
            "scans": len(info["scans"]),
            "path": self.config.db_path,
        }

    def api_database_clear(self, keep_cache=False):
        # Emptying the database underneath a running scan would leave the scan
        # writing rows into tables it no longer has a scan record in.
        if self.scanner.is_running():
            raise ApiError(
                "A scan is running. Stop it first, then empty the database.", 409)
        if self.ai.is_running():
            raise ApiError(
                "Suggestions are still being written. Stop them first.", 409)

        before = self.db.usage()["bytes_on_disk"]
        removed = self.db.clear(keep_cache=keep_cache)
        # Deleting rows only frees pages inside the file; without this the
        # database keeps its old size and nothing looks like it happened.
        self.db.vacuum()
        after = self.db.usage()["bytes_on_disk"]
        return {
            "removed": removed,
            "rows": sum(removed.values()),
            "bytes_before": before,
            "bytes_after": after,
            "kept_cache": bool(keep_cache),
        }

    def api_browse(self, query):
        raw = (query.get("path") or [""])[0]
        roots = available_roots(self.config["roots_allowlist"])
        if not raw:
            return {"path": "", "parent": None, "dirs": [
                {"name": r, "path": r} for r in roots]}
        try:
            path = check_allowed(raw, self.config["roots_allowlist"])
        except UnsafePath as exc:
            raise ApiError(str(exc), 403)
        if not os.path.isdir(path):
            raise ApiError("Not a directory: %s" % path, 404)

        dirs, files, skipped = [], 0, 0
        excludes = set(self.config["exclude_dirs"])
        try:
            for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in excludes:
                            skipped += 1
                            continue
                        dirs.append({"name": entry.name, "path": entry.path})
                    elif entry.is_file(follow_symlinks=False):
                        files += 1
                except OSError:
                    continue
        except PermissionError:
            raise ApiError("Permission denied: %s" % path, 403)

        parent = os.path.dirname(path)
        if not any(os.path.normpath(path) == os.path.normpath(r) for r in roots):
            parent_out = parent
        else:
            parent_out = ""
        return {"path": path, "parent": parent_out, "dirs": dirs,
                "file_count": files, "skipped_dirs": skipped}

    def api_scan_start(self, body):
        root = (body.get("root") or "").strip()
        if not root:
            raise ApiError("A directory is required")
        try:
            scan_id = self.scanner.start(root, body.get("options") or {})
        except (RuntimeError, ValueError) as exc:
            raise ApiError(str(exc), 409)
        except UnsafePath as exc:
            raise ApiError(str(exc), 403)
        return {"scan_id": scan_id, "status": self.scanner.status()}

    def api_scans(self):
        rows = self.db.query(
            "SELECT * FROM scans ORDER BY id DESC LIMIT 50")
        return {"scans": [dict(r) for r in rows]}

    def api_groups(self, query):
        scan_id = self._latest_scan_id(query)
        if scan_id is None:
            return {"groups": [], "total": 0, "scan_id": None, "summary": {}}

        where = ["g.scan_id = ?"]
        params: list = [scan_id]

        kind = (query.get("kind") or ["all"])[0]
        if kind in ("exact", "near"):
            where.append("g.kind = ?")
            params.append(kind)
        min_sim = (query.get("min_similarity") or ["0"])[0]
        try:
            if float(min_sim) > 0:
                where.append("g.similarity >= ?")
                params.append(float(min_sim))
        except ValueError:
            pass
        min_size = (query.get("min_size") or ["0"])[0]
        try:
            if int(min_size) > 0:
                where.append("g.max_size >= ?")
                params.append(int(min_size))
        except ValueError:
            pass
        search = (query.get("q") or [""])[0].strip()
        if search:
            where.append(
                "EXISTS (SELECT 1 FROM group_members m JOIN files f ON f.id=m.file_id "
                "WHERE m.group_id=g.id AND f.path LIKE ?)")
            params.append("%" + search + "%")
        if (query.get("suggested_only") or ["0"])[0] in ("1", "true"):
            where.append("s.group_id IS NOT NULL")

        # Only groups that still have at least two live (present) files are an
        # actionable duplicate set. Once a delete/quarantine/link resolves a
        # group down to a single remaining copy it drops out of the list — this
        # is what makes the grid update the moment a deletion goes through.
        where.append(
            "(SELECT COUNT(*) FROM group_members gm JOIN files gf ON gf.id = gm.file_id "
            "WHERE gm.group_id = g.id AND gf.status = 'present') >= 2")

        sort = (query.get("sort") or ["wasted"])[0]
        column = SORT_COLUMNS.get(sort, SORT_COLUMNS["wasted"])
        direction = "ASC" if (query.get("dir") or ["desc"])[0].lower() == "asc" else "DESC"
        limit = min(int((query.get("limit") or ["100"])[0] or 100), 500)
        offset = max(int((query.get("offset") or ["0"])[0] or 0), 0)

        clause = " AND ".join(where)
        total = self.db.one(
            "SELECT COUNT(*) AS c FROM groups g "
            "LEFT JOIN suggestions s ON s.group_id=g.id WHERE %s" % clause,
            tuple(params),
        )["c"]
        rows = self.db.query(
            """SELECT g.*, s.source AS sugg_source, s.confidence AS sugg_confidence,
                      s.summary AS sugg_summary, s.keep_file_id, s.merge_plan
               FROM groups g LEFT JOIN suggestions s ON s.group_id = g.id
               WHERE %s ORDER BY %s %s, g.id ASC LIMIT ? OFFSET ?"""
            % (clause, column, direction),
            tuple(params) + (limit, offset),
        )
        groups = [dict(r) for r in rows]
        for group in groups:
            files = [dict(f) for f in self._members(group["id"])]
            group["files"] = files
            # file_count/wasted_bytes are frozen at scan time; after a deletion
            # they are stale. Recompute from the copies that are actually still
            # present so the row shows reality (deleted copies remain listed,
            # struck through, but no longer count toward waste).
            present = [f for f in files if f.get("status") == "present"]
            sizes = [f["size"] for f in present]
            group["file_count"] = len(present)
            group["max_size"] = max(sizes) if sizes else 0
            group["wasted_bytes"] = (sum(sizes) - max(sizes)) if len(sizes) >= 2 else 0
            group["folder_span"] = len({f["parent"] for f in present})

        # Summary mirrors the same present-only accounting so the header totals
        # drop as soon as duplicates are removed.
        summary = self.db.one(
            """SELECT COUNT(*) AS groups,
                      COALESCE(SUM(pw), 0) AS wasted,
                      COALESCE(SUM(pc), 0) AS files
               FROM (
                 SELECT SUM(CASE WHEN f.status='present' THEN f.size ELSE 0 END)
                          - MAX(CASE WHEN f.status='present' THEN f.size ELSE 0 END) AS pw,
                        SUM(CASE WHEN f.status='present' THEN 1 ELSE 0 END) AS pc
                 FROM groups g
                 JOIN group_members gm ON gm.group_id = g.id
                 JOIN files f ON f.id = gm.file_id
                 WHERE g.scan_id = ?
                 GROUP BY g.id
                 HAVING pc >= 2
               )""",
            (scan_id,),
        )
        return {
            "scan_id": scan_id, "total": total, "limit": limit, "offset": offset,
            "groups": groups, "summary": dict(summary),
        }

    def api_group(self, group_id):
        row = self.db.one("SELECT * FROM groups WHERE id=?", (group_id,))
        if not row:
            raise ApiError("Unknown group", 404)
        group = dict(row)
        group["files"] = self._members(group_id, with_meta=True)
        sugg = self.db.one("SELECT * FROM suggestions WHERE group_id=?", (group_id,))
        group["suggestion"] = dict(sugg) if sugg else None
        if group["suggestion"]:
            try:
                group["suggestion"]["verdicts"] = json.loads(
                    group["suggestion"]["verdicts"] or "{}")
            except ValueError:
                group["suggestion"]["verdicts"] = {}
        return group

    def _members(self, group_id, with_meta=False):
        """Files in a group. `with_meta` opens each image to read its EXIF, so
        it is only ever set for a single expanded group, never for a page of
        results."""
        rows = self.db.query(
            """SELECT f.id, f.path, f.parent, f.name, f.ext, f.size, f.mtime,
                      f.status, f.md5, m.similarity
               FROM group_members m JOIN files f ON f.id = m.file_id
               WHERE m.group_id = ? ORDER BY f.size DESC, f.path ASC""",
            (group_id,),
        )
        sugg = self.db.one(
            "SELECT verdicts FROM suggestions WHERE group_id=?", (group_id,))
        verdicts = {}
        if sugg and sugg["verdicts"]:
            try:
                verdicts = json.loads(sugg["verdicts"])
            except ValueError:
                verdicts = {}
        out = []
        for row in rows:
            item = dict(row)
            verdict = verdicts.get(str(row["id"]))
            item["action"] = (verdict or {}).get("action")
            item["reason"] = (verdict or {}).get("reason")
            item["is_image"] = (row["ext"] or "").lower() in hashing.IMAGE_EXTS
            if with_meta and item["is_image"] and row["status"] == "present":
                item["meta"] = hashing.image_meta(row["path"])
            out.append(item)
        return out

    def api_suggest(self, body):
        scan_id = body.get("scan_id") or self._latest_scan_id({})
        if scan_id is None:
            raise ApiError("No scan to analyse")
        scan_id = int(scan_id)
        group_ids = [int(g) for g in (body.get("group_ids") or [])]
        engine = (body.get("engine") or "auto").lower()

        if engine in ("auto", "ai"):
            avail = self.ai.availability()
            if avail["ok"]:
                try:
                    count = self.ai.start(scan_id, group_ids or None)
                except (RuntimeError, ValueError) as exc:
                    raise ApiError(str(exc), 409)
                return {"engine": "ai", "groups": count, "status": self.ai.status()}
            if engine == "ai":
                raise ApiError(avail["reason"], 409)
        count = heuristic_suggestions(self.db, scan_id, group_ids or None)
        return {"engine": "heuristic", "groups": count,
                "note": self.ai.availability().get("reason", "")}

    def api_delete(self, body):
        file_ids = [int(f) for f in (body.get("file_ids") or [])]
        if not file_ids:
            raise ApiError("No files selected")
        if not body.get("confirm"):
            raise ApiError("Deletion must be confirmed")
        mode = body.get("mode") or self.config["delete_mode"]
        # An absent flag falls back to the stored setting, so a client that
        # does not know about simulation cannot accidentally switch it off.
        dry_run = body.get("dry_run")
        if dry_run is None:
            dry_run = self.config["dry_run"]
        return self.actions.delete_files(file_ids, mode, dry_run=bool(dry_run))

    def api_folders(self, query):
        """Pairs of folders that hold the same files.

        The per-file view hides the shape of the problem: two hundred rows
        saying "this clip is in two places" are really one fact, that
        .../Video and .../Videos are the same folder twice. This aggregates
        the groups back up to the folder pairs that produced them.
        """
        scan_id = self._latest_scan_id(query)
        if scan_id is None:
            return {"pairs": [], "scan_id": None}
        limit = min(int((query.get("limit") or ["60"])[0] or 60), 300)

        rows = self.db.query(
            """SELECT
                 CASE WHEN fa.parent < fb.parent THEN fa.parent ELSE fb.parent END AS a,
                 CASE WHEN fa.parent < fb.parent THEN fb.parent ELSE fa.parent END AS b,
                 COUNT(*) AS shared_files,
                 SUM(min(fa.size, fb.size)) AS shared_bytes
               FROM group_members ma
               JOIN group_members mb
                 ON mb.group_id = ma.group_id AND mb.file_id > ma.file_id
               JOIN files fa ON fa.id = ma.file_id
               JOIN files fb ON fb.id = mb.file_id
               JOIN groups g ON g.id = ma.group_id
               WHERE g.scan_id = ? AND fa.parent <> fb.parent
               GROUP BY a, b
               ORDER BY shared_bytes DESC
               LIMIT ?""",
            (scan_id, limit),
        )

        totals = {r["parent"]: r["c"] for r in self.db.query(
            "SELECT parent, COUNT(*) c FROM files WHERE scan_id=? GROUP BY parent",
            (scan_id,))}

        pairs = []
        for row in rows:
            a, b = row["a"], row["b"]
            related, why = folder_kinship(a, b)
            smaller = min(totals.get(a, 0), totals.get(b, 0)) or 1
            pairs.append({
                "a": a, "b": b,
                "a_files": totals.get(a, 0), "b_files": totals.get(b, 0),
                "shared_files": row["shared_files"],
                "shared_bytes": row["shared_bytes"] or 0,
                # How much of the smaller folder is already in the larger one.
                # 100% means one folder is contained in the other.
                "overlap": round(min(row["shared_files"] / smaller, 1.0) * 100),
                "related": related, "why": why,
            })
        # Name kinship first: those are one folder that got split, which is a
        # different problem from two folders that happen to share files.
        pairs.sort(key=lambda p: (p["related"], p["shared_bytes"]), reverse=True)
        return {"pairs": pairs, "scan_id": scan_id}

    def api_thumb(self, query):
        raw = (query.get("file_id") or [""])[0]
        try:
            file_id = int(raw)
        except ValueError:
            raise ApiError("Bad file_id")
        row = self.db.one("SELECT path, ext FROM files WHERE id=?", (file_id,))
        if not row:
            raise ApiError("Unknown file", 404)
        if (row["ext"] or "").lower() not in hashing.IMAGE_EXTS:
            raise ApiError("Not an image", 404)
        # The id comes from our own database, but it still names a path, so it
        # goes through the same allowlist as everything else.
        path = check_allowed(row["path"], self.config["roots_allowlist"])
        data = hashing.image_thumbnail(path)
        if not data:
            raise ApiError("No preview available", 404)
        # Content is immutable for the lifetime of a scan and the URL pins the
        # file id, so the browser may keep it. Private: it is someone's photo.
        return Raw(data, "image/jpeg", {"Cache-Control": "private, max-age=3600"})

    def api_actions(self, query):
        scan_id = self._latest_scan_id(query)
        limit = min(int((query.get("limit") or ["200"])[0] or 200), 1000)
        if scan_id is None:
            return {"actions": []}
        rows = self.db.query(
            "SELECT * FROM actions WHERE scan_id=? ORDER BY id DESC LIMIT ?",
            (scan_id, limit),
        )
        return {"actions": [dict(r) for r in rows], "scan_id": scan_id}

    def _latest_scan_id(self, query):
        raw = (query.get("scan_id") or [""])[0]
        if raw:
            try:
                return int(raw)
            except ValueError:
                raise ApiError("Bad scan_id")
        live = self.scanner.status().get("scan_id")
        if live:
            return live
        row = self.db.one(
            "SELECT id FROM scans WHERE state IN ('done','cancelled') "
            "ORDER BY id DESC LIMIT 1")
        return row["id"] if row else None


def make_handler(app: App):
    token = app.config["auth_token"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "dupfinder/" + __version__
        protocol_version = "HTTP/1.1"

        # -- plumbing ---------------------------------------------------
        def log_message(self, fmt, *args):  # quieter default log
            if self.path.startswith("/api/scan/status"):
                return
            super().log_message(fmt, *args)

        def _send(self, status, payload=None, body=None, ctype="application/json",
                  extra_headers=None):
            if body is None:
                body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorised(self, query) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer ") and header[7:].strip() == token:
                return True
            return (query.get("token") or [""])[0] == token

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            if length > 8 * 1024 * 1024:
                raise ApiError("Request body too large", 413)
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise ApiError("Body is not valid JSON")

        # -- verbs ------------------------------------------------------
        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if not path.startswith("/api/"):
                    return self._serve_static(path, query)
                if not self._authorised(query):
                    return self._send(401, {"error": "Authentication required"})
                body = self._read_body() if method == "POST" else None
                result = app.handle(method, path, query, body)
                if isinstance(result, Raw):
                    return self._send(200, body=result.body, ctype=result.ctype,
                                      extra_headers=result.headers)
                self._send(200, result)
            except ApiError as exc:
                self._send(exc.status, {"error": exc.message})
            except UnsafePath as exc:
                self._send(403, {"error": str(exc)})
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

        # -- static -----------------------------------------------------
        def _serve_static(self, path, query):
            if token and not self._authorised(query) and path not in ("/", "/index.html"):
                return self._send(401, {"error": "Authentication required"})
            if path in ("/", ""):
                path = "/index.html"
            # posixpath.normpath + strip leading separators blocks traversal.
            rel = posixpath.normpath(path).lstrip("/")
            target = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not target.startswith(STATIC_DIR + os.sep) and target != STATIC_DIR:
                return self._send(403, {"error": "Forbidden"})
            if not os.path.isfile(target):
                return self._send(404, {"error": "Not found"})
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            with open(target, "rb") as fh:
                data = fh.read()
            self._send(200, body=data, ctype=ctype,
                       extra_headers={"Cache-Control": "no-cache"})

    return Handler


def serve(config):
    app = App(config)
    handler = make_handler(app)
    httpd = ThreadingHTTPServer((config["host"], int(config["port"])), handler)
    httpd.daemon_threads = True
    return app, httpd
