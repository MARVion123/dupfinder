"""Checks on the signature window index.

    python3 tests/test_fuzzy_index.py


Two claims to settle. That it finds every pair the exhaustive comparison finds
- if it drops one, the tool silently stops reporting duplicates. And that it
finds pairs the old capped version could not reach, which is the reason for
doing this at all.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dupfinder import hashing                              # noqa: E402

fails = []


def check(label, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label, "" if ok else "   <- " + detail))
    if not ok:
        fails.append(label)


def signature(seed, length=60, mutate=0):
    """A plausible signature: base64 characters, optionally perturbed."""
    rng = random.Random(seed)
    s1 = "".join(rng.choice(hashing.B64) for _ in range(length))
    s2 = "".join(rng.choice(hashing.B64) for _ in range(length // 2))
    if mutate:
        chars = list(s1)
        for _ in range(mutate):
            i = rng.randrange(len(chars))
            chars[i] = rng.choice(hashing.B64)
        s1 = "".join(chars)
    return "49152:%s:%s" % (s1, s2)


# A population where some files are near-copies of each other and most are not.
sigs = {}
for i in range(300):
    sigs[i] = signature(i)
for i in range(300, 340):                 # 40 near-copies of the first 40
    sigs[i] = signature(i - 300, mutate=4)

print("exhaustive comparison as the reference")
truth = set()
ids = sorted(sigs)
for i, a in enumerate(ids):
    for b in ids[i + 1:]:
        if hashing.fuzzy_compare(sigs[a], sigs[b]) > 0:
            truth.add((a, b))
print("    %d pairs score above zero out of %d possible"
      % (len(truth), len(ids) * (len(ids) - 1) // 2))

print("\nthe index")
index = {}
for fid, sig in sigs.items():
    for key in hashing.fuzzy_grams(sig):
        index.setdefault(key, []).append(fid)
found = set()
for key, members in index.items():
    if len(members) > 200:                # the same cutoff the scanner applies
        continue
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            found.add((a, b) if a < b else (b, a))

missing = truth - found
check("finds every pair the exhaustive comparison found", not missing,
      "missed %d, e.g. %s" % (len(missing), sorted(missing)[:3]))
check("does not need to score every possible pair",
      len(found) < len(ids) * (len(ids) - 1) // 2,
      "%d candidates vs %d possible" % (len(found), len(ids) * (len(ids) - 1) // 2))
print("    %d candidate pairs to score instead of %d  (%.1f%%)"
      % (len(found), len(ids) * (len(ids) - 1) // 2,
         100.0 * len(found) / (len(ids) * (len(ids) - 1) // 2)))

print("\nwhat the old cap did to the same population")
cap = 200                                  # smaller than the population, as on a NAS
capped = sorted(ids)[:cap]
capped_pairs = set()
for i, a in enumerate(capped):
    for b in capped[i + 1:]:
        if hashing.fuzzy_compare(sigs[a], sigs[b]) > 0:
            capped_pairs.add((a, b))
lost = truth - capped_pairs
check("the cap loses real pairs", bool(lost),
      "it lost none here, so this population does not show the problem")
print("    a cap of %d finds %d of %d real pairs, losing %d"
      % (cap, len(capped_pairs), len(truth), len(lost)))

print("\ncomparing across block sizes")
rng = random.Random(1)
def _s(n):
    return "".join(rng.choice(hashing.B64) for _ in range(n))

shared = _s(50)
low = "100:%s:%s" % (_s(50), shared)      # levels 100 and 200
high = "200:%s:%s" % (shared, _s(25))     # levels 200 and 400
check("identical on the shared level scores 100",
      hashing.fuzzy_compare(low, high) == 100, str(hashing.fuzzy_compare(low, high)))
check("and the same in the other direction",
      hashing.fuzzy_compare(high, low) == 100, str(hashing.fuzzy_compare(high, low)))
check("unrelated on the shared level still scores 0",
      hashing.fuzzy_compare(low, "200:%s:%s" % (_s(50), _s(25))) == 0)
check("block sizes four apart share no level",
      hashing.fuzzy_compare("100:%s:%s" % (shared, _s(25)),
                            "400:%s:%s" % (shared, _s(25))) == 0)

print()
sys.exit(1 if fails else 0)
