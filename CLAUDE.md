# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Single-file Kivy app (`main.py`, ~3300 lines) — a field audio recorder
for LIFT XML lexicon databases, primary target Android, secondary
desktop/iOS. Records audio for dictionary entries and writes filenames
back into the LIFT XML.

## Architecture you must know before editing

### Recorder is a *pure peer* of the AZT collab daemon

- The recorder does not host or import `azt_collabd`. All
  collaboration (git, credentials, project registry, sync) goes through
  `azt_collab_client`, configured once at startup with
  `azt_collab_client.configure(app_id='azt-recorder')` (main.py:28).
- `collab.py` is a **tombstone** that raises `ImportError`. Direct
  `import azt_collabd` from this app is forbidden — if you find yourself
  reaching for it, you are doing something wrong.
- `azt_collab_client/` in this repo is a **symlink** to the canonical
  copy in the sibling `../azt-collab/` repo. Edits propagate
  bidirectionally. Fresh clones must run
  `ln -s ../azt-collab/azt_collab_client azt_collab_client`.
- Desktop transport: `azt_collab_client` auto-spawns
  `python -m azt_collabd` over loopback HTTP. The daemon spawns into
  the recorder's venv, which is why `dulwich` must be installed here
  even though the recorder never imports it.
- Android transport: ContentProvider against the standalone server APK
  (`org.atoznback.aztcollab`). Both APKs must be signed with
  `/home/kentr/bin/azt-suite.keystore`. No Android fallback to loopback.
- Status-code business logic uses `result.has(S.CODE)` — never parse
  translated strings (see `AZT_COLLAB_README.md` for the full code list).

### Picker / theme migration is in progress

- `azt_collab_picker_migration.xml` tracks the live migration of
  WelcomeScreen, LangPickerScreen, project-create/clone popups, and
  `theme.py` from this repo into `azt_collab_client/ui/`.
- Steps 1–6 done as of recorder 1.27.x. `theme.py` has been removed
  from this repo; the recorder now imports `theme` from
  `azt_collab_client.ui` (main.py:24-27).
- Open follow-ups: copy `langtags_mini.json.gz` into the shared client
  (currently passed via `langtags_path=` kwarg in `build()`), dedupe
  the two `theme.py` copies, fold
  `show_start_over`/`new_from_template`/`open_file`/`clone_dialog`
  into the picker.

### Audio recording is platform-split

main.py:3233+ branches on platform: Android uses `MediaRecorder` via
pyjnius (AAC/M4A), iOS uses `AVAudioRecorder` via pyobjus (FLAC),
desktop uses `sounddevice` + `soundfile` (PCM WAV). Each path writes
the filename back into the LIFT entry's `<citation><form>` with the
audiolang tag — preserving this contract is the whole point of the app.

### Asset & storage layout

- `azt_images/` is ~320 MB and is **excluded from the APK** (see
  `buildozer.spec` `source.exclude_dirs`). On Android the app reads
  these from external storage via `lift.py`'s `bundled_images_dir`.
  Don't try to bundle them.
- `audio/` (next to the `.lift` file) is created at record time;
  filenames follow `{NNNN}_{shortguid}_{slug}.{ext}`.

## Common commands

### Setup / desktop dev

```bash
bash setup_from_nuke.sh        # full venv recreation (also runs first buildozer pass)
source env/bin/activate
python main.py                 # desktop run
```

The manual-install path needs `kivy pillow typing_extensions dulwich`
plus `buildozer`; see `README.md` "Setup" section.

### Android build

```bash
bash build.sh                  # buildozer android clean → patch_p4a.sh → debug build
# or step by step:
buildozer android clean
./patch_p4a.sh                 # MUST re-run after every clean (suppresses host pkg-config)
PKG_CONFIG_PATH="" PKG_CONFIG_LIBDIR="" PKG_CONFIG=false buildozer android debug
buildozer android deploy run logcat
```

`patch_p4a.sh` patches the p4a Kivy recipe to stop host pkg-config
from leaking x86_64 system headers into the cross-compilation. Without
it, the build picks up `/usr/include/x86_64-linux-gnu` and fails.

`buildozer.spec` reads the version from `main.py`'s `__version__`
string via regex; bump there, not in the spec.

### Tests

`tests/stepN.sh` are stack verification scripts (not unit tests).
Each exercises one slice end-to-end against a temp `$AZT_HOME`. Run
them with the recorder's venv active:

```bash
bash tests/step12.sh   # LIFT merge driver
bash tests/step16.sh   # sister-app example (auto-spawns daemon)
```

Step 16 kills any running daemon, sets `AZT_HOME=/tmp/aztest_home`,
and runs `examples/sister_app.py` (which lives in the symlinked
sibling). If the daemon hangs, `pkill -f "python -m azt_collabd"`.

## Project-specific conventions

### Versioning + changelog discipline

For any change touching this repo, follow the workflow in
`~/.claude-sil/CLAUDE.md` (loaded globally):

- Bump `__version__` in `main.py` (debug for docs, minor for new
  capability, major for breaking). `buildozer.spec` reads it
  automatically.
- Add a `CHANGELOG.txt` entry (top of file, terse).
- Append explicit diffs (not summaries) to `claud_diffs.txt` with one
  rationale paragraph per change.
- If the change affected the build, update `setup_from_nuke.sh` to
  encode the fix.

### Kivy specifics

The `# Kivy hide/show pattern` block in the global CLAUDE.md captures
hard-won lessons about this codebase (height:0 panels still intercept
touches; minimum_height is unreliable when starting collapsed; etc.).
Re-read it before touching any collapsible UI.

### Cross-repo edits

Many fixes for "recorder problems" actually live in `../azt-collab/`
(daemon bugs, client API gaps, shared UI). Read the sibling repo
freely; the user has granted standing read access there. The
`azt_collab_client/` symlink means client edits land in the
authoritative location automatically.

### Identity strings

`appinfo.py` holds canonical identity (`APP_NAME`, `APP_SLUG='azt-recorder'`,
`APP_USER_AGENT`). Don't hardcode these elsewhere — sister apps reuse
the same `azt_collab_client.configure()` hook with their own slug, and
drift breaks the suite contract.
