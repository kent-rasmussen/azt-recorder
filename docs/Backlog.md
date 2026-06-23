# Recorder backlog

Peer-side issues flagged for later work. Pick from the top.
Each item names the file/line seam and the shape of the fix
so it's clear whether a given diagnosis report has already
been logged.

## Re-recording over an existing audio file leaves a duplicate MOOV atom and ~1.9 MB of stale bytes

**Symptom.** Saved M4A files contain two MOOV atoms with ~1.9 MB
of "junk" between them. `ffmpeg` warns `Found duplicated MOOV
Atom. Skipped it.` and plays the file fine; git happily stores
the bloat.

**Cause (strong hypothesis, not yet field-confirmed).**
`_start_android_recording` opens the audio FD with mode `'w'`
at `main.py:5670`:

```python
pfd = resolver.openFileDescriptor(Uri.parse(aac_path), 'w')
```

`ContentResolver.openFileDescriptor` accepts `'w'`, `'wa'`,
`'wt'` — `'w'` does **not** truncate. MediaRecorder writes the
new MP4 starting at offset 0 but doesn't shrink the file
beyond what it writes. M4A puts the MOOV atom at the end of
the file (duration only known on stop), so if the previous
recording at the same filename was longer than the new one,
the old MOOV remains as trailing bytes — duplicate MOOV
exactly as observed.

Reusing the same audio path on re-recording happens whenever
the user holds the button on an entry that already has an
audio filename (the slug-based filename is deterministic per
entry).

**Fix.** Change `'w'` → `'wt'` at `main.py:5670` so the
ContentProvider truncates the existing file before
MediaRecorder begins writing. Single-character change. No
state-machine refactor needed. Worth a manual re-record
regression check that overwriting an existing entry's audio
produces a clean single-MOOV M4A with no stale tail.

**Note.** The filesystem branch (`mr.setOutputFile(aac_path)`
at `main.py:5676`, the non-URI path) goes through
MediaRecorder's own file open which is documented to
truncate, so this bug is URI-storage-only — Android-only
in practice.
