# version.py -- deloop's own app version, as a bare integer matching the
# repo's git tag scheme (v1, v2, v3, ...). Compared against a GitHub
# release's tag_name by ota.py -- see its module docstring.
#
# The value below is a placeholder only. The release workflow
# (.github/workflows/release.yml) overwrites this file's CURRENT_VERSION
# before compiling each release's assets -- it never commits that change
# back, so don't read this number as "the current released version" from
# git history alone.

CURRENT_VERSION = 0
