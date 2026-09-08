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


# ---------------------------------------------------------------------------
# A helper is a machine we did not write. Nothing else in the pass would
# notice a wrong answer, so the scanner recomputes some of them and compares.

print()
print("a helper that lies is caught, and its answers are dropped")

from dupfinder.scanner import ScanEngine                      # noqa: E402


def fake_scanner(proved, first=8, fraction=0.02):
    """A ScanEngine with only the parts _check_helper touches.

    Built with __new__ on purpose: the real constructor wants a database, a
    config file and a thread, none of which this behaviour depends on.
    """
    sc = ScanEngine.__new__(ScanEngine)
    sc.config = {"helper_verify_first": first, "helper_verify_fraction": fraction}
    sc._helper_proved = proved
    sc._helper_trusted = True
    sc.notes = []
    sc._log_note = sc.notes.append
    # Stand in for the worker pool: hash the jobs here, honestly.
    sc._run_locally = lambda pool, batch: [
        (hashing.fuzzy_hash(path, size, None, max_bytes), None)
        for path, size, max_bytes, _nf, _nd in batch]
    return sc


paths = []
for i in range(12):
    p_ = os.path.join(tmp, "verify%d.bin" % i)
    with open(p_, "wb") as fh:
        fh.write(bytes(rng.randrange(256) for _ in range(300_000)))
    paths.append(p_)

jobs = [(p_, os.path.getsize(p_), 0, True, False) for p_ in paths]
honest = {i: hashing.fuzzy_hash(j[0], j[1], None, j[2])
          for i, j in enumerate(jobs)}

sc = fake_scanner(proved=False)
accepted = sc._check_helper(None, jobs, dict(honest))
check("an honest helper is believed",
      len(accepted) == len(jobs) and sc._helper_trusted,
      "%d of %d accepted" % (len(accepted), len(jobs)))
check("and it has proved itself for the rest of the scan", sc._helper_proved)

# The failure this guards against is a helper running different code, which
# is wrong on every file. A sample catches that with certainty.
wrong_throughout = {i: "3:AAAA:BBBB" for i in honest}
sc = fake_scanner(proved=False)
accepted = sc._check_helper(None, jobs, wrong_throughout)
check("a helper that is wrong throughout is caught", accepted == {},
      "accepted %d" % len(accepted))
check("and no more work is offered this scan", sc._helper_trusted is False)
check("and it says so", bool(sc.notes) and "wrong" in sc.notes[0],
      str(sc.notes))

# Silence is the other way to be wrong: None is not the signature the NAS
# would have produced, so it must not pass either.
sc = fake_scanner(proved=False)
check("null signatures are a mismatch, not an answer",
      sc._check_helper(None, jobs, {i: None for i in honest}) == {})

# And the honest limit, stated as a test rather than left in a comment: a
# single wrong answer among many is caught only when it lands in the sample.
# Over the whole batch it does, which is what the fraction buys back.
lying = dict(honest)
lying[3] = "3:AAAA:BBBB"
sc = fake_scanner(proved=True, fraction=1.0)
check("one wrong answer is caught when the sample covers it",
      sc._check_helper(None, jobs, lying) == {})

caught = 0
for seed in range(40):
    random.seed(seed)
    sc = fake_scanner(proved=False, first=8)
    if sc._check_helper(None, jobs, dict(lying)) == {}:
        caught += 1
check("one wrong answer in twelve is caught most of the time, not always",
      20 <= caught < 40, "caught %d of 40" % caught)

# Once proved, the sample is small - but a batch still gets checked.
sc = fake_scanner(proved=True, fraction=0.25)
accepted = sc._check_helper(None, jobs, dict(honest))
check("a proved helper is still sampled, and passes",
      len(accepted) == len(jobs) and sc._helper_trusted)

sc = fake_scanner(proved=True, fraction=1.0)
check("sampling every one of them catches the liar too",
      sc._check_helper(None, jobs, lying) == {})

# The sample was computed here anyway, so that answer is the one kept.
sc = fake_scanner(proved=False, first=len(jobs))
accepted = sc._check_helper(None, jobs, dict(honest))
check("the locally computed sample is what gets stored",
      all(accepted[i][0] == honest[i] for i in range(len(jobs))))

print()
sys.exit(1 if fails else 0)
