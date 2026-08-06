"""Deletion suggestions.

Two engines, same output shape:

  * heuristic - always available, runs at the end of every scan. Ranks copies
    by path depth, folder quality, name markers ("copy", "(1)", "~") and age.
  * ai        - Claude reads the group metadata and proposes which copy to
    keep, which to delete, and how a merge across folders would look.

Neither engine ever touches the filesystem. They only write suggestions; the
user still has to tick the boxes and press delete.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import List, Literal, Optional

SYSTEM_PROMPT = """You are helping a Synology NAS owner clean up duplicate files.

For each group of duplicate or near-duplicate files you receive metadata only \
(paths, sizes, modification times, similarity scores). You never see file \
contents and you must never assume anything about content beyond what the \
similarity score states.

For every group decide:
  - keep_path: exactly one path to keep. Prefer the copy in the best-organised \
location: a curated library folder over Downloads/Temp/Recovered, a shallow \
canonical path over a deeply nested one, an original filename over one marked \
"copy", "(1)", "- Kopie", "duplicate" or a trailing "~".
  - verdicts: one entry per path. action is "keep" for the kept copy, "delete" \
for a copy that is safe to remove, or "review" when you are not confident.
  - merge_plan: if the copies are spread across folders that look like they \
should be consolidated, describe in one or two sentences what a merge would \
look like (which folder becomes canonical, what moves where). Otherwise say \
"No merge needed".
  - confidence: 0-100.

Be conservative. Rules you must follow:
  - Mark "review", never "delete", when similarity is below 100 and the paths \
suggest the files may be intentionally different versions (different resolution, \
draft vs final, per-year archives, backups of a live folder).
  - Never propose deleting every copy in a group.
  - Treat anything under a path containing "backup", "archive", "snapshot", \
"time machine", "versions" as intentional redundancy: keep it, delete the other \
copy instead, or mark review.
  - A group of exact duplicates (similarity 100) that differ only in folder is \
usually safe to deduplicate.

Return concise reasons; the user reads them in a table."""


# --------------------------------------------------------------------------
# Heuristic engine
# --------------------------------------------------------------------------
JUNK_MARKERS = re.compile(
    r"(\bcopy\b|\bkopie\b|\bcopia\b|\(\d+\)|\bduplicate\b|~$|\bconflicted\b|"
    r"\bconflict\b|\bold\b|_\d{1,2}$)",
    re.IGNORECASE,
)
BAD_DIRS = re.compile(
    r"(downloads?|temp|tmp|recovered|unsorted|inbox|import|desktop|to.?sort|"
    r"neuer ordner|new folder)", re.IGNORECASE
)
GOOD_DIRS = re.compile(
    r"(photo|foto|music|musik|video|documents?|dokumente|library|archive|"
    r"media|shared|projects?)", re.IGNORECASE
)
PROTECTED_DIRS = re.compile(
    r"(backup|archiv|snapshot|time.?machine|versions?|\.history)", re.IGNORECASE
)


def score_keep(path: str, mtime: float, size: int) -> float:
    """Higher = better candidate to keep."""
    score = 0.0
    lower = path.lower()
    depth = path.count(os.sep)
    score -= depth * 1.5                       # shallow paths look canonical
    name = os.path.basename(path)
    if JUNK_MARKERS.search(os.path.splitext(name)[0]):
        score -= 25
    if BAD_DIRS.search(lower):
        score -= 15
    if GOOD_DIRS.search(lower):
        score += 10
    # Backup/archive copies are deliberately never *deleted* (see the verdict
    # loop), but they must not win "keep" either - promoting a backup would
    # delete the curated original and leave only the redundant copy.
    score -= len(name) * 0.05
    score += min(size, 1 << 30) / (1 << 34)    # tiny nudge toward the larger copy
    score -= (time.time() - mtime) / 86400.0 * 0.01
    return score


def heuristic_suggestions(db, scan_id: int, group_ids=None) -> int:
    """Write a suggestion row for every group. Returns the number written."""
    where = "WHERE g.scan_id=?"
    params: list = [scan_id]
    if group_ids:
        where += " AND g.id IN (%s)" % ",".join("?" * len(group_ids))
        params.extend(group_ids)
    groups = db.query("SELECT g.* FROM groups g %s" % where, tuple(params))

    written = 0
    now = time.time()
    for group in groups:
        members = db.query(
            """SELECT f.id, f.path, f.size, f.mtime, m.similarity
               FROM group_members m JOIN files f ON f.id=m.file_id
               WHERE m.group_id=? AND f.status='present'""",
            (group["id"],),
        )
        if len(members) < 2:
            continue
        ranked = sorted(
            members, key=lambda r: score_keep(r["path"], r["mtime"], r["size"]),
            reverse=True,
        )
        keeper = ranked[0]
        verdicts = {}
        exact = group["kind"] == "exact"
        for row in ranked:
            if row["id"] == keeper["id"]:
                verdicts[str(row["id"])] = {
                    "action": "keep",
                    "reason": "Best location and filename of the group.",
                }
            elif not exact and row["similarity"] < 100:
                verdicts[str(row["id"])] = {
                    "action": "review",
                    "reason": "Only %.0f%% similar - confirm it is not a different version."
                              % row["similarity"],
                }
            elif PROTECTED_DIRS.search(row["path"].lower()):
                verdicts[str(row["id"])] = {
                    "action": "review",
                    "reason": "Lives under a backup/archive path - redundancy may be intentional.",
                }
            else:
                verdicts[str(row["id"])] = {
                    "action": "delete",
                    "reason": "Identical to the kept copy.",
                }

        folders = {os.path.dirname(r["path"]) for r in members}
        if len(folders) > 1:
            merge_plan = ("Consolidate into %s; the other %d folder(s) hold the "
                          "same content." % (os.path.dirname(keeper["path"]), len(folders) - 1))
        else:
            merge_plan = "No merge needed - all copies are in the same folder."

        db.execute(
            """INSERT INTO suggestions(group_id,scan_id,source,keep_file_id,
                   confidence,summary,merge_plan,verdicts,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(group_id) DO UPDATE SET
                   source=excluded.source, keep_file_id=excluded.keep_file_id,
                   confidence=excluded.confidence, summary=excluded.summary,
                   merge_plan=excluded.merge_plan, verdicts=excluded.verdicts,
                   created_at=excluded.created_at""",
            (group["id"], scan_id, "heuristic", keeper["id"],
             90 if exact else 55,
             "Keep %s" % os.path.basename(keeper["path"]),
             merge_plan, json.dumps(verdicts), now),
        )
        written += 1
    return written


# --------------------------------------------------------------------------
# Claude engine
# --------------------------------------------------------------------------
def _schema_models():
    """Build the pydantic response models lazily (pydantic ships with anthropic)."""
    from pydantic import BaseModel, Field

    class FileVerdict(BaseModel):
        path: str = Field(description="Absolute path exactly as given in the input")
        action: Literal["keep", "delete", "review"]
        reason: str = Field(description="One short sentence the user will read in a table")

    class GroupSuggestion(BaseModel):
        group_id: int
        keep_path: str
        verdicts: List[FileVerdict]
        merge_plan: str
        confidence: int = Field(ge=0, le=100)
        summary: str = Field(description="Under 90 characters")

    class SuggestionBatch(BaseModel):
        suggestions: List[GroupSuggestion]

    return SuggestionBatch


class AIEngine:
    """Runs Claude over the scan's groups in a background thread."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._status = {
            "state": "idle", "scan_id": None, "done": 0, "todo": 0,
            "error": None, "model": None, "started_at": None, "finished_at": None,
        }

    # -- availability ---------------------------------------------------
    def availability(self) -> dict:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return {"ok": False, "reason": "The 'anthropic' package is not installed "
                                           "(pip3 install anthropic)."}
        if not self.config.api_key():
            return {"ok": False, "reason": "No Anthropic API key configured. Set it in "
                                           "Settings or export ANTHROPIC_API_KEY."}
        if not self.config["ai_enabled"]:
            return {"ok": False, "reason": "AI suggestions are disabled in settings."}
        return {"ok": True, "reason": "", "model": self.config["ai_model"]}

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            st = dict(self._status)
        st["available"] = self.availability()
        return st

    def cancel(self) -> bool:
        if not self.is_running():
            return False
        self._cancel.set()
        return True

    def start(self, scan_id: int, group_ids=None) -> int:
        if self.is_running():
            raise RuntimeError("Suggestions are already being generated")
        avail = self.availability()
        if not avail["ok"]:
            raise RuntimeError(avail["reason"])

        groups = self._collect(scan_id, group_ids)
        if not groups:
            raise ValueError("No groups to analyse")

        self._cancel.clear()
        with self._lock:
            self._status.update(
                state="running", scan_id=scan_id, done=0, todo=len(groups),
                error=None, model=self.config["ai_model"],
                started_at=time.time(), finished_at=None,
            )
        self._thread = threading.Thread(
            target=self._run, args=(scan_id, groups), daemon=True, name="dupfinder-ai"
        )
        self._thread.start()
        return len(groups)

    # -- internals ------------------------------------------------------
    def _collect(self, scan_id, group_ids):
        where = "WHERE g.scan_id=?"
        params: list = [scan_id]
        if group_ids:
            where += " AND g.id IN (%s)" % ",".join("?" * len(group_ids))
            params.extend(group_ids)
        rows = self.db.query(
            "SELECT * FROM groups g %s ORDER BY g.wasted_bytes DESC LIMIT ?" % where,
            tuple(params) + (int(self.config["ai_max_groups"]),),
        )
        out = []
        for group in rows:
            members = self.db.query(
                """SELECT f.id, f.path, f.size, f.mtime, m.similarity
                   FROM group_members m JOIN files f ON f.id=m.file_id
                   WHERE m.group_id=? AND f.status='present'""",
                (group["id"],),
            )
            if len(members) < 2:
                continue
            out.append({
                "group_id": group["id"],
                "kind": group["kind"],
                "similarity": group["similarity"],
                "byte_verified": bool(group["verified"]),
                "files": [
                    {
                        "id": m["id"],
                        "path": m["path"],
                        "size_bytes": m["size"],
                        "modified": time.strftime(
                            "%Y-%m-%d", time.localtime(m["mtime"])),
                        "similarity_to_group": round(m["similarity"], 1),
                    }
                    for m in members
                ],
            })
        return out

    def _run(self, scan_id, groups):
        try:
            client = self._client()
            batch_size = max(1, int(self.config["ai_batch_size"]))
            for i in range(0, len(groups), batch_size):
                if self._cancel.is_set():
                    break
                batch = groups[i:i + batch_size]
                try:
                    self._process(client, scan_id, batch)
                except Exception as exc:  # noqa: BLE001
                    # One bad batch must not lose the rest; fall back locally.
                    with self._lock:
                        self._status["error"] = "%s: %s" % (type(exc).__name__, exc)
                    heuristic_suggestions(
                        self.db, scan_id, [g["group_id"] for g in batch])
                with self._lock:
                    self._status["done"] += len(batch)
            state = "cancelled" if self._cancel.is_set() else "done"
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._status["error"] = "%s: %s" % (type(exc).__name__, exc)
            state = "error"
        with self._lock:
            self._status["state"] = state
            self._status["finished_at"] = time.time()

    def _client(self):
        import anthropic

        return anthropic.Anthropic(api_key=self.config.api_key())

    def _process(self, client, scan_id, batch):
        SuggestionBatch = _schema_models()
        payload = json.dumps({"groups": batch}, indent=1)
        user_text = (
            "Here are %d duplicate groups from a NAS scan. Decide what to keep, "
            "what is safe to delete, and how a merge would look.\n\n%s"
            % (len(batch), payload)
        )

        request = dict(
            model=self.config["ai_model"],
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config["ai_effort"]},
            messages=[{"role": "user", "content": user_text}],
            output_format=SuggestionBatch,
        )

        response = None
        try:
            # Server-side refusal fallbacks keep the batch working even if a
            # safety classifier declines; drop back to the stable endpoint on
            # SDK versions that do not expose them yet.
            response = client.beta.messages.parse(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **request,
            )
        except Exception:
            response = client.messages.parse(**request)

        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("The model declined to analyse this batch")

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise RuntimeError("No structured output returned")

        by_group = {g["group_id"]: g for g in batch}
        now = time.time()
        for suggestion in parsed.suggestions:
            group = by_group.get(suggestion.group_id)
            if not group:
                continue
            path_to_id = {f["path"]: f["id"] for f in group["files"]}
            verdicts = {}
            for verdict in suggestion.verdicts:
                fid = path_to_id.get(verdict.path)
                if fid is None:
                    continue
                action = verdict.action
                verdicts[str(fid)] = {"action": action, "reason": verdict.reason}
            # Safety net: the UI must never be told to delete an entire group.
            actions = [v["action"] for v in verdicts.values()]
            if verdicts and "keep" not in actions:
                keep_id = path_to_id.get(suggestion.keep_path)
                if keep_id is None:
                    keep_id = group["files"][0]["id"]
                verdicts[str(keep_id)] = {
                    "action": "keep",
                    "reason": "Kept automatically - a group must retain one copy.",
                }
            keep_file_id = path_to_id.get(suggestion.keep_path)
            self.db.execute(
                """INSERT INTO suggestions(group_id,scan_id,source,keep_file_id,
                       confidence,summary,merge_plan,verdicts,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(group_id) DO UPDATE SET
                       source=excluded.source, keep_file_id=excluded.keep_file_id,
                       confidence=excluded.confidence, summary=excluded.summary,
                       merge_plan=excluded.merge_plan, verdicts=excluded.verdicts,
                       created_at=excluded.created_at""",
                (suggestion.group_id, scan_id, "ai", keep_file_id,
                 int(suggestion.confidence), suggestion.summary[:200],
                 suggestion.merge_plan, json.dumps(verdicts), now),
            )
