"""Checks on the video frame signature.

    python3 tests/test_video_signature.py


It runs without ffmpeg, so the frame *extraction* is not covered here.
Everything after it is not: _dhash_png takes the PNG bytes ffmpeg would write,
and video_compare decides what the scanner does with the result. Those are the
parts that carry the logic, so those are the parts worth pinning down.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dupfinder import hashing                              # noqa: E402

assert hashing.pillow_available(), "needs Pillow"
from PIL import Image, ImageDraw                           # noqa: E402

fails = []


def check(label, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label, "" if ok else "   <- " + detail))
    if not ok:
        fails.append(label)


def frame(seed, size=(160, 120), blur=0):
    """A synthetic 'frame': a few shapes, deterministic from the seed."""
    im = Image.new("L", size, 20 + seed * 7 % 60)
    d = ImageDraw.Draw(im)
    for i in range(5):
        x = (seed * 37 + i * 29) % size[0]
        y = (seed * 17 + i * 41) % size[1]
        d.ellipse([x, y, x + 40 + i * 3, y + 30 + i * 2], fill=200 - i * 25)
    if blur:
        # Stand-in for a lower-bitrate re-encode: same picture, softened.
        im = im.resize((size[0] // blur, size[1] // blur)).resize(size)
    return im


def as_png(im):
    buf = io.BytesIO()
    im.resize((9, 8)).save(buf, format="PNG")       # ffmpeg does the scaling
    return buf.getvalue()


def sig(seeds, blur=0):
    return ":".join([hashing.VIDEO_SIG_PREFIX]
                    + [hashing._dhash_png(as_png(frame(s, blur=blur))) for s in seeds])


print("frame hashing")
one = hashing._dhash_png(as_png(frame(1)))
check("a frame hashes to 16 hex chars", bool(one) and len(one) == 16, str(one))
check("the same frame hashes the same", one == hashing._dhash_png(as_png(frame(1))))
check("a different frame hashes differently", one != hashing._dhash_png(as_png(frame(2))))
check("rubbish bytes return None", hashing._dhash_png(b"not a png") is None)

print("\nsignature comparison")
same = sig([1, 2, 3])
check("identical signatures score 100", hashing.video_compare(same, same) == 100,
      str(hashing.video_compare(same, same)))

reencoded = sig([1, 2, 3], blur=4)
score = hashing.video_compare(same, reencoded)
check("a softened re-encode still scores high", score >= 80, "%d%%" % score)

different = sig([9, 10, 11])
other = hashing.video_compare(same, different)
check("unrelated material scores low", other < 80, "%d%%" % other)
print("       (re-encode %d%% against unrelated %d%%)" % (score, other))

print("\nguards")
check("mixed with an image hash scores 0",
      hashing.video_compare(same, "a" * 16) == 0)
check("different frame counts score 0",
      hashing.video_compare(same, sig([1, 2])) == 0)
check("None is handled", hashing.video_compare(same, None) == 0)
check("an image hash is not mistaken for a video signature",
      not hashing.is_video_signature("aaaaaaaaaaaaaaaa"))
check("a video signature is recognised", hashing.is_video_signature(same))
check("video_dhash returns None without ffmpeg",
      hashing.video_dhash(__file__) is None)

# The regression that cost a 67-hour scan. DSM ships a stripped /bin/ffmpeg
# with no ffprobe next to it. ffmpeg_available() asked only about ffmpeg, so
# the pass reported itself as on - while video_dhash seeks to fractions of a
# running time it gets from ffprobe, so with ffprobe absent every single file
# came back None. On and finding nothing, with nothing said.
print("")
print("ffprobe is part of the answer, not an extra")
saved = (hashing._FFMPEG_CHECKED, hashing._FFMPEG, hashing._FFPROBE)
try:
    def pretend(ffmpeg, ffprobe):
        hashing._FFMPEG_CHECKED = True
        hashing._FFMPEG = ffmpeg
        hashing._FFPROBE = ffprobe

    pretend("/bin/ffmpeg", None)
    check("ffmpeg without ffprobe does not count as available",
          hashing.ffmpeg_available() is False)
    report = hashing.video_support()
    check("and the report names the missing piece",
          report["missing"] == ["ffprobe"], str(report["missing"]))
    check("the report is not ok", report["ok"] is False)

    pretend(None, "/bin/ffprobe")
    check("ffprobe alone is no better", hashing.ffmpeg_available() is False)
    check("and ffmpeg is named", "ffmpeg" in hashing.video_support()["missing"])

    pretend("/bin/ffmpeg", "/bin/ffprobe")
    check("both present: available", hashing.ffmpeg_available() is True)
    report = hashing.video_support()
    check("nothing missing", report["ok"] and report["missing"] == [],
          str(report["missing"]))
    check("and it says where they are",
          report["ffmpeg"] == "/bin/ffmpeg" and report["ffprobe"] == "/bin/ffprobe")
finally:
    hashing._FFMPEG_CHECKED, hashing._FFMPEG, hashing._FFPROBE = saved

print()
sys.exit(1 if fails else 0)
