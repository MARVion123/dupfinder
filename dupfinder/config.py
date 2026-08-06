"""Configuration handling.

The config lives as JSON next to the database so it survives package upgrades.
Everything has a sane default; the UI can change most of it at runtime.
"""

from __future__ import annotations

import json
import os
import threading

DEFAULT_DATA_DIR = os.environ.get(
    "DUPFINDER_DATA_DIR", "/var/packages/dupfinder/var"
)

# Directories that are always skipped. @eaDir holds Synology thumbnails and
# index sidecars - scanning it produces thousands of meaningless "duplicates".
DEFAULT_EXCLUDES = [
    "@eaDir",
    "#recycle",
    "#snapshot",
    "@tmp",
    "@sharesnap",
    "@sharebin",
    "@S2S",
    "@synologydrive",
    "@synothumb",
    "lost+found",
    ".dupfinder-trash",
    ".git",
    ".svn",
    "node_modules",
    "$RECYCLE.BIN",
    "System Volume Information",
]

DEFAULTS = {
    # --- server ---
    "host": "0.0.0.0",
    "port": 8777,
    # If set, every request must carry this token (Bearer header or ?token=).
    # Strongly recommended when the port is reachable from outside the NAS.
    "auth_token": "",
    # Only paths below one of these roots may be browsed, scanned or deleted.
    "roots_allowlist": [
        "/volume1",
        "/volume2",
        "/volume3",
        "/volume4",
        "/volumeUSB1",
        "/volumeSATA1",
        "/var/services/homes",
    ],
    # --- scanning ---
    "exclude_dirs": list(DEFAULT_EXCLUDES),
    "exclude_globs": ["*.!sync", "*.part", "*.crdownload", "Thumbs.db", ".DS_Store"],
    "min_size": 1,
    "max_size": 0,  # 0 = no limit
    "follow_symlinks": False,
    "cross_filesystem": True,
    # Byte-for-byte confirmation of every MD5 match. Slower, but turns a
    # "same hash" claim into a proof. On by default because deletion is final.
    "verify_bytes": True,
    # --- near-duplicate (fuzzy) pass ---
    "near_duplicates": True,
    "near_threshold": 70,          # 0-100, minimum score to report a near pair
    "fuzzy_min_size": 4096,
    "fuzzy_max_size": 536870912,   # 512 MiB - ignored while fuzzy_max_bytes > 0
    # Bytes read per file for the fuzzy hash. The CTPH loop is pure Python and
    # runs at a couple of MiB/s, so hashing a whole film takes minutes while
    # hashing its first 16 MiB takes seconds. The trade: two files that only
    # start to differ past this point are reported as similar. That affects the
    # similarity column alone - exact duplicates go through the full MD5 and a
    # byte-for-byte comparison, and are unaffected. 0 = read the whole file.
    "fuzzy_max_bytes": 16777216,   # 16 MiB
    "fuzzy_bucket_cap": 600,       # max files compared pairwise inside one bucket
    "image_similarity": True,      # perceptual hash for images (needs Pillow)
    # --- deletion ---
    # quarantine  -> move into <root>/.dupfinder-trash/<scan-id>/ (reversible)
    # recycle     -> move into the DSM share recycle bin (#recycle)
    # permanent   -> unlink immediately
    "delete_mode": "quarantine",
    "protect_last_copy": True,     # never let a group reach zero surviving files
    # Rehearsal mode. Every check runs and the log fills up, but no file is
    # touched. Sticky on purpose: it is the setting you want to leave on while
    # you learn to trust the suggestions, not one to re-tick every time.
    "dry_run": False,
    # --- AI suggestions ---
    "ai_enabled": True,
    "ai_model": "claude-opus-5",
    "ai_effort": "high",
    "ai_batch_size": 12,
    "ai_max_groups": 300,
    # Read from the environment if empty. Never returned to the browser.
    "anthropic_api_key": "",
}

_LOCK = threading.Lock()


class Config:
    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.path = os.path.join(self.data_dir, "config.json")
        self._values = dict(DEFAULTS)
        os.makedirs(self.data_dir, exist_ok=True)
        self.load()

    # -- persistence ---------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                for key, value in stored.items():
                    if key in DEFAULTS:
                        self._values[key] = value
        except FileNotFoundError:
            self.save()
        except (OSError, ValueError):
            # A corrupt config must not stop the service from booting.
            pass

    def save(self) -> None:
        with _LOCK:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._values, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    # -- access --------------------------------------------------------
    def __getitem__(self, key):
        return self._values[key]

    def get(self, key, default=None):
        return self._values.get(key, default)

    def update(self, values: dict) -> dict:
        changed = {}
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            expected = type(DEFAULTS[key])
            if expected is bool and not isinstance(value, bool):
                value = str(value).lower() in ("1", "true", "yes", "on")
            elif expected is int and not isinstance(value, bool):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif expected is list and not isinstance(value, list):
                continue
            elif expected is str and not isinstance(value, str):
                value = str(value)
            self._values[key] = value
            changed[key] = value
        if changed:
            self.save()
        return changed

    def public(self) -> dict:
        """Config safe to hand to the browser (no secrets)."""
        out = dict(self._values)
        out["anthropic_api_key"] = "set" if self.api_key() else ""
        out["auth_token"] = "set" if self._values.get("auth_token") else ""
        return out

    def api_key(self) -> str:
        return (
            self._values.get("anthropic_api_key")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        ).strip()

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "dupfinder.db")
