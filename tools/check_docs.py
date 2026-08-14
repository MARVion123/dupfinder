"""Catch the ways the documentation goes quietly wrong.

    python3 tools/check_docs.py

The product site lives in this repository and the code moves underneath it. It
has already rotted twice: the install instructions described a package that
could not be installed, and every install command spelled out one particular
build number, which was wrong the moment the build was rebuilt.

None of that is something a human notices while reading a diff, and none of it
is a judgement call. What is left over - whether the prose is still *true* -
this cannot check, and does not pretend to.
"""

from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ("docs/index.html", "docs/de/index.html")
PROSE = ("README.md",) + PAGES

failures: list[str] = []
checked = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checked
    checked += 1
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name, "" if ok else "\n         " + detail))
    if not ok:
        failures.append(name)


def read(rel: str) -> str:
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


# --- 1. no build number frozen into an instruction --------------------------
# The one that has actually bitten. Commands must use a glob or a placeholder
# so they survive the next build.

FROZEN = re.compile(r"dupfinder-\d+\.\d+\.\d+-\d{4}\.spk")

print("\nbuild numbers")
for rel in PROSE:
    hits = sorted(set(FROZEN.findall(read(rel))))
    check("%s names no particular build" % rel, not hits,
          "found %s - use dupfinder-*.spk so it does not go stale" % ", ".join(hits))


# --- 2. every repo path mentioned really exists -----------------------------
# Renaming a script and leaving the docs pointing at the old name is the other
# way this rots, and it reads perfectly fine in a diff.

# The lookbehind is what makes this usable. Without it the pattern also fires
# inside absolute NAS paths (/volume1/dupfinder/src/dupfinder/install/...) and
# inside URLs (github.com/MARVion123/dupfinder/releases/...), reporting both as
# missing repository files. A check that cries wolf gets ignored, which is
# worse than not having it.
MENTIONED = re.compile(
    r"(?<![\w/.-])((?:install|tools|tests|dupfinder|docs)/[A-Za-z0-9_./-]*[A-Za-z0-9_-])")

# Build outputs are not in git on purpose.
IGNORED_PREFIXES = ("install/spk/dist/",)

print("\npaths that the docs tell people to run")
for rel in PROSE:
    # Rejoin shell line continuations first. A URL broken across lines with a
    # trailing backslash puts its second half at the start of a line, where the
    # lookbehind cannot see that it is the middle of a URL.
    text = re.sub(r"\\\s*\n\s*", "", read(rel))
    missing = set()
    for path in MENTIONED.findall(text):
        path = path.rstrip(".,:;)")
        if "*" in path or "<" in path or path.endswith("/"):
            continue
        if path.startswith(IGNORED_PREFIXES):
            continue
        # Directories are mentioned as prose too; only check things with a
        # suffix, which is what a command actually invokes.
        if "." not in os.path.basename(path):
            continue
        if not os.path.exists(os.path.join(REPO, path)):
            missing.add(path)
    check("%s: referenced files exist" % rel, not missing,
          "missing: %s" % ", ".join(sorted(missing)))


# --- 3. the two languages have not drifted apart ----------------------------
# A section added to one page and forgotten in the other is invisible unless
# you read both, which nobody does.

print("\nthe two language versions")
sections = {}
for rel in PAGES:
    sections[rel] = re.findall(r'<section id="([^"]+)"', read(rel))

only_en = set(sections[PAGES[0]]) - set(sections[PAGES[1]])
only_de = set(sections[PAGES[1]]) - set(sections[PAGES[0]])
check("same sections on both pages", not only_en and not only_de,
      "only English: %s | only German: %s" % (sorted(only_en) or "-", sorted(only_de) or "-"))
check("sections in the same order", sections[PAGES[0]] == sections[PAGES[1]],
      "%s\n         vs %s" % (sections[PAGES[0]], sections[PAGES[1]]))

anchors = {}
for rel in PAGES:
    text = read(rel)
    anchors[rel] = (set(re.findall(r'id="([^"]+)"', text)),
                    set(re.findall(r'href="#([^"]+)"', text)))
for rel, (ids, refs) in anchors.items():
    check("%s: in-page links resolve" % rel, not (refs - ids),
          "dangling: %s" % sorted(refs - ids))


# --- 4. the images the pages ask for are actually committed -----------------

print("\nassets")
for rel in PAGES:
    base = os.path.dirname(os.path.join(REPO, rel))
    srcs = [s for s in re.findall(r'src="([^"]+)"', read(rel)) if not s.startswith("data:")]
    gone = [s for s in srcs if not os.path.isfile(os.path.normpath(os.path.join(base, s)))]
    check("%s: %d image(s) present" % (rel, len(srcs)), not gone, "missing: %s" % gone)
    # Raster images only. The favicon is an inline SVG on purpose - it is a few
    # hundred bytes and saves a request.
    inlined = re.findall(r"data:image/(png|jpeg|jpg|webp|gif);base64", read(rel))
    check("%s: no inlined raster images" % rel, not inlined,
          "found %d - they belong in docs/assets/, where they cost a third less"
          % len(inlined))


# --- 5. the site and the package agree about where to point -----------------

print("\nthe site and the package agree")
spk = read("install/spk/build_spk.py")
homepage = re.search(r'HOMEPAGE = "([^"]+)"', spk)
check("build_spk.py declares a homepage", bool(homepage))
if homepage:
    url = homepage.group(1).rstrip("/")
    for rel in PAGES:
        text = read(rel)
        canonical = re.search(r'rel="canonical" href="([^"]+)"', text)
        check("%s: canonical matches the package's helpurl" % rel,
              bool(canonical) and canonical.group(1).rstrip("/").startswith(url),
              "package points at %s, page says %s"
              % (url, canonical.group(1) if canonical else "(no canonical)"))


# --- 6. nothing half-written ------------------------------------------------

print("\nleftovers")
for rel in PROSE:
    text = read(rel)
    marks = [m for m in ("@PORT@", "TODO:", "FIXME", "lorem ipsum") if m in text]
    check("%s: no placeholders" % rel, not marks, "found: %s" % marks)


print()
if failures:
    print("%d of %d checks failed: %s" % (len(failures), checked, ", ".join(failures)))
    print("\nThe docs describe something the repository no longer contains.")
    sys.exit(1)
print("%d checks passed - the mechanical parts of the docs are consistent." % checked)
print("Whether the prose is still true is not something this can tell you.")
