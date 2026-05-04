#!/usr/bin/env bash
# setup_from_nuke.sh — Recreate the venv from scratch after nuking env/
# Usage: bash setup_from_nuke.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/env"
COLLAB_DIR="$(cd "$SCRIPT_DIR/../azt-collab" && pwd)"

echo "=== azt_recorder venv setup ==="

# 1. Remove old venv if it exists
if [ -d "$VENV_DIR" ]; then
    echo "Removing existing venv at $VENV_DIR ..."
    rm -rf "$VENV_DIR"
fi

# 2. Create fresh venv
echo "Creating venv with $(python3 --version) ..."
python3 -m venv "$VENV_DIR"

# 3. Upgrade pip and install setuptools (provides distutils on Python 3.12+)
echo "Upgrading pip and installing setuptools ..."
"$VENV_DIR/bin/pip" install --upgrade pip setuptools

# 4. Install buildozer and its deps
echo "Installing buildozer ..."
"$VENV_DIR/bin/pip" install buildozer

# 5. Install app runtime dependencies for local dev/testing.
# Mirrors buildozer.spec's `requirements = ...` line so a desktop run
# (`python main.py`) imports the same surface as the APK ships. Kivy
# is pinned to match the spec.
echo "Installing app dependencies (matches buildozer.spec) ..."
"$VENV_DIR/bin/pip" install \
    'kivy==2.3.1' pillow sounddevice soundfile numpy certifi filetype \
    typing_extensions dulwich

# 6. Wire up the load-bearing symlinks into ../azt-collab. Per the suite
# contract, the recorder is a pure peer of the daemon: it consumes the
# canonical client by symlink, never by copy. Idempotent (`ln -sfn`).
if [ ! -d "$COLLAB_DIR" ]; then
    echo "WARN: ../azt-collab not found at $COLLAB_DIR — skipping symlinks." >&2
    echo "      Clone azt-collab next to azt_recorder, then re-run." >&2
else
    echo "Creating sister-app symlinks into $COLLAB_DIR ..."
    ln -sfn "../azt-collab/azt_collab_client" \
        "$SCRIPT_DIR/azt_collab_client"
    ln -sfn "../azt-collab/examples" "$SCRIPT_DIR/examples"
    ln -sfn "../azt-collab/android/manifest_extras_peer.xml" \
        "$SCRIPT_DIR/manifest_extras.xml"
fi

# 7. Clean stale buildozer internals (may have hardcoded paths from another project)
BUILD_BASE="$SCRIPT_DIR/.buildozer/android/platform/build-arm64-v8a_armeabi-v7a/build"
HOSTPY="$BUILD_BASE/other_builds/hostpython3"
BUILD_VENV="$BUILD_BASE/venv"
if [ -d "$HOSTPY" ]; then
    echo "Removing stale hostpython3 build (forces rebuild with correct paths) ..."
    rm -rf "$HOSTPY"
fi
if [ -d "$BUILD_VENV" ]; then
    echo "Removing stale buildozer build venv (will be recreated by buildozer) ..."
    rm -rf "$BUILD_VENV"
fi

# 8. Run initial buildozer build to trigger hostpython3 rebuild, then patch it
echo "Running buildozer to rebuild hostpython3 (may take a while) ..."
echo "(The first build attempt may fail — that's expected, we patch hostpython3 next)"
"$VENV_DIR/bin/buildozer" android debug 2>&1 || true

# 9. Install setuptools into hostpython3's Lib (it's Python 3.11, needs setuptools)
HOSTPY_REBUILT="$SCRIPT_DIR/.buildozer/android/platform/build-arm64-v8a_armeabi-v7a/build/other_builds/hostpython3/desktop/hostpython3"
if [ -d "$HOSTPY_REBUILT" ]; then
    echo "Installing setuptools into hostpython3 ..."
    "$HOSTPY_REBUILT/native-build/python3" -m pip install --target="$HOSTPY_REBUILT/Lib" setuptools
fi

echo ""
echo "=== Done ==="
echo "Activate with:  source $VENV_DIR/bin/activate"
echo "Then run:       buildozer android debug"
