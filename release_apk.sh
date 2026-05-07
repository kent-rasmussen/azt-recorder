#!/usr/bin/env bash
# release_apk.sh — upload the latest bin/*.apk to a GitHub release
# under a stable asset name, derived from buildozer.spec so the script
# is project-agnostic.
#
# Reads from buildozer.spec in the project dir (defaults to the
# script's dir):
#   package.name        — used as the uploaded asset name (<pkg>.apk)
#   source.dir          — for %(source.dir)s interpolation
#   version.filename    — file to read the version from
#   version.regex       — Python regex with one capture group for the
#                         version string
# Falls back to parsing the version from the APK filename
# (<pkg>-<version>-...) when the documented regex+file pair doesn't
# yield a match.
#
# Repo slug is taken from `git remote get-url origin`.
#
# Local APK names stay verbose (e.g.
# azt_recorder-1.37.15-arm64-v8a_armeabi-v7a-release.apk); the asset
# is symlinked under <package.name>.apk so check_for_update finds it
# under a stable name on every release.
#
# Usage:
#     bash release_apk.sh [project_dir]

set -euo pipefail
echo $PWD
#cd "${1:-$(dirname "$0")}"

[ -f buildozer.spec ] || { echo "FAIL: no buildozer.spec in $PWD" >&2; exit 1; }

spec_get() {
    sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*(.+)[[:space:]]*$/\1/p" \
        buildozer.spec | head -n 1
}

PKG=$(spec_get 'package\.name')
[ -n "$PKG" ] || { echo "FAIL: package.name not set in buildozer.spec" >&2; exit 1; }

SRC_DIR=$(spec_get 'source\.dir')
SRC_DIR=${SRC_DIR:-.}
VER_FILE=$(spec_get 'version\.filename')
VER_FILE=${VER_FILE//%(source.dir)s/$SRC_DIR}
VER_REGEX=$(spec_get 'version\.regex')

# 1) Documented regex+file (the Python regex buildozer itself uses).
ver=
if [ -n "$VER_FILE" ] && [ -f "$VER_FILE" ] && [ -n "$VER_REGEX" ]; then
    ver=$(VER_REGEX="$VER_REGEX" VER_FILE="$VER_FILE" python3 -c '
import os, re
m = re.search(os.environ["VER_REGEX"], open(os.environ["VER_FILE"]).read())
print(m.group(1) if m and m.groups() else "")
')
fi

# Latest APK under bin/.
src=$(ls -1 bin/*.apk 2>/dev/null | sort -V | tail -n 1)
[ -n "$src" ] || { echo "FAIL: no APK in bin/" >&2; exit 1; }

# 2) Fallback: parse <pkg>-<version>-... from the APK filename.
if [ -z "$ver" ]; then
    ver=$(basename "$src" | sed -nE "s/^${PKG}-([0-9][0-9.]*)-.*\$/\1/p")
fi
[ -n "$ver" ] || { echo "FAIL: could not determine version" >&2; exit 1; }

# Repo slug from origin.
remote=$(git remote get-url origin 2>/dev/null || true)
slug=$(echo "$remote" \
    | sed -E 's#\.git$##; s#/$##' \
    | sed -nE 's#.*github\.com[:/]+([^/]+/[^/]+)$#\1#p')
[ -n "$slug" ] || { echo "FAIL: could not derive github repo slug from origin ($remote)" >&2; exit 1; }

stage=$(mktemp -d)/${PKG}.apk
ln -s "$(realpath "$src")" "$stage"

echo "package: $PKG"
echo "version: $ver"
echo "repo:    $slug"
echo "src:     $src"
echo "asset:   $(basename "$stage")"
echo

tag="v$ver"
if gh release view "$tag" --repo "$slug" >/dev/null 2>&1; then
    gh release upload "$tag" "$stage" --repo "$slug" --clobber
    # If a previous run left it as draft, publish + mark latest now
    # that the asset is in place.
    gh release edit "$tag" --repo "$slug" --draft=false --latest
else
    gh release create "$tag" --repo "$slug" --title "$tag" \
        --latest --generate-notes "$stage"
fi
