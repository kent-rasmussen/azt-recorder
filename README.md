# LIFT Recorder

A mobile field recorder for LIFT XML lexicon databases. Works on **Android** (primary) and **iOS** / desktop (secondary).

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
pip install kivy sounddevice soundfile numpy pillow
python main.py
```

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

Changes take effect when you press **Apply & Go**.

## Private-use lang tag handling

The app correctly handles complex BCP 47 private-use tags. The headword language `lol-x-his30100` is a legitimate vernacular code. Only these five specific suffixes are treated as metadata (excluded from headword display, handled separately):

| Suffix | Meaning |
|---|---|
| `-x-audio` | Audio filename (written back here after recording) |
| `-x-ipa` | IPA transcription |
| `-x-tone` | Tone transcription |
| `-x-cvprofile` | CV profile |
| `-x-py` | Python analysis data |
