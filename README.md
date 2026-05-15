# A-Z+T Recorder -- Record My Wordlist

A mobile field recorder for LIFT XML lexicon databases. Part of the A-Z+T suite of linguistic tools. Works on **Android** (primary) and **iOS** / desktop (secondary).

Records the highest quality audio the device supports, stores files in an `audio/` directory next to the `.lift` file, and writes filenames back into the LIFT XML using `Entry.lc.textvaluebylang(lang=db.audiolang)` — i.e. a `<citation><form lang="{vernlang}-Zxxx-x-audio">` element.

## Features

- Open any `.lift` file from device storage
- Browse entries with: headword (citation form), glosses, CAWL number, illustration image
- **Push-to-talk** recording — hold to record, release to save
- Audio stored as:
  - Android: AAC/M4A @ 48 kHz / 256 kbps (via `MediaRecorder`)
  - iOS: FLAC @ 48 kHz lossless (via `AVAudioRecorder`)
  - Desktop: PCM WAV @ 48 kHz (via `sounddevice`, for testing)
- Config screen:
  - Toggle which gloss languages appear
  - Filter by CAWL number or range (e.g. `1-100`, `42`, `200-300`)
  - Search by gloss text
  - Show only unrecorded entries
  - Five colour themes (Earth, Ocean, Forest, Slate, Light)
- **Collaboration** via GitHub or GitLab:
  - GitHub App device flow (no PATs needed — just enter a code)
  - Auto-create repos, auto-sync on navigation
  - Clone existing repositories from the welcome screen
- **Internationalisation** — UI language selector in Settings; French included, easy to add more
- **Image picker** with openclipart, FreeSVG, and Wikimedia Commons sources
- Recorded filename written back into LIFT XML immediately after each recording

## File layout expected

```
my_dictionary/
├── my_dictionary.lift       ← the LIFT file you open
├── audio/                   ← created automatically; WAV/M4A files go here
│   ├── 0001_784e1105_body.m4a
│   └── ...
└── images/                  ← optional; illustration PNGs matched by href
    ├── 0001_body.png
    └── ...
```

## LIFT XML write-back

After each recording, `lift.py` writes:

```xml
<entry guid="784e1105-...">
  ...
  <citation>
    <form lang="lol-x-his30100-Zxxx-x-audio">
      <text>0001_784e1105_body.m4a</text>
    </form>
  </citation>
</entry>
```

This is exactly `Entry.lc.textvaluebylang(lang=self.db.audiolang)` as used in `azt/lift.py`.

## Setup (desktop / development)

```bash
# Quick setup (creates venv, installs everything):
bash setup_from_nuke.sh

# Or manually:
python3 -m venv env
source env/bin/activate
pip install --upgrade pip setuptools
pip install buildozer kivy pillow typing_extensions dulwich
python main.py
```

`dulwich` is the git library used by the `azt_collabd` daemon. The
client auto-spawns the daemon into the recorder's venv on desktop, so
it has to be importable here even though the recorder never imports it
directly. `setup_from_nuke.sh` already installs it; the manual snippet
above mirrors that.

## Collaboration architecture

The recorder is a **pure peer** of the AZT collaboration daemon
(`azt_collabd`); it does not host or import the daemon. All git
operations, credentials, and project-registry state live behind a
single thin client library, `azt_collab_client`:

- On **desktop**, `azt_collab_client` auto-spawns
  `python -m azt_collabd` over loopback HTTP on first use.
- On **Android**, the recorder talks to the standalone AZT collab
  server APK (`org.atoznback.aztcollab`) via a ContentProvider. Both
  APKs must be signed with the same suite keystore, and the server APK
  has to be installed — there is no Android fallback to loopback.

`azt_collab_client/` in this repo is a symlink to the canonical copy
in the sibling `../azt-collab/` repo. Set it up once with:

```bash
ln -s ../azt-collab/azt_collab_client azt_collab_client
```

For the peer conformity contract (bootstrap, LIFT/audio/CAWL handles,
picker-cancel handling, etc.) see
[`azt_collab_client/CLIENT_INTEGRATION.md`](azt_collab_client/CLIENT_INTEGRATION.md).
For wiring a new sister app as a peer (manifest, signing, install
prompts), see [`README_NewClient.txt`](README_NewClient.txt).

## Build for Android

Install [Buildozer](https://buildozer.readthedocs.io/):

```bash
pip install buildozer
# On Linux/macOS:
buildozer android debug deploy run
```

First build downloads the Android NDK/SDK automatically (~10 min).

Requires: Linux or macOS, Java 17, Python 3.9+.

## Build for iOS

```bash
pip install kivy-ios
toolchain build python3 kivy pillow
toolchain create LIFTRecorder .
# Open the generated Xcode project and build/sign normally
```

## Configuration screen

Accessed via the ⚙ button in the top-right corner of the recorder screen.

| Setting | Description |
|---|---|
| Gloss languages | Toggle which languages appear as glosses |
| CAWL range | Filter entries by CAWL number, e.g. `1-100` or `42,50-60` |
| Gloss search | Show only entries whose glosses contain this text |
| Only unrecorded | Skip entries that already have audio |

Changes take effect when you close the settings screen (X button).

## Private-use lang tag handling

The app correctly handles complex BCP 47 private-use tags. The headword language `lol-x-his30100` is a legitimate vernacular code. Only these five specific suffixes are treated as metadata (excluded from headword display, handled separately):

| Suffix | Meaning |
|---|---|
| `-x-audio` | Audio filename (written back here after recording) |
| `-x-ipa` | IPA transcription |
| `-x-tone` | Tone transcription |
| `-x-cvprofile` | CV profile |
| `-x-py` | Python analysis data |
