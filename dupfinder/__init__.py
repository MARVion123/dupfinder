"""dupfinder - deep duplicate finder for Synology DSM 7.4."""

__version__ = "1.0.0"

# The build number lives in build_spk.py and used to go no further than the
# package INFO, so a running service could only ever say "1.0.0" - while the
# question people actually have, every single time, is which build is on the
# box. Answering it meant a shell and synopkg. The build writes _build.py
# into the payload; a checkout has no such file and says so.
try:
    from ._build import BUILD as __build__
except ImportError:                                     # running from a checkout
    __build__ = "dev"

__release__ = "%s-%s" % (__version__, __build__)
