#!/bin/bash
# Move shared collab code into a sibling azt-shared/ directory and
# replace it in the recorder with symlinks.
#
# Run from the azt_recorder project root:
#   bash tools/migrate_to_shared.sh
#
# After the run:
#   - ../azt-shared/  has the moved files, initialized as its own git repo.
#   - azt_recorder/   has symlinks pointing into ../azt-shared/. The
#                     recorder builds and tests unchanged because every
#                     import path / file path stays the same.
#   - Nothing is committed to the recorder repo. Review with `git status`
#     and commit yourself.
#
# To set up a sister app later, run the helper printed at the end.

set -euo pipefail

# ── Sanity checks ──────────────────────────────────────────────────────────
[ -f main.py ] || { echo "Run from the azt_recorder project root."; exit 1; }
[ -d azt_collabd ] || { echo "azt_collabd/ not found — already migrated?"; exit 1; }
[ -L azt_collabd ] && { echo "azt_collabd is already a symlink — already migrated?"; exit 1; }

SHARED="../azt-collab"
if [ -e "$SHARED" ]; then
    echo "$SHARED already exists; aborting (move it aside or rename)."
    exit 1
fi

# ── Items to move ──────────────────────────────────────────────────────────
SHAREABLES=(
    azt_collabd
    azt_collab_client
    examples
    android
    azt_collabd_plan.xml
    azt_collabd_cleanup_drafts.xml
)

echo "Will move the following items into $SHARED/:"
for item in "${SHAREABLES[@]}"; do
    if [ -e "$item" ]; then
        echo "  - $item"
    else
        echo "  - $item  (skip; not present)"
    fi
done
echo
read -p "Continue? [y/N] " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "Aborted."; exit 0; }

# ── Create shared dir ──────────────────────────────────────────────────────
mkdir -p "$SHARED"

# ── Move + symlink ─────────────────────────────────────────────────────────
for item in "${SHAREABLES[@]}"; do
    if [ -e "$item" ]; then
        echo ">> moving $item -> $SHARED/$item"
        mv "$item" "$SHARED/"
        # Symlink target is RELATIVE so the recorder repo stays portable
        # if someone clones it next to azt-shared/ on another machine.
        ln -s "../azt-shared/$item" "$item"
    fi
done

# ── Init the shared repo ───────────────────────────────────────────────────
(
    cd "$SHARED"
    git init -q -b main
    cat > .gitignore <<'GIT_IGNORE'
__pycache__/
*.pyc
*.egg-info/
.buildozer/
.DS_Store
GIT_IGNORE
    git add -A
    git -c user.email='split@local' \
        -c user.name='split-from-azt_recorder' \
        commit -q -m 'Initial: split out from azt_recorder'
)

# ── Stage the symlink replacements in the recorder ─────────────────────────
echo
echo ">> staging symlink replacements in recorder"
git add -A

# ── Print next steps ───────────────────────────────────────────────────────
cat <<NEXT

Done.

Recorder side
-------------
The recorder now has six symlinks pointing into $SHARED/. Imports,
buildozer.spec android.add_src, and the example loader all work
unchanged because the symlinks make the paths resolve identically.

Review:
    git status
    ls -l azt_collabd azt_collab_client examples android

Quick smoke (no rebuild needed):
    env/bin/python -c "import azt_collabd, azt_collab_client; print('ok')"
    bash tests/step15.sh

Commit when ready:
    git commit -m 'Move collab modules to ../azt-shared'

Shared side
-----------
$SHARED is its own git repo on branch main with one initial commit.
You'll want to:
    cd $SHARED
    git remote add origin <url-of-the-azt-shared-repo>
    git push -u origin main

Sister app setup (later)
------------------------
For a new sister app, sibling-clone it next to azt-shared/ and run:

    cd ../my-sister-app
    for x in azt_collabd azt_collab_client examples android \\
             azt_collabd_plan.xml azt_collabd_cleanup_drafts.xml; do
        ln -s "../azt-shared/\$x" "\$x"
    done

Both apks must be signed with the suite keystore (see
android/SUITE_FINGERPRINT in azt-shared) for the ContentProvider
permission gate to let them call each other.
NEXT
