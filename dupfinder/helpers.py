"""Work offered to other machines, and never depended on.

The near-duplicate pass is the one a NAS cannot afford. Measured on a four-core
DS: 0.33 MiB/s per core on the CTPH loop, 0.99 across the three workers it
uses, two seconds per file. A desktop does the same work about forty times
faster, and the 2 MiB window a helper needs costs 0.018 s over gigabit against
0.049 s to hash - so the network is not the limit, the CPU is.

The contract, which is the whole design:

    The NAS finishes every scan on its own.

A helper that connects makes the pass faster. A helper that vanishes mid-scan
makes it slower and nothing else: jobs it does not return within a timeout are
computed on the NAS. People start a scan and go on holiday, and a scan that
quietly depended on a desktop that went to sleep would waste the week rather
than the night.

Only the byte hash is ever offered. Perceptual hashing of images is fast
compiled code and not worth a round trip, and video frames need ffmpeg on the
machine holding the file.

A helper never names a path. It asks for work and is handed bytes the server
chose - so a helper cannot read anything the scan was not going to hash
anyway, which a "give me this byte range of that file" endpoint would have
allowed.
"""

from __future__ import annotations

import threading
import time
import uuid


class HelperPool:
    """Jobs on offer to other machines. Thread-safe.

    Lives for the length of one scan. `offer` puts work up; `claim` and
    `submit` are driven by the HTTP endpoints; `collect` is what the scanner
    calls to take back whatever arrived in time.
    """

    def __init__(self, timeout: float = 60.0, absent_after: float = 30.0):
        # How long a claimed job may stay out before the NAS does it anyway.
        self.timeout = float(timeout)
        # A helper that has not asked for work in this long counts as gone, so
        # later chunks stop paying the timeout for a machine that left.
        self.absent_after = float(absent_after)
        self._lock = threading.Lock()
        self._ready: dict[str, dict] = {}      # job id -> job, claimable
        self._out: dict[str, tuple] = {}       # job id -> (job, deadline)
        self._done: dict[str, str | None] = {}  # job id -> signature
        self._last_seen = 0.0
        self._helpers: dict[str, float] = {}   # helper id -> last seen
        self.completed = 0                     # jobs a helper actually returned
        self.reclaimed = 0                     # jobs it did not

    # -- the scanner's side --------------------------------------------
    def active(self) -> bool:
        """Has any helper asked for work recently enough to be worth waiting for?"""
        with self._lock:
            return (time.time() - self._last_seen) < self.absent_after

    def offer(self, jobs) -> list[str]:
        """Put jobs up for claiming. Returns their ids, in the order given."""
        ids = []
        now = time.time()
        with self._lock:
            for job in jobs:
                job_id = uuid.uuid4().hex[:16]
                self._ready[job_id] = job
                ids.append(job_id)
        return ids

    def collect(self, job_ids, deadline: float) -> dict[str, str | None]:
        """Wait for offered jobs until `deadline`, then give up on the rest.

        Returns only what came back. Whatever is missing from the result is the
        caller's to compute - that is the point.
        """
        wanted = set(job_ids)
        got: dict[str, str | None] = {}
        while wanted and time.time() < deadline:
            with self._lock:
                for job_id in list(wanted):
                    if job_id in self._done:
                        got[job_id] = self._done.pop(job_id)
                        wanted.discard(job_id)
                # Nothing is out and nothing is claimable: no point waiting.
                idle = not any(j in self._out or j in self._ready for j in wanted)
            if not wanted or idle:
                break
            time.sleep(0.05)

        with self._lock:
            for job_id in wanted:
                self._ready.pop(job_id, None)
                self._out.pop(job_id, None)
                self._done.pop(job_id, None)
            self.completed += len(got)
            self.reclaimed += len(wanted)
        return got

    def withdraw(self) -> None:
        """Drop everything still on offer, at the end of a scan."""
        with self._lock:
            self._ready.clear()
            self._out.clear()
            self._done.clear()

    # -- the helper's side, driven by the HTTP endpoints ---------------
    def claim(self, helper_id: str):
        """Hand out one job, or None. Also records that a helper is alive."""
        now = time.time()
        with self._lock:
            self._last_seen = now
            self._helpers[helper_id] = now
            # A job whose deadline passed goes back on offer - the helper
            # holding it may have died. If the scanner has meanwhile computed
            # it itself, collect() has already forgotten the id and a late
            # answer is dropped.
            for job_id, (job, deadline) in list(self._out.items()):
                if deadline < now:
                    del self._out[job_id]
                    self._ready[job_id] = job
            if not self._ready:
                return None, None
            job_id, job = next(iter(self._ready.items()))
            del self._ready[job_id]
            self._out[job_id] = (job, now + self.timeout)
            return job_id, job

    def submit(self, job_id: str, signature) -> bool:
        """Take a result back. False if the job was already given up on."""
        with self._lock:
            if job_id not in self._out:
                return False
            del self._out[job_id]
            self._done[job_id] = signature
            return True

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            live = sum(1 for seen in self._helpers.values()
                       if now - seen < self.absent_after)
            return {"helpers": live, "done_by_helpers": self.completed,
                    "taken_back": self.reclaimed, "waiting": len(self._ready)}
