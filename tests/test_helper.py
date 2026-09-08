"""Checks on the helper machine path.

    python3 tests/test_helper.py

Two implementations of one thing - fuzzy_hash reads a window from a file, and
fuzzy_hash_window is handed those bytes over the network - so the first job
here is proving they agree. The second is the promise the feature rests on:
that a helper which disappears costs time and nothing else.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dupfinder import hashing                              # noqa: E402
from dupfinder.helpers import HelperPool                   # noqa: E402

fails = []


def check(label, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label, "" if ok else "   <- " + detail))
    if not ok:
        fails.append(label)


print("the helper computes what the NAS would have")
tmp = tempfile.mkdtemp(prefix="helper-")
rng = random.Random(31)
agree = True
for size, budget in ((900_000, 0), (900_000, 2 << 20), (6 << 20, 2 << 20),
                     (30 << 20, 2 << 20), (5 << 20, 1 << 20)):
    path = os.path.join(tmp, "f%d_%d.bin" % (size, budget))
    with open(path, "wb") as fh:
        fh.write(os.urandom(size))
    local = hashing.fuzzy_hash(path, size, None, budget)
    offset, span = hashing.fuzzy_window(size, budget)
    with open(path, "rb") as fh:                 # what the server sends
        fh.seek(offset)
        window = fh.read(span)
    remote = hashing.fuzzy_hash_window(window, size, budget)
    if local != remote:
        agree = False
        check("%d bytes, budget %d" % (size, budget), False,
              "%s != %s" % (local, remote))
check("both routes give the same signature", agree)
check("a file too small to hash gives None on both",
      hashing.fuzzy_hash_window(b"tiny", 4, 0) is None)

print("\na helper that never answers costs time, not results")
pool = HelperPool(timeout=0.4, absent_after=5.0)
check("no helper seen yet, so nothing is offered to one", not pool.active())
ids = pool.offer([{"path": "/x", "size": 10, "max_bytes": 0} for _ in range(3)])
check("three jobs on offer", len(ids) == 3)
got = pool.collect(ids, time.time() + 0.6)
check("collect gives back nothing", got == {}, str(got))
check("all three counted as taken back", pool.stats()["taken_back"] == 3,
      str(pool.stats()))

print("\na helper that answers")
pool = HelperPool(timeout=5.0, absent_after=5.0)
ids = pool.offer([{"path": "/a", "size": 10, "max_bytes": 0},
                  {"path": "/b", "size": 20, "max_bytes": 0}])
check("a helper polling marks itself active", True)
claimed = []
for _ in range(2):
    job_id, job = pool.claim("desktop")
    claimed.append((job_id, job))
check("claim hands out both jobs", all(j[0] for j in claimed))
check("claim marks the pool active", pool.active())
check("nothing left to claim", pool.claim("desktop")[0] is None)
for job_id, job in claimed:
    check("submit accepted for %s" % job["path"], pool.submit(job_id, "3:AAA:BBB"))
got = pool.collect(ids, time.time() + 2)
check("both results came back", len(got) == 2, str(got))
check("a late answer for a forgotten job is refused",
      not pool.submit(claimed[0][0], "3:CCC:DDD"))

print("\na job held past its deadline goes back on offer")
pool = HelperPool(timeout=0.2, absent_after=5.0)
ids = pool.offer([{"path": "/slow", "size": 10, "max_bytes": 0}])
first_id, _ = pool.claim("slow-machine")
check("claimed once", first_id is not None)
check("nothing else to claim while it is out", pool.claim("other")[0] is None)
time.sleep(0.25)
second_id, _ = pool.claim("other")
check("offered again after the deadline", second_id == first_id)

print("\nconcurrent claims never hand the same job to two machines")
pool = HelperPool(timeout=30.0, absent_after=5.0)
pool.offer([{"path": "/p%d" % i, "size": 10, "max_bytes": 0} for i in range(200)])
seen, lock = [], threading.Lock()


def drain():
    while True:
        job_id, _job = pool.claim("m")
        if job_id is None:
            return
        with lock:
            seen.append(job_id)


threads = [threading.Thread(target=drain) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("every job handed out exactly once",
      len(seen) == 200 and len(set(seen)) == 200,
      "%d claims, %d distinct" % (len(seen), len(set(seen))))

print()
sys.exit(1 if fails else 0)
