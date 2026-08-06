"""Path safety.

Everything the service touches - browsing, scanning, deleting - is funnelled
through here. A model-suggested or browser-supplied path is untrusted input;
it must resolve to a real location under one of the configured roots before
any filesystem call is made with it.
"""

from __future__ import annotations

import os


class UnsafePath(Exception):
    pass


def normalise(path: str) -> str:
    if not path:
        raise UnsafePath("empty path")
    # realpath collapses "..", resolves symlinks, and defeats encoded traversal
    # once the browser has already URL-decoded it.
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def is_within(child: str, parent: str) -> bool:
    child = os.path.normpath(child)
    parent = os.path.normpath(parent)
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def check_allowed(path: str, roots: list[str]) -> str:
    """Resolve `path` and confirm it lives under an allowed root."""
    resolved = normalise(path)
    if not roots:
        return resolved
    for root in roots:
        try:
            root_resolved = normalise(root)
        except UnsafePath:
            continue
        if not os.path.isdir(root_resolved):
            continue
        if is_within(resolved, root_resolved):
            return resolved
    raise UnsafePath(
        "%s is outside the allowed roots (%s)" % (path, ", ".join(roots))
    )


def available_roots(roots: list[str]) -> list[str]:
    out = []
    for root in roots:
        try:
            resolved = normalise(root)
        except UnsafePath:
            continue
        if os.path.isdir(resolved) and resolved not in out:
            out.append(resolved)
    return out
