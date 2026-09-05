"""Hashing primitives: quick hash, full MD5, fuzzy (CTPH) hash, image dHash.

The fuzzy hash is a pure-Python spamsum/ssdeep implementation. It is what lets
the scanner say "these two files are 84% alike" instead of only "identical" or
"different" - no C extension needed, which matters on a stock DSM box.
"""

from __future__ import annotations

import hashlib
import io
import os

CHUNK = 1024 * 1024
EDGE = 64 * 1024

# --- spamsum constants (matching the reference ssdeep implementation) -----
ROLL_WINDOW = 7
HASH_PRIME = 0x01000193
HASH_INIT = 0x28021967
SPAMSUM_LENGTH = 64
MIN_BLOCKSIZE = 3
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
MASK32 = 0xFFFFFFFF


class Cancelled(Exception):
    """Raised when a long-running hash is aborted by the user."""


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# ---------------------------------------------------------------------------
# Level 1: cheap signature (head + tail + size). Kills most non-duplicates
# after reading 128 KiB instead of the whole file.
# ---------------------------------------------------------------------------
def quick_hash(path: str, size: int) -> str:
    h = hashlib.md5()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        head = fh.read(EDGE)
        h.update(head)
        if size > EDGE * 2:
            fh.seek(-EDGE, os.SEEK_END)
            h.update(fh.read(EDGE))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Level 2: full MD5.
# ---------------------------------------------------------------------------
def full_md5(path: str, cancel=None, progress=None) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            _check(cancel)
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
            if progress is not None:
                progress(len(block))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Level 3: byte-for-byte comparison. MD5 collisions are astronomically
# unlikely but deletion is irreversible, so we can afford to prove it.
# ---------------------------------------------------------------------------
def files_identical(path_a: str, path_b: str, cancel=None) -> bool:
    try:
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            while True:
                _check(cancel)
                ba = fa.read(CHUNK)
                bb = fb.read(CHUNK)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Level 4: context-triggered piecewise hash (fuzzy / CTPH).
# ---------------------------------------------------------------------------
class _Roller:
    __slots__ = ("win", "h1", "h2", "h3", "n")

    def __init__(self):
        self.win = [0] * ROLL_WINDOW
        self.h1 = 0
        self.h2 = 0
        self.h3 = 0
        self.n = 0

    def update(self, b: int) -> int:
        self.h2 = (self.h2 - self.h1) & MASK32
        self.h2 = (self.h2 + ROLL_WINDOW * b) & MASK32
        self.h1 = (self.h1 + b) & MASK32
        self.h1 = (self.h1 - self.win[self.n]) & MASK32
        self.win[self.n] = b
        self.n = (self.n + 1) % ROLL_WINDOW
        self.h3 = ((self.h3 << 5) & MASK32) ^ b
        return (self.h1 + self.h2 + self.h3) & MASK32


def _sum_hash(h: int, b: int) -> int:
    return (((h * HASH_PRIME) & MASK32) ^ b) & MASK32


def _digest_stream(fh, blocksize: int, cancel=None, limit: int = 0, offset: int = 0):
    """One pass over the file at a given block size -> (sig1, sig2).

    `limit` > 0 stops after that many bytes, starting at `offset`.

    The roller and both sum hashes are inlined here on purpose, at the cost of
    readability: this loop body runs once per byte of every candidate file, and
    on a NAS CPU the three function calls the tidy version needed cost more
    than the arithmetic between them. Worth about 1.6x. `_Roller` and
    `_sum_hash` above are kept as the readable statement of what this does.
    """
    win = [0] * ROLL_WINDOW
    r1 = r2 = r3 = 0
    n = 0
    h1 = h2 = HASH_INIT
    sig1: list[str] = []
    sig2: list[str] = []
    add1 = sig1.append
    add2 = sig2.append
    cap1 = SPAMSUM_LENGTH - 1
    cap2 = SPAMSUM_LENGTH // 2 - 1
    bs2 = blocksize * 2
    rw = ROLL_WINDOW
    prime = HASH_PRIME
    mask = MASK32
    init = HASH_INIT
    b64 = B64
    remaining = limit if limit > 0 else -1
    fh.seek(offset)
    while remaining != 0:
        _check(cancel)
        block = fh.read(CHUNK if remaining < 0 else min(CHUNK, remaining))
        if not block:
            break
        if remaining > 0:
            remaining -= len(block)
        for b in block:
            h1 = ((h1 * prime) & mask) ^ b
            h2 = ((h2 * prime) & mask) ^ b
            r2 = (r2 - r1 + rw * b) & mask
            r1 = (r1 + b - win[n]) & mask
            win[n] = b
            n = n + 1 if n + 1 < rw else 0
            r3 = ((r3 << 5) & mask) ^ b
            r = (r1 + r2 + r3) & mask
            if r % blocksize == blocksize - 1:
                if len(sig1) < cap1:
                    add1(b64[h1 % 64])
                    h1 = init
                # r % 2*bs == 2*bs-1 implies r % bs == bs-1, so the second
                # trigger can only ever fire when the first one did. Nesting it
                # saves a modulo on every byte.
                if r % bs2 == bs2 - 1 and len(sig2) < cap2:
                    add2(b64[h2 % 64])
                    h2 = init
    if r1 or r2 or r3:
        add1(b64[h1 % 64])
        add2(b64[h2 % 64])
    return "".join(sig1), "".join(sig2)


def fuzzy_blocksize_for(size: int, max_bytes: int = 0) -> int:
    """The block size fuzzy_hash() starts from for a file of this size.

    Derivable without touching the file, which lets the scanner spot a cached
    signature that was produced under a different byte budget.
    """
    if size < MIN_BLOCKSIZE * SPAMSUM_LENGTH:
        return 0
    span = size if max_bytes <= 0 else min(size, max_bytes)
    blocksize = MIN_BLOCKSIZE
    while blocksize * SPAMSUM_LENGTH < span:
        blocksize *= 2
    return blocksize


def fuzzy_hash(path: str, size: int | None = None, cancel=None,
               max_bytes: int = 0) -> str | None:
    """Return "blocksize:sig1:sig2", or None if the file is too small/unreadable.

    `max_bytes` > 0 hashes only that many bytes, taken from the *middle* of the
    file rather than the start. The block size then follows the number of bytes
    actually hashed rather than the file length, so a 2 GiB and a 6 GiB video
    still land in the same bucket and remain comparable to each other.

    The middle, because the start of a media file is the container header and
    the opening of the stream: the part two unrelated recordings from the same
    camera share, and the part a remux rewrites. Measured on a payload wrapped
    in two different headers, against an unrelated file with the same header:

        window            remux    remux, header 300 KiB shorter   unrelated
        2 MiB from start    71%                              72%          0%
        1 MiB from middle  100%                              88%          0%

    Half the bytes, and the difference decides the outcome rather than just
    looking better: the near-duplicate threshold is 70 by default and often
    raised to 90, and at 90 neither prefix score is reported at all. Three
    smaller windows spread across the file were tried too and are worse under a
    shifted payload (44%) - a short window tolerates less drift.

    A shift larger than the window still defeats this, and so does a
    re-encode; neither is something byte-level hashing can reach.
    """
    if size is None:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
    if size < MIN_BLOCKSIZE * SPAMSUM_LENGTH:
        return None
    span = size if max_bytes <= 0 else min(size, max_bytes)

    blocksize = MIN_BLOCKSIZE
    while blocksize * SPAMSUM_LENGTH < span:
        blocksize *= 2

    # Centre the window. Files smaller than the budget are read whole, so this
    # is 0 for them and nothing about the small-file case changes.
    offset = max(0, (size - span) // 2) if max_bytes > 0 else 0

    try:
        with open(path, "rb") as fh:
            # The loop below halves the block size and starts over when the
            # signature came out too short. Buffering the window to spare those
            # re-reads was tried and reverted: on low-entropy files, where the
            # retries actually fire, it changed 19.50s to 19.61s. The cost is
            # the per-byte Python loop running again, not the read - the second
            # pass comes out of the page cache either way.
            for _ in range(6):
                sig1, sig2 = _digest_stream(fh, blocksize, cancel, span, offset)
                # Too few trigger points -> signature carries little information.
                if len(sig1) >= SPAMSUM_LENGTH // 2 or blocksize <= MIN_BLOCKSIZE:
                    return "%d:%s:%s" % (blocksize, sig1, sig2)
                blocksize = max(MIN_BLOCKSIZE, blocksize // 2)
    except (OSError, Cancelled) as exc:
        if isinstance(exc, Cancelled):
            raise
        return None
    return None


def _strip_runs(s: str) -> str:
    """Collapse runs of >3 identical characters (ssdeep does this too)."""
    out = []
    run = 0
    prev = ""
    for ch in s:
        if ch == prev:
            run += 1
        else:
            run = 1
            prev = ch
        if run <= 3:
            out.append(ch)
    return "".join(out)


def _has_common_substring(a: str, b: str, n: int = 7) -> bool:
    if len(a) < n or len(b) < n:
        return False
    seen = {a[i:i + n] for i in range(len(a) - n + 1)}
    return any(b[i:i + n] in seen for i in range(len(b) - n + 1))


def _edit_distance(a: str, b: str) -> int:
    """Weighted Levenshtein: insert/delete = 1, substitute = 2 (ssdeep weights)."""
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(0, lb + 1))
    cur = [0] * (lb + 1)
    for i in range(1, la + 1):
        cur[0] = i
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 2
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev, cur = cur, prev
    return prev[lb]


def _score(sig_a: str, sig_b: str, blocksize: int) -> int:
    a = _strip_runs(sig_a)
    b = _strip_runs(sig_b)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if not _has_common_substring(a, b):
        return 0
    dist = _edit_distance(a, b)
    # ssdeep's staged integer scaling: normalise the distance against the
    # combined signature length, then re-express it as a percentage.
    scaled = (dist * SPAMSUM_LENGTH) // (len(a) + len(b))
    score = 100 - (scaled * 100) // SPAMSUM_LENGTH
    if score <= 0:
        return 0
    # Small block sizes describe small files; cap the confidence accordingly so
    # two tiny files sharing a few trigger points cannot claim 99% similarity.
    cap = int(blocksize / MIN_BLOCKSIZE * min(len(a), len(b)))
    return min(score, cap, 100)


def fuzzy_compare(hash_a: str | None, hash_b: str | None) -> int:
    """Similarity 0-100 between two fuzzy hashes."""
    if not hash_a or not hash_b:
        return 0
    try:
        bs_a, a1, a2 = hash_a.split(":", 2)
        bs_b, b1, b2 = hash_b.split(":", 2)
        bs_a = int(bs_a)
        bs_b = int(bs_b)
    except ValueError:
        return 0
    if bs_a == bs_b:
        return max(_score(a1, b1, bs_a), _score(a2, b2, bs_a * 2))
    if bs_a == bs_b * 2:
        return _score(a2, b1, bs_a)
    if bs_b == bs_a * 2:
        return _score(a1, b2, bs_b)
    return 0


def fuzzy_blocksize(h: str | None) -> int:
    if not h:
        return 0
    try:
        return int(h.split(":", 1)[0])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Level 5 (optional): perceptual hash for images.
# Catches re-encoded / resized photos that share no bytes at all. Needs Pillow;
# degrades silently to "no image signal" when it is not installed.
# ---------------------------------------------------------------------------
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif",
}

_PIL_CHECKED = False
_PIL_IMAGE = None
_PIL_OPS = None


def pillow_available() -> bool:
    global _PIL_CHECKED, _PIL_IMAGE, _PIL_OPS
    if not _PIL_CHECKED:
        _PIL_CHECKED = True
        try:
            from PIL import Image, ImageOps  # type: ignore

            _PIL_IMAGE = Image
            _PIL_OPS = ImageOps
        except Exception:
            _PIL_IMAGE = None
            _PIL_OPS = None
    return _PIL_IMAGE is not None


def image_dhash(path: str) -> str | None:
    """64-bit difference hash of an image, as 16 hex chars."""
    if not pillow_available():
        return None
    try:
        with _PIL_IMAGE.open(path) as im:  # type: ignore[union-attr]
            im = im.convert("L").resize((9, 8))
            px = list(im.getdata())
    except Exception:
        return None
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits <<= 1
            if px[base + col] > px[base + col + 1]:
                bits |= 1
    return "%016x" % bits


# EXIF tag numbers straight from the spec - Pillow hands them back as plain
# ints, and importing PIL.ExifTags just to name them would make this module
# depend on Pillow at import time.
_EXIF_IFD = 0x8769
_EXIF_DATETIME_ORIGINAL = 0x9003
_EXIF_DATETIME_DIGITIZED = 0x9004
_TIFF_DATETIME = 0x0132
_TIFF_MAKE = 0x010F
_TIFF_MODEL = 0x0110


def _exif_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value).replace("\x00", " ").strip()
    return value or None


def image_meta(path: str) -> dict | None:
    """Dimensions and, for photos, the capture date and the camera.

    Deliberately not called while listing results: that would mean opening and
    header-parsing every image on the page. It runs for one group at a time,
    when the group is expanded.
    """
    if not pillow_available():
        return None
    try:
        with _PIL_IMAGE.open(path) as im:  # type: ignore[union-attr]
            meta = {"width": im.width, "height": im.height, "format": im.format}
            try:
                exif = im.getexif()
            except Exception:
                exif = None
    except Exception:
        return None
    if not exif:
        return meta

    try:
        sub = exif.get_ifd(_EXIF_IFD) or {}
    except Exception:
        sub = {}
    for source, tag in ((sub, _EXIF_DATETIME_ORIGINAL),
                        (sub, _EXIF_DATETIME_DIGITIZED),
                        (exif, _TIFF_DATETIME)):
        raw = _exif_text(source.get(tag)) if source else None
        if raw:
            # EXIF writes "2019:07:14 18:22:05". Only the date part uses colons
            # as separators, so replace exactly the first two.
            meta["taken"] = raw.replace(":", "-", 2)
            break

    make = _exif_text(exif.get(_TIFF_MAKE))
    model = _exif_text(exif.get(_TIFF_MODEL))
    if make and model and model.lower().startswith(make.split()[0].lower()):
        make = None                      # "NIKON CORPORATION" + "NIKON D750"
    camera = " ".join(x for x in (make, model) if x)
    if camera:
        meta["camera"] = camera
    return meta


def image_thumbnail(path: str, size: int = 360) -> bytes | None:
    """JPEG bytes of a downscaled preview, or None if it cannot be rendered."""
    if not pillow_available():
        return None
    try:
        with _PIL_IMAGE.open(path) as im:  # type: ignore[union-attr]
            # Honour the EXIF orientation flag. Without this, phone photos are
            # shown rotated here and upright in every other viewer, which reads
            # as "these two files differ" when they do not.
            im = _PIL_OPS.exif_transpose(im)  # type: ignore[union-attr]
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80, optimize=True)
            return buf.getvalue()
    except Exception:
        return None


def dhash_similarity(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 0
    try:
        diff = bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 0
    return int(round((64 - diff) * 100 / 64))
