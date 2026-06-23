"""Recorder-specific p4a hook.

Delegates to the suite-shared hook
(~/bin/raspy/buildozer_tweaks/p4a_hook.py) for all the suite-
wide work, then injects the recorder's FileProvider <provider>
declaration into the rendered AndroidManifest.xml so the
Signal-compatible share-log path works (CLIENT_INTEGRATION.md
§ 14b-iii — Signal refuses MediaStore URIs and requires URIs
from the sender's own ContentProvider authority).

Pattern matches the shared hook for compatibility:
- Single-positional-arg toolchain functions (`before_apk_*`).
- Direct string-based manifest edit at the current working
  directory (p4a cd's to the dist dir before
  ``before_apk_assemble``).
- Sentinel comment for idempotency on re-runs.
- ``dist_name`` gate so the injection only happens on the
  recorder APK build.

Activated by ``p4a.hook = %(source.dir)s/p4a_hook_recorder.py``
in buildozer.spec — no env-var indirection needed; the hook
travels with the source tree.
"""

import os
import sys

_SHARED_HOOK_DIR = '/home/kentr/bin/raspy/buildozer_tweaks'
if _SHARED_HOOK_DIR not in sys.path:
    sys.path.insert(0, _SHARED_HOOK_DIR)
try:
    import p4a_hook as _shared
except ImportError:
    _shared = None


_RECORDER_PROVIDER_BLOCK = '''\
        <!-- recorder-fileprovider-injection (p4a_hook_recorder.py) -->
        <provider
            android:name="androidx.core.content.FileProvider"
            android:authorities="org.atoznback.aztrecorder.fileprovider"
            android:exported="false"
            android:grantUriPermissions="true">
            <meta-data
                android:name="android.support.FILE_PROVIDER_PATHS"
                android:resource="@xml/file_provider_paths" />
        </provider>
'''


def _call_shared(name, toolchain):
    if _shared is None:
        return
    fn = getattr(_shared, name, None)
    if callable(fn):
        fn(toolchain)


def before_apk_build(toolchain):
    _call_shared('before_apk_build', toolchain)


def before_apk_assemble(toolchain):
    """Delegate to the shared hook for its <application> child
    injections (aztcollab service / pick intent / self-replace
    receiver / bundle-reset receiver — all idempotent + gated
    by ``dist_name``), then add the recorder's FileProvider on
    top with our own sentinel comment + dist_name gate."""
    _call_shared('before_apk_assemble', toolchain)
    _inject_recorder_fileprovider(toolchain)


def _inject_recorder_fileprovider(toolchain):
    """Inject the recorder FileProvider <provider> into the
    rendered AndroidManifest.xml. Gated on
    ``dist_name == 'aztrecorder'`` so the daemon / viewer / any
    other suite app sharing this code path doesn't accidentally
    inherit it. Idempotent via sentinel comment so a re-run on
    the same dist skips cleanly."""
    if getattr(toolchain.args, 'dist_name', None) != 'aztrecorder':
        return
    candidates = [
        'AndroidManifest.xml',
        os.path.join('src', 'main', 'AndroidManifest.xml'),
    ]
    seen_any = False
    for path in candidates:
        if not os.path.exists(path):
            continue
        seen_any = True
        with open(path) as f:
            src = f.read()
        if 'recorder-fileprovider-injection' in src:
            print(f'[hook-recorder] {path} already has '
                  f'FileProvider, skipping')
            continue
        if '</application>' not in src:
            print(f'[hook-recorder] {path} has no </application>, '
                  f'skipping')
            continue
        new = src.replace(
            '</application>',
            _RECORDER_PROVIDER_BLOCK + '    </application>',
            1)
        with open(path, 'w') as f:
            f.write(new)
        print(f'[hook-recorder] injected FileProvider into {path}')
    if not seen_any:
        print('[hook-recorder] no AndroidManifest.xml candidates '
              'found in dist dir')
