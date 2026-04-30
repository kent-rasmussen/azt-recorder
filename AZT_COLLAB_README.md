# azt-collab

Shared collaboration backend for the A-Z+T suite of linguistic tools.

A single local daemon (`azt_collabd`) per device manages git
collaboration for LIFT projects: GitHub/GitLab credentials, project
registry, debounced push/pull with LIFT-aware three-way merge,
per-project locking, and crash recovery. Suite apps consume it through
a thin client library (`azt_collab_client`) that auto-discovers the
daemon and is platform-agnostic.

This repo is the **canonical source** for the daemon, the client, the
Android ContentProvider glue, and the sister-app example. AZT suite
apps consume it as sibling-directory symlinks; the recorder lives in
`../azt_recorder/`.

## What's in here

```
azt-collab/
  azt_collabd/                  # daemon: dulwich, scheduler, dispatch
    server.py                   # HTTP transport + dispatch table
    repo.py                     # git operations, merge integration
    lift_merge.py               # LIFT-aware three-way merge
    merge_commit.py             # merge commit message format
    scheduler.py                # debouncer + connectivity watcher
    store.py                    # credentials (credentials.json)
    projects.py                 # project registry (projects.json)
    settings.py                 # config.json runtime knobs
    locks.py                    # per-project flock
    paths.py                    # $AZT_HOME resolution
    config.py                   # GitHub App identity (slug etc.)
    status.py                   # Status/Result + AuthError + codes
    net.py                      # SSL patching + is_online
    auth.py                     # GitHub device flow, GitLab API
    android_cp/service.py       # pyjnius shim for ContentProvider
    ui/app.py                   # standalone Kivy settings UI
  azt_collab_client/            # thin client used by every suite app
    __init__.py                 # public API
    transports/loopback.py      # localhost HTTP transport
    transports/android_cp.py    # ContentProvider transport
    translate.py                # Status code → user string
    paths.py                    # mirror of azt_collabd/paths.py
    status.py                   # decode-only Status/Result
    projects.py                 # decode-only Project/ProjectStatus
    rpc.py                      # facade over transports
  android/
    SUITE_FINGERPRINT           # SHA-256 of the suite signing key
    src/main/java/.../AZTCollabProvider.java
  examples/sister_app.py        # runnable demo for a new suite app
  azt_collabd_plan.xml          # original 16-step migration plan
  azt_collabd_cleanup_drafts.xml # outstanding cleanup tasks
```

## Architecture in 30 seconds

- **One daemon per device.** Started by whichever AZT app reaches the
  client library first. Auto-spawned via `python -m azt_collabd` on
  desktop; runs in-process on Android via the host APK.
- **Single source of truth on disk** at `$AZT_HOME` (default
  `~/.local/share/azt/` on Linux, `~/Library/Application Support/azt/`
  on macOS): credentials, project registry, lock files, crash log.
- **Two transports.** Loopback HTTP (desktop, Android fallback) and
  Android ContentProvider (preferred when sibling suite apps share the
  daemon).
- **Sync flow.** Client calls `request_sync(langcode, contributor)`,
  daemon debounces (default 500 ms) and runs commit-first → fetch →
  fast-forward / merge / push, with `merge_retry_max` race retries.
- **LIFT-aware merge.** `<entry guid="...">` is the merge key.
  Conflicts get `<annotation name="azt-lift-conflict">` markers;
  divergent versions are kept side by side.

## Setting up a new sister app

Assumes you have a sibling directory `../my-sister-app/` already
holding your app's source.

### 1. Symlink the shared modules

```bash
cd ../my-sister-app
for x in azt_collabd azt_collab_client examples android \
         azt_collabd_plan.xml azt_collabd_cleanup_drafts.xml; do
    ln -s "../azt-collab/$x" "$x"
done
```

After this, `import azt_collabd` and `import azt_collab_client` work
from your app's source.

### 2. Identify the app at startup

Once, before any client call:

```python
import azt_collabd
azt_collabd.configure(app_slug='azt-my-sister-app')

import azt_collab_client
azt_collab_client.configure(app_id='azt-my-sister-app')
```

`configure()` is idempotent and keyword-only; only override what you
want. Defaults match the recorder so calling with nothing works.

### 3. (Android only) Install the ContentProvider callbacks

In your app's startup hook (Kivy: `App.on_start`):

```python
from kivy.utils import platform
if platform == 'android':
    try:
        from azt_collabd.android_cp import service as _cp
        _cp.install_callbacks()
    except Exception as ex:
        print(f'aztcollab provider install failed: {ex}')
```

This is a no-op on desktop, so the same call is safe everywhere.

### 4. (Android only) buildozer.spec

```ini
android.permissions = INTERNET, ..., org.atoznback.AZT_COLLAB_ACCESS

# Java glue
android.add_src = android/src/main/java

# Suite-level signature permission
android.manifest_extra_xml = <permission android:name="org.atoznback.AZT_COLLAB_ACCESS" android:protectionLevel="signature" />

# Provider element — note the *unique* authority per app
android.manifest_application_extra_xml = <provider android:name="org.atoznback.aztcollab.AZTCollabProvider" android:authorities="org.atoznback.my_sister_app.aztcollab" android:exported="true" android:permission="org.atoznback.AZT_COLLAB_ACCESS" android:grantUriPermissions="true" />
```

Each suite app declares its own authority (`<package>.aztcollab`) but
they all share the same custom permission name and signature. Discovery
in `azt_collab_client.transports.android_cp.discover()` picks any
responder with an authority ending in `.aztcollab`.

### 5. (Android only) Sign with the suite keystore

The custom permission is `protectionLevel="signature"`. Your APK has to
be signed with the same keystore as other suite APKs or Android refuses
the install-time permission grant.

The expected SHA-256 fingerprint is in `android/SUITE_FINGERPRINT`.
Verify your build matches:

```bash
keytool -printcert -jarfile bin/my-sister-app-*-unsigned.apk \
    | grep SHA256
```

Sign with `jarsigner` (JDK) or `apksigner` (build-tools):

```bash
jarsigner -verbose -sigalg SHA256withRSA -digestalg SHA-256 \
    -keystore /path/to/azt-suite.keystore \
    bin/my-sister-app-*-unsigned.apk <alias>
```

Don't commit the keystore. Reference its path via `buildozer.spec`
`android.signing.keystore` or pass at build time.

## Client API quick reference

```python
from azt_collab_client import (
    # Lifecycle
    configure, is_online, ServerUnavailable,

    # Credentials (server-owned credentials.json)
    get_credentials_status, set_collab_host,
    save_github_tokens, mark_github_app_installed,
    save_gitlab_credentials, migrate_from_prefs,

    # Projects (server-owned projects.json)
    list_projects, open_project, register_project,
    project_status, record_project_sync_time,

    # Sync
    sync_project,        # synchronous, returns Result
    request_sync,        # debounced, returns job_id
    poll_job,            # poll a request_sync job

    # Translation
    translate_status, translate_result, set_translator,

    # Status codes + dataclasses
    S, Status, Result, Project, ProjectStatus,
)
```

A typical sister-app sync flow:

```python
register_project('fra', '/path/to/working_tree', '/path/.../fra.lift')
job_id = request_sync('fra', contributor='Kent')
# ...later, after debounce_ms...
status = poll_job(job_id)
if status['state'] == 'DONE':
    print(translate_result(status['result']))
```

See `examples/sister_app.py` for an end-to-end demo runnable with:

```bash
python examples/sister_app.py /path/to/some_project_dir
```

## Status codes worth checking

Drive business logic with `Result.has(S.CODE)`, not by parsing
translated strings.

| Code | Meaning |
|---|---|
| `OK` | (rarely; ops emit specific codes) |
| `PUSHED`, `PULLED`, `COMMITTED_AND_PUSHED` | sync made network progress |
| `COMMITTED_LOCAL`, `COMMITTED_OFFLINE` | local-only commit landed |
| `NOTHING_TO_COMMIT` | working tree was clean |
| `NOT_A_REPO`, `NO_REMOTE` | project setup incomplete |
| `AUTH_REQUIRED`, `APP_NOT_INSTALLED`, `REPO_NOT_AUTHORIZED`, `ACCESS_DENIED` | credentials problem (translate for the user) |
| `CONFLICTS` | merge had conflicts; entries flagged with `<annotation name="azt-lift-conflict">`. `result.has(S.CONFLICTS)` carries `paths` param. |
| `BUSY` | another op holds the per-project lock |
| `SERVER_UNAVAILABLE`, `SERVICE_RESTARTED` | transport-level (the client retries automatically; surface to user only on persistent failure) |

Full list: `azt_collab_client/status.py`.

## Configuration

`$AZT_HOME/config.json` — runtime knobs, env-var overrides:

```json
{
  "sync.debounce_ms": 500,
  "sync.merge_retry_max": 3,
  "sync.connectivity_poll_s": 30
}
```

| Key | Env var | Default |
|---|---|---|
| `sync.debounce_ms` | `AZT_SYNC_DEBOUNCE_MS` | 500 |
| `sync.merge_retry_max` | `AZT_SYNC_MERGE_RETRY_MAX` | 3 |
| `sync.connectivity_poll_s` | `AZT_SYNC_CONNECTIVITY_POLL_S` | 30 |
| `AZT_HOME` (dir override) | `AZT_HOME` | platform default |
| Disable auto-spawn | `AZT_CLIENT_AUTOSPAWN=0` | enabled |

GitHub App identity (used for the device-flow client_id and bot
committer name) is set by the host app via `azt_collabd.configure`,
but env vars also work:

| Env var | Default |
|---|---|
| `AZT_GITHUB_APP_CLIENT_ID` | `Iv23li66Fo9MBReatv6i` |
| `AZT_GITHUB_APP_SLUG` | `azt-recorder` |
| `AZT_GITHUB_COLLABORATOR` | `kent-rasmussen` |

## Daemon CLI

```bash
python -m azt_collabd          # start the daemon (foreground)
python -m azt_collabd ui       # standalone Kivy settings UI
python -m azt_collabd help     # entrypoint listing
```

The daemon is auto-spawned by the client library; running manually is
mostly useful for development or debugging.

## Testing

`tests/` (in `../azt_recorder/`) holds the canonical step-by-step
verification scripts. Each `tests/stepN.sh` exercises one slice of the
stack. Run with the recorder's venv:

```bash
cd ../azt_recorder
bash tests/step12.sh   # LIFT merge driver
bash tests/step16.sh   # sister-app example
```

Sister apps can copy + adapt these patterns; nothing in `tests/` needs
to be sister-app-specific.

## Plans + cleanup

- `azt_collabd_plan.xml`: the original 16-step migration plan that
  produced this codebase. Done.
- `azt_collabd_cleanup_drafts.xml`: outstanding follow-up tasks
  (Android ContentProvider transport variants, sync settings UI button,
  keystore policy notes).

## Conventions

- The backend has **no Kivy and no i18n imports**. UI marshaling and
  translation are the host app's job.
- All ops return structured `Result`s, not log strings. Substring
  matching on translated text is a regression and should be replaced
  with `Result.has(S.CODE)`.
- The daemon is the only thing that talks to dulwich. Clients write
  files into the working tree (or stream through the ContentProvider
  on Android) and ask the daemon to commit.
- Per-project advisory locking via `flock` on POSIX. Operations that
  cross projects are independent.
- `azt_collabd.config.configure(...)` for identity, called once at
  host startup. Defaults match the recorder.
