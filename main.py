"""
A-Z+T Recorder — field audio recorder for LIFT XML lexicon databases.
Part of the A-Z+T suite of linguistic tools.

Records high-quality audio for dictionary entries, stores WAV files in an
'audio/' directory next to the LIFT file, and writes filenames back into
the LIFT XML via Entry.lc (citation form) with the audiolang tag.

Runs on Android (primary) and iOS/desktop (secondary) via Kivy.
"""

import os
import sys
import traceback
import warnings

from appinfo import APP_NAME, APP_TAGLINE, APP_USER_AGENT, APP_ICON, APP_SLUG
from i18n import _ as _tr, set_language, current_language, available_languages

# Tell the collab backend who we are. Defaults match the recorder, but
# calling configure() documents the contract and lets other suite apps
# override identity values via the same hook.
import azt_collab_client
from azt_collab_client import peer_pref, set_peer_pref
from azt_collab_client.ui import theme
azt_collab_client.configure(app_id='azt-recorder')
# Route client status/popup translations through the recorder's catalog
# so a single language preference covers both. The recorder's _ already
# falls back to the client catalog for client-owned strings.
azt_collab_client.set_translator(_tr)


class _DeviceFlowFailure(Exception):
    """Internal sentinel: GitHub device flow returned a structured Status
    code from the server (e.g. AUTH_DENIED). Carries the code + params
    so the UI can translate via azt_collab_client.translate_status."""
    def __init__(self, code, params=None):
        super().__init__(code)
        self.code = code
        self.params = params or {}


os.environ.setdefault('KIVY_NO_ENV_CONFIG', '1')
warnings.filterwarnings('ignore', message='.*olefile.*')


# ── Language-name lookup ───────────────────────────────────────────────────
# Used by the settings-page gloss-languages summary so the user sees
# "Lingala (RDC)" rather than "ln-CD". Defers to LangToggle._LANG_NAMES
# (defined further down) as the in-repo source of truth — that dict is
# also what the gloss-picker overlay shows, so the two stay aligned.
def _lang_display_name(code):
    return LangToggle._LANG_NAMES.get(code, code)

# ── Crash logging — runs before any Kivy import ────────────────────────────────
# On Android: p4a sets $ANDROID_PRIVATE to the app's private files dir (always writable).
#             Also tries /sdcard/ (may need MANAGE_EXTERNAL_STORAGE on API 30+).
# On desktop: ~/azt_recorder.log
_LOG_PATH = ''  # resolved by _setup_logging; read by App.share_log


def _setup_logging():
    global _LOG_PATH
    _on_android = os.path.exists('/system/build.prop')
    candidates = []
    if _on_android:
        # ANDROID_PRIVATE is set by p4a bootstrap, e.g. /data/user/0/org.x.y/files
        # This is the app's private filesDir — exists on every
        # Android install regardless of SD card presence.
        android_private = os.environ.get('ANDROID_PRIVATE', '')
        if android_private:
            candidates.append(os.path.join(android_private, 'azt_recorder.log'))
        # Defensive fallback when ANDROID_PRIVATE somehow isn't set:
        # query Context.getFilesDir() via jnius directly. Always works
        # on Android, doesn't depend on env-var plumbing.
        try:
            from jnius import autoclass
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            if activity is not None:
                files_dir = str(activity.getFilesDir().getAbsolutePath())
                if files_dir:
                    candidates.append(
                        os.path.join(files_dir, 'azt_recorder.log'))
        except Exception:
            pass
        # /sdcard fallback. Unreliable on API 30+ (scoped storage)
        # but cheap to try; the env-var path above is the normal one.
        candidates.append('/sdcard/azt_recorder.log')
    else:
        candidates += [os.path.join(os.path.expanduser('~'), 'azt_recorder.log')]
    fh = None
    for path in candidates:
        try:
            # Rotate the previous session's log out of the way before
            # truncating — `<path>.prev` keeps the prior session
            # available after a crash + relaunch, so the user can still
            # share the log that captures the original failure.
            if os.path.exists(path):
                prev = path + '.prev'
                try:
                    if os.path.exists(prev):
                        os.remove(prev)
                    os.replace(path, prev)
                except OSError:
                    pass
            fh = open(path, 'w', buffering=1, encoding='utf-8')
            _LOG_PATH = path
            break
        except OSError:
            continue
    if fh is None:
        return  # nowhere to write; rely on adb logcat

    class _Tee:
        """Write to both the original stream and the log file."""
        def __init__(self, original):
            self._orig = original
        def write(self, data):
            try: fh.write(data)
            except Exception: pass
            try: self._orig.write(data)
            except Exception: pass
        def flush(self):
            try: fh.flush()
            except Exception: pass
            try: self._orig.flush()
            except Exception: pass
        def fileno(self): return self._orig.fileno()

    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)

    _orig_hook = sys.excepthook
    def _hook(etype, evalue, etb):
        msg = ''.join(traceback.format_exception(etype, evalue, etb))
        print('[CRASH]', msg, flush=True)
        _orig_hook(etype, evalue, etb)
    sys.excepthook = _hook

    print(f'[LOG] azt_recorder starting — log: {path}', flush=True)

_setup_logging()

# ── Server-crash mirroring into the peer log ───────────────────────────────
# When the recorder observes a SERVER_UNAVAILABLE / SERVER_ERROR (or
# catches ServerUnavailable directly), it queries /v1/health and tees
# the daemon's `last_crash` into azt_recorder.log under a
# [server-crash] prefix — verbatim lines get a `| ` continuation so
# they're visually distinct from the recorder's own [CRASH] traceback.
# Without this, the recorder log shows a "server unavailable" line and
# the user would have to also grab $AZT_HOME/state/crash.log to see
# why. Rate-limited by (server started_at, hash(text)) so the same
# crash isn't re-dumped on every retry.

_LAST_SERVER_CRASH_FINGERPRINT = None


def _log_server_crash_if_any(context):
    """Pull /v1/health on a worker thread and dump the daemon's
    `last_crash` (if any) into the recorder log, clearly marked as
    coming from the SERVER's crash record — not the recorder's own.

    Best-effort: if /v1/health is itself unreachable (daemon fully
    down, or just-exited 'spawn_exited' case), we log that fact and
    return — the on-disk $AZT_HOME/state/crash.log is daemon-owned
    territory the peer doesn't reach into.

    ``context`` is a short string naming the peer-side call site
    (``auto_sync``, ``do_sync``, ``go_collab``, ``open_server_ui``)
    so the log can be grep-filtered by failure path."""

    def _worker():
        global _LAST_SERVER_CRASH_FINGERPRINT
        try:
            from azt_collab_client.rpc import (
                health as _health, ServerUnavailable)
        except ImportError:
            return
        try:
            h = _health()
        except ServerUnavailable as ex:
            print(f'[server-crash] context={context}: /v1/health '
                  f'unreachable — daemon is fully down ({ex}); '
                  f'recorder log cannot mirror the daemon crash '
                  f'log because the daemon process is gone. Pull '
                  f'$AZT_HOME/state/crash.log directly for the '
                  f'postmortem.', flush=True)
            return
        except Exception as ex:
            print(f'[server-crash] context={context}: /v1/health '
                  f'raised: {ex}', flush=True)
            return
        if not isinstance(h, dict):
            return
        last_crash = h.get('last_crash')
        started_at = h.get('started_at')
        if not last_crash:
            print(f'[server-crash] context={context}: no last_crash '
                  f'on /v1/health (server started_at={started_at}); '
                  f'failure was not a daemon-side crash',
                  flush=True)
            return
        text = str(last_crash)
        fingerprint = (started_at, hash(text))
        if fingerprint == _LAST_SERVER_CRASH_FINGERPRINT:
            print(f'[server-crash] context={context}: same daemon '
                  f'crash as previously logged; skipping replay',
                  flush=True)
            return
        _LAST_SERVER_CRASH_FINGERPRINT = fingerprint
        print(f'[server-crash] context={context}: server '
              f'started_at={started_at}; verbatim daemon crash '
              f'follows (from /v1/health.last_crash) — lines '
              f'prefixed with "| " are the SERVER log, not the '
              f'recorder log:', flush=True)
        for line in text.splitlines():
            print(f'[server-crash] | {line}', flush=True)
        print('[server-crash] (end of daemon crash log)',
              flush=True)

    import threading
    threading.Thread(target=_worker, daemon=True).start()


# On Android the default KIVY_HOME lands inside the read-only app bundle.
# Point it to a writable location before Kivy is imported.
if 'ANDROID_PRIVATE' in os.environ:
    _kivy_home = os.path.join(os.environ['ANDROID_PRIVATE'], '.kivy')
    os.environ.setdefault('KIVY_HOME', _kivy_home)

from kivy.app import App
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition, NoTransition
from kivy.core.window import Window
from kivy.utils import platform
from kivy.clock import Clock
from kivy.metrics import dp, sp
from kivy.properties import (
    StringProperty, ListProperty, NumericProperty,
    BooleanProperty, ObjectProperty, DictProperty
)

from lift import LIFTDatabase

# ── Android permissions ────────────────────────────────────────────────────────
if platform == 'android':
    from android.permissions import request_permissions, Permission  # noqa
    request_permissions([
        Permission.RECORD_AUDIO,
        Permission.READ_EXTERNAL_STORAGE,
        Permission.WRITE_EXTERNAL_STORAGE,
    ])

# ── Charis SIL font registration ──────────────────────────────────────────────
# Discovery + LabelBase.register lives in azt_collab_client.ui.fonts so the
# recorder, the standalone picker, and the settings UI all agree on the
# search path. The shared helper checks the recorder's fonts/ dir first.
# Download from https://software.sil.org/charis/ or `apt install fonts-sil-charis`.
from azt_collab_client.ui import register_charis
_FONT_NAME = register_charis()
_CHARIS_AVAILABLE = _FONT_NAME == 'CharisSIL'
print(f'[font] CharisSIL='
      f'{"loaded" if _CHARIS_AVAILABLE else "MISSING — Roboto fallback"}',
      file=sys.stderr, flush=True)

# ── Gloss display mode ────────────────────────────────────────────────────────
# True → long glosses scroll horizontally inside a fixed-height row
# (MarqueeLabel); short glosses stay static. False → fall back to the
# 1.55.2 wrap behavior (multi-line label, row height grows). Kept as a
# constant so flipping back is one edit, no settings UI noise.
GLOSS_USE_MARQUEE = True

# ── KV layout ─────────────────────────────────────────────────────────────────
# Font name is injected at build time so every widget uses Charis SIL if available.
KV_TEMPLATE = '''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp
#:import T azt_collab_client.ui.theme
#:import _ i18n._
#:set FONT '{font_name}'

<RootScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        pos: root.pos
        size: root.size
        Widget:
            size_hint_y: None
            height: root._inset_top
        ScreenManager:
            id: sm
            RecorderScreen:
                name: 'recorder'
            ConfigScreen:
                name: 'config'
            CollabScreen:
                name: 'collab'
            ImagePickerScreen:
                name: 'imagepicker'
        Widget:
            size_hint_y: None
            height: root._inset_bottom

<RecorderScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        id: content_box
        orientation: 'vertical'
        # ── Top bar ──────────────────────────────────────────────────────────
        BoxLayout:
            size_hint_y: None
            height: dp(52)
            padding: dp(8), dp(6)
            spacing: dp(8)
            canvas.before:
                Color:
                    rgba: T.SURFACE
                Rectangle:
                    pos: self.pos
                    size: self.size
            Button:
                id: progress_label
                text: ''
                font_size: sp(13)
                font_name: FONT
                color: T.TEXT_DIM
                halign: 'left'
                valign: 'middle'
                text_size: self.size
                background_color: T.TRANSPARENT
                background_normal: ''
                on_release: app.show_goto_dialog()
                size_hint_x: 1
                # Ellipsize from the left so the [k/n] split
                # suffix at the right (the thing that
                # distinguishes one phone from another) stays
                # visible even on narrow screens or when the
                # lang prefix is long. shorten_from='left'
                # drops characters off the front with a '…'
                # prefix; the lang code is the same across
                # every phone on a project, so losing it from
                # this label is the right thing to lose.
                shorten: True
                shorten_from: 'left'
                split_str: ''
            Button:
                # Gated on app.has_project (same pattern as the gear
                # to its right) so sync + settings reveal together
                # when load_lift finishes, not one-by-one through
                # the partial-load window. size_hint_x flips between
                # 1 (proportional fill) and None+width=0 so the
                # adjacent progress_label button absorbs the space
                # while we're hidden.
                size_hint_x: 1 if app.has_project else None
                width: 0
                opacity: 1 if app.has_project else 0
                disabled: not app.has_project
                background_color: T.TRANSPARENT
                background_normal: ''
                on_release: app.do_sync()
                BoxLayout:
                    center: self.parent.center
                    size_hint: None, 1
                    width: dp(160)
                    spacing: dp(4)
                    Image:
                        source: 'icons/sync_dark.png'
                        size_hint_x: None
                        width: dp(28)
                        allow_stretch: True
                        keep_ratio: True
                    Label:
                        id: sync_status_label
                        text: ''
                        font_size: sp(13)
                        font_name: FONT
                        color: T.TEXT_MID
                        # markup=True so _sync_status_info can wrap
                        # the uncommitted-files count in [color=ff4444]
                        # — surfaces the "commits aren't happening"
                        # state (daemon-unreachable accumulation).
                        markup: True
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
                        # v0.47.0 sync labels can exceed the dp(128)
                        # label width (e.g. "WAN-113 LAN-1 +1 · offline"
                        # is 26 chars at sp(13) ≈ 169dp). With text_size
                        # constraining the layout box, Kivy wraps and
                        # the wrapped tail lines get clipped or hidden
                        # behind the top bar background — observed
                        # symptom: badge appearing as just "· offline"
                        # with the WAN-N / LAN-N parts invisible.
                        # ``shorten: True`` keeps the text on one line
                        # and ellipsises overflow; ``shorten_from:
                        # 'right'`` truncates from the trailing edge
                        # so the load-bearing state label at the
                        # start stays visible (the offline suffix is
                        # the part most likely to get dropped).
                        shorten: True
                        shorten_from: 'right'
            Button:
                # Gear → Settings. Hidden + disabled until a
                # project loads (app.has_project flips True at
                # the end of load_lift). Reaching Settings in
                # the no-project state triggers _hide_box_tree
                # zeroing every descendant, and the children
                # never come back without an app restart —
                # gating the button is the cheapest defence.
                size_hint_x: None
                width: dp(44) if app.has_project else 0
                opacity: 1 if app.has_project else 0
                disabled: not app.has_project
                background_color: T.TRANSPARENT
                background_normal: ''
                on_release: app.go_config()
                Image:
                    source: 'icons/gear.png'
                    size: dp(28), dp(28)
                    size_hint: None, None
                    center: self.parent.center
                    allow_stretch: True
                    keep_ratio: True
        # ── Severe-alert banner (CLIENT_INTEGRATION.md § 17,
        # never-silenced row: DATA_LOSS_RISK + COMMIT_REPEATEDLY_
        # FAILED). Sticky — stays up until the user taps to
        # dismiss, because the message tells them to share the
        # daemon log and a 1.5s toast wouldn't survive long
        # enough to read. Button rather than Label so the tap-
        # to-dismiss is native; on_release fires _dismiss_severe_
        # alert. Visually distinct red background. Starts
        # collapsed (height: 0 / opacity: 0).
        Button:
            id: severe_alert_banner
            text: ''
            font_size: sp(12)
            font_name: FONT
            color: 1, 1, 1, 1
            background_color: 0.55, 0.18, 0.18, 1
            background_normal: ''
            size_hint_y: None
            height: 0
            opacity: 0
            halign: 'center'
            valign: 'middle'
            text_size: self.width - dp(16), None
            markup: True
            on_release: app._dismiss_severe_alert()
        # ── Cache-progress indicator (CLIENT_INTEGRATION.md § 10.5 —
        # required whenever a CAWL prefetch is in flight; conveys "network
        # in use" so the user doesn't disconnect Wi-Fi mid-warm). Starts
        # collapsed; expanded by _tick_cache_status while cached < total,
        # collapsed again when caching catches up.
        Label:
            id: cache_status_label
            text: ''
            font_size: sp(11)
            font_name: FONT
            color: T.ACCENT
            size_hint_y: None
            height: 0
            opacity: 0
            halign: 'center'
            valign: 'middle'
            text_size: self.width, None
        # ── Image ────────────────────────────────────────────────────────────
        FloatLayout:
            id: image_container
            size_hint_y: None
            height: entry_image.height
            AsyncImage:
                id: entry_image
                source: ''
                size_hint: 1, None
                height: 0
                opacity: 0
                allow_stretch: True
                keep_ratio: True
                pos: self.parent.pos
            ImageRedoBtn:
                id: image_redo_btn
                size_hint: None, None
                size: dp(80), dp(80)
                pos: image_container.x + image_container.width - self.width - dp(4), image_container.y + dp(4)
                background_color: T.TRANSPARENT
                background_normal: ''
                opacity: 1 if entry_image.opacity > 0 else 0
                disabled: entry_image.opacity == 0
                Image:
                    source: 'icons/redo.png'
                    size: dp(64), dp(64)
                    size_hint: None, None
                    center: self.parent.center
                    allow_stretch: True
                    keep_ratio: True
        # ── Glosses ──────────────────────────────────────────────────────────
        BoxLayout:
            id: gloss_box
            orientation: 'vertical'
            size_hint_y: 1
            padding: dp(4), 0
            spacing: dp(2)
        # ── Status line ───────────────────────────────────────────────────────
        Label:
            id: status_label
            text: ''
            font_size: sp(12)
            font_name: FONT
            color: T.TEXT_DIM
            size_hint_y: None
            height: dp(28)
            halign: 'center'
            valign: 'middle'
            text_size: self.width, None
        # ── Button area (record OR play+redo) ─────────────────────────────────
        BoxLayout:
            id: btn_area
            size_hint_y: None
            height: dp(100)
            padding: dp(4), dp(4)
            spacing: dp(4)
            # Populated dynamically by refresh_recorder_ui

<ConfigScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        # Top bar
        BoxLayout:
            size_hint_y: None
            height: dp(52)
            padding: dp(8), dp(6)
            canvas.before:
                Color:
                    rgba: T.SURFACE
                Rectangle:
                    pos: self.pos
                    size: self.size
            # Title-bar layout:
            #   [version] [share] [Update]  …open space…  [X]
            # Version label sized to its text so the action
            # buttons sit immediately to its right (not stranded
            # next to the close X). A spacer Widget after the
            # action buttons takes the leftover space so X stays
            # right-anchored.
            Label:
                text: app.version_string
                font_size: sp(11)
                font_name: FONT
                color: T.TEXT_DIM
                halign: 'left'
                valign: 'middle'
                padding_x: dp(8)
                size_hint_x: None
                width: self.texture_size[0] + dp(16)
                text_size: None, self.height
            # Share-this-app icon. Title-bar location replaces the
            # body button used through 1.55.5; same on_release.
            Button:
                size_hint_x: None
                width: dp(44)
                background_color: T.TRANSPARENT
                background_normal: ''
                on_release: app.share_apk()
                Image:
                    source: 'icons/share_dark.png'
                    size: dp(24), dp(24)
                    size_hint: None, None
                    center: self.parent.center
                    allow_stretch: True
                    keep_ratio: True
            # Explicit update check — distinct from the silent
            # bootstrap probe. Always shows a modal with the
            # outcome so the user can tell "no newer release"
            # apart from "couldn't reach GitHub".
            Button:
                text: _('Update')
                font_size: sp(13)
                font_name: FONT
                color: T.ACCENT
                background_color: T.TRANSPARENT
                background_normal: ''
                size_hint_x: None
                width: dp(70)
                on_release: app.check_for_update_explicit()
            # Spacer — pushes X to the right edge.
            Widget:
                size_hint_x: 1
            IconBtn:
                text: 'X'
                size_hint_x: None
                width: dp(44)
                on_release: root.apply_and_go()
        ScrollView:
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                # ── UI Language (very top, no header — buttons speak
                # for themselves) ──────────────────────────────────────
                BoxLayout:
                    id: lang_selector_row
                    size_hint_y: None
                    height: dp(44)
                    spacing: dp(8)
                # ── Database-dependent settings (hidden when no project) ──
                BoxLayout:
                    id: db_settings_box
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(14)
                    # ── Recording task selector ────────────────────────────
                    BoxLayout:
                        id: rec_task_row
                        size_hint_y: None
                        height: 0
                        opacity: 0
                        spacing: dp(8)
                        Label:
                            text: _('Recording:')
                            font_size: sp(15)
                            font_name: FONT
                            color: T.TEXT_DIM
                            size_hint_x: None
                            size: self.texture_size
                            valign: 'middle'
                        RecBtn:
                            id: rec_task_btn
                            text: ''
                            font_size: sp(15)
                            normal_color: T.SURFACE
                            on_release: root._show_rec_overlay()
                    # ── Three blue collapsible buttons ─────────────────────
                    BoxLayout:
                        size_hint_y: None
                        height: dp(44)
                        spacing: dp(8)
                        RecBtn:
                            id: gloss_toggle_btn
                            text: _('Gloss languages')
                            size_hint_x: None
                            width: dp(160)
                            normal_color: T.ACCENT
                            font_size: sp(14)
                            on_release: root._show_gloss_overlay()
                        # Marquee so a long comma-joined list of
                        # selected gloss langs stays single-line and
                        # scrolls instead of wrapping into the row
                        # below. _update_gloss_summary sets .text;
                        # MarqueeLabel decides static-vs-scroll based
                        # on whether the text fits its width.
                        MarqueeLabel:
                            id: gloss_summary_label
                            text: ''
                            font_size: sp(14)
                            font_name: FONT
                            color: T.TEXT_DIM
                    BoxLayout:
                        size_hint_y: None
                        height: dp(44)
                        spacing: dp(8)
                        RecBtn:
                            id: filter_toggle_btn
                            text: _('Filter words')
                            size_hint_x: None
                            width: dp(160)
                            normal_color: T.ACCENT
                            font_size: sp(14)
                            on_release: root.toggle_filter_panel()
                        # Marquee for the same reason as the gloss
                        # summary above — long filter combinations
                        # (CAWL + gloss + unrecorded) stay one line
                        # and scroll. _update_filter_summary sets
                        # plain comma-joined text; no markup ever
                        # inserted, so markup:True is unused.
                        MarqueeLabel:
                            id: filter_summary_label
                            text: ''
                            font_size: sp(12)
                            font_name: FONT
                            color: T.TEXT_DIM
                    BoxLayout:
                        id: filter_panel
                        orientation: 'vertical'
                        size_hint_y: None
                        height: 0
                        opacity: 0
                        spacing: dp(8)
                        Label:
                            id: cawl_label
                            text: _('CAWL number or range (e.g. 1-100, 42, leave blank for all)')
                            font_size: sp(13)
                            font_name: FONT
                            color: T.TEXT_DIM
                            size_hint_y: None
                            height: dp(36)
                            halign: 'left'
                            text_size: self.width, None
                        TextInput:
                            id: cawl_input
                            hint_text: _('e.g. 1-500')
                            font_size: sp(16)
                            font_name: FONT
                            size_hint_y: None
                            height: dp(48)
                            background_color: T.SURFACE
                            foreground_color: T.TEXT
                            cursor_color: T.ACCENT
                            multiline: False
                            on_text_validate: root.apply_cawl(self.text)
                            # 'number' gives the digit keypad with
                            # ``-`` and ``,`` accessible for CAWL
                            # range syntax. No ``input_filter`` —
                            # 'int' would block ``-`` and ``,``;
                            # apply_cawl's parser handles validation.
                            input_type: 'number'
                        # ── Wordlist split (§ 21) ─────────────────
                        # Team-size row is always visible inside the
                        # expanded filter panel. Slot row appears
                        # below it only when team_size is set.
                        # Buttons are built dynamically by
                        # ConfigScreen._refresh_split_rows so the
                        # team_size value drives [1/n]..[n/n] without
                        # KV recomposition.
                        Label:
                            id: split_devices_label
                            text: _('Split across devices')
                            font_size: sp(13)
                            font_name: FONT
                            color: T.TEXT_DIM
                            size_hint_y: None
                            height: dp(36)
                            halign: 'left'
                            text_size: self.width, None
                        BoxLayout:
                            id: team_size_row
                            orientation: 'horizontal'
                            size_hint_y: None
                            height: dp(48)
                            spacing: dp(6)
                        Label:
                            id: which_device_label
                            text: _('Which device is this?')
                            font_size: sp(13)
                            font_name: FONT
                            color: T.TEXT_DIM
                            size_hint_y: None
                            height: 0
                            halign: 'left'
                            text_size: self.width, None
                        BoxLayout:
                            # Vertical container so claimed slots
                            # (which need their device-name labels
                            # readable) can stack one per line
                            # underneath the horizontal row of
                            # available + own-claim buttons. Height
                            # is set dynamically by _rebuild_slot_row
                            # since it varies with the number of
                            # other devices currently claiming a
                            # slot.
                            id: slot_row
                            orientation: 'vertical'
                            size_hint_y: None
                            height: 0
                            spacing: dp(4)
                        Label:
                            id: gloss_search_label
                            text: _('Gloss search (filter by gloss text)')
                            font_size: sp(13)
                            font_name: FONT
                            color: T.TEXT_DIM
                            size_hint_y: None
                            height: dp(36)
                            halign: 'left'
                            text_size: self.width, None
                        TextInput:
                            id: gloss_search_input
                            hint_text: _('search gloss text…')
                            font_size: sp(16)
                            font_name: FONT
                            size_hint_y: None
                            height: dp(48)
                            background_color: T.SURFACE
                            foreground_color: T.TEXT
                            cursor_color: T.ACCENT
                            multiline: False
                        BoxLayout:
                            id: filter_bottom_row
                            orientation: 'horizontal'
                            size_hint_y: None
                            height: dp(56)
                            spacing: dp(8)
                            # "Show past work" toggle was here.
                            # Pulled in 1.52.x — the same toggle
                            # lives in the Go-To popup, which is
                            # day-to-day more accessible (one tap
                            # from the recorder bar vs. walking
                            # into Settings → Filter words).
                            RecBtn:
                                id: filter_ok_btn
                                text: _('OK')
                                size_hint_x: 1
                                height: dp(56)
                                normal_color: T.GREEN
                                font_size: sp(15)
                                on_release: root.toggle_filter_panel()
                # ── Project ───────────────────────────────────────────
                # Setup Collaboration + Select Project both act on the
                # project, so they share a header.
                SectionLabel:
                    text: _('Project')
                RecBtn:
                    text: _('Setup Collaboration')
                    size_hint_x: None
                    width: min(self.parent.width - dp(40), dp(360))
                    pos_hint: {{'center_x': 0.5}}
                    normal_color: T.SURFACE
                    on_release: app.go_collab()
                RecBtn:
                    text: _('Select Project')
                    size_hint_x: None
                    width: min(self.parent.width - dp(40), dp(360))
                    pos_hint: {{'center_x': 0.5}}
                    normal_color: T.BTN_INACTIVE
                    on_release: app.start_over()
                # ── Conserve power ──────────────────────────────────
                # Audio quality and auto-sync cadence are both power
                # / network trade-offs. Always visible (these are
                # peer-pref-backed suite-wide settings, not
                # project-bound), and grouped under one header so
                # the user knows where to look when battery / data
                # is the constraint. Audio quality additionally has
                # a hidden auto-degradation ceiling (audio_quality_
                # ceiling, set on repeated record failures) that
                # clamps the picker — see _start_android_recording.
                SectionLabel:
                    text: _('Conserve power')
                # ── Audio quality selector ─────────────────────────────
                BoxLayout:
                    id: audio_quality_row
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(8)
                    Label:
                        text: _('Audio quality:')
                        font_size: sp(15)
                        font_name: FONT
                        color: T.TEXT_DIM
                        size_hint_x: None
                        size: self.texture_size
                        valign: 'middle'
                    RecBtn:
                        id: audio_quality_btn
                        text: ''
                        font_size: sp(15)
                        normal_color: T.SURFACE
                        on_release: root._show_audio_quality_overlay()
                # ── Debug ────────────────────────────────────────────
                # Diagnostic affordances live at the bottom — most
                # users will never tap these, but when something is
                # wrong the support flow is "scroll all the way down,
                # share the log".
                SectionLabel:
                    text: _('Debug')
                RecBtn:
                    # Double-brace escapes are mandatory below: the
                    # whole KV template gets passed through
                    # KV_TEMPLATE.format(font_name=_FONT_NAME) before
                    # Kivy ever sees it, so any single-brace placeholder
                    # in KV is interpreted by the outer format() as a
                    # substitution target (and crashes with KeyError if
                    # the key isn't in the kwargs). Double braces survive
                    # the outer pass as a single brace pair, then the
                    # inner .format(app=...) on the gettext-translated
                    # string substitutes correctly at rule-eval time.
                    # NB: this comment lives inside the KV string too,
                    # so avoid putting any single-brace patterns here.
                    # If APP_NAME in appinfo.py ever changes, update
                    # the literal string below to match.
                    text: _('Share {{app}} log').format(app='A-Z+T Recorder')
                    halign: 'left'
                    padding: [dp(52), 0]
                    text_size: self.size
                    valign: 'middle'
                    size_hint_x: None
                    width: min(self.parent.width - dp(40), dp(360))
                    pos_hint: {{'center_x': 0.5}}
                    normal_color: T.SURFACE
                    on_release: app.share_log()
                    Image:
                        source: 'icons/share_dark.png'
                        size_hint: None, None
                        size: dp(24), dp(24)
                        x: self.parent.x + dp(16)
                        center_y: self.parent.center_y
                Widget:
                    size_hint_y: None
                    height: dp(40)

<CollabScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        # Top bar
        BoxLayout:
            size_hint_y: None
            height: dp(52)
            padding: dp(8), dp(6)
            canvas.before:
                Color:
                    rgba: T.SURFACE
                Rectangle:
                    pos: self.pos
                    size: self.size
            Label:
                text: _('Setup Collaboration')
                font_size: sp(17)
                font_name: FONT
                bold: True
                color: T.ACCENT
                halign: 'left'
                valign: 'middle'
                text_size: self.size
                padding_x: dp(8)
            IconBtn:
                text: 'X'
                size_hint_x: None
                width: dp(44)
                on_release: root.close_collab()
        ScrollView:
            do_scroll_x: False
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                # ── Your name ─────────────────────────────────────────────
                SectionLabel:
                    text: _('Your name [color=ff4444]*[/color]')
                    markup: True
                TextInput:
                    id: name_input
                    hint_text: _('Your name (for commit attribution)')
                    font_size: sp(15)
                    font_name: FONT
                    size_hint_y: None
                    height: dp(48)
                    background_color: T.SURFACE
                    foreground_color: T.TEXT
                    cursor_color: T.ACCENT
                    multiline: False
                    on_text: root._update_connect_enabled()
                # ── Host toggle ───────────────────────────────────────────
                BoxLayout:
                    size_hint_y: None
                    height: dp(40)
                    spacing: dp(8)
                    RecBtn:
                        id: host_github_btn
                        text: _('GitHub')
                        font_size: sp(14)
                        normal_color: T.GREEN
                        on_release: root.set_host('github')
                    RecBtn:
                        id: host_gitlab_btn
                        text: _('GitLab')
                        font_size: sp(14)
                        normal_color: T.BTN_INACTIVE
                        on_release: root.set_host('gitlab')
                # ── GitHub section ────────────────────────────────────────
                BoxLayout:
                    id: gh_section
                    orientation: 'vertical'
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(14)
                    SectionLabel:
                        text: _('GitHub account')
                    Label:
                        id: gh_status_label
                        text: _('Not connected')
                        font_size: sp(14)
                        font_name: FONT
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: dp(28)
                        halign: 'left'
                        text_size: self.width, None
                    RecBtn:
                        id: gh_connect_btn
                        text: _('Connect to GitHub')
                        normal_color: T.GREEN
                        on_release: root.start_device_flow()
                    Label:
                        id: device_instructions_label
                        text: ''
                        font_size: sp(13)
                        font_name: FONT
                        markup: True
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: dp(0)
                        halign: 'center'
                        text_size: self.width, None
                    RecBtn:
                        id: install_app_btn
                        text: _('Install GitHub App')
                        normal_color: T.GREEN
                        size_hint_y: None
                        height: dp(0)
                        opacity: 0
                        on_release: root.open_install_page()
                    BoxLayout:
                        id: device_code_box
                        size_hint_y: None
                        height: dp(0)
                        opacity: 0
                        spacing: dp(8)
                        padding: dp(20), 0
                        Label:
                            id: device_code_label
                            text: ''
                            font_size: sp(28)
                            font_name: FONT
                            bold: True
                            color: T.TEXT
                            halign: 'center'
                            valign: 'middle'
                            text_size: self.size
                        RecBtn:
                            id: copy_code_btn
                            text: _('Copy')
                            size_hint_x: None
                            width: dp(64)
                            font_size: sp(13)
                            normal_color: T.BTN_INACTIVE
                            on_release: root.copy_code()
                # ── GitLab section ────────────────────────────────────────
                BoxLayout:
                    id: gl_section
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(14)
                    SectionLabel:
                        text: _('GitLab account')
                    TextInput:
                        id: gl_token_input
                        hint_text: _('Personal access token')
                        font_size: sp(14)
                        font_name: FONT
                        size_hint_y: None
                        height: dp(48)
                        background_color: T.SURFACE
                        foreground_color: T.TEXT
                        cursor_color: T.ACCENT
                        multiline: False
                        password: True
                    TextInput:
                        id: gl_username_input
                        hint_text: _('GitLab username')
                        font_size: sp(14)
                        font_name: FONT
                        size_hint_y: None
                        height: dp(48)
                        background_color: T.SURFACE
                        foreground_color: T.TEXT
                        cursor_color: T.ACCENT
                        multiline: False
                    RecBtn:
                        text: _('Save GitLab credentials')
                        normal_color: T.GREEN
                        on_release: root.save_gitlab_credentials()
                # Project-bound surfaces (Publish, Grant collaborator
                # access, Share-repo) moved to the daemon settings UI
                # in 1.41.5 / azt_collabd 0.41.0 per CLIENT_INTEGRATION.md
                # § 13. Peers delegate via the button below.
                SectionLabel:
                    text: _('This project')
                RecBtn:
                    text: _('Open Sync Settings')
                    normal_color: T.ACCENT
                    on_release: root.open_server_ui()
                # ── Log ───────────────────────────────────────────────────
                SectionLabel:
                    text: _('Last operation')
                Label:
                    id: log_label
                    text: ''
                    font_size: sp(12)
                    font_name: FONT
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: self.texture_size[1] + dp(16)
                    halign: 'left'
                    valign: 'top'
                    text_size: self.width, None
                Widget:
                    size_hint_y: 1

<ImagePickerScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        # Prompt (fixed, doesn't scroll)
        Label:
            id: prompt_label
            text: ''
            font_size: sp(30)
            font_name: FONT
            color: T.ACCENT
            size_hint_y: None
            height: dp(72)
            halign: 'center'
            valign: 'middle'
            text_size: self.width, None
            padding: dp(12), dp(8)
        # Scrollable image grid
        ScrollView:
            GridLayout:
                id: image_grid
                cols: 2
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(8)
                padding: dp(8)
        # Bottom buttons — web sources
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            padding: dp(12), dp(2)
            spacing: dp(6)
            RecBtn:
                text: _('openclipart')
                normal_color: T.GREEN
                font_size: sp(12)
                on_release: root.fetch_openclipart()
            RecBtn:
                text: _('FreeSVG')
                normal_color: T.TEAL
                font_size: sp(12)
                on_release: root.fetch_freesvg()
            RecBtn:
                text: _('Wikimedia')
                normal_color: T.BLUE
                font_size: sp(12)
                on_release: root.fetch_wikimedia()
        # Bottom buttons — local sources
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            padding: dp(12), dp(2)
            spacing: dp(6)
            RecBtn:
                text: _('Photo')
                normal_color: T.BTN_INACTIVE
                font_size: sp(12)
                on_release: root.take_photo()
            RecBtn:
                text: _('File')
                normal_color: T.BTN_INACTIVE
                font_size: sp(12)
                on_release: root.pick_from_file()
            RecBtn:
                text: _('Cancel')
                normal_color: T.SURFACE
                font_size: sp(12)
                on_release: app.go_recorder()

# ── Reusable widgets ──────────────────────────────────────────────────────────

<RecordButton>:
    size_hint: 1, 1
    canvas:
        Color:
            rgba: T.RED if self.recording else T.ACCENT
        RoundedRectangle:
            pos: self.x + dp(4), self.y + dp(4)
            size: self.width - dp(8), self.height - dp(8)
            radius: [dp(12)]
        # Filled circle (record icon) or square (stop icon)
        Color:
            rgba: (1, 1, 1, 1)
        Ellipse:
            pos: self.center_x - dp(14), self.center_y - dp(14)
            size: dp(28), dp(28)
            angle_start: 0 if not self.recording else 0
            angle_end: 360 if not self.recording else 0
        Rectangle:
            pos: self.center_x - dp(11), self.center_y - dp(11)
            size: (0, 0) if not self.recording else (dp(22), dp(22))

<PlayButton>:
    canvas:
        Color:
            rgba: T.GREEN
        RoundedRectangle:
            pos: self.x + dp(4), self.y + dp(4)
            size: self.width - dp(8), self.height - dp(8)
            radius: [dp(12)]
        # Triangle (play icon)
        Color:
            rgba: (1, 1, 1, 1)
        Triangle:
            points: self.center_x - dp(12), self.center_y - dp(16), self.center_x - dp(12), self.center_y + dp(16), self.center_x + dp(16), self.center_y

<RedoButton>:
    canvas:
        Color:
            rgba: T.SURFACE
        RoundedRectangle:
            pos: self.x + dp(4), self.y + dp(4)
            size: self.width - dp(8), self.height - dp(8)
            radius: [dp(12)]
        Color:
            rgba: T.ACCENT[:3] + (0.6,)
        Line:
            rounded_rectangle: self.x + dp(4), self.y + dp(4), self.width - dp(8), self.height - dp(8), dp(12)
            width: dp(1.5)
    Label:
        text: 'X'
        font_size: sp(36)
        font_name: FONT
        bold: True
        color: T.ACCENT
        center: root.center

<GlossRow>:
    size_hint_y: None
    height: dp(70)
    canvas.before:
        Color:
            rgba: T.SURFACE
        RoundedRectangle:
            pos: self.x, self.y + dp(2)
            size: self.width, self.height - dp(4)
            radius: [dp(8)]
    BoxLayout:
        spacing: dp(10)
        padding: dp(12), dp(8)
        Label:
            text: root.lang
            font_size: sp(13)
            font_name: FONT
            color: T.TEXT_DIM
            size_hint_x: None
            width: dp(44)
            halign: 'right'
            valign: 'middle'
            text_size: self.size
        BoxLayout:
            # Populated in GlossRow.__init__ — either a MarqueeLabel
            # (marquee mode, single line, scrolls on overflow) or a
            # wrapping Label (wrap mode, multi-line, grows the row).
            id: gloss_holder


<RecBtn@Button>:
    normal_color: T.ACCENT
    size_hint_y: None
    height: dp(52)
    background_color: T.TRANSPARENT
    background_normal: ''
    canvas.before:
        Color:
            rgba: self.normal_color or T.ACCENT
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    color: (1, 1, 1, 1)
    font_size: sp(16)
    font_name: FONT
    bold: True

<NavBtn@Button>:
    size_hint_y: 1
    background_color: T.TRANSPARENT
    background_normal: ''
    canvas.before:
        Color:
            rgba: T.SURFACE
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    color: T.ACCENT
    font_size: sp(16)
    font_name: FONT
    bold: True

<IconBtn@Button>:
    background_color: T.TRANSPARENT
    background_normal: ''
    color: T.TEXT_DIM
    font_size: sp(20)
    font_name: FONT

<SectionLabel@Label>:
    size_hint_y: None
    height: dp(32)
    font_size: sp(12)
    font_name: FONT
    bold: True
    color: T.ACCENT
    halign: 'left'
    valign: 'middle'
    text_size: self.size
    text: ''

<CheckboxStyled@CheckBox>:
    size_hint_x: None
    width: dp(40)
    color: T.ACCENT

<LangToggle>:
    size_hint_y: None
    height: dp(44)
    canvas.before:
        Color:
            rgba: T.GREEN_DARK if root.active else T.SURFACE
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(6)]
    Label:
        text: root.display_name
        font_size: sp(15)
        font_name: FONT
        bold: root.active
        color: T.GREEN_BRIGHT if root.active else T.TEXT
        halign: 'center'
        valign: 'middle'
        text_size: self.size
'''

KV = KV_TEMPLATE.format(font_name=_FONT_NAME)


# ── Widget classes ─────────────────────────────────────────────────────────────

from kivy.uix.widget import Widget
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView


class ImageRedoBtn(Widget):
    """Tap-only button over the entry image. Ignores touches that become swipes."""
    _touch_id = None

    def on_touch_down(self, touch):
        if self.disabled or self.opacity == 0:
            return False
        if self.collide_point(*touch.pos):
            self._touch_id = touch.uid
            return True           # claim but wait for up
        return False

    def on_touch_move(self, touch):
        if touch.uid == self._touch_id:
            # Any movement cancels the tap
            self._touch_id = None
        return False

    def on_touch_up(self, touch):
        if touch.uid == self._touch_id:
            self._touch_id = None
            if self.collide_point(*touch.pos):
                app = App.get_running_app()
                app.show_image_picker()
                return True
        return False


class RecordButton(Widget):
    recording = BooleanProperty(False)


class PlayButton(Widget):
    pass


class RedoButton(Widget):
    pass




class MarqueeLabel(ScrollView):
    """Single-line label that horizontally scrolls when its text
    overflows the widget's width; static (left-aligned, no
    animation) when the text fits. Display-only — never consumes
    touches, so RecorderScreen's swipe handler still sees touches
    landing on it inside the gloss_box swipe zone.

    Cost: one text rasterisation per text change (same as a normal
    Label), then per-frame work is a single ScrollView.scroll_x
    property update animated via kivy.animation.Animation —
    cheaper than character-ticker re-rasterisation because the
    texture is built once and the GPU does the translation."""

    text = StringProperty('')
    font_size = NumericProperty(sp(16))
    font_name = StringProperty('Roboto')
    color = ListProperty([1, 1, 1, 1])
    bold = BooleanProperty(False)

    # Constant pixel-per-second scroll rate: same across every
    # marquee on screen, so a longer gloss simply takes longer to
    # finish and cycles less often. (Previously animated scroll_x
    # 0→1 over a duration scaled by overflow, which also gave
    # constant speed mathematically — but the bounce-back-then-
    # forward motion made the speed feel variable. One-way scroll
    # below removes that.)
    SCROLL_SPEED = dp(25)
    START_PAUSE = 1.5        # sec at left edge before scrolling
    END_PAUSE = 2.0          # sec at right edge before reset

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.do_scroll_x = False
        self.do_scroll_y = False
        self.bar_width = 0
        self._label = Label(
            text=self.text, font_size=self.font_size,
            font_name=self.font_name, color=self.color,
            bold=self.bold,
            size_hint=(None, None),
            halign='left', valign='middle',
            shorten=False,
        )
        self.add_widget(self._label)
        for prop in ('text', 'font_size', 'font_name', 'color', 'bold'):
            self.bind(**{prop: lambda inst, val, p=prop:
                         setattr(self._label, p, val)})
        self._label.bind(texture_size=self._on_texture_size)
        self.bind(size=self._on_layout)
        self._anim = None
        self._pending = None  # Clock.schedule_once handle for edge pauses

    def _on_texture_size(self, *args):
        tw, th = self._label.texture_size
        self._label.width = tw
        self._label.height = max(th, self.height)
        self._update_animation()

    def _on_layout(self, *args):
        self._label.text_size = (None, self.height)
        if self._label.texture_size[1] < self.height:
            self._label.height = self.height
        self._update_animation()

    def _update_animation(self):
        from kivy.animation import Animation
        self._cancel_all()
        self.scroll_x = 0
        tw = self._label.texture_size[0]
        cw = self.width
        if tw <= cw or cw <= 0:
            return
        # Constant pixel-per-second velocity, independent of
        # overflow length:
        #   scroll_x animates 0→1 over scroll_time seconds.
        #   pixel displacement of label inside viewport
        #     = scroll_x * (tw - cw).
        #   d/dt = (tw - cw) / scroll_time
        #        = (tw - cw) * SCROLL_SPEED / (tw - cw)
        #        = SCROLL_SPEED  ← constant
        # So a 300 px overflow takes 3× as long to finish as a
        # 100 px overflow, but both move at exactly SCROLL_SPEED
        # px/sec while moving. Longer glosses cycle less often;
        # they don't move faster.
        distance = tw - cw
        scroll_time = distance / float(self.SCROLL_SPEED)

        def _begin_scroll(_dt):
            # START_PAUSE elapsed — kick off the scroll.
            self._pending = None
            self.scroll_x = 0
            anim = Animation(scroll_x=1.0, duration=scroll_time,
                             t='linear')
            anim.bind(on_complete=_at_end)
            anim.start(self)
            self._anim = anim

        def _at_end(_anim, _widget):
            # Scroll finished — hold at the right edge for
            # END_PAUSE, then jump back to start and re-arm.
            self._anim = None
            self._pending = Clock.schedule_once(
                _loop_back, self.END_PAUSE)

        def _loop_back(_dt):
            # Instant snap to left, then wait START_PAUSE before
            # scrolling again.
            self._pending = None
            self.scroll_x = 0
            self._pending = Clock.schedule_once(
                _begin_scroll, self.START_PAUSE)

        # Kick off the very first cycle's start pause. Both edge
        # waits use Clock.schedule_once rather than a no-property
        # Animation(duration=N) — in some Kivy versions the latter
        # completes instantly because it has nothing to interpolate,
        # which is what made the edge pauses look broken in 1.55.7.
        self._pending = Clock.schedule_once(
            _begin_scroll, self.START_PAUSE)

    def _cancel_all(self):
        if self._anim is not None:
            self._anim.cancel(self)
            self._anim = None
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def on_parent(self, instance, parent):
        if parent is None:
            self._cancel_all()

    def on_touch_down(self, touch):
        return False

    def on_touch_move(self, touch):
        return False

    def on_touch_up(self, touch):
        return False


class GlossRow(BoxLayout):
    lang = StringProperty('')
    gloss = StringProperty('')

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # gloss_holder is the KV-defined placeholder BoxLayout; we
        # fill it on the next frame so self.ids is populated.
        Clock.schedule_once(self._populate_gloss, 0)

    def _populate_gloss(self, _dt):
        holder = self.ids.get('gloss_holder')
        if holder is None:
            return
        if GLOSS_USE_MARQUEE:
            inner = MarqueeLabel(
                text=self.gloss,
                font_size=sp(30),
                font_name=_FONT_NAME,
                bold=True,
                color=theme.TEXT,
            )
            self.bind(gloss=lambda _i, v: setattr(inner, 'text', v))
        else:
            inner = Label(
                text=self.gloss,
                font_size=sp(30),
                font_name=_FONT_NAME,
                bold=True,
                color=theme.TEXT,
                halign='left',
                valign='middle',
                size_hint_y=None,
                pos_hint={'center_y': 0.5},
            )
            inner.bind(width=lambda *_:
                       setattr(inner, 'text_size',
                               (inner.width, None)))
            inner.bind(texture_size=lambda *_:
                       setattr(inner, 'height',
                               inner.texture_size[1]))
            inner.bind(height=lambda *_:
                       setattr(self, 'height',
                               max(dp(70), inner.height + dp(16))))
            self.bind(gloss=lambda _i, v: setattr(inner, 'text', v))
        holder.add_widget(inner)


class LangToggle(BoxLayout):
    _LANG_NAMES = {
        'en': 'English', 'es': 'Español', 'pt': 'Português',
        'fr': 'Français', 'de': 'Deutsch', 'id': 'Indonesia',
        'sw': 'Kiswahili', 'ar': 'العربية', 'zh': '中文',
        'ru': 'Русский', 'hi': 'हिन्दी','ha': 'Hausa',
        'id': 'Bahasa Indonesia', 'swh': 'Swahili',
        'ln-CD': 'Lingala (RDC)'
    }
    lang = StringProperty('')
    display_name = StringProperty('')
    active = BooleanProperty(True)
    callback = ObjectProperty(None)

    def on_lang(self, *args):
        self.display_name = self._LANG_NAMES.get(self.lang, self.lang)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self.active = not self.active
            if self.callback:
                self.callback(self.lang, self.active)
            return True
        return super().on_touch_down(touch)


# ── Screens ────────────────────────────────────────────────────────────────────

class RootScreen(Screen):
    _inset_top = NumericProperty(0)
    _inset_bottom = NumericProperty(0)

    def on_kv_post(self, *args):
        if platform == 'android':
            self._read_insets_android()
        elif platform == 'ios':
            self._read_insets_ios()

    def _read_insets_android(self):
        try:
            from jnius import autoclass
            # Edge-to-edge is only enforced on API 35+ (Android 15);
            # older versions already reserve space for system bars.
            Build = autoclass('android.os.Build$VERSION')
            if Build.SDK_INT < 35:
                print(f'[insets] SDK {Build.SDK_INT} < 35, skipping')
                return
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            window = activity.getWindow()
            decor = window.getDecorView()
            insets = decor.getRootWindowInsets()
            if insets is None:
                return
            # API 30+: getInsets(Type.systemBars())
            # Values are in physical pixels; Kivy sizes are also in pixels
            # on Android, so use raw values (no density conversion).
            try:
                WindowInsets = autoclass('android.view.WindowInsets$Type')
                sys_insets = insets.getInsets(WindowInsets.systemBars())
                self._inset_top = sys_insets.top
                self._inset_bottom = sys_insets.bottom
            except Exception:
                # Pre-API 30 fallback
                self._inset_top = insets.getStableInsetTop()
                self._inset_bottom = insets.getStableInsetBottom()
            print(f'[insets] top={self._inset_top:.0f}  bottom={self._inset_bottom:.0f}')
        except Exception as ex:
            print(f'[insets] could not read: {ex}')

    def _read_insets_ios(self):
        try:
            from pyobjus import autoclass as objc_class
            UIApplication = objc_class('UIApplication')
            app = UIApplication.sharedApplication()
            window = app.keyWindow
            if window is None:
                return
            insets = window.safeAreaInsets
            # UIKit points; Kivy on iOS uses points, so use directly
            self._inset_top = insets.top
            self._inset_bottom = insets.bottom
            print(f'[insets] top={self._inset_top:.0f}  bottom={self._inset_bottom:.0f}')
        except Exception as ex:
            print(f'[insets] could not read: {ex}')


class ImagePickerScreen(Screen):
    """Image selection screen — shows all available images for the current entry."""
    _entry = None
    _shown_urls = None  # set of URLs already in the grid
    _openclipart_page = 1
    _freesvg_page = 1
    _wikimedia_continue = ''  # continuation token for paging

    def populate(self, entry):
        self._entry = entry
        # Prompt with selected glosses
        app = App.get_running_app()
        self._glosses = []
        if entry and app.recorder:
            for lang in app.recorder.active_gloss_langs:
                for g in entry.get('glosses', {}).get(lang, []):
                    self._glosses.append(g)
        prompt = f"Which image for {', '.join(self._glosses)}?" if self._glosses else 'Select an image'
        lbl = self.ids.get('prompt_label')
        if lbl:
            lbl.text = prompt

        grid = self.ids.get('image_grid')
        if not grid:
            return
        grid.clear_widgets()
        self._shown_urls = set()
        self._openclipart_page = 1
        self._freesvg_page = 1
        self._wikimedia_continue = ''

        # Gather CAWL images for this entry — local paths pulled
        # through from the daemon via CAWLHandle (Stage 2). Other
        # buttons (web-source fetches, user-picked files) added later
        # carry remote URLs; _add_image_buttons / _download_and_set
        # handle both shapes by prefix.
        db = app.recorder.db if app.recorder else None
        sources = db.all_image_paths(entry) if db else []

        # Each image = ~1/4 of screen in a 2x2 grid
        # Use dp-based size: half screen height minus chrome
        screen_h = Window.height
        self._cell_h = max(int(screen_h / 2.5), dp(200))

        # Use 1 column if ≤2 images, else 2
        grid.cols = 1 if len(sources) <= 2 else 2

        self._add_image_buttons(grid, sources, self._cell_h)

        # Auto-fetch from web sources if internet available and few images
        if len(sources) < 10 and self._glosses:
            import threading
            threading.Thread(
                target=self._auto_web_images, args=(self._cell_h,), daemon=True).start()

    def _add_image_buttons(self, grid, urls, cell_h):
        from kivy.uix.image import AsyncImage
        from kivy.uix.behaviors import ButtonBehavior

        class _ImageBtn(ButtonBehavior, AsyncImage):
            pass

        if self._shown_urls is None:
            self._shown_urls = set()
        for url in urls:
            if url in self._shown_urls:
                continue
            self._shown_urls.add(url)
            btn = _ImageBtn(
                source=url,
                allow_stretch=True,
                keep_ratio=True,
                size_hint_y=None,
                height=cell_h,
            )
            btn._image_url = url
            btn.bind(on_release=lambda b: self._select_url(b._image_url))
            grid.add_widget(btn)

    def _select_url(self, url):
        """Download the selected image, scale it, save to images/, update LIFT."""
        app = App.get_running_app()
        if not app.recorder or not self._entry:
            return
        import threading
        threading.Thread(
            target=self._download_and_set, args=(url,), daemon=True).start()
        app.go_recorder()

    def _download_and_set(self, source):
        """Background: acquire image bytes, scale, save, update LIFT XML.

        *source* may be a local path (CAWL pull-through tmpfile from
        the daemon, Stage 2; or any other local file) or an HTTP URL
        (web-source fetches — openclipart / freesvg / wikimedia).
        Local paths are opened directly; URLs go through urlopen.
        Image writes route through app._save_image_for_entry which
        handles both URI projects (MediaHandle through the daemon's
        provider, per the 0.35.2 contract) and desktop (direct
        filesystem write to db.images_dir)."""
        app = App.get_running_app()
        entry = self._entry
        try:
            from PIL import Image as PILImage
            if source.startswith('http://') or source.startswith('https://'):
                import io
                import urllib.request
                ctx = app._ssl_context()
                req = urllib.request.Request(source)
                with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                    data = resp.read()
                img = PILImage.open(io.BytesIO(data))
            else:
                img = PILImage.open(source)
            max_dim = 1284
            w, h = img.size
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
            dest = app._save_image_for_entry(img, entry)
            if not dest:
                return
            Clock.schedule_once(lambda dt: app.refresh_recorder_ui(), 0)
        except Exception as ex:
            print(f'[image-picker] load error: {ex}')

    def pick_from_file(self):
        """Let user pick an image from device storage or camera."""
        app = App.get_running_app()
        if platform == 'android':
            self._pick_from_file_android()
        elif platform == 'ios':
            self._pick_from_file_ios()
        else:
            self._pick_from_file_desktop()

    def _pick_from_file_desktop(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root_tk = tk.Tk()
            root_tk.withdraw()
            path = filedialog.askopenfilename(
                title=_tr('Select image'),
                filetypes=[('Images', '*.png *.jpg *.jpeg *.bmp *.gif'),
                           ('All files', '*.*')],
            )
            root_tk.destroy()
            if path:
                self._set_local_image(path)
        except Exception as ex:
            print(f'Desktop image picker error: {ex}')

    def _pick_from_file_android(self):
        try:
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.setType('image/*')
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            chooser = Intent.createChooser(intent, autoclass('java.lang.String')('Select image'))
            PythonActivity.mActivity.startActivityForResult(chooser, 1002)
        except Exception as ex:
            print(f'Android image picker error: {ex}')

    def _pick_from_file_ios(self):
        """Use UIImagePickerController to pick an image on iOS."""
        self._ios_launch_image_picker(camera=False)

    def _ios_launch_image_picker(self, camera=False):
        try:
            from pyobjus import autoclass as objc_class
            from pyobjus.dylib_manager import load_framework
            load_framework('/System/Library/Frameworks/UIKit.framework')
            UIImagePickerController = objc_class('UIImagePickerController')
            picker = UIImagePickerController.alloc().init()
            # 0 = photo library, 1 = camera
            picker.sourceType = 1 if camera else 0
            UIApplication = objc_class('UIApplication')
            root_vc = UIApplication.sharedApplication().keyWindow.rootViewController
            root_vc.presentViewController_animated_completion_(picker, True, None)
            self._ios_image_picker = picker
            Clock.schedule_interval(self._poll_ios_image_picker, 0.3)
        except Exception as ex:
            print(f'iOS image picker error: {ex}')

    def _poll_ios_image_picker(self, dt):
        """Check if the iOS image picker has been dismissed with a result."""
        try:
            picker = self._ios_image_picker
            if picker.presentingViewController is not None:
                return  # still presented
            Clock.unschedule(self._poll_ios_image_picker)
            # Extract the picked image info
            info = getattr(picker, '_picked_info', None)
            if info:
                from pyobjus import autoclass as objc_class
                url = info.objectForKey_('UIImagePickerControllerImageURL')
                if url:
                    path = url.path.UTF8String()
                    if path:
                        self._set_local_image(path)
            self._ios_image_picker = None
        except Exception as ex:
            print(f'iOS image picker poll error: {ex}')
            Clock.unschedule(self._poll_ios_image_picker)
            self._ios_image_picker = None

    def _set_local_image(self, source_path):
        """Copy and scale a local image file into images/."""
        app = App.get_running_app()
        if not app.recorder or not self._entry:
            return
        import threading
        threading.Thread(
            target=self._copy_and_set, args=(source_path,), daemon=True).start()
        Clock.schedule_once(lambda dt: app.go_recorder(), 0)

    def _copy_and_set(self, source_path):
        """Background: read a local-file image, scale, route through
        app._save_image_for_entry (URI projects go through MediaHandle,
        desktop goes through filesystem)."""
        app = App.get_running_app()
        entry = self._entry
        try:
            from PIL import Image as PILImage
            img = PILImage.open(source_path)
            max_dim = 1284
            w, h = img.size
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
            dest = app._save_image_for_entry(img, entry)
            if not dest:
                return
            # Bust Kivy image cache so the new file is reloaded for
            # immediate display.
            from kivy.cache import Cache
            Cache.remove('kv.image', dest)
            Cache.remove('kv.texture', dest)
            def _update_ui(dt):
                app.refresh_recorder_ui()
                app._show_toast(_tr('Image updated'))
            Clock.schedule_once(_update_ui, 0)
        except Exception as ex:
            print(f'[image-picker] local image error: {ex}')
            traceback.print_exc()

    def fetch_openclipart(self):
        """Fetch images from openclipart.org based on glosses."""
        if not self._glosses:
            return
        import threading
        cell_h = int(Window.height * 0.4)
        threading.Thread(
            target=self._do_openclipart, args=(cell_h,), daemon=True).start()

    def _auto_web_images(self, cell_h):
        """Background auto-fetch from web sources — disabled for now.

        openclipart / FreeSVG / Wikimedia auto-fetch is off because the
        upstream APIs have been flaky / abusive lately (rate limits,
        empty responses, slow first-byte). The manual
        ``fetch_openclipart`` / ``fetch_freesvg`` / ``fetch_wikimedia``
        buttons in the image picker still work; restore the auto-fetch
        loop here when the upstream story stabilises."""
        return

    # ── openclipart ───────────────────────────────────────────────────────

    def _do_openclipart(self, cell_h):
        """Background: query openclipart.org for images matching glosses."""
        import urllib.request, urllib.parse, re
        app = App.get_running_app()
        query = ' '.join(self._glosses[:3])
        try:
            ctx = app._ssl_context()
            encoded_q = urllib.parse.quote(query)
            page = self._openclipart_page
            api_url = f'https://openclipart.org/search/?query={encoded_q}&page={page}'
            req = urllib.request.Request(api_url,
                headers={'User-Agent': f'{APP_USER_AGENT}/{__version__}'})
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                raw = resp.read()
            if not raw:
                print(f'[openclipart] empty response for "{query}"')
                return
            html = raw.decode('utf-8', errors='replace')
            urls = []
            for m in re.finditer(r'src=["\'](/image/800px/\d+)["\']', html):
                url = 'https://openclipart.org' + m.group(1)
                if url not in urls:
                    urls.append(url)
                if len(urls) >= 6:
                    break
            n = len(urls)
            print(f'[openclipart] found {n} images for "{query}" (page {page})')
            if urls:
                self._openclipart_page = page + 1
                Clock.schedule_once(
                    lambda dt: self._append_web_images(urls, cell_h, 'openclipart'), 0)
            else:
                Clock.schedule_once(
                    lambda dt: app._show_toast(_tr('No images found on openclipart')), 0)
        except Exception as ex:
            print(f'[openclipart] fetch error: {ex}')

    # ── FreeSVG ───────────────────────────────────────────────────────────

    def fetch_freesvg(self):
        """Fetch images from freesvg.org based on glosses."""
        if not self._glosses:
            return
        import threading
        cell_h = int(Window.height * 0.4)
        threading.Thread(
            target=self._do_freesvg, args=(cell_h,), daemon=True).start()

    def _do_freesvg(self, cell_h):
        """Background: query freesvg.org for public domain SVG images."""
        import urllib.request, urllib.parse, re
        app = App.get_running_app()
        query = ' '.join(self._glosses[:3])
        try:
            ctx = app._ssl_context()
            encoded_q = urllib.parse.quote(query)
            page = self._freesvg_page
            api_url = f'https://freesvg.org/search?q={encoded_q}&p={page}'
            req = urllib.request.Request(api_url,
                headers={'User-Agent': f'{APP_USER_AGENT}/{__version__}'})
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                raw = resp.read()
            if not raw:
                return
            html = raw.decode('utf-8', errors='replace')
            # Thumbnails at /storage/img/thumb/FILENAME.png
            urls = []
            for m in re.finditer(
                    r'src=["\'](/storage/img/thumb/[^"\']+)["\']', html):
                url = 'https://freesvg.org' + urllib.parse.quote(m.group(1))
                if url not in urls:
                    urls.append(url)
                if len(urls) >= 6:
                    break
            n = len(urls)
            print(f'[freesvg] found {n} images for "{query}" (page {page})')
            if urls:
                self._freesvg_page = page + 1
                Clock.schedule_once(
                    lambda dt: self._append_web_images(urls, cell_h, 'FreeSVG'), 0)
            else:
                Clock.schedule_once(
                    lambda dt: app._show_toast(_tr('No images found on FreeSVG')), 0)
        except Exception as ex:
            print(f'[freesvg] fetch error: {ex}')

    # ── Wikimedia Commons ─────────────────────────────────────────────────

    def fetch_wikimedia(self):
        """Fetch public domain images from Wikimedia Commons."""
        if not self._glosses:
            return
        import threading
        cell_h = int(Window.height * 0.4)
        threading.Thread(
            target=self._do_wikimedia, args=(cell_h,), daemon=True).start()

    def _do_wikimedia(self, cell_h):
        """Background: query Wikimedia Commons for public domain images."""
        import urllib.request, urllib.parse, json
        app = App.get_running_app()
        query = ' '.join(self._glosses[:3])
        try:
            ctx = app._ssl_context()
            encoded_q = urllib.parse.quote(query + ' drawing')
            params = (
                f'action=query&generator=search'
                f'&gsrsearch={encoded_q}'
                f'&gsrnamespace=6&gsrlimit=20'
                f'&prop=imageinfo&iiprop=url|extmetadata'
                f'&iiurlwidth=400&format=json'
            )
            if self._wikimedia_continue:
                params += f'&gsroffset={self._wikimedia_continue}'
            api_url = f'https://commons.wikimedia.org/w/api.php?{params}'
            req = urllib.request.Request(api_url,
                headers={'User-Agent': f'{APP_USER_AGENT}/{__version__}'})
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                data = json.loads(resp.read())
            pages = data.get('query', {}).get('pages', {})
            # Filter to public domain / CC0 only
            pd_keywords = ('public domain', 'cc0', 'pd-')
            urls = []
            for page in pages.values():
                ii = page.get('imageinfo', [{}])[0]
                meta = ii.get('extmetadata', {})
                lic = meta.get('LicenseShortName', {}).get('value', '')
                if not any(k in lic.lower() for k in pd_keywords):
                    continue
                thumb = ii.get('thumburl', '')
                if thumb:
                    urls.append(thumb)
                if len(urls) >= 6:
                    break
            # Save continuation for next page
            cont = data.get('continue', {}).get('gsroffset', '')
            self._wikimedia_continue = str(cont) if cont else ''
            n = len(urls)
            print(f'[wikimedia] found {n} PD images for "{query}"')
            if urls:
                Clock.schedule_once(
                    lambda dt: self._append_web_images(urls, cell_h, 'Wikimedia'), 0)
            else:
                Clock.schedule_once(
                    lambda dt: app._show_toast(_tr('No public domain images on Wikimedia')), 0)
        except Exception as ex:
            print(f'[wikimedia] fetch error: {ex}')

    # ── Shared append helper ──────────────────────────────────────────────

    def _append_web_images(self, urls, cell_h, source_name):
        """Append web-fetched images to the picker grid (main thread)."""
        grid = self.ids.get('image_grid')
        if not grid:
            return
        # Count how many are genuinely new before _add_image_buttons dedup
        new_urls = [u for u in urls if u not in (self._shown_urls or set())]
        total = len(grid.children) + len(new_urls)
        if total > 2:
            grid.cols = 2
        self._add_image_buttons(grid, urls, cell_h)
        if new_urls:
            app = App.get_running_app()
            app._show_toast(_tr('{count} images from {source}').format(count=len(new_urls), source=source_name))

    def take_photo(self):
        """Launch camera to take a photo."""
        if platform == 'android':
            self._take_photo_android()
        elif platform == 'ios':
            self._ios_launch_image_picker(camera=True)
        else:
            # Desktop: no camera — use file picker instead
            self.pick_from_file()

    def _take_photo_android(self):
        try:
            from android.permissions import request_permissions, Permission, check_permission
            if not check_permission(Permission.CAMERA):
                request_permissions(
                    [Permission.CAMERA],
                    callback=lambda perms, grants: (
                        self._launch_camera() if grants and grants[0] else None
                    ),
                )
                return
            self._launch_camera()
        except Exception as ex:
            print(f'Camera permission error: {ex}')

    def _launch_camera(self):
        try:
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            MediaStore = autoclass('android.provider.MediaStore')
            intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
            PythonActivity.mActivity.startActivityForResult(intent, 1003)
        except Exception as ex:
            print(f'Camera launch error: {ex}')


class RecorderScreen(Screen):
    _touch_start_x = None
    _swiping = False
    _dragging = False          # True once finger moved past dead-zone
    _swipe_touch = None        # the grabbed Touch object

    # ── helpers ───────────────────────────────────────────────────────────
    def _in_swipe_zone(self, touch):
        """Return True if the touch lands on the image or gloss area."""
        for wid_name in ('entry_image', 'gloss_box'):
            w = self.ids.get(wid_name)
            if w and w.collide_point(*touch.pos):
                return True
        return False

    # ── touch dispatch ────────────────────────────────────────────────────
    def on_touch_down(self, touch):
        if self._swiping:
            return super().on_touch_down(touch)
        if self._in_swipe_zone(touch):
            self._touch_start_x = touch.x
            self._dragging = False
            self._swipe_touch = touch
        else:
            self._touch_start_x = None
            self._swipe_touch = None
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        if (self._swiping
                or self._swipe_touch is not touch
                or self._touch_start_x is None):
            return super().on_touch_move(touch)

        dx = touch.x - self._touch_start_x
        dead_zone = self.width * 0.03          # ~10-15 px on most screens

        if not self._dragging:
            if abs(dx) < dead_zone:
                return super().on_touch_move(touch)
            # Past dead-zone → claim the touch so children stop receiving it
            self._dragging = True
            touch.grab(self)

        content = self.ids.get('content_box')
        if content:
            content.x = dx                     # follow the finger
        return True                            # consumed

    def on_touch_up(self, touch):
        # Ignore touches we didn't claim
        if self._swipe_touch is not touch:
            return super().on_touch_up(touch)

        if touch.grab_current is self:
            touch.ungrab(self)

        start_x = self._touch_start_x
        self._touch_start_x = None
        self._swipe_touch = None

        if self._swiping or not self._dragging:
            self._dragging = False
            return super().on_touch_up(touch)

        self._dragging = False

        content = self.ids.get('content_box')
        if not content:
            return super().on_touch_up(touch)

        dx = touch.x - start_x if start_x is not None else 0.0
        threshold = self.width * 0.2

        if dx > threshold:
            self._finish_swipe(content, 'prev')
            return True
        elif dx < -threshold:
            self._finish_swipe(content, 'next')
            return True
        else:
            # Snap back — abandoned swipe
            self._snap_back(content)
            return True

    # ── animation ─────────────────────────────────────────────────────────
    def _snap_back(self, content):
        """Rubber-band back to x=0."""
        from kivy.animation import Animation
        Animation.cancel_all(content, 'x')
        anim = Animation(x=0, duration=0.15, t='out_cubic')
        anim.start(content)

    def _finish_swipe(self, content, direction):
        """Slide the rest of the way off-screen, navigate, slide new content in."""
        from kivy.animation import Animation
        Animation.cancel_all(content, 'x')
        self._swiping = True
        slide_out = self.width * (1 if direction == 'prev' else -1)

        def _on_slide_out(anim, widget):
            app = App.get_running_app()
            if direction == 'prev':
                app.nav_prev()
            else:
                app.nav_next()
            # Jump to opposite side, slide back in
            content.x = -slide_out
            anim_in = Animation(x=0, duration=0.15, t='out_cubic')
            anim_in.bind(on_complete=lambda *a: setattr(self, '_swiping', False))
            anim_in.start(content)

        anim_out = Animation(x=slide_out, duration=0.15, t='in_cubic')
        anim_out.bind(on_complete=_on_slide_out)
        anim_out.start(content)


def _render_slot_picker_into(available_row, claimed_column,
                              team_size, my_slot, slots,
                              on_pick, state=None):
    """Shared slot-picker renderer used by both the modal
    popup (``_show_slot_picker``) and the inline filter-modal
    slot row (``_rebuild_slot_row``). They ask the same
    question — "which device is this?" — and now run through
    the same code so a fix to one is a fix to both.

    *available_row* — horizontal ``BoxLayout``. Receives one
    button per slot in ``[1..team_size]`` that's free OR
    claimed by this device (``my_slot``). The matching button
    for ``my_slot`` is highlighted with ``theme.ACCENT``.

    *claimed_column* — vertical ``BoxLayout``. Receives one
    button per slot claimed by ANOTHER device, labelled with
    that device's name.

    *team_size* — number of slots in range.

    *my_slot* — this device's claim. Callers that don't have
    a direct ``my_slot`` (the modal popup, when firing
    because the recorder's ``split_my_slot`` is empty) should
    derive it from ``slots`` + this device's ``peer_id``
    before calling; otherwise the device's own claim would
    land in the claimed-by-other column with its own name.

    *slots* — full ``list_slots`` dict.

    *on_pick(slot_str)* — called when any button is tapped.

    *state* (dict, optional) — when supplied, enables
    in-place colour updates when the structure key
    (``team_size`` + set of available slots + set of
    claimed-by-other slots) hasn't changed since the last
    call. The dict is mutated to carry forward a
    ``button_map`` keyed by slot string. Pass ``None`` for
    one-shot use (the modal popup, where each open builds
    fresh widgets anyway)."""
    from kivy.uix.button import Button

    # Partition slots in [1..team_size]. Anything outside the
    # range is ignored — the release-stale-slot worker is
    # already untangling those daemon-side; showing them in
    # the picker would confuse the user.
    other_claims = {}
    for s, c in (slots or {}).items():
        k_str = str(s)
        try:
            slot_int = int(k_str)
        except (TypeError, ValueError):
            continue
        if slot_int < 1 or slot_int > team_size:
            continue
        if k_str == my_slot:
            continue  # our own claim → top row, not claimed list
        other_claims[k_str] = c or {}

    available_slots = [
        str(k) for k in range(1, team_size + 1)
        if str(k) not in other_claims]
    claimed_slots = sorted(other_claims.keys(),
                           key=lambda s: int(s) if s.isdigit() else 0)

    top_buttons = []      # (k_str, bg)
    for k_str in available_slots:
        bg = theme.ACCENT if k_str == my_slot else theme.SURFACE
        top_buttons.append((k_str, bg))

    claimed_buttons = []  # (k_str, device, bg)
    for k_str in claimed_slots:
        device = (other_claims[k_str].get('device_name', '')
                  or _tr('Unknown'))
        claimed_buttons.append((k_str, device, theme.BTN_INACTIVE))

    new_top_set = {k for k, _ in top_buttons}
    new_claimed_pairs = {(k, d) for k, d, _ in claimed_buttons}

    # In-place colour update when structure is unchanged.
    if state is not None:
        prev_team_size = state.get('team_size', 0)
        prev_top_set = state.get('top_set', set())
        prev_claimed_pairs = state.get('claimed_pairs', set())
        button_map = state.get('button_map') or {}
        structure_unchanged = (
            prev_team_size == team_size
            and prev_top_set == new_top_set
            and prev_claimed_pairs == new_claimed_pairs
            and bool(button_map))
        if structure_unchanged:
            for k_str, bg in top_buttons:
                btn = button_map.get(k_str)
                if btn is not None:
                    btn.background_color = bg
            for k_str, _device, bg in claimed_buttons:
                btn = button_map.get(k_str)
                if btn is not None:
                    btn.background_color = bg
            return

    # Full rebuild.
    button_map = {}
    available_row.clear_widgets()
    available_row.height = dp(56) if top_buttons else 0
    for k_str, bg in top_buttons:
        btn = Button(
            text=f'{k_str}/{team_size}',
            font_size=sp(13), font_name=_FONT_NAME,
            background_normal='', background_color=bg,
            color=theme.TEXT)
        btn.bind(on_release=lambda b, s=k_str: on_pick(s))
        available_row.add_widget(btn)
        button_map[k_str] = btn

    claimed_column.clear_widgets()
    n_claimed = len(claimed_buttons)
    claimed_column.height = (
        n_claimed * dp(48) + max(0, n_claimed - 1) * dp(4)
    ) if n_claimed else 0
    for k_str, device, bg in claimed_buttons:
        btn = Button(
            text=f'{k_str}/{team_size} ({device})',
            font_size=sp(13), font_name=_FONT_NAME,
            background_normal='', background_color=bg,
            color=theme.TEXT,
            size_hint_y=None, height=dp(48))
        btn.bind(on_release=lambda b, s=k_str: on_pick(s))
        claimed_column.add_widget(btn)
        button_map[k_str] = btn

    if state is not None:
        state['team_size'] = team_size
        state['top_set'] = new_top_set
        state['claimed_pairs'] = new_claimed_pairs
        state['button_map'] = button_map


class ConfigScreen(Screen):
    only_unrecorded = BooleanProperty(False)

    def on_kv_post(self, base_widget):
        """Collapse the inline filter_panel right after KV
        applies so its children's natural heights don't bleed
        touches into the Filter words button above. The inline
        panel is now dead weight — the filter UI is a
        gloss-style fresh-build ModalView in _show_filter_overlay
        — but the widget tree retains it (with all children at
        height=0) so self.ids references stay valid for any
        legacy code that still resolves them."""
        panel = self.ids.get('filter_panel')
        if panel is not None and panel.parent is not None:
            self._collapse_filter_panel()

    def on_enter(self):
        app = App.get_running_app()
        # Start each Settings entry in summary mode for the
        # team-size row — if the user previously tapped
        # [change] then navigated away without picking, we
        # shouldn't keep the picker buttons open on re-entry.
        self._team_size_editing = False
        self._build_lang_selector()
        # Conserve-power selectors are peer-pref-backed (suite-wide),
        # so they populate regardless of project state — they used to
        # live inside db_settings_box and only filled in when a
        # project was loaded; that was a layout accident, not a real
        # gate.
        self._build_audio_quality_row()
        has_db = app.recorder is not None
        # Show/hide database-dependent sections.
        # When hiding: zero height on every descendant so nothing has a
        # hit area (disabled=True alone doesn't block touches in Kivy).
        box = self.ids.get('db_settings_box')
        if box:
            if has_db:
                # If we previously hid the box (no-project
                # state), restore every descendant's saved
                # height + opacity. Without this they stay at
                # 0 forever and the gloss + filter toggle
                # buttons render invisibly.
                self._show_box_tree(box)
                box.opacity = 1
                Clock.schedule_once(
                    lambda dt, b=box: setattr(b, 'height', b.minimum_height), 0)
            else:
                self._hide_box_tree(box)
        if not app.recorder:
            return
        cawl_in = self.ids.get('cawl_input')
        if cawl_in:
            cawl_in.text = app.recorder.cawl_filter or ''
        gs = self.ids.get('gloss_search_input')
        if gs:
            gs.text = app.recorder.gloss_search or ''
        # Restore show-past-work pref (default False). The
        # Settings-side toggle moved into the Go-To popup in
        # 1.52.x; this just keeps the recorder's
        # only_unrecorded flag aligned with the persisted pref
        # at init time.
        show_past = bool(peer_pref('show_past_work', False))
        self.only_unrecorded = not show_past
        # Filter summary label + gloss summary label reflect
        # current state; the actual filter widgets live in the
        # modal that `_open_filter_modal` builds on demand.
        self._update_filter_summary()
        self._update_gloss_summary()
        self._filter_open = False
        # Build recording options
        self._build_rec_options(app)

    @staticmethod
    def _hide_box_tree(widget):
        """Zero the height/opacity of *widget* and all
        descendants so nothing has a touch hit-area when the
        section is hidden. Saves the prior heights on
        ``widget._saved_heights`` so ``_show_box_tree`` can put
        them back when the section returns — without this the
        gloss + filter toggle buttons (and every other
        descendant) stay at height=0 forever once the no-project
        path fires, even after a project loads."""
        saved = []

        def _zero(w):
            if hasattr(w, 'height'):
                saved.append((w, w.height, w.opacity))
                w.height = 0
                w.opacity = 0
            if hasattr(w, 'children') and w.children:
                for child in w.children:
                    _zero(child)
        _zero(widget)
        widget._saved_heights = saved

    @staticmethod
    def _show_box_tree(widget):
        """Restore heights + opacities saved by
        ``_hide_box_tree``. No-op when the widget wasn't
        previously hidden (no saved state)."""
        saved = getattr(widget, '_saved_heights', None)
        if not saved:
            widget.opacity = 1
            return
        for w, h, op in saved:
            w.height = h
            w.opacity = op
        widget._saved_heights = None

    def _build_lang_selector(self):
        """Populate the UI language selector row with one button per language."""
        from kivy.uix.button import Button
        row = self.ids.get('lang_selector_row')
        if row is None:
            return
        row.clear_widgets()
        cur = current_language()
        for code, name in available_languages():
            btn = Button(
                text=name,
                font_size=sp(14),
                font_name=_FONT_NAME,
                size_hint_x=1,
                background_color=theme.ACCENT if code == cur else theme.SURFACE,
                color=theme.TEXT,
            )
            btn.bind(on_release=lambda b, c=code: self._set_ui_language(c))
            row.add_widget(btn)

    def _set_ui_language(self, lang_code):
        """Change the UI language and rebuild all screens. Persistence
        lives in azt_collab_client.i18n ($AZT_HOME/config.json), so a
        change here is visible to the picker / settings subprocess on
        their next mtime poll."""
        if lang_code == current_language():
            return
        app = App.get_running_app()
        set_language(lang_code)
        app.subtitle = _tr(APP_TAGLINE)
        # Rebuild all screens so translated strings take effect
        sm = app.root.ids.sm
        old_transition = sm.transition
        sm.transition = NoTransition()
        screens_info = [(s.name, type(s)) for s in list(sm.screens)]
        sm.clear_widgets()
        for name, cls in screens_info:
            sm.add_widget(cls(name=name))
        sm.current = 'config'
        # Re-link config_screen reference
        app.config_screen = sm.get_screen('config')
        # Restore normal transition after rebuild
        Clock.schedule_once(lambda dt: setattr(sm, 'transition', old_transition), 0.1)

    def _toggle_lang(self, lang, active):
        app = App.get_running_app()
        langs = set(app.recorder.active_gloss_langs)
        if active:
            langs.add(lang)
        else:
            langs.discard(lang)
        chosen = sorted(langs)
        app.recorder.active_gloss_langs = chosen
        # Persist the selection across boots/installs. RecorderController
        # filters this list against `all_gloss_langs` on next load so a
        # project that doesn't have the saved langs falls through to
        # the top-3 default — see RecorderController.__init__.
        set_peer_pref('gloss_langs', chosen)
        self._update_gloss_summary()

    def _update_gloss_summary(self):
        """Show selected gloss langs next to the button."""
        lbl = self.ids.get('gloss_summary_label')
        if not lbl:
            return
        app = App.get_running_app()
        if not app.recorder:
            lbl.text = ''
            return
        langs = list(app.recorder.active_gloss_langs)
        names = [_lang_display_name(c) for c in langs]
        lbl.text = ', '.join(names) if names else _tr('(none selected)')

    _filter_open = False

    def _show_gloss_overlay(self):
        """Show a modal overlay with one toggle per available gloss language."""
        print('[gloss] _show_gloss_overlay')
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        from kivy.uix.gridlayout import GridLayout
        from kivy.uix.modalview import ModalView
        from kivy.uix.scrollview import ScrollView

        app = App.get_running_app()
        if not app.recorder:
            return
        langs = list(app.recorder.all_gloss_langs)
        if not langs:
            return

        cols = 3
        rows = (len(langs) + cols - 1) // cols
        grid_h = rows * dp(44) + max(0, rows - 1) * dp(6)

        view = ModalView(
            size_hint=(0.9, None),
            height=min(grid_h + dp(96), Window.height * 0.85),
            background_color=theme.OVERLAY_DARK,
            auto_dismiss=True,
        )
        outer = BoxLayout(
            orientation='vertical', spacing=dp(8),
            padding=(dp(10), dp(10)),
        )
        scroll = ScrollView(size_hint=(1, 1))
        grid = GridLayout(
            cols=cols, spacing=dp(6),
            size_hint_y=None,
        )
        grid.bind(minimum_height=grid.setter('height'))
        for lang in langs:
            grid.add_widget(LangToggle(
                lang=lang,
                active=lang in app.recorder.active_gloss_langs,
                callback=self._toggle_lang,
            ))
        scroll.add_widget(grid)
        outer.add_widget(scroll)

        done_btn = Button(
            text=_tr('Done'),
            font_name=_FONT_NAME,
            font_size=sp(16),
            size_hint_y=None, height=dp(44),
            background_color=theme.ACCENT,
            color=theme.TEXT,
        )
        done_btn.bind(on_release=lambda inst: view.dismiss())
        outer.add_widget(done_btn)

        view.add_widget(outer)
        view.open()

    def _expand_filter_panel(self):
        panel = self.ids.get('filter_panel')
        if not panel:
            return
        # Restore child heights for the non-split widgets.
        # Split-section heights + panel.height are owned by
        # _refresh_split_rows below (three-state logic:
        # team_size=0 / editing / summary).
        for cid in ('cawl_label', 'gloss_search_label'):
            w = self.ids.get(cid)
            if w:
                w.height = dp(36)
        for cid in ('cawl_input', 'gloss_search_input'):
            w = self.ids.get(cid)
            if w:
                w.height = dp(48)
                w.disabled = False
        team_size_row = self.ids.get('team_size_row')
        if team_size_row:
            team_size_row.height = dp(48)
        bottom_row = self.ids.get('filter_bottom_row')
        if bottom_row:
            bottom_row.height = dp(56)
            bottom_row.opacity = 1
        ok_btn = self.ids.get('filter_ok_btn')
        if ok_btn:
            ok_btn.height = dp(56)
            ok_btn.disabled = False
        panel.opacity = 1
        self._filter_open = True
        # Build the team-size + slot button rows now that we know
        # team_size; without this, an expand-while-already-loaded
        # would leave the rows empty.
        self._refresh_split_rows()
        btn = self.ids.get('filter_toggle_btn')
        if btn:
            btn.normal_color = theme.BTN_INACTIVE

    def _collapse_filter_panel(self):
        panel = self.ids.get('filter_panel')
        if not panel:
            return
        # Zero out every child of filter_panel — when panel.height
        # is 0 but children retain their natural heights, Kivy's
        # vertical BoxLayout stacks them outside the panel's nominal
        # bounds. The "CAWL number…" / "Gloss search…" Labels
        # (height: dp(36)) were ending up inside the filter button
        # row's y-range and blocking its on_release. Zero everything,
        # including the Labels — and the dp(56)-tall OK button
        # inside filter_bottom_row, whose `<RecBtn@Button>:` root
        # rule pins `size_hint_y: None` + an explicit height so it
        # doesn't shrink with its (zeroed) parent row.
        for cid in ('cawl_label', 'gloss_search_label',
                    'split_devices_label', 'which_device_label'):
            w = self.ids.get(cid)
            if w:
                w.height = 0
        for cid in ('cawl_input', 'gloss_search_input'):
            w = self.ids.get(cid)
            if w:
                w.height = 0
                w.disabled = True
                w.focus = False
        for cid in ('team_size_row', 'slot_row'):
            w = self.ids.get(cid)
            if w:
                w.height = 0
        # slot_row got nested children in 1.51.2 (vertical
        # container of a horizontal top sub-row + per-claim
        # buttons). Zeroing slot_row.height alone leaves the
        # nested children with their natural heights, and Kivy's
        # BoxLayout-with-height-0 doesn't auto-hide them — they
        # render at indeterminate positions and swallow taps on
        # the Filter words / Gloss languages buttons above (same
        # failure mode the cawl_label / gloss_search_label
        # zeroing above guards against, just one level deeper).
        # Drop the nested widgets here; the next expand will
        # re-trigger _rebuild_slot_row via _refresh_split_rows.
        slot_row = self.ids.get('slot_row')
        if slot_row:
            slot_row.clear_widgets()
        bottom_row = self.ids.get('filter_bottom_row')
        if bottom_row:
            bottom_row.height = 0
            bottom_row.opacity = 0
        ok_btn = self.ids.get('filter_ok_btn')
        if ok_btn:
            ok_btn.height = 0
            ok_btn.disabled = True
        panel.height = 0
        panel.opacity = 0
        self._filter_open = False
        btn = self.ids.get('filter_toggle_btn')
        if btn:
            btn.normal_color = theme.ACCENT

    def toggle_filter_panel(self):
        """Filter words button + OK button inside the modal both
        bind here. First tap opens a gloss-style modal with
        fresh widgets; second tap (OK) dismisses, applies
        edits, and refreshes the recorder UI."""
        if getattr(self, '_filter_modal', None) is not None:
            self._filter_modal.dismiss()
            return
        self._show_filter_overlay()

    def _show_filter_overlay(self):
        """Build a gloss-style fresh-widget ModalView with the
        CAWL filter input, the team_size + slot rows, and the
        gloss-search input. Mirrors _show_gloss_overlay's
        pattern — no re-parenting of inline widgets, no
        self.ids lookups for the controls.

        Widget refs are stashed on self._filter_modal_widgets
        so _rebuild_team_size_row and _rebuild_slot_row can
        update the rows in place when daemon state changes
        while the modal is open."""
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        from kivy.uix.label import Label
        from kivy.uix.modalview import ModalView
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.textinput import TextInput

        app = App.get_running_app()
        if not app.recorder:
            return

        view = ModalView(
            size_hint=(0.95, None),
            height=min(Window.height * 0.92, dp(560)),
            background_color=theme.OVERLAY_DARK,
            auto_dismiss=True,
        )
        outer = BoxLayout(
            orientation='vertical', spacing=dp(8),
            padding=(dp(10), dp(10)),
        )
        scroll = ScrollView(size_hint=(1, 1))
        inner = BoxLayout(
            orientation='vertical', spacing=dp(8),
            size_hint_y=None,
        )
        inner.bind(minimum_height=inner.setter('height'))

        def _label(text, height=dp(36)):
            lbl = Label(
                text=text, font_size=sp(13), font_name=_FONT_NAME,
                color=theme.TEXT_DIM,
                size_hint_y=None, height=height,
                halign='left', valign='middle',
            )
            lbl.bind(
                size=lambda w, s: setattr(w, 'text_size', (s[0], None)))
            return lbl

        # CAWL filter
        inner.add_widget(_label(_tr(
            'CAWL number or range (e.g. 1-100, 42, leave blank for all)')))
        cawl_in = TextInput(
            text=app.recorder.cawl_filter or '',
            hint_text=_tr('e.g. 1-500'),
            font_size=sp(16), font_name=_FONT_NAME,
            size_hint_y=None, height=dp(48),
            background_color=theme.SURFACE,
            foreground_color=theme.TEXT,
            cursor_color=theme.ACCENT,
            multiline=False, input_type='number',
        )
        inner.add_widget(cawl_in)

        # Split team
        inner.add_widget(_label(_tr('Split across devices')))
        ts_row = BoxLayout(
            orientation='horizontal', size_hint_y=None,
            height=dp(48), spacing=dp(6),
        )
        inner.add_widget(ts_row)
        inner.add_widget(_label(_tr('Which device is this?')))
        slot_row = BoxLayout(
            orientation='vertical', size_hint_y=None,
            height=dp(48), spacing=dp(4),
        )
        # Two sub-containers inside slot_row — a horizontal
        # row of available slots and a vertical column of
        # claimed-by-other slots. The shared renderer
        # _render_slot_picker_into fills these.
        slot_available = BoxLayout(
            orientation='horizontal', size_hint_y=None,
            height=dp(48), spacing=dp(6),
        )
        slot_claimed = BoxLayout(
            orientation='vertical', size_hint_y=None,
            height=0, spacing=dp(4),
        )
        slot_row.add_widget(slot_available)
        slot_row.add_widget(slot_claimed)
        inner.add_widget(slot_row)

        # Gloss search
        inner.add_widget(_label(_tr(
            'Gloss search (filter by gloss text)')))
        gs_in = TextInput(
            text=app.recorder.gloss_search or '',
            font_size=sp(16), font_name=_FONT_NAME,
            size_hint_y=None, height=dp(48),
            background_color=theme.SURFACE,
            foreground_color=theme.TEXT,
            cursor_color=theme.ACCENT,
            multiline=False,
        )
        inner.add_widget(gs_in)

        scroll.add_widget(inner)
        outer.add_widget(scroll)

        ok_btn = Button(
            text=_tr('OK'),
            font_size=sp(15), font_name=_FONT_NAME,
            size_hint_y=None, height=dp(56),
            background_color=theme.GREEN, color=theme.TEXT,
        )
        ok_btn.bind(on_release=lambda inst: view.dismiss())
        outer.add_widget(ok_btn)

        view.add_widget(outer)

        # Stash modal-widget refs so _rebuild_team_size_row /
        # _rebuild_slot_row target these (fresh, modal-owned)
        # widgets instead of the inline KV ones.
        self._filter_modal_widgets = {
            'cawl_input': cawl_in,
            'gloss_search_input': gs_in,
            'team_size_row': ts_row,
            'slot_row': slot_row,
            'slot_available': slot_available,
            'slot_claimed': slot_claimed,
        }
        self._filter_modal = view
        self._filter_open = True
        # Fresh modal = fresh widgets; clear the idempotence
        # caches so the next populate triggers a full rebuild.
        self._team_size_row_state = None
        # State dict for the shared _render_slot_picker_into
        # — carries the button_map + structure key across
        # rebuilds for in-place colour updates.
        self._slot_picker_state = {}

        def _on_dismiss(*_):
            # Apply CAWL + gloss-search edits.
            text = cawl_in.text.strip()
            prior = (app.recorder.cawl_filter or '').strip()
            app.recorder.cawl_filter = text
            set_peer_pref('cawl_filter', text or None)
            if text != prior:
                set_peer_pref(
                    'cawl_filter_source',
                    'manual' if text else None)
            app.recorder.gloss_search = gs_in.text.strip()
            app.recorder.rebuild_queue()
            self._update_filter_summary()
            app.refresh_recorder_ui()
            # Drop refs — the modal's widgets are about to be
            # GC'd; the rebuild methods should fall through to
            # no-op state until the next open.
            self._filter_modal_widgets = None
            self._filter_modal = None
            self._filter_open = False
            self._slot_picker_state = None
            btn = self.ids.get('filter_toggle_btn')
            if btn:
                btn.normal_color = theme.ACCENT
        view.bind(on_dismiss=_on_dismiss)

        btn = self.ids.get('filter_toggle_btn')
        if btn:
            btn.normal_color = theme.BTN_INACTIVE

        view.open()

        # Pull daemon state for the freshly-opened modal — this
        # populates team_size_row + slot_row via _refresh_split_rows
        # which now routes through self._filter_modal_widgets.
        app._populate_split_state()

    def _update_filter_summary(self):
        """Show a one-line summary of active filters next to the button."""
        lbl = self.ids.get('filter_summary_label')
        if not lbl:
            return
        app = App.get_running_app()
        if not app.recorder:
            lbl.text = ''
            return
        parts = []
        cawl = app.recorder.cawl_filter or ''
        if cawl.strip():
            parts.append(f'CAWL: {cawl.strip()}')
        gloss = app.recorder.gloss_search or ''
        if gloss.strip():
            parts.append(f'gloss: "{gloss.strip()}"')
        if app.recorder.only_unrecorded:
            parts.append(_tr('unrecorded only'))
        lbl.text = ', '.join(parts) if parts else ''

    def apply_cawl(self, text):
        cleaned = text.strip()
        App.get_running_app().recorder.cawl_filter = cleaned
        set_peer_pref('cawl_filter', cleaned or None)
        # § 21: user typed in the field — flip the filter source
        # off split mode so populate_split_state stops
        # overwriting it on subsequent syncs. Empty field clears
        # the source entirely.
        set_peer_pref('cawl_filter_source',
                      'manual' if cleaned else None)

    def _refresh_split_rows(self):
        """Refresh the modal's team-size + slot button rows from
        the recorder's current ``split_team_size`` /
        ``split_my_slot`` state. No-op when the filter modal
        isn't open — the inline panel is dead weight; the
        progress-text top-bar reads ``recorder.split_*``
        directly without needing widget refresh.

        Rebuilds are deferred one frame via
        ``Clock.schedule_once(0)`` so the current touch dispatch
        and layout pass settle before children change
        underneath the user's finger."""
        widgets = getattr(self, '_filter_modal_widgets', None)
        if not widgets:
            return
        app = App.get_running_app()
        if not (app and app.recorder):
            return
        team_size = int(getattr(app.recorder, 'split_team_size', 0) or 0)
        my_slot = str(getattr(app.recorder, 'split_my_slot', '') or '')

        # Sync cawl_input text when split-derived filter has
        # changed (e.g. team_size flipped → range recomputed).
        if peer_pref('cawl_filter_source', None) == 'split':
            cawl_in = widgets.get('cawl_input')
            if cawl_in is not None:
                new_text = app.recorder.cawl_filter or ''
                if cawl_in.text != new_text:
                    cawl_in.text = new_text
        self._update_filter_summary()

        # Defer the row rebuilds one frame so any in-flight
        # touch dispatch + the layout pass finish before
        # widgets change.
        Clock.schedule_once(
            lambda dt: self._rebuild_team_size_row(team_size), 0)
        Clock.schedule_once(
            lambda dt: self._rebuild_slot_row(team_size, my_slot), 0)

        # ── Deferred row rebuilds. Both team_size_row and
        # slot_row clear+rebuild happen one frame later so the
        # current touch dispatch returns and the layout pass
        # picks up the height changes above before children
        # are torn down.
        Clock.schedule_once(
            lambda dt: self._rebuild_team_size_row(team_size), 0)
        Clock.schedule_once(
            lambda dt: self._rebuild_slot_row(team_size, my_slot), 0)

    def _rebuild_team_size_row(self, team_size):
        """Rebuild team_size_row's children. Two modes:

        - team_size > 0 AND not editing → summary "Number of
          groups: X  [Change]"
        - team_size == 0 OR user tapped [Change] → picker
          [2][3][4][5+]

        Targets the modal's ts_row when the filter overlay is
        open (via self._filter_modal_widgets). No-op when the
        modal is closed — there's no inline UI to update; the
        progress-text top-bar reads from
        recorder.split_team_size directly.

        Idempotent on (team_size, editing) so repeated populate
        cycles triggered by ContentObserver wakeups after a
        daemon commit don't tear down and rebuild widgets when
        the displayed state hasn't changed."""
        from kivy.uix.button import Button
        from kivy.uix.label import Label
        widgets = getattr(self, '_filter_modal_widgets', None)
        if not widgets:
            return
        ts_row = widgets.get('team_size_row')
        if ts_row is None:
            return
        editing = getattr(self, '_team_size_editing', False)
        state_key = (team_size, editing)
        prev = getattr(self, '_team_size_row_state', None)
        if state_key == prev:
            return
        # Every transition is a flicker candidate. Logging the
        # (prev → new) tuple in logcat lets the user / dev see
        # what triggered the rebuild — transient team_size=0,
        # editing flip, modal first-open from None, etc.
        print(f'[split] ts_row rebuild: {prev} -> {state_key}',
              file=sys.stderr)
        self._team_size_row_state = state_key
        ts_row.clear_widgets()
        if team_size and not editing:
            # Summary mode.
            summary = Label(
                text=_tr('Split across {n} devices').format(n=team_size),
                font_size=sp(14), font_name=_FONT_NAME,
                color=theme.TEXT,
                halign='left', valign='middle',
                size_hint_x=3)
            summary.bind(
                size=lambda w, s: setattr(w, 'text_size', s))
            change_btn = Button(
                text=_tr('change'),
                font_size=sp(14), font_name=_FONT_NAME,
                background_normal='',
                background_color=theme.BTN_INACTIVE,
                color=theme.TEXT,
                size_hint_x=1)
            change_btn.bind(
                on_release=lambda *_:
                    self._enter_team_size_editing())
            ts_row.add_widget(summary)
            ts_row.add_widget(change_btn)
            return
        # Picker mode.
        for n in (2, 3, 4):
            btn = Button(
                text=str(n), font_size=sp(15),
                font_name=_FONT_NAME,
                background_normal='',
                background_color=(
                    theme.ACCENT if n == team_size else theme.SURFACE),
                color=theme.TEXT)
            btn.bind(on_release=lambda b, _n=n:
                     self._on_pick_team_size(_n))
            ts_row.add_widget(btn)
        plus_btn = Button(
            text='5+', font_size=sp(15),
            font_name=_FONT_NAME,
            background_normal='',
            background_color=(
                theme.ACCENT if team_size >= 5 else theme.SURFACE),
            color=theme.TEXT)
        plus_btn.bind(
            on_release=lambda *_: self._open_team_size_plus_dialog())
        ts_row.add_widget(plus_btn)
        # Cancel button — back out of picker mode without
        # touching team_size. Sits at the right end of the
        # [2][3][4][5+] row so a user who tapped [Change] by
        # accident has a one-tap retreat.
        cancel_btn = Button(
            text=_tr('Cancel'), font_size=sp(13),
            font_name=_FONT_NAME,
            background_normal='',
            background_color=theme.BTN_INACTIVE,
            color=theme.TEXT)
        cancel_btn.bind(
            on_release=lambda *_: self._cancel_team_size_editing())
        ts_row.add_widget(cancel_btn)

    def _enter_team_size_editing(self):
        """Flip team_size_row to picker mode on the next frame.
        Bound to the [Change] button in summary mode."""
        self._team_size_editing = True
        self._refresh_split_rows()

    # Heights used by _rebuild_slot_row + the panel-height adjuster.
    _SLOT_TOP_ROW_H = dp(48)
    _SLOT_CLAIMED_BTN_H = dp(40)
    _SLOT_CLAIMED_SPACING = dp(4)

    def _rebuild_slot_row(self, team_size, my_slot):
        """Refresh the inline filter-modal slot picker. Delegates
        to the shared ``_render_slot_picker_into`` renderer (also
        used by the modal popup ``_show_slot_picker``) — the two
        ask the same question and now share rendering code.

        No-op when the filter modal isn't open. Maintains the
        ``self._slot_picker_state`` dict across calls so the
        shared renderer can do in-place colour updates when only
        the highlight has moved (no tear-down flicker)."""
        widgets = getattr(self, '_filter_modal_widgets', None)
        if not widgets:
            return
        available = widgets.get('slot_available')
        claimed = widgets.get('slot_claimed')
        if available is None or claimed is None:
            return
        if not team_size:
            available.clear_widgets()
            available.height = 0
            claimed.clear_widgets()
            claimed.height = 0
            self._apply_slot_row_height(0)
            return
        app = App.get_running_app()
        try:
            from azt_collab_client import list_slots
        except ImportError:
            list_slots = lambda _lc: {}
        langcode = getattr(app, '_current_langcode', '') or ''
        slots = list_slots(langcode) if langcode else {}

        if not isinstance(getattr(self, '_slot_picker_state', None), dict):
            self._slot_picker_state = {}
        _render_slot_picker_into(
            available, claimed,
            team_size, my_slot, slots,
            self._on_pick_slot,
            state=self._slot_picker_state)
        self._apply_slot_row_height(available.height + claimed.height)

    def _apply_slot_row_height(self, target_h):
        """Set the modal's slot_row height to fit its children.
        The modal's ScrollView handles overflow, so no parent-
        height-chase dance is needed — the inline path's
        db_settings_box.height = minimum_height refresh from
        earlier versions is gone."""
        widgets = getattr(self, '_filter_modal_widgets', None)
        if not widgets:
            return
        slot_row = widgets.get('slot_row')
        if slot_row is not None:
            slot_row.height = target_h

    def _on_pick_team_size(self, n):
        """Persist team_size to the project KV. Resets the
        editing flag so ts_row returns to summary mode."""
        app = App.get_running_app()
        prev = int(
            getattr(app.recorder, 'split_team_size', 0) or 0)
        if prev == n:
            # No-op tap on the already-current value: just
            # bounce back to summary mode.
            self._team_size_editing = False
            self._refresh_split_rows()
            return
        langcode = getattr(app, '_current_langcode', '') or ''
        if not langcode:
            return
        try:
            from azt_collab_client import project_kv_set
        except ImportError:
            return
        project_kv_set(langcode, 'team_size', n)
        self._team_size_editing = False
        app._populate_split_state()
        self._refresh_split_rows()

    def _cancel_team_size_editing(self):
        """Back out of picker mode without changing team_size.
        Bound to the Cancel button rendered alongside the
        [2][3][4][5+] options."""
        self._team_size_editing = False
        self._refresh_split_rows()

    def _open_team_size_plus_dialog(self):
        """Numeric-input dialog for team sizes ≥ 5. Tapping OK
        flows through the same _on_pick_team_size path."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.textinput import TextInput
        from kivy.uix.button import Button
        from kivy.uix.label import Label

        box = BoxLayout(
            orientation='vertical', padding=dp(12), spacing=dp(10))
        box.add_widget(Label(
            text=_tr('How many devices on the team?'),
            font_size=sp(14), font_name=_FONT_NAME,
            size_hint_y=None, height=dp(36)))
        num_input = TextInput(
            text='5', multiline=False,
            size_hint_y=None, height=dp(48),
            font_size=sp(18), font_name=_FONT_NAME,
            input_filter='int', input_type='number')
        box.add_widget(num_input)
        btn_row = BoxLayout(
            orientation='horizontal', size_hint_y=None,
            height=dp(48), spacing=dp(8))
        cancel = Button(text=_tr('Cancel'), font_size=sp(13))
        ok = Button(
            text=_tr('OK'), font_size=sp(13),
            background_color=theme.ACCENT)
        btn_row.add_widget(cancel)
        btn_row.add_widget(ok)
        box.add_widget(btn_row)
        popup = Popup(
            title=_tr('Team size'),
            content=box,
            size_hint=(0.8, None), height=dp(200),
            auto_dismiss=False)
        cancel.bind(on_release=lambda *_: popup.dismiss())

        def _confirm(*_):
            try:
                n = int((num_input.text or '').strip())
            except ValueError:
                popup.dismiss()
                return
            if n >= 2:
                popup.dismiss()
                self._on_pick_team_size(n)
            else:
                popup.dismiss()
        ok.bind(on_release=_confirm)
        num_input.bind(on_text_validate=_confirm)
        popup.open()

    def _on_pick_slot(self, slot_str):
        """Claim *slot_str* for this device. § 21 hard rule #2
        routes CONTRIBUTOR_UNSET through the same path the
        popup picker uses."""
        from azt_collab_client import (
            claim_slot as _claim_slot, get_contributor)
        app = App.get_running_app()
        if not (get_contributor() or '').strip():
            app._show_contributor_required_for_slot()
            return
        langcode = getattr(app, '_current_langcode', '') or ''
        if not langcode:
            return
        # Mark this slot as in-flight before the RPC so the
        # _populate_split_state we trigger ourselves below doesn't
        # see a stale list_slots (commit hasn't been flushed yet)
        # and re-fire the picker. Cleared by the apply step once
        # the daemon's list_slots catches up; cleared here on
        # outright RPC failure.
        app._claim_pending_slot = str(slot_str)
        ok = _claim_slot(langcode, slot_str)
        if not ok:
            app._claim_pending_slot = ''
            print(f'[split] claim_slot({slot_str}) returned False',
                  file=sys.stderr)
            return
        set_peer_pref('cawl_filter_source', 'split')
        # Optimistic local mirror so the synchronous
        # _refresh_split_rows below sees the new value. Without
        # this, recorder.split_my_slot is still '' while
        # list_slots already shows our claim — the rebuild
        # treats slot_str as "claimed by another device" and
        # the user sees their device name flash into the
        # claimed-by-other column for one frame before the
        # worker apply corrects it. Worker apply confirms the
        # value via peer_id; on lost-race it overwrites to ''
        # and re-fires the picker, same shape as before.
        if app.recorder is not None:
            app.recorder.split_my_slot = str(slot_str)
        app._populate_split_state()
        self._refresh_split_rows()

    def _build_theme_buttons(self):
        """Populate theme selector row with one button per theme."""
        from kivy.uix.button import Button
        row = self.ids.get('theme_row')
        if not row:
            return
        row.clear_widgets()
        for name in theme.THEME_NAMES:
            is_active = (name == theme.current_theme)
            btn = Button(
                text=name,
                font_size=sp(14),
                font_name=_FONT_NAME,
                background_normal='',
                background_color=theme.GREEN if is_active else theme.SURFACE,
                color=theme.TEXT,
                size_hint_x=1,
            )
            btn.bind(on_release=lambda b, n=name: self._set_theme(n))
            row.add_widget(btn)

    def _set_theme(self, name):
        """Save the selected theme and offer to restart."""
        if name == theme.current_theme:
            return
        app = App.get_running_app()
        set_peer_pref('theme', name)
        # Update button highlights
        row = self.ids.get('theme_row')
        if row:
            for btn in row.children:
                btn.background_color = theme.GREEN if btn.text == name \
                    else theme.SURFACE
        # Confirm restart
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        from kivy.uix.label import Label
        from kivy.uix.modalview import ModalView
        mv = ModalView(size_hint=(0.8, None), height=dp(140),
                       background_color=theme.OVERLAY_DARK)
        box = BoxLayout(orientation='vertical', padding=dp(12), spacing=dp(12))
        box.add_widget(Label(
            text=f'Restart with {name} theme?',
            font_size=sp(16), font_name=_FONT_NAME,
            color=theme.TEXT, halign='center',
            size_hint_y=None, height=dp(40),
        ))
        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
        cancel_btn = Button(
            text=_tr('Cancel'), font_size=sp(14), font_name=_FONT_NAME,
            background_normal='', background_color=theme.SURFACE,
            color=theme.TEXT,
        )
        ok_btn = Button(
            text=_tr('OK'), font_size=sp(14), font_name=_FONT_NAME,
            background_normal='', background_color=theme.GREEN,
            color=theme.TEXT,
        )
        def _cancel(b):
            # Revert pref to current theme
            set_peer_pref('theme', theme.current_theme)
            if row:
                for btn in row.children:
                    btn.background_color = theme.GREEN \
                        if btn.text == theme.current_theme else theme.SURFACE
            mv.dismiss()
        def _restart(b):
            mv.dismiss()
            if platform == 'android':
                from jnius import autoclass
                Intent = autoclass('android.content.Intent')
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                intent = activity.getPackageManager().getLaunchIntentForPackage(
                    activity.getPackageName())
                intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                activity.startActivity(intent)
                activity.finishAffinity()
            else:
                app.stop()
                os.execv(sys.executable, [sys.executable] + sys.argv)
        cancel_btn.bind(on_release=_cancel)
        ok_btn.bind(on_release=_restart)
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(ok_btn)
        box.add_widget(btn_row)
        mv.add_widget(box)
        mv.open()

    # Assumed second-form field types (until secondformfield is available)
    _SECOND_FORM_FIELDS = {'Noun': 'Plural', 'Verb': 'Imperative'}

    def _build_rec_options(self, app):
        """Populate recording task row; always visible, tappable when >1 option."""
        row = self.ids.get('rec_task_row')
        btn = self.ids.get('rec_task_btn')
        if not row or not btn:
            return
        available = self._available_rec_tasks(app)
        self._rec_available = available
        # Restore saved selection (or default to citation)
        saved_key = peer_pref('rec_task', 'citation') or 'citation'
        # If saved key is no longer available, fall back to citation
        if not any(k == saved_key for k, _ in available):
            saved_key = 'citation'
            set_peer_pref('rec_task', saved_key)
        saved_label = next((t for k, t in available if k == saved_key), available[0][1])
        btn.text = saved_label
        row.height = dp(44)
        row.opacity = 1
        # Audio quality selector is always populated alongside the
        # recording-task row, regardless of how many rec tasks are
        # available.
        self._build_audio_quality_row()

    _AUDIO_QUALITY_LABELS = (
        ('high',         'High'),
        ('high_aac',     'High AAC'),
        ('medium',       'Medium'),
        ('medium_aac',   'Medium AAC'),
        ('low',          'Low'),
        ('low_aac',      'Low AAC'),
        ('very_low',     'Very Low'),
        ('very_low_aac', 'Very Low AAC'),
    )

    def _build_audio_quality_row(self):
        """Populate audio-quality row + button text from peer_pref."""
        row = self.ids.get('audio_quality_row')
        btn = self.ids.get('audio_quality_btn')
        if not row or not btn:
            return
        default = RecorderController._DEFAULT_AUDIO_PROFILE
        saved = peer_pref('audio_quality', default) or default
        # Defensive: if a stored value isn't one of the recognised
        # profiles (e.g. a stale legacy key), fall back to the
        # default and re-pin the pref.
        if saved not in dict(self._AUDIO_QUALITY_LABELS):
            saved = default
            set_peer_pref('audio_quality', saved)
        label = next(
            (l for k, l in self._AUDIO_QUALITY_LABELS if k == saved),
            saved)
        btn.text = _tr(label)
        row.height = dp(44)
        row.opacity = 1

    def _show_audio_quality_overlay(self):
        """Modal picker for the audio-quality profile."""
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        from kivy.uix.modalview import ModalView
        default = RecorderController._DEFAULT_AUDIO_PROFILE
        current = peer_pref('audio_quality', default) or default
        options = self._AUDIO_QUALITY_LABELS
        # If auto-degradation has pinned a ceiling, hide profiles
        # above it — they've already failed on this device and
        # offering them again would just re-trigger the same toast.
        ladder = RecorderController._PROFILE_LADDER
        ceiling = peer_pref('audio_quality_ceiling')
        if ceiling and ceiling in ladder:
            allowed = set(ladder[ladder.index(ceiling):])
            options = tuple((k, l) for k, l in options if k in allowed)
        view = ModalView(
            size_hint=(0.85, None),
            height=dp(52 * len(options) + 20),
            background_color=theme.OVERLAY_DARK,
            auto_dismiss=True,
        )
        box = BoxLayout(
            orientation='vertical', spacing=dp(4),
            padding=(dp(10), dp(10)),
        )
        for key, label in options:
            is_selected = key == current
            opt = Button(
                text=_tr(label),
                font_name=_FONT_NAME,
                font_size=sp(16),
                size_hint_y=None, height=dp(44),
                background_color=theme.GREEN if is_selected
                else theme.SURFACE,
                color=theme.TEXT,
            )

            def _select(k=key, l=label, v=view):
                set_peer_pref('audio_quality', k)
                aq_btn = self.ids.get('audio_quality_btn')
                if aq_btn:
                    aq_btn.text = _tr(l)
                v.dismiss()

            opt.bind(on_release=lambda inst, cb=_select: cb())
            box.add_widget(opt)
        view.add_widget(box)
        view.open()

    def _show_rec_overlay(self):
        """Show a modal overlay with recording task options."""
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        from kivy.uix.modalview import ModalView

        available = getattr(self, '_rec_available', [])
        if len(available) <= 1:
            return

        current_key = peer_pref('rec_task', 'citation') or 'citation'

        view = ModalView(
            size_hint=(0.85, None),
            height=dp(52 * len(available) + 20),
            background_color=theme.OVERLAY_DARK,
            auto_dismiss=True,
        )
        box = BoxLayout(
            orientation='vertical', spacing=dp(4),
            padding=(dp(10), dp(10)),
        )

        for key, label in available:
            is_selected = key == current_key
            btn = Button(
                text=label,
                font_name=_FONT_NAME,
                font_size=sp(16),
                size_hint_y=None, height=dp(44),
                background_color=theme.GREEN if is_selected
                else theme.SURFACE,
                color=theme.TEXT,
            )

            def _select(k=key, l=label, v=view):
                set_peer_pref('rec_task', k)
                task_btn = self.ids.get('rec_task_btn')
                if task_btn:
                    task_btn.text = l
                v.dismiss()

            btn.bind(on_release=lambda inst, cb=_select: cb())
            box.add_widget(btn)

        view.add_widget(box)
        view.open()

    @staticmethod
    def _available_rec_tasks(app):
        """Return list of (key, label) for recording tasks relevant to the data.

        Checks for actual LIFT field elements rather than just grammatical-info.
        For second forms, assumes Noun→Plural and Verb→Imperative field types.
        """
        tasks = [('citation', 'Citation forms')]
        if not app.recorder:
            return tasks
        db = app.recorder.db
        vernlang = db.vernlang

        # Check for actual field elements in the LIFT data
        has_plural = False
        has_imperative = False
        has_example = False
        for entry in db.entries:
            el = entry.get('_el')
            if el is None:
                continue
            # Check top-level field elements: Entry/field[@type=X]/form[@lang=vernlang]/text
            for field_el in el.findall('field'):
                ft = field_el.get('type', '')
                if ft == 'Plural' and not has_plural:
                    for form in field_el.findall('form'):
                        if form.get('lang') == vernlang and db._text(form):
                            has_plural = True
                            break
                elif ft == 'Imperative' and not has_imperative:
                    for form in field_el.findall('form'):
                        if form.get('lang') == vernlang and db._text(form):
                            has_imperative = True
                            break
            # Check for examples in senses
            if not has_example:
                for sense in el.findall('sense'):
                    if sense.find('example') is not None:
                        has_example = True
                        break
            if has_plural and has_imperative and has_example:
                break
        if has_plural:
            tasks.append(('noun', 'Second forms (nouns)'))
        if has_imperative:
            tasks.append(('verb', 'Second forms (verbs)'))
        if has_example:
            tasks.append(('example', 'Examples'))
        return tasks


    def apply_and_go(self):
        app = App.get_running_app()
        if app.recorder:
            cawl_in = self.ids.get('cawl_input')
            if cawl_in:
                text = cawl_in.text.strip()
                prior = (app.recorder.cawl_filter or '').strip()
                app.recorder.cawl_filter = text
                set_peer_pref('cawl_filter', text or None)
                # § 21: only flip the source flag when the text
                # actually changed. Closing Settings without
                # touching the CAWL field should not demote a
                # split-derived value to 'manual' (which would
                # suppress [k/n] in progress_text and stop the
                # filter from auto-recomputing on next sync).
                if text != prior:
                    set_peer_pref(
                        'cawl_filter_source',
                        'manual' if text else None)
            gs = self.ids.get('gloss_search_input')
            if gs:
                app.recorder.gloss_search = gs.text.strip()
            app.recorder.only_unrecorded = self.only_unrecorded
            app.recorder.rebuild_queue()
            app.go_recorder()
        else:
            app.start_over()


class CollabScreen(Screen):

    def on_enter(self):
        app = App.get_running_app()
        from azt_collab_client import get_contributor
        w = self.ids.get('name_input')
        if w and not w.text:
            w.text = get_contributor() or ''
        # Publish / Grant collaborator surfaces moved to the daemon
        # settings UI (CLIENT_INTEGRATION.md § 13 + Phase 3 strip-out).
        # Restore host selection + creds state from the server-owned store
        from azt_collab_client import get_credentials_status
        cred_status = get_credentials_status()
        host = cred_status.get('host', 'github')
        self.set_host(host, save=False)
        self._update_gh_status()
        # Restore GitLab fields
        gl_user = self.ids.get('gl_username_input')
        if gl_user and not gl_user.text:
            gl_user.text = cred_status.get('gitlab', {}).get('username', '')
        self._update_connect_enabled()
        # Show name overlay on first visit (blank name)
        name_w = self.ids.get('name_input')
        if name_w and not name_w.text.strip():
            self._show_name_overlay()

    def on_leave(self):
        """Save name whenever we leave this screen."""
        self._save_settings()

    def close_collab(self):
        """X button: go back to config (if a project is loaded);
        otherwise relaunch the picker."""
        app = App.get_running_app()
        sm = app.root.ids.sm
        sm.transition = SlideTransition(direction='right')
        if app.recorder:
            sm.current = 'config'
        else:
            app.start_over()

    def _update_connect_enabled(self):
        """Grey out connect/reconnect button when name is blank."""
        name_w = self.ids.get('name_input')
        has_name = bool(name_w and name_w.text.strip())
        btn = self.ids.get('gh_connect_btn')
        if btn:
            btn.normal_color = theme.GREEN if has_name else theme.BTN_INACTIVE
            btn.disabled = not has_name

    def _show_name_overlay(self):
        """Show a one-time overlay prompting the user to enter their name."""
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.widget import Widget
        overlay = BoxLayout(orientation='vertical', padding=dp(40), spacing=dp(20))
        from kivy.graphics import Color, Rectangle
        with overlay.canvas.before:
            Color(0, 0, 0, 0.7)
            bg_rect = Rectangle(pos=overlay.pos, size=overlay.size)
        overlay.bind(pos=lambda w, p: setattr(bg_rect, 'pos', p),
                     size=lambda w, s: setattr(bg_rect, 'size', s))
        overlay.add_widget(Widget(size_hint_y=1))
        msg = Label(
            text=_tr("Type your name as you want it to appear online, then click 'Connect'"),
            font_size=sp(16), font_name=_FONT_NAME, color=theme.TEXT,
            halign='center', valign='middle',
            size_hint_y=None, height=dp(80))
        msg.bind(size=msg.setter('text_size'))
        overlay.add_widget(msg)
        ok_btn = Builder.load_string(
            'RecBtn:\n'
            '    text: "OK"\n'
            '    normal_color: T.ACCENT\n'
        )
        ok_btn.bind(on_release=lambda b: self.remove_widget(overlay))
        overlay.add_widget(ok_btn)
        overlay.add_widget(Widget(size_hint_y=1))
        self.add_widget(overlay)

    @staticmethod
    def _langcode_from_project(app):
        """Ask the daemon for the current project's langcode rather
        than opening the local repo (azt_collab_client rule: no
        reading project state from the local filesystem). Note:
        CollabScreen itself is unreachable since 1.37.0 — Setup
        Collaboration delegates to open_server_ui — so this method is
        currently dead code kept only to satisfy the screen's existing
        on_enter contract while it lingers in the tree."""
        if not app.recorder:
            return ''
        cached = getattr(app, '_current_langcode', '')
        if cached:
            return cached
        try:
            from azt_collab_client import derive_langcode
            return derive_langcode(
                app.recorder.db.dir, app.recorder.db.path) or ''
        except Exception:
            return ''

    def set_host(self, host, save=True):
        """Toggle between github and gitlab sections."""
        self._host = host
        gh_btn = self.ids.get('host_github_btn')
        gl_btn = self.ids.get('host_gitlab_btn')
        gh_sec = self.ids.get('gh_section')
        gl_sec = self.ids.get('gl_section')

        active_color = theme.GREEN
        inactive_color = theme.BTN_INACTIVE

        # Store parent + index for re-insertion
        if not hasattr(self, '_host_parent'):
            if gh_sec and gh_sec.parent:
                self._host_parent = gh_sec.parent
                self._gh_index = list(self._host_parent.children).index(gh_sec)
                self._gl_index = list(self._host_parent.children).index(gl_sec)

        parent = getattr(self, '_host_parent', None)

        if host == 'gitlab':
            if gh_btn:
                gh_btn.normal_color = inactive_color
            if gl_btn:
                gl_btn.normal_color = active_color
            if gh_sec and gh_sec.parent:
                gh_sec.parent.remove_widget(gh_sec)
            if gl_sec and not gl_sec.parent and parent:
                parent.add_widget(gl_sec, index=self._gl_index)
            if gl_sec:
                gl_sec.height = gl_sec.minimum_height
                gl_sec.opacity = 1
        else:
            if gh_btn:
                gh_btn.normal_color = active_color
            if gl_btn:
                gl_btn.normal_color = inactive_color
            if gl_sec and gl_sec.parent:
                gl_sec.parent.remove_widget(gl_sec)
            if gh_sec and not gh_sec.parent and parent:
                parent.add_widget(gh_sec, index=self._gh_index)
            if gh_sec:
                gh_sec.height = gh_sec.minimum_height
                gh_sec.opacity = 1

        if save:
            from azt_collab_client import set_collab_host
            set_collab_host(host)

    def save_gitlab_credentials(self):
        """Save GitLab PAT and username to the server-owned credentials store."""
        token_w = self.ids.get('gl_token_input')
        user_w = self.ids.get('gl_username_input')
        token = token_w.text.strip() if token_w else ''
        username = user_w.text.strip() if user_w else ''
        if not token or not username:
            self._set_log(_tr('Enter both GitLab username and token.'))
            return
        from azt_collab_client import save_gitlab_credentials
        save_gitlab_credentials(username, token)
        self._set_log(_tr('GitLab credentials saved for {username}').format(username=username))

    def _update_gh_status(self):
        """Update the GitHub connection status label and install button."""
        from azt_collab_client import get_credentials_status
        status = get_credentials_status()
        gh = status.get('github', {})
        username = gh.get('username', '')
        connected = gh.get('connected', False)
        lbl = self.ids.get('gh_status_label')
        btn = self.ids.get('gh_connect_btn')
        if lbl:
            if connected and username:
                lbl.text = _tr('Connected as {username}').format(username=username)
                lbl.color = theme.GREEN
            else:
                lbl.text = _tr('Not connected')
                lbl.color = theme.TEXT_DIM
        if btn:
            btn.text = _tr('Reconnect') if connected else _tr('Connect to GitHub')
        # Hide install button + instructions if app is already installed
        installed = gh.get('app_installed', False)
        install_btn = self.ids.get('install_app_btn')
        inst = self.ids.get('device_instructions_label')
        if installed:
            if install_btn:
                install_btn.height = 0
                install_btn.opacity = 0
            if inst:
                inst.text = ''
                inst.height = 0

    # ── Internal helpers ───────────────────────────────────────────────────

    def open_link(self, url):
        """Open a URL in the device browser."""
        import webbrowser
        webbrowser.open(str(url))

    def open_server_ui(self):
        """Open the standalone AZT collab settings UI.

        Delegates to ``azt_collab_client.open_server_ui()``; the client
        owns the platform branching (desktop spawn / Android intent /
        desktop-only fallback) so peer apps stay thin."""
        from azt_collab_client import open_server_ui as _open_server_ui
        result = _open_server_ui()
        if result.get('ok'):
            self._set_log(_tr('Opened sync settings.'))
            return
        err = result.get('error', 'unknown')
        if err == 'desktop_only':
            self._set_log(_tr('Sync settings UI is desktop-only for now.'))
        elif err == 'spawn_exited':
            detail = (result.get('detail')
                      or f"rc={result.get('returncode', '?')}")
            self._set_log(_tr(
                'Sync settings UI failed to start: {detail}')
                          .format(detail=detail))
        else:
            # 'server_apk_not_installed' falls through here; bootstrap()
            # owns the install prompt at startup, so we don't compete.
            self._set_log(_tr('Could not open sync settings: {error}')
                          .format(error=err))
        # Mirror the daemon's last_crash on non-benign failures —
        # 'spawn_exited' is the prime case.
        if err not in ('desktop_only', 'server_apk_not_installed'):
            _log_server_crash_if_any('open_server_ui')

    def open_install_page(self):
        """Open the GitHub App installation page."""
        from azt_collab_client import github_app_install_url
        url = github_app_install_url()
        if not url:
            self._set_log(_tr('GitHub App install URL not available.'))
            return
        import webbrowser
        webbrowser.open(url)

    def copy_code(self):
        """Copy the device code to clipboard."""
        lbl = self.ids.get('device_code_label')
        if lbl and lbl.text:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(lbl.text)
            self._set_log(_tr('Code copied to clipboard'))

    def _save_settings(self):
        # Persists the committer name. repo_slug used to be written
        # here too but moved to the daemon settings UI in 1.41.5 per
        # CLIENT_INTEGRATION.md § 13 (Phase 3 strip-out).
        from azt_collab_client import set_contributor
        w = self.ids.get('name_input')
        if w:
            set_contributor(w.text)

    def _set_log(self, text):
        lbl = self.ids.get('log_label')
        if lbl:
            lbl.text = text

    def _run(self, busy_msg, func, *args):
        """Show busy_msg, run func(*args) in a thread, show result.
        Accepts a str or a Result — Results are translated for display."""
        self._set_log(busy_msg)
        import threading
        from azt_collab_client import translate_result, Result as _Result
        def _worker():
            result = func(*args)
            if isinstance(result, _Result):
                text = translate_result(result)
            else:
                text = result or ''
            Clock.schedule_once(lambda dt: self._set_log(text), 0)
        threading.Thread(target=_worker, daemon=True).start()

    # ── Device flow ────────────────────────────────────────────────────────

    def start_device_flow(self):
        """Begin GitHub device flow authentication via the server."""
        # Defocus any TextInput so keyboard doesn't pop up
        name_input = self.ids.get('name_input')
        if name_input:
            name_input.focus = False
        from azt_collab_client import (
            github_app_client_id, mark_github_app_installed)
        if not github_app_client_id():
            self._set_log(_tr('GitHub App client_id not configured.'))
            return
        # Reset install status on reconnect
        mark_github_app_installed(False)
        self._set_log(_tr('Starting GitHub authorization...'))
        import threading
        threading.Thread(target=self._device_flow_worker, daemon=True).start()

    def _device_flow_worker(self):
        from azt_collab_client import (
            github_device_flow_start, github_device_flow_status,
            github_app_install_url, translate_status, Status as _Status)
        try:
            resp = github_device_flow_start()
            if not resp.get('ok'):
                raise RuntimeError(resp.get('error', 'unknown'))
            job_id = resp['job_id']
            user_code = resp['user_code']
            verification_uri = resp.get(
                'verification_uri', 'https://github.com/login/device')
            interval = max(1, int(resp.get('interval', 5)))
            expires_in = int(resp.get('expires_in', 900))

            def _show_code(dt):
                inst = self.ids.get('device_instructions_label')
                if inst:
                    inst.text = (f'Opening {verification_uri} …\n'
                                 f'Enter this code:')
                    inst.height = dp(40)
                lbl = self.ids.get('device_code_label')
                if lbl:
                    lbl.text = user_code
                box = self.ids.get('device_code_box')
                if box:
                    box.height = dp(48)
                    box.opacity = 1
                from kivy.core.clipboard import Clipboard
                Clipboard.copy(user_code)
                self._set_log(_tr('Code copied to clipboard \u2014 type it on the GitHub page'))
            Clock.schedule_once(_show_code, 0)

            def _open_browser(dt):
                import webbrowser
                webbrowser.open(verification_uri)
            Clock.schedule_once(_open_browser, 3)

            # Poll the server until DONE / FAILED. The server is the
            # one talking to GitHub — peers only check the job status,
            # so tokens never cross the client/server boundary.
            import time
            deadline = time.time() + expires_in + 30
            status_resp = {}
            while time.time() < deadline:
                time.sleep(interval)
                status_resp = github_device_flow_status(job_id)
                if not status_resp.get('ok'):
                    raise RuntimeError(
                        status_resp.get('error', 'server_unavailable'))
                if status_resp.get('state') in ('DONE', 'FAILED'):
                    break
            if status_resp.get('state') != 'DONE':
                if status_resp.get('state') == 'FAILED':
                    raise _DeviceFlowFailure(
                        status_resp.get('error', 'AUTH_FAILED'),
                        status_resp.get('error_params') or {})
                raise RuntimeError('device_flow_timeout')

            _username = status_resp.get('username', '') or 'unknown'
            _app_installed = bool(status_resp.get('app_installed', False))
            _url = github_app_install_url()
            def _done(dt):
                lbl = self.ids.get('device_code_label')
                if lbl:
                    lbl.text = ''
                box = self.ids.get('device_code_box')
                if box:
                    box.height = 0
                    box.opacity = 0
                self._update_gh_status()
                inst = self.ids.get('device_instructions_label')
                install_btn = self.ids.get('install_app_btn')
                if _app_installed:
                    if inst:
                        inst.text = ''
                        inst.height = 0
                    if install_btn:
                        install_btn.height = 0
                        install_btn.opacity = 0
                    self._set_log(_tr('Connected as {username}').format(username=_username))
                else:
                    if inst:
                        inst.text = (
                            _tr('Now install the app to grant repository access.') + '\n'
                            + _tr('Select "All repositories".')
                        )
                        inst.height = dp(40)
                    if install_btn:
                        install_btn.height = dp(48)
                        install_btn.opacity = 1
                    self._set_log(_tr('Connected as {username} \u2014 install app for repo access').format(username=_username))
                    if _url:
                        import webbrowser
                        webbrowser.open(_url)
            Clock.schedule_once(_done, 0)

        except Exception as ex:
            if isinstance(ex, _DeviceFlowFailure):
                err_msg = translate_status(_Status(ex.code, ex.params))
            else:
                err_msg = str(ex)
            def _err(dt):
                lbl = self.ids.get('device_code_label')
                if lbl:
                    lbl.text = ''
                box = self.ids.get('device_code_box')
                if box:
                    box.height = 0
                    box.opacity = 0
                inst = self.ids.get('device_instructions_label')
                if inst:
                    inst.text = ''
                    inst.height = 0
                install_btn = self.ids.get('install_app_btn')
                if install_btn:
                    install_btn.height = 0
                    install_btn.opacity = 0
                self._set_log(_tr('Authorization failed: {error}').format(error=err_msg))
            Clock.schedule_once(_err, 0)



# ── Recorder controller ────────────────────────────────────────────────────────

class RecorderController:
    """
    Owns playback state, audio recording, and LIFT XML writes.
    Decoupled from the UI — the UI reads properties from here.
    """

    def __init__(self, db: 'LIFTDatabase', langcode: str = ''):
        self.db = db
        # Langcode is daemon-authoritative; carried into the controller
        # so the per-project pending-save queue can persist under a
        # (langcode, guid) key (template-cloned projects share GUIDs —
        # see NOTES_TO_DAEMON "Fresh GUIDs when creating a project from
        # a template"). Empty langcode disables persistence.
        self._langcode = langcode
        self.queue = []          # list of entry dicts
        self.index = 0
        # cawl_filter persists across boots/installs: some workflows
        # pin a CAWL range and rely on it sticking. Stored suite-wide
        # so the same scope follows the user across peers.
        self.cawl_filter = peer_pref('cawl_filter', '') or ''
        self.gloss_search = ''
        self.only_unrecorded = False
        # Wordlist-split state (CLIENT_INTEGRATION.md § 21).
        # Populated by populate_split_state on project-load and
        # every sync-completion. ``split_team_size`` is the
        # project-shared team count; ``split_my_slot`` is this
        # device's claimed slot ("1", "2", …) or '' if unclaimed.
        # progress_text reads both for the "[k/n]" suffix.
        self.split_team_size = 0
        self.split_my_slot = ''
        # One-shot "go outside filter" target. Set by the Go-To
        # dialog when the user enables the "Go outside filter"
        # checkbox and enters a CAWL number not present in the
        # current filtered queue. ``current`` returns this entry
        # while it's set, regardless of ``queue``; the next call
        # to ``go_next`` / ``go_prev`` clears it and snaps back to
        # the in-filter entry closest by CAWL number.
        self._one_shot_entry = None
        self._recording = False
        self._playing = False
        self._pending_rerecord = False
        self._audio_path = None
        self._recorder = None
        self._record_pfd = None
        # Set in _start_android_recording before the JNI chain runs so
        # the start/stop/validation failure paths know which profile
        # to degrade. None on non-Android, where there's no ladder.
        self._record_profile_key = None
        # Cleared on every start; flipped to True only when the
        # platform start path returns clean. stop_recording gates the
        # LIFT basename write on this so a failed start cannot leave
        # a bogus filename in <citation><form>.
        self._record_ok = False
        # Wall-clock monotonic timestamp stamped when mr.start() has
        # actually returned (in _publish_start_success, not in
        # start_recording). Read by stop_recording's min-hold-gate
        # so the gate measures real recorded-audio duration, not
        # wall-clock since touch-down. Reset to None after a clean
        # stop or a cancelled start.
        self._record_started_at = None
        # Async-start state machine (CLIENT_INTEGRATION-style ref:
        # main.py H2 plan, 1.41.27+):
        # - _start_pending is True while a worker thread is running
        #   _start_native_recording (the blocking JNI chain
        #   openFD/prepare/start which can take 150-400ms on slow
        #   MTK chips). Gates re-entrant start_recording calls.
        # - _start_cancel_requested is set by stop_recording when a
        #   touch_up lands DURING that window. The worker checks it
        #   after mr.start() returns; if set, the just-started
        #   MediaRecorder is torn down without writing the LIFT
        #   reference (same end-state as the min-hold-gate path).
        self._start_pending = False
        self._start_cancel_requested = False

        # Has the user changed anything on the current entry (audio
        # recorded, image picked, etc.) since the last sync? Set by
        # set_audio's caller and the image-pick / image-bake paths;
        # cleared by nav_prev / nav_next after firing _auto_commit_sync.
        # Pure browse swipes leave it False and skip the commit RPC.
        self._dirty = False

        # LIFT saves stranded by a transient daemon hiccup
        # (ServerUnavailable / ContentProvider FD recycle / FS
        # pressure inside ``set_audio`` → ``_save`` →
        # ``atomic_open_write``). Keyed by entry guid → audio
        # filename; retried on every _update_sync_status tick by
        # ``retry_pending_lift_saves``. The on-disk audio bytes
        # are preserved (re-record overwrites the same
        # deterministic path), so the worst case if the daemon
        # never comes back is "audio file exists, LIFT reference
        # missing".
        #
        # Persisted via ``peer_pref('pending_lift_saves')`` under
        # the controller's langcode so a force-kill before the
        # auto-retry tick fires doesn't lose the binding. On
        # filesystem projects ``LIFTDatabase.bind_orphan_audio``
        # (called from ``load_lift``) is the primary recovery
        # path; the persisted queue covers URI projects, where
        # the daemon's provider exposes no list_audio RPC.
        self._pending_lift_saves = self._load_persisted_pending()

        # Start-attempt token incremented each start_recording.
        # The watchdog (``_start_watchdog``) only fires force-
        # cancel if its captured token still matches when it
        # runs, so a successful start that resolves _start_pending
        # within the timeout doesn't get spuriously cancelled by
        # a late-firing watchdog from an earlier attempt.
        self._record_attempt = 0

        # Per-recording poll wakes every 500ms while _recording
        # is True. Tracks two things on the same tick:
        #   - getMaxAmplitude (Android only) to catch the
        #     "another app has the mic and we got a silent
        #     stream" case (e.g. Zoom holding the mic while
        #     the recorder thinks it's capturing).
        #   - elapsed monotonic time vs the MediaRecorder
        #     setMaxDuration(60_000) cap, as a peer-side
        #     backstop in case the internal duration-reached
        #     event doesn't flip our state machine.
        self._recording_poll_event = None
        self._max_amplitude_seen = 0

        # Gloss languages — `all_gloss_langs` is what the LIFT
        # actually has; `active_gloss_langs` is the user's pick. The
        # pick persists across boots/installs: if the user previously
        # selected langs that are also present in this project, honor
        # that. Otherwise default to up-to-three from what's available.
        self.all_gloss_langs = sorted(db.gloss_langs)
        saved = peer_pref('gloss_langs', None) or []
        kept = [l for l in saved if l in self.all_gloss_langs]
        if kept:
            self.active_gloss_langs = kept
        else:
            self.active_gloss_langs = self.all_gloss_langs[:] \
                if len(self.all_gloss_langs) <= 3 \
                else self.all_gloss_langs[:3]

        self.rebuild_queue()

    # ── Queue management ───────────────────────────────────────────────────────

    def rebuild_queue(self):
        entries = self.db.entries[:]

        # CAWL filter
        cf = self.cawl_filter.strip()
        if cf:
            cawl_set = self._parse_cawl_filter(cf)
            entries = [e for e in entries if e.get('cawl') in cawl_set]

        # Gloss search
        gs = self.gloss_search.strip().lower()
        if gs:
            def matches(e):
                for lang in self.active_gloss_langs:
                    for g in e.get('glosses', {}).get(lang, []):
                        if gs in g.lower():
                            return True
                return False
            entries = [e for e in entries if matches(e)]

        # Only unrecorded
        if self.only_unrecorded:
            entries = [e for e in entries if not e.get('audio_filename')]

        self.queue = entries
        self.index = max(0, min(self.index, len(self.queue) - 1))
        self._notify_ui()

    def _parse_cawl_filter(self, text):
        """Parse '1-100,200,250-260' into a set of zero-padded CAWL strings."""
        result = set()
        for part in text.replace(' ', '').split(','):
            if '-' in part:
                try:
                    lo, hi = part.split('-', 1)
                    for i in range(int(lo), int(hi) + 1):
                        result.add(f'{i:04d}')
                except ValueError:
                    pass
            else:
                try:
                    result.add(f'{int(part):04d}')
                except ValueError:
                    pass
        return result

    def sorted_cawl_numbers(self):
        """Return every project entry's CAWL number as a sorted
        list of ints. Reads the unfiltered model (``db.entries``)
        — not ``queue`` — so the result is invariant under the
        active filter. Used by the word-list split feature to
        partition the full list across team devices."""
        nums = []
        for e in self.db.entries:
            cawl = e.get('cawl', '')
            if not cawl:
                continue
            try:
                nums.append(int(cawl))
            except (TypeError, ValueError):
                continue
        nums.sort()
        return nums

    @staticmethod
    def compute_split_range(sorted_nums, team_size, my_slot):
        """Partition *sorted_nums* into *team_size* contiguous
        slices and return slice *my_slot* (1-indexed) as a
        ``'lo-hi'`` range string suitable for ``cawl_filter``.

        Sizes differ by at most 1: the first ``len(nums) %
        team_size`` slices get one extra. ``lo`` and ``hi`` are
        the min and max CAWL numbers actually present in the
        slice — gaps inside the slice are harmless because
        ``_parse_cawl_filter`` intersects ranges against the
        actual entries.

        Returns ``''`` for invalid inputs (empty list, slot out
        of range, team_size < 1) so callers can treat the result
        uniformly as "no filter to apply"."""
        if (not sorted_nums or team_size < 1 or my_slot < 1
                or my_slot > team_size):
            return ''
        total = len(sorted_nums)
        base = total // team_size
        rem = total % team_size
        start = sum(
            (base + 1) if k < rem else base
            for k in range(my_slot - 1))
        size = (base + 1) if (my_slot - 1) < rem else base
        end = start + size - 1
        return f'{sorted_nums[start]}-{sorted_nums[end]}'

    # ── Navigation ─────────────────────────────────────────────────────────────

    def go_next(self):
        if self._one_shot_entry is not None:
            self._snap_back_from_one_shot()
            return
        if self.index < len(self.queue) - 1:
            self._pending_rerecord = False
            self._stop_active_player()
            self.index += 1
            self._notify_ui()

    def go_prev(self):
        if self._one_shot_entry is not None:
            self._snap_back_from_one_shot()
            return
        if self.index > 0:
            self._pending_rerecord = False
            self._stop_active_player()
            self.index -= 1
            self._notify_ui()

    def _snap_back_from_one_shot(self):
        """End the outside-filter excursion. Drops the one-shot
        entry and points ``index`` at the in-filter entry closest
        to it by CAWL number — closest in either direction so a
        swipe that lands the user nearby feels natural regardless
        of which way they swiped. Caller (go_next / go_prev) is
        the one-shot UX contract: any swipe returns to filter."""
        target_cawl = (self._one_shot_entry or {}).get('cawl', '')
        self._one_shot_entry = None
        self._pending_rerecord = False
        self._stop_active_player()
        if target_cawl and self.queue:
            try:
                target_n = int(target_cawl)
                best_i = min(
                    range(len(self.queue)),
                    key=lambda i: abs(
                        int(self.queue[i].get('cawl', '0') or '0')
                        - target_n))
                self.index = best_i
            except (TypeError, ValueError):
                self.index = 0
        self._notify_ui()

    @property
    def current(self):
        if self._one_shot_entry is not None:
            return self._one_shot_entry
        if not self.queue:
            return None
        return self.queue[self.index]

    # ── UI data properties ─────────────────────────────────────────────────────

    @property
    def headword(self):
        e = self.current
        if not e:
            return ''
        return e.get('headword', '')

    @property
    def list_name(self):
        """Short name for the wordlist, from field/@type (max 8 chars)."""
        name = self.db.list_type
        if not name:
            name = os.path.splitext(os.path.basename(self.db.path))[0]
        return name[:8]

    @property
    def progress_text(self):
        if not self.queue:
            return 'No entries'
        cawl = self.current.get('cawl', '')
        cawl_num = cawl.lstrip('0') or '0' if cawl else ''
        lang = self.db.vernlang or self.list_name
        # Tight format: one space after lang, then cawl/range with no
        # internal spaces, then optional [k/n] split suffix.
        # Examples:
        #   en-US-x-kent 204/200-500[2/4]   (split active)
        #   en-US-x-kent 204/200-500        (manual range filter)
        #   en-US-x-kent 204/1700           (no filter)
        cf = self.cawl_filter.strip()
        right = cf if cf else str(len(self.queue))
        # Compose the centre: "cawl_num/right" if we have a cawl
        # number, else just "right" (preserves the pre-split shape
        # for non-CAWL list types).
        centre = f'{cawl_num}/{right}' if cawl_num else right
        # Split suffix: only when this device has an active slot
        # claim AND a team_size pulled from the project KV AND
        # the filter is currently split-derived. Without the
        # source gate, a manually-typed range like "200-500"
        # would still pick up a stale [k/n] from a prior pick.
        team_size = getattr(self, 'split_team_size', 0)
        my_slot = getattr(self, 'split_my_slot', '')
        is_split = (peer_pref('cawl_filter_source', None) == 'split')
        if team_size and my_slot and is_split:
            centre = f'{centre}[{my_slot}/{team_size}]'
        return f'{lang} {centre}'

    @property
    def has_image(self):
        p = self.image_path
        return bool(p) and os.path.exists(p)

    @property
    def image_path(self):
        e = self.current
        if not e:
            return ''
        p = e.get('image_path', '')
        if p:
            return p
        # Lazy resolve — _parse_entry leaves image_path empty so the
        # potentially-slow ContentProvider read / URL lookup happens
        # once per entry on first access (typically when the entry
        # becomes current) rather than 1700× during load.
        p = self.db._resolve_image_path(
            e.get('illustration_href', ''), e.get('cawl', ''))
        e['image_path'] = p
        return p

    @property
    def has_recording(self):
        e = self.current
        if not e:
            return False
        return bool(e.get('audio_filename'))

    @property
    def status_text(self):
        e = self.current
        if not e:
            return ''
        if self._recording:
            return _tr('Recording...')
        fn = e.get('audio_filename')
        if fn:
            return fn
        return _tr('Not yet recorded')

    # ── Recording ──────────────────────────────────────────────────────────────

    # Minimum hold time, in seconds. Stops fired before this much
    # has elapsed since start_recording are treated as accidental
    # taps: the in-flight MediaRecorder is torn down without
    # advertising the basename into the LIFT, and the user gets a
    # "hold to record" toast. Tuned for slow MediaTek chips where
    # MediaRecorder.prepare() can take 150-400ms by itself; the gate
    # has to be longer than that or a slow start path looks like a
    # short hold.
    _MIN_HOLD_SEC = 0.4

    # Android AAC quality profiles, selected via the
    # peer_pref('audio_quality') key. Only applies to Android (iOS
    # uses FLAC, desktop uses PCM WAV — both inherently lossless and
    # not affected by these parameters).
    #
    # Eight profiles: full cross of four quality tiers × two
    # containers (MPEG_4/.m4a vs AAC_ADTS/.aac). The temporarily-
    # wide table is for A/B isolation in the field: when a device
    # is rescued by switching to e.g. 'low_aac' we want to know
    # whether the rescue came from the lower bitrate, the alternate
    # container, or both. Expect to prune to ~3 once the data is in.
    #
    # Quality tiers:
    #   high      — 256k / 48kHz   (historical 1.41.20 default)
    #   medium    — 128k / 44.1kHz (1.41.21 default; safe everywhere)
    #   low       —  64k / 22.05kHz (telephone-quality voice)
    #   very_low  —  32k / 16kHz   (deep rescue, ~4KB/sec on disk)
    #
    # Containers:
    #   MPEG_4   → .m4a (broadest playback support)
    #   AAC_ADTS → .aac (bypasses MPEG_4-specific HAL bugs reported
    #              on some mid-tier MediaTek encoders)
    #
    # MediaPlayer sniffs format from content (not extension), so
    # .m4a and .aac coexist fine in the same LIFT's audio dir.
    _AUDIO_PROFILES = {
        'high':         {'bitrate': 256_000, 'sample_rate': 48_000,
                         'output_format': 'MPEG_4',   'ext': '.m4a'},
        'high_aac':     {'bitrate': 256_000, 'sample_rate': 48_000,
                         'output_format': 'AAC_ADTS', 'ext': '.aac'},
        'medium':       {'bitrate': 128_000, 'sample_rate': 44_100,
                         'output_format': 'MPEG_4',   'ext': '.m4a'},
        'medium_aac':   {'bitrate': 128_000, 'sample_rate': 44_100,
                         'output_format': 'AAC_ADTS', 'ext': '.aac'},
        'low':          {'bitrate':  64_000, 'sample_rate': 22_050,
                         'output_format': 'MPEG_4',   'ext': '.m4a'},
        'low_aac':      {'bitrate':  64_000, 'sample_rate': 22_050,
                         'output_format': 'AAC_ADTS', 'ext': '.aac'},
        'very_low':     {'bitrate':  32_000, 'sample_rate': 16_000,
                         'output_format': 'MPEG_4',   'ext': '.m4a'},
        'very_low_aac': {'bitrate':  32_000, 'sample_rate': 16_000,
                         'output_format': 'AAC_ADTS', 'ext': '.aac'},
    }
    _DEFAULT_AUDIO_PROFILE = 'high'

    # Order in which auto-degradation walks the profile table on
    # repeated start/stop/validation failures. Container swap before
    # bitrate drop: dropping bitrate is permanent fidelity loss, but
    # MPEG_4 → AAC_ADTS dodges the MTK MPEG_4 HAL wedges noted in the
    # _AUDIO_PROFILES comment without giving up any bits.
    _PROFILE_LADDER = (
        'high', 'high_aac',
        'medium', 'medium_aac',
        'low', 'low_aac',
        'very_low', 'very_low_aac',
    )

    # Minimum fraction of (held_seconds * bitrate / 8) bytes we expect
    # the on-disk encoded file to have. Real AAC/M4A overhead pushes
    # the ratio close to 1.0 once headers are written, so a result
    # well below 0.25 means the encoder produced essentially nothing
    # — a silent failure mode observed on flaky MediaTek HAL builds.
    _POST_STOP_MIN_RATIO = 0.25

    # Peak MediaRecorder.getMaxAmplitude (Android, 0-32767)
    # below which we treat the take as silent input. Speech
    # routinely peaks in the thousands; a sub-100 ceiling is
    # well above any real noise floor and well below normal
    # voice. Used by ``_poll_recording`` + the post-stop
    # silent-input branch to detect "another app has the mic"
    # cases (Zoom, phone calls, voice assistants).
    _SILENT_AMP_THRESHOLD = 100

    # Backstop for ``setMaxDuration(60_000)``: MediaRecorder
    # stops itself at the cap, but the peer state machine
    # (``_recording``, etc.) doesn't know unless we listen.
    # Polling elapsed time is simpler than wiring an
    # ``OnInfoListener`` PythonJavaClass and gives us the
    # same end-result one tick later. 0.5 s grace past 60 s
    # avoids racing the encoder's internal stop.
    _MAX_DURATION_BACKSTOP_S = 60.5

    # How long start_recording will wait for the worker
    # thread to publish success / failure before the
    # watchdog force-cancels the attempt. Slow MediaTek
    # devices have been observed at 150-400 ms; 5 s is
    # an order of magnitude past that and well below the
    # threshold where the user starts wondering if the app
    # crashed.
    _START_WATCHDOG_S = 5.0

    def _degrade_profile(self, failed_key):
        """Drop the audio-quality ceiling one rung below `failed_key`
        and clamp the user-facing pref to it. Persisted via peer_pref
        so the device-level adaptation survives crashes and reboots,
        matching the recorder's policy of making resource decisions
        transparently rather than as a user-tunable setting."""
        if not failed_key:
            return None
        try:
            idx = self._PROFILE_LADDER.index(failed_key)
        except ValueError:
            return None
        if idx >= len(self._PROFILE_LADDER) - 1:
            # Already at the lowest rung — nothing to drop to. The
            # user still needs to know the take didn't make it.
            self._toast_record_failed(at_floor=True)
            return None
        new_key = self._PROFILE_LADDER[idx + 1]
        # peer_pref persistence is an RPC; ServerUnavailable
        # here must NOT block the in-memory degrade nor
        # propagate out of stop_recording (which would skip
        # _notify_ui and wedge the UI). Log and continue —
        # the next successful peer_pref write picks up the
        # ceiling.
        try:
            set_peer_pref('audio_quality_ceiling', new_key)
            set_peer_pref('audio_quality', new_key)
        except Exception as ex:
            print(f'[record] degrade persist failed: {ex} '
                  f'(in-memory ceiling still applied)',
                  file=sys.stderr, flush=True)
        print(f'[record] degraded {failed_key} → {new_key} '
              f'(ceiling pinned)')
        self._toast_record_failed(at_floor=False)
        return new_key

    def _toast_record_failed(self, at_floor):
        from kivy.app import App as _App
        app = _App.get_running_app()
        if app is None:
            return
        msg = (_tr('Recording failed at the lowest quality. '
                   'Please try again.')
               if at_floor
               else _tr('Recording failed; quality lowered. '
                        'Please try again.'))
        Clock.schedule_once(
            lambda dt: app._show_toast(msg), 0)

    def start_recording(self):
        """Spawn a worker thread to do the blocking JNI setup, return
        to the Kivy touch dispatcher immediately. The MediaRecorder
        prepare/start chain can take 150-400ms on slow MediaTek
        devices; running it inline blocked the touch loop and let
        touch_up race the setup. See the H2 plan in CHANGELOG 1.41.27
        for the full state machine."""
        if self._recording or self._start_pending or not self.current:
            return
        path = self._make_audio_path()
        # Clear before the attempt so a previous successful flag
        # cannot survive into a now-failing start.
        self._record_ok = False
        self._start_pending = True
        self._start_cancel_requested = False
        self._record_started_at = None
        # Bump the attempt token so the watchdog (scheduled
        # below) and any late publish callbacks can tell
        # whether they're operating on the live attempt or a
        # superseded one.
        self._record_attempt += 1
        attempt = self._record_attempt
        # Defer the "preparing" UI rebuild to the next frame so the
        # Kivy touch dispatch that triggered us can return cleanly
        # before refresh_recorder_ui touches widgets.
        Clock.schedule_once(lambda dt: self._notify_ui(), 0)
        import threading
        threading.Thread(
            target=self._start_worker,
            args=(path,),
            daemon=True,
        ).start()
        # Watchdog: if neither publish callback has flipped
        # _start_pending to False within _START_WATCHDOG_S,
        # force-cancel so the UI doesn't sit on the
        # "preparing" visual indefinitely.
        Clock.schedule_once(
            lambda dt, a=attempt: self._start_watchdog(a),
            self._START_WATCHDOG_S)

    def _start_worker(self, path):
        """Run the blocking JNI setup on a worker thread. Marshals
        the outcome back to the main thread via Clock.schedule_once
        — never mutates publish-relevant state (`_recording`,
        `_record_started_at`) directly; that's the main thread's
        job in `_publish_start_*`."""
        try:
            actual_path = self._start_native_recording(path)
        except Exception as ex:
            Clock.schedule_once(
                lambda dt, e=ex: self._publish_start_failure(e), 0)
            return
        Clock.schedule_once(
            lambda dt, p=actual_path: self._publish_start_success(p), 0)

    def _publish_start_success(self, actual_path):
        """Main-thread callback after the worker's start succeeded.
        Stamps _record_started_at NOW (real recording duration
        reference) and flips _recording True. If the user already
        released the button while the worker was running, tears
        down without writing the LIFT reference — same end-state
        as the min-hold-gate tap path."""
        # Stale-callback gate: the watchdog (or a later start)
        # may have already force-cancelled this attempt by
        # clearing _start_pending. Tear down whatever
        # MediaRecorder the worker spun up so the mic isn't
        # held by a leaked recorder, then bail without
        # flipping _recording / scheduling play_audio.
        if not self._start_pending:
            print('[record] stale _publish_start_success; '
                  'tearing down', file=sys.stderr, flush=True)
            try:
                self._stop_native_recording()
            except Exception as ex:
                print(f'[record] stale teardown error: {ex}',
                      file=sys.stderr, flush=True)
            return
        self._start_pending = False
        if self._start_cancel_requested:
            print('[record] start completed but user already '
                  'released; tearing down')
            self._record_ok = False
            # Clear before teardown: an immediate stop-after-start
            # nearly always throws (no moov atom yet), and we don't
            # want a user-cancel to count as device evidence for
            # degrading the profile.
            self._record_profile_key = None
            self._stop_native_recording()
            self._record_started_at = None
            Clock.schedule_once(lambda dt: self._notify_ui(), 0)
            # Coaching toast — same shape as the min-hold-gate one.
            from kivy.app import App as _App
            app = _App.get_running_app()
            if app is not None:
                Clock.schedule_once(
                    lambda dt: app._show_toast(
                        _tr('Hold the button to record')), 0)
            return
        if actual_path:
            self._audio_path = actual_path
        self._recording = True
        self._record_ok = True
        import time as _time
        self._record_started_at = _time.monotonic()
        # Reset the silent-input tracker and arm the
        # per-recording poll. Same Clock event covers two
        # concerns: amplitude tracking (for silent-input
        # detection) and the setMaxDuration backstop.
        self._max_amplitude_seen = 0
        self._recording_poll_event = Clock.schedule_interval(
            self._poll_recording, 0.5)
        # Diagnostic — incident logs (e.g. 1.46.43 "60 s
        # wedge" report) couldn't tell whether the worker had
        # reached this point without a marker.
        print(f'[record] publish_start_success: '
              f'audio_path={self._audio_path!r}',
              file=sys.stderr, flush=True)
        Clock.schedule_once(lambda dt: self._notify_ui(), 0)

    def _publish_start_failure(self, ex):
        """Main-thread callback after the worker's start raised."""
        # Stale-callback gate: the watchdog already cleaned
        # up if _start_pending is False. Logging this error
        # is still useful (the original exception is the
        # diagnostic), but the second toast / second degrade
        # would just be noise on top of the watchdog toast.
        if not self._start_pending:
            print(f'[record] stale _publish_start_failure '
                  f'(watchdog already cleaned up): {ex}',
                  file=sys.stderr, flush=True)
            return
        print(f'Recording start failed: {ex}')
        self._start_pending = False
        self._record_ok = False
        self._record_started_at = None
        # Drop the cancel flag too so a later well-timed start
        # doesn't inherit a stale one.
        self._start_cancel_requested = False
        # Auto-degrade so the next attempt has a chance. No-op on
        # non-Android (profile key stays None).
        self._degrade_profile(self._record_profile_key)
        Clock.schedule_once(lambda dt: self._notify_ui(), 0)

    def _start_watchdog(self, attempt):
        """Force-cancel a stuck start. Scheduled by
        ``start_recording`` to fire ``_START_WATCHDOG_S``
        seconds later; no-ops if the worker has already
        resolved _start_pending (the common case) or if a
        later start has bumped the attempt counter past us.

        Stuck-start symptom this closes: worker thread's
        ``MediaRecorder.prepare()`` / ``.start()`` hangs on a
        bad device or while the mic is contended by another
        app. Without this watchdog the user sees the
        "preparing" UI indefinitely; subsequent record-button
        presses early-return at ``_start_pending`` so the
        button looks dead."""
        if not self._start_pending:
            return
        if self._record_attempt != attempt:
            return
        print(f'[record] start watchdog fired after '
              f'{self._START_WATCHDOG_S}s; force-cancelling '
              f'attempt {attempt}', file=sys.stderr, flush=True)
        self._start_pending = False
        self._record_ok = False
        self._record_started_at = None
        self._record_profile_key = None
        # A successful late publish callback would now see
        # _start_pending == False (stale-callback gate above)
        # and tear down its leaked MediaRecorder.
        from kivy.app import App as _App
        app = _App.get_running_app()
        if app is not None:
            Clock.schedule_once(
                lambda dt: app._show_toast(
                    _tr('Recording setup timed out. '
                        'Please try again.')), 0)
        Clock.schedule_once(lambda dt: self._notify_ui(), 0)

    def _poll_recording(self, dt):
        """Per-recording tick (500 ms). Two concerns:

        1. Track ``MediaRecorder.getMaxAmplitude`` so
           ``stop_recording`` can detect the "another app
           has the mic, we captured silence" case (Zoom
           call, phone call, voice assistant) and surface a
           diagnostic toast instead of writing a silent-but-
           correctly-sized file.
        2. Enforce the ``setMaxDuration(60_000)`` cap
           peer-side. MediaRecorder stops itself when the
           cap fires, but our state machine wouldn't know
           without a listener; polling elapsed time is the
           simpler equivalent.

        Returns False to cancel the schedule_interval when
        recording ends."""
        if not self._recording:
            return False
        if platform == 'android' and self._recorder is not None:
            try:
                amp = self._recorder.getMaxAmplitude()
                if amp > self._max_amplitude_seen:
                    self._max_amplitude_seen = amp
            except Exception:
                pass
        if self._record_started_at is not None:
            import time as _time
            elapsed = _time.monotonic() - self._record_started_at
            if elapsed >= self._MAX_DURATION_BACKSTOP_S:
                print(f'[record] max-duration backstop fired '
                      f'at {elapsed:.2f}s; auto-stopping',
                      file=sys.stderr, flush=True)
                self.stop_recording()
                return False
        return True

    def stop_recording(self):
        # Cancel the per-recording poll at the very top so it
        # stops in every exit path below (early returns, the
        # tap-not-hold branch, the full stop, the exception
        # paths). Idempotent — None when no poll is armed.
        ev = self._recording_poll_event
        if ev is not None:
            try:
                ev.cancel()
            except Exception:
                pass
            self._recording_poll_event = None
        if self._start_pending:
            # User released the button while the worker was still
            # setting up. Flag the cancel; the publish callback
            # will tear down the MediaRecorder once start returns.
            # Don't proceed to the regular stop flow — there's
            # nothing to stop yet, and the post-publish teardown
            # handles the audio-path / LIFT bookkeeping uniformly.
            self._start_cancel_requested = True
            return
        if not self._recording:
            return
        import time as _time
        held = _time.monotonic() - (self._record_started_at or 0)
        if held < self._MIN_HOLD_SEC:
            # Tap, not hold. Tear down the in-flight MediaRecorder
            # without writing the LIFT reference — a sub-_MIN_HOLD_SEC
            # M4A is missing the moov atom and isn't playable anyway,
            # and the platform-specific stop path is exactly where
            # IllegalStateException-style wedges originate.
            print(f'[record] tap-not-hold ({held:.3f}s < '
                  f'{self._MIN_HOLD_SEC}s); ignoring')
            self._recording = False
            self._record_ok = False
            self._pending_rerecord = False
            # Tap-not-hold is a UX signal, not device evidence — clear
            # the profile key so the stop-exception path can't degrade
            # off an immediate-stop wedge.
            self._record_profile_key = None
            self._record_started_at = None
            if platform == 'android':
                # Move the blocking native stop off the main thread —
                # MediaRecorder.stop()+release() flushes the encoder
                # and on slow chips / URI-provider FDs can block for
                # seconds, freezing the touch dispatcher. Capture the
                # handles and null self._recorder so a fresh start
                # can race in safely.
                recorder = self._recorder
                pfd = self._record_pfd
                self._recorder = None
                self._record_pfd = None
                import threading
                threading.Thread(
                    target=self._stop_android_handles_only,
                    args=(recorder, pfd),
                    daemon=True, name='stop-tap').start()
            else:
                self._stop_native_recording()
            Clock.schedule_once(lambda dt: self._notify_ui(), 0)
            # Coaching toast on the main thread.
            from kivy.app import App as _App
            app = _App.get_running_app()
            if app is not None:
                Clock.schedule_once(
                    lambda dt: app._show_toast(
                        _tr('Hold the button to record')), 0)
            return
        self._recording = False
        self._pending_rerecord = False
        if platform != 'android':
            # iOS/desktop: native stop is fast (AVAudioRecorder.stop()
            # / sounddevice stream close). Keep the existing
            # synchronous flow so the worker refactor below stays
            # scoped to the slow Android-only blocking case.
            self._stop_native_recording()
            self._record_started_at = None
            if self._audio_path and self._record_ok:
                filename = os.path.basename(self._audio_path)
                guid = self.current['guid']
                self.current['audio_filename'] = filename
                self._dirty = True
                import threading
                threading.Thread(
                    target=self._lift_audio_write_worker,
                    args=(guid, filename), daemon=True,
                    name='lift-audio-write').start()
            Clock.schedule_once(lambda dt: self._notify_ui(), 0)
            if self._record_ok:
                Clock.schedule_once(
                    lambda dt: self.play_audio(), 0.5)
            return
        # Android: MediaRecorder.stop()+release() flushes the encoder
        # and writes the moov atom; on slow MTK chips and over URI-
        # provider FDs that can block for seconds, freezing the touch
        # dispatcher so the user can't swipe to the next entry until
        # stop returned. Move it onto a worker, then publish the
        # validated state back to the main thread for the LIFT
        # advertise + UI refresh + playback schedule. Capture the
        # MediaRecorder + pfd handles on the main thread so a fresh
        # start_recording press while the worker is still finalising
        # the previous recording races in safely against the nulled
        # self._recorder.
        if (self._audio_path and self._record_ok
                and self.current is not None):
            # Optimistically mark dirty so a swipe that happens
            # before the worker's continuation runs still includes
            # this recording in its commit boundary. The LIFT write
            # lands shortly after via _lift_audio_write_worker; the
            # daemon's debounced commit picks it up on the next
            # boundary if it missed this one.
            self._dirty = True
        recorder = self._recorder
        pfd = self._record_pfd
        self._recorder = None
        self._record_pfd = None
        audio_path = self._audio_path
        profile_key = self._record_profile_key
        max_amp = self._max_amplitude_seen
        record_ok = self._record_ok
        entry = self.current
        guid = (entry.get('guid', '') if entry else '')
        self._record_started_at = None
        import threading
        threading.Thread(
            target=self._stop_record_worker_android,
            args=(recorder, pfd, held, audio_path, profile_key,
                  max_amp, record_ok, entry, guid),
            daemon=True, name='stop-record').start()

    def _stop_android_handles_only(self, recorder, pfd):
        """Tap-not-hold worker: stop+release the captured
        MediaRecorder + close the captured pfd off the main thread.
        No post-stop validation — the audio is junk by definition
        (held < _MIN_HOLD_SEC) and won't be advertised."""
        if recorder:
            try:
                recorder.stop()
            except Exception as ex:
                print(f'Android stop() (tap) error: {ex}')
            finally:
                try:
                    recorder.release()
                except Exception as ex:
                    print(f'Android release() (tap) error: {ex}')
        if pfd is not None:
            try:
                pfd.close()
            except Exception:
                pass
        self._clear_keep_screen_on_android()

    def _stop_record_worker_android(self, recorder, pfd, held,
                                    audio_path, profile_key, max_amp,
                                    record_ok, entry, guid):
        """Run the blocking native stop + post-stop checks off the
        main thread. Publishes back to main thread via
        _publish_stop_finish for the LIFT advertise + UI work."""
        if recorder:
            try:
                recorder.stop()
            except Exception as ex:
                print(f'Android stop() error: {ex}')
                record_ok = False
                # Stop-time wedges are the MTK HAL failure mode the
                # ladder exists for. _degrade_profile writes a
                # peer_pref (RPC) which is fine off-main.
                self._degrade_profile(profile_key)
            finally:
                try:
                    recorder.release()
                except Exception as ex:
                    print(f'Android release() error: {ex}')
        if pfd is not None:
            try:
                pfd.close()
            except Exception:
                pass
        self._clear_keep_screen_on_android()
        # Post-stop file-size validation (filesystem projects only).
        if (record_ok and audio_path
                and not self.db.is_uri and profile_key):
            try:
                size = os.path.getsize(audio_path)
            except OSError as ex:
                print(f'[record] post-stop stat failed: {ex}')
                size = 0
            profile = self._AUDIO_PROFILES.get(profile_key)
            if profile:
                expected = held * profile['bitrate'] / 8
                if size < expected * self._POST_STOP_MIN_RATIO:
                    print(f'[record] post-stop validation failed: '
                          f'{size} bytes for {held:.2f}s '
                          f'(expected ~{int(expected)})')
                    record_ok = False
                    try:
                        os.remove(audio_path)
                    except OSError:
                        pass
                    self._degrade_profile(profile_key)
        # Silent-input detection (the encoder produces a correctly-
        # sized M4A even when the mic input is silence, so the file-
        # size check above passes).
        silent = (record_ok and held > 1.0
                  and max_amp < self._SILENT_AMP_THRESHOLD)
        if silent:
            print(f'[record] silent-input detected: peak '
                  f'amplitude={max_amp} over {held:.2f}s '
                  f'(threshold {self._SILENT_AMP_THRESHOLD})',
                  file=sys.stderr, flush=True)
            record_ok = False
            if audio_path and not self.db.is_uri:
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
        Clock.schedule_once(
            lambda dt: self._publish_stop_finish(
                record_ok, audio_path, entry, guid, silent), 0)

    def _publish_stop_finish(self, record_ok, audio_path, entry,
                             guid, silent):
        """Main-thread continuation after
        _stop_record_worker_android. Updates self._record_ok,
        advertises audio onto the captured entry's dict + LIFT,
        refreshes UI, schedules playback. The captured entry is
        used (not self.current) so a swipe that landed during the
        worker still advertises against the originally-recorded
        entry."""
        self._record_ok = record_ok
        if silent:
            from kivy.app import App as _App
            _app = _App.get_running_app()
            if _app is not None:
                _app._show_toast(
                    _tr('No audio detected — another app may '
                        'have the microphone. Close any '
                        'recording / call apps and try again.'))
        if audio_path and record_ok and entry is not None:
            filename = os.path.basename(audio_path)
            entry['audio_filename'] = filename
            import threading
            threading.Thread(
                target=self._lift_audio_write_worker,
                args=(guid, filename), daemon=True,
                name='lift-audio-write').start()
        self._notify_ui()
        if record_ok and self.current is entry:
            # Playback reads the file directly (filesystem path or
            # provider URI), not LIFT, so it's safe to schedule
            # without waiting for the LIFT write to land. Only play
            # if the user hasn't swiped away — playing the previous
            # entry's audio over the freshly-loaded next entry
            # would be confusing.
            Clock.schedule_once(lambda dt: self.play_audio(), 0.5)

    def _clear_keep_screen_on_android(self):
        """Drop the FLAG_KEEP_SCREEN_ON set by _start_android_recording.
        Marshals to the Activity UI thread via run_on_ui_thread —
        callable from any thread (Kivy main, worker, etc.)."""
        try:
            from jnius import autoclass
            _WMLP = autoclass(
                'android.view.WindowManager$LayoutParams')
            PythonActivity = autoclass(
                'org.kivy.android.PythonActivity')
            window = PythonActivity.mActivity.getWindow()
            _flag = _WMLP.FLAG_KEEP_SCREEN_ON

            def _clear(w=window, f=_flag):
                try:
                    w.clearFlags(f)
                except Exception as ex:
                    print(f'[record] KEEP_SCREEN_ON clear (UI '
                          f'thread) failed: {ex}')
            try:
                from android.runnable import run_on_ui_thread
                run_on_ui_thread(_clear)()
            except ImportError:
                _clear()
        except Exception as ex:
            print(f'[record] KEEP_SCREEN_ON clear setup failed: {ex}')

    def _lift_audio_write_worker(self, guid, filename):
        """Off-main-thread LIFT write for a freshly-recorded audio
        file. Mirrors the success / failure branches the inline
        version used to do — success clears any prior pending entry;
        failure queues for auto-retry. § 17c Rule 7 (daemon-bound
        write off the main thread)."""
        try:
            self.db.set_audio(guid, filename)
        except Exception as ex:
            prev = self._pending_lift_saves.get(guid)
            if prev is not None and prev != filename:
                print(f'[record] replacing pending LIFT save '
                      f'for {guid}: {prev!r} → {filename!r}',
                      file=sys.stderr, flush=True)
            self._pending_lift_saves[guid] = filename
            self._persist_pending(guid, filename)
            print(f'[record] LIFT save failed: {ex} '
                  f'(queued for auto-retry)',
                  file=sys.stderr, flush=True)
            from kivy.app import App as _App
            _app = _App.get_running_app()
            if _app is not None:
                Clock.schedule_once(
                    lambda dt: _app._show_toast(
                        _tr('Audio captured but reference not '
                            'saved — will retry automatically.')), 0)
            return
        # Successful save clears any prior failed attempt for the
        # same entry — e.g. user retried by re-recording before
        # the auto-retry tick fired.
        if self._pending_lift_saves.pop(guid, None) is not None:
            self._unpersist_pending(guid)

    def play_audio(self):
        # Defensive: tear down any stale player from a previous tap
        # before starting a new one. The duration-based _playing reset
        # timer is best-effort; an unfired timer (or a timer whose
        # MediaPlayer never advanced past prepare) can leave the flag
        # stuck and silently swallow subsequent taps. Stopping here
        # ensures every fresh tap gets a clean MediaPlayer regardless
        # of what state the prior one was in.
        self._stop_active_player()
        e = self.current
        if not e or not e.get('audio_filename'):
            return
        filename = e['audio_filename']
        # Resolve to either a content:// URI (URI projects) or a
        # filesystem path (desktop / iOS / legacy Android). On URI
        # projects we don't probe existence — let the resolver fail
        # cleanly if the daemon hasn't materialised the file yet.
        if self.db.is_uri:
            path = self.db.audio_target(filename)
        else:
            for search_dir in (self.db.audio_dir, self.db.dir):
                candidate = os.path.join(search_dir, filename)
                if os.path.exists(candidate):
                    path = candidate
                    break
            else:
                print(f'Audio file not found: {filename}')
                return

        self._playing = True

        if platform == 'android':
            # MediaPlayer.prepare() blocks the calling thread; on
            # URI projects it calls ContentResolver.openFileDescriptor
            # under the hood, which is a daemon round-trip and can
            # stall for seconds if the daemon's project_lock is held
            # by an incoming LAN receive-pack. Run prepare on a
            # worker so the UI thread doesn't freeze; marshal start
            # back to main via Clock so widget state mutation
            # (_player, _playing timer) stays main-thread-only.
            import threading
            threading.Thread(
                target=self._play_android_worker,
                args=(path,), daemon=True,
                name='play-prepare').start()
        elif platform == 'ios':
            try:
                from pyobjus import autoclass
                from pyobjus.dylib_manager import load_framework
                load_framework('/System/Library/Frameworks/AVFoundation.framework')
                NSURL = autoclass('NSURL')
                AVAudioPlayer = autoclass('AVAudioPlayer')
                url = NSURL.fileURLWithPath_(path)
                player, _ = AVAudioPlayer.alloc().initWithContentsOfURL_error_(url, None)
                player.play()
                self._player = player  # keep reference alive
                # Estimate duration and clear flag
                duration = player.duration
                if duration > 0:
                    Clock.schedule_once(lambda dt: setattr(self, '_playing', False), duration + 0.1)
                else:
                    self._playing = False
            except Exception as ex:
                self._playing = False
                print(f'iOS play error: {ex}')
        else:
            try:
                from kivy.core.audio import SoundLoader
                # Stop any previous playback
                if getattr(self, '_sound', None):
                    try:
                        self._sound.stop()
                        self._sound.unload()
                    except Exception:
                        pass
                    self._sound = None
                sound = SoundLoader.load(path)
                if sound:
                    self._sound = sound  # must persist — GC stops SDL2 stream
                    sound.bind(on_stop=lambda *a: setattr(self, '_playing', False))
                    if sound.state == 'stop' and sound.length > 0:
                        sound.play()
                    else:
                        sound.bind(on_load=lambda *a: sound.play())
                        Clock.schedule_once(lambda dt: sound.play()
                            if sound.state == 'stop' else None, 0.3)
                else:
                    self._playing = False
                    print(f'SoundLoader could not load: {path}')
            except Exception as ex:
                self._playing = False
                print(f'Desktop play error: {ex}')

    def _play_android_worker(self, path):
        """Off-main-thread MediaPlayer setup. setDataSource + prepare
        block on ContentResolver.openFileDescriptor for URI projects;
        under LAN-merge lock contention this can stall for seconds.
        Worker does the prep; start + lifecycle bookkeeping marshal
        to the main thread via Clock so Kivy widget state stays
        single-threaded."""
        try:
            from jnius import autoclass
            MediaPlayer = autoclass('android.media.MediaPlayer')
            mp = MediaPlayer()
            if self.db.is_uri:
                Uri = autoclass('android.net.Uri')
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                mp.setDataSource(
                    PythonActivity.mActivity, Uri.parse(path))
            else:
                mp.setDataSource(path)
            mp.prepare()
        except Exception as ex:
            self._playing = False
            print(f'Android play prepare error: {ex}')
            return

        def _start_on_main(dt):
            try:
                mp.start()
                self._player = mp
                duration_ms = mp.getDuration()
                if duration_ms > 0:
                    Clock.schedule_once(
                        lambda dt: setattr(self, '_playing', False),
                        duration_ms / 1000.0 + 0.1)
                else:
                    Clock.schedule_once(
                        lambda dt: setattr(self, '_playing', False), 2.0)
            except Exception as ex:
                self._playing = False
                print(f'Android play start error: {ex}')

        Clock.schedule_once(_start_on_main, 0)

    def clear_audio(self):
        """Mark entry for re-recording without deleting the existing file."""
        if not self.current:
            return
        self._pending_rerecord = True
        self._notify_ui()

    def _stop_active_player(self):
        """Release any in-flight MediaPlayer / AVAudioPlayer / SDL2
        Sound so a fresh play_audio call starts from a known state.
        Idempotent; safe to call when nothing is playing."""
        player = getattr(self, '_player', None)
        if player is not None:
            try:
                # Android MediaPlayer + iOS AVAudioPlayer both have
                # stop()/release()-or-equivalent; either ignore
                # exceptions (stale handles, already-released).
                if hasattr(player, 'release'):
                    try:
                        player.stop()
                    except Exception:
                        pass
                    player.release()
                elif hasattr(player, 'stop'):
                    player.stop()
            except Exception:
                pass
            self._player = None
        sound = getattr(self, '_sound', None)
        if sound is not None:
            try:
                sound.stop()
            except Exception:
                pass
            try:
                sound.unload()
            except Exception:
                pass
            self._sound = None
        self._playing = False

    def _make_audio_path(self):
        e = self.current
        # Filename: {cawl}_{guid}_{en_gloss}.wav (extension is replaced
        # by the platform-specific recorder: .m4a on Android, .flac on
        # iOS, .wav on desktop).
        cawl = e.get('cawl', '0000')
        guid = e.get('guid', 'unknown')[:8]
        gloss = e.get('glosses', {}).get('en', [''])[0]
        safe_gloss = ''.join(c if c.isalnum() or c in '_ ' else '_' for c in gloss)[:24].strip().replace(' ', '_')
        filename = f'{cawl}_{guid}_{safe_gloss}.wav'
        if not self.db.is_uri:
            os.makedirs(self.db.audio_dir, exist_ok=True)
        return self.db.audio_target(filename)

    def _start_native_recording(self, path):
        """Returns the actual on-disk path/URI used (extension may
        differ from ``path`` per platform). Raises on failure so
        ``start_recording`` can keep recorder state in sync."""
        if platform == 'android':
            return self._start_android_recording(path)
        if platform == 'ios':
            return self._start_ios_recording(path)
        return self._start_desktop_recording(path)

    def _stop_native_recording(self):
        if platform == 'android':
            self._stop_android_recording()
        elif platform == 'ios':
            self._stop_ios_recording()
        else:
            self._stop_desktop_recording()

    # Android: use MediaRecorder via pyjnius for maximum quality (PCM WAV)
    def _start_android_recording(self, path):
        from jnius import autoclass
        MediaRecorder = autoclass('android.media.MediaRecorder')
        AudioSource = autoclass('android.media.MediaRecorder$AudioSource')
        OutputFormat = autoclass('android.media.MediaRecorder$OutputFormat')
        AudioEncoder = autoclass('android.media.MediaRecorder$AudioEncoder')

        # Resolve the audio-quality profile up front — both the
        # container (MPEG_4 → .m4a, AAC_ADTS → .aac) and the
        # bitrate/sample-rate flow from here. See _AUDIO_PROFILES
        # for the full table.
        prof_key = (peer_pref('audio_quality')
                    or self._DEFAULT_AUDIO_PROFILE)
        # Clamp to the auto-degradation ceiling if one has been pinned
        # by a past failure on this device. The user can still pick a
        # higher value in the picker, but the ceiling wins here — that
        # request was for a profile this device already proved it
        # can't handle.
        ceiling = peer_pref('audio_quality_ceiling')
        if ceiling and ceiling in self._PROFILE_LADDER \
                and prof_key in self._PROFILE_LADDER:
            if (self._PROFILE_LADDER.index(prof_key)
                    < self._PROFILE_LADDER.index(ceiling)):
                print(f'[record] ceiling clamp: {prof_key} → {ceiling}')
                prof_key = ceiling
        # Stash for the failure handlers so they know what to degrade.
        self._record_profile_key = prof_key
        profile = self._AUDIO_PROFILES.get(
            prof_key,
            self._AUDIO_PROFILES[self._DEFAULT_AUDIO_PROFILE])
        print(f'[record] audio profile: {prof_key} '
              f'(bitrate={profile["bitrate"]} '
              f'sample_rate={profile["sample_rate"]} '
              f'container={profile["output_format"]})')
        # Path-extension swap follows the profile: .m4a for MPEG_4,
        # .aac for AAC_ADTS. Basename sits at the end of both a
        # filesystem path and a content:// URI, so str.replace works.
        ext = profile['ext']
        aac_path = path.replace('.wav', ext)
        mr = MediaRecorder()
        pfd = None
        try:
            mr.setAudioSource(AudioSource.MIC)
            mr.setOutputFormat(
                getattr(OutputFormat, profile['output_format']))
            if self.db.is_uri:
                # ContentResolver-acquired ParcelFileDescriptor: hand
                # the underlying Java FileDescriptor straight to
                # MediaRecorder. We must NOT detachFd() here — the pfd
                # owns the fd lifetime and we close it after release.
                Uri = autoclass('android.net.Uri')
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                ctx = PythonActivity.mActivity
                resolver = ctx.getContentResolver()
                pfd = resolver.openFileDescriptor(Uri.parse(aac_path), 'w')
                if pfd is None:
                    raise IOError(
                        f'openFileDescriptor returned null for {aac_path!r}')
                mr.setOutputFile(pfd.getFileDescriptor())
            else:
                mr.setOutputFile(aac_path)
            mr.setAudioEncoder(AudioEncoder.AAC)
            mr.setAudioEncodingBitRate(profile['bitrate'])
            mr.setAudioSamplingRate(profile['sample_rate'])
            mr.setAudioChannels(1)
            # 60-second ceiling per recording. Bounds the worst-case
            # encoder memory footprint and gives the user a natural
            # "max duration" stopping point on a held button.
            mr.setMaxDuration(60_000)
            mr.prepare()
            mr.start()
            # Keep the screen on while recording — a long hold with
            # no other touch events looks like idle to aggressive
            # OEM power-management (HiOS, MIUI, etc.); the
            # WindowManager flag pins the activity foreground so the
            # MediaRecorder surface doesn't get frozen mid-capture.
            #
            # window.addFlags mutates the view hierarchy, which
            # Android only permits from the Activity UI thread
            # (SDLActivity on p4a). Clock.schedule_once marshals to
            # the *Kivy* main thread (SDLThread) — a different
            # thread that still raises CalledFromWrongThread-
            # Exception when it touches Window. Use p4a's
            # android.runnable.run_on_ui_thread to dispatch to the
            # actual UI thread. Pre-resolving LayoutParams /
            # Activity / Window here keeps the UI-thread closure
            # to two getters + one call.
            try:
                _WMLP = autoclass(
                    'android.view.WindowManager$LayoutParams')
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                _window = PythonActivity.mActivity.getWindow()
                # FLAG_KEEP_SCREEN_ON = 0x80; using the constant via
                # autoclass instead of the magic number so future
                # API renames don't silently no-op.
                _flag = _WMLP.FLAG_KEEP_SCREEN_ON

                def _add_keep_screen_on(w=_window, f=_flag):
                    try:
                        w.addFlags(f)
                    except Exception as ex:
                        print(f'[record] KEEP_SCREEN_ON add (UI '
                              f'thread) failed: {ex}')
                try:
                    from android.runnable import run_on_ui_thread
                    run_on_ui_thread(_add_keep_screen_on)()
                except ImportError:
                    # Non-p4a environment shouldn't reach here
                    # (the outer block is Android-only), but fall
                    # back to a direct call for safety.
                    _add_keep_screen_on()
            except Exception as ex:
                # Best-effort; never fail a record because the
                # power-management hint didn't take.
                print(f'[record] KEEP_SCREEN_ON setup failed: {ex}')
        except Exception:
            try:
                mr.release()
            except Exception:
                pass
            if pfd is not None:
                try:
                    pfd.close()
                except Exception:
                    pass
            raise
        self._recorder = mr
        self._record_pfd = pfd
        return aac_path

    def _stop_android_recording(self):
        if self._recorder:
            try:
                self._recorder.stop()
            except Exception as ex:
                # IllegalStateException here typically means the M4A
                # has no moov atom — file is unplayable (tap-not-hold,
                # immediate stop after start, etc.). Don't let
                # stop_recording advertise the basename.
                print(f'Android stop() error: {ex}')
                self._record_ok = False
                # Stop-time wedges are exactly the MTK HAL failure
                # mode the ladder exists for. Degrade so the next
                # attempt avoids the same profile.
                self._degrade_profile(self._record_profile_key)
            finally:
                # release() is the cleanup pair to start() — it must
                # run regardless of whether stop() threw. A wedged
                # MediaRecorder left un-released holds the mic + a
                # native buffer until GC; on slow MTK chips this has
                # been observed to wedge subsequent record attempts.
                # release() can itself throw on a sufficiently bad
                # state — defend so the exception doesn't propagate
                # up and mask whatever stop() raised.
                try:
                    self._recorder.release()
                except Exception as ex:
                    print(f'Android release() error: {ex}')
                self._recorder = None
        pfd = self._record_pfd
        if pfd is not None:
            try:
                pfd.close()
            except Exception:
                pass
            self._record_pfd = None
        # Release the KEEP_SCREEN_ON flag set in _start_android_recording.
        # Must run on the Activity UI thread (SDLActivity), not the Kivy
        # main thread (SDLThread) — Clock.schedule_once routes to the
        # latter, which still raises CalledFromWrongThreadException on
        # window.clearFlags. Use p4a's android.runnable.run_on_ui_thread
        # to marshal to the real UI thread. No-op if start failed before
        # addFlags ran.
        try:
            from jnius import autoclass
            _WMLP = autoclass('android.view.WindowManager$LayoutParams')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            window = PythonActivity.mActivity.getWindow()
            _flag = _WMLP.FLAG_KEEP_SCREEN_ON

            def _clear_keep_screen_on(w=window, f=_flag):
                try:
                    w.clearFlags(f)
                except Exception as ex:
                    print(f'[record] KEEP_SCREEN_ON clear (UI '
                          f'thread) failed: {ex}')
            try:
                from android.runnable import run_on_ui_thread
                run_on_ui_thread(_clear_keep_screen_on)()
            except ImportError:
                _clear_keep_screen_on()
        except Exception as ex:
            print(f'[record] KEEP_SCREEN_ON clear setup failed: {ex}')

    # iOS: use AVAudioRecorder via pyobjus for maximum quality
    def _start_ios_recording(self, path):
        from pyobjus import autoclass, objc_dict
        from pyobjus.dylib_manager import load_framework
        load_framework('/System/Library/Frameworks/AVFoundation.framework')
        NSURL = autoclass('NSURL')
        AVAudioRecorder = autoclass('AVAudioRecorder')
        AVFormatIDKey = 'AVFormatIDKey'
        AVSampleRateKey = 'AVSampleRateKey'
        AVNumberOfChannelsKey = 'AVNumberOfChannelsKey'
        AVEncoderAudioQualityKey = 'AVEncoderAudioQualityKey'
        AVAudioQualityMax = 127

        # Record as FLAC for lossless quality
        flac_path = path.replace('.wav', '.flac')
        url = NSURL.fileURLWithPath_(flac_path)
        settings = objc_dict({
            AVFormatIDKey: 1718378851,  # kAudioFormatFLAC
            AVSampleRateKey: 48000.0,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQualityMax,
        })
        recorder, err = AVAudioRecorder.alloc().initWithURL_settings_error_(
            url, settings, None)
        if recorder is None:
            raise RuntimeError(f'AVAudioRecorder init failed: {err}')
        if not recorder.record():
            raise RuntimeError('AVAudioRecorder.record() returned NO')
        self._recorder = recorder
        return flac_path

    def _stop_ios_recording(self):
        if self._recorder:
            try:
                self._recorder.stop()
            except Exception as ex:
                print(f'iOS stop error: {ex}')
                self._record_ok = False
            finally:
                self._recorder = None

    # Desktop fallback: sounddevice → WAV (for development/testing)
    def _start_desktop_recording(self, path):
        import sounddevice as sd
        self._desktop_frames = []
        self._desktop_samplerate = 48000

        def callback(indata, frames, time, status):
            self._desktop_frames.append(indata.copy())

        stream = sd.InputStream(
            samplerate=self._desktop_samplerate,
            channels=1,
            dtype='int16',
            callback=callback,
        )
        try:
            stream.start()
        except Exception:
            try:
                stream.close()
            except Exception:
                pass
            raise
        self._desktop_stream = stream
        return path

    def _stop_desktop_recording(self):
        try:
            import soundfile as sf
            import numpy as np
            self._desktop_stream.stop()
            self._desktop_stream.close()
            if not self._desktop_frames:
                # No samples were captured (start raced past stop, or
                # the input device produced nothing). Don't write a
                # zero-byte file and don't advertise a basename.
                self._record_ok = False
                return
            data = np.concatenate(self._desktop_frames, axis=0)
            sf.write(self._audio_path, data, self._desktop_samplerate,
                     subtype='PCM_16')
        except Exception as ex:
            print(f'Desktop stop error: {ex}')
            self._record_ok = False

    # ── UI notification ────────────────────────────────────────────────────────

    def _notify_ui(self):
        """Tell the running app to refresh UI from current state."""
        app = App.get_running_app()
        if app:
            Clock.schedule_once(lambda dt: app.refresh_recorder_ui(), 0)

    # ── Pending-LIFT-save persistence + retry ──────────────────────────────────

    _PENDING_PREF_KEY = 'pending_lift_saves'

    def _load_persisted_pending(self):
        """Restore stranded-save entries persisted for this project's
        langcode. Empty dict if no langcode (peer_pref store unavailable)
        or no entries recorded for this langcode."""
        if not self._langcode:
            return {}
        store = peer_pref(self._PENDING_PREF_KEY, {}) or {}
        return dict(store.get(self._langcode, {}))

    def _persist_pending(self, guid, filename):
        """Record (guid → filename) for this project's langcode in
        peer_pref. Reads-modifies-writes the cross-project store so
        other projects' queues are preserved."""
        if not self._langcode:
            return
        store = peer_pref(self._PENDING_PREF_KEY, {}) or {}
        store.setdefault(self._langcode, {})[guid] = filename
        set_peer_pref(self._PENDING_PREF_KEY, store)

    def _unpersist_pending(self, guid):
        """Remove (guid) from the persisted queue for this project's
        langcode. Cleans up the langcode bucket entirely if empty, and
        the whole key if no buckets remain."""
        if not self._langcode:
            return
        store = peer_pref(self._PENDING_PREF_KEY, {}) or {}
        bucket = store.get(self._langcode)
        if not bucket:
            return
        bucket.pop(guid, None)
        if not bucket:
            store.pop(self._langcode, None)
        if store:
            set_peer_pref(self._PENDING_PREF_KEY, store)
        else:
            set_peer_pref(self._PENDING_PREF_KEY, None)

    def retry_pending_lift_saves(self):
        """Retry any LIFT saves stranded by a transient daemon
        hiccup. Driven from the App's 10 s _update_sync_status
        tick. Idempotent: set_audio against an entry that
        already carries the reference is a no-op (the inner
        ``_find_entry`` / ``find('form')`` re-resolve to the
        same nodes), so a retry that races a recovered first
        attempt costs nothing.

        Failures stay in the queue for the next tick. On
        success for the currently-displayed entry, mirror the
        write back onto ``self.current`` and schedule a UI
        refresh so playback works without a re-load."""
        if not self._pending_lift_saves:
            return
        current_guid = (self.current.get('guid', '')
                        if getattr(self, 'current', None) else '')
        for guid in list(self._pending_lift_saves.keys()):
            filename = self._pending_lift_saves[guid]
            try:
                self.db.set_audio(guid, filename)
            except Exception as ex:
                print(f'[record] retry LIFT save for {guid} still '
                      f'failing: {ex}', file=sys.stderr, flush=True)
                continue
            self._pending_lift_saves.pop(guid, None)
            self._unpersist_pending(guid)
            print(f'[record] retry LIFT save for {guid} succeeded',
                  file=sys.stderr, flush=True)
            if guid == current_guid:
                self.current['audio_filename'] = filename
                self._dirty = True
                Clock.schedule_once(lambda dt: self._notify_ui(), 0)


# ── Main App ───────────────────────────────────────────────────────────────────

__version__ = '1.55.17'


class LIFTRecorderApp(App):
    title = APP_NAME
    subtitle = StringProperty(_tr(APP_TAGLINE))
    icon = APP_ICON
    # ConfigScreen top bar shows what THIS APK is shipping: the
    # recorder app version plus the azt_collab_client snapshot baked
    # in at build time. The daemon settings UI shows its own APK's
    # equivalents (server APK's daemon + its bundled client); cross-
    # comparison is by user inspection of both screens. We don't
    # round-trip to the daemon for version info here.
    version_string = StringProperty(
        f'v{__version__} · '
        f'client {azt_collab_client.__version__}')
    # BooleanProperty so KV bindings (the gear button's disabled
    # / opacity hooks) react when the project loads. Set True at
    # the end of ``load_lift``; flipped False if the recorder
    # ever clears (project switch in progress, etc.). Keeping the
    # gear off until True prevents the user from reaching the
    # Settings page in the no-project state — and dodges the
    # _hide_box_tree path entirely.
    has_project = BooleanProperty(False)
    recorder: RecorderController = None
    config_screen: ConfigScreen = None
    # App-level cache of the last successfully-applied
    # split state. Survives RecorderController swaps (which
    # happen on every _reload_and_restore — ContentObserver
    # wakeup, sync result, etc.) so the transient-zero guard
    # in _populate_split_state_apply has a real "previous"
    # value even when a fresh controller has split_team_size=0.
    # Reset in load_lift so a new project starts from blank.
    _last_known_team_size = 0
    _last_known_my_slot = ''

    # Expose _() to KV templates as app._()
    @staticmethod
    def _tr(msg):
        return _tr(msg)

    # ── Preferences ──────────────────────────────────────────────────────────
    # Live peer-shared UI prefs (theme, show_past_work, rec_task,
    # cawl_filter, gloss_langs) live in $AZT_HOME/config.json via
    # azt_collab_client.peer_pref / set_peer_pref. The committer name
    # lives there too via get_contributor / set_contributor.
    # Daemon-owned state (project langcode, last_project, credentials,
    # CAWL image_repo) is read on demand from the daemon — no peer-side
    # mirror; see the no-daemon-owned-caches rule. The legacy peer-
    # private prefs.json under user_data_dir is kept readable for one-
    # shot migration only — see _migrate_prefs_to_suite_store.

    @property
    def _prefs_path(self):
        return os.path.join(self.user_data_dir, 'prefs.json')

    def _load_legacy_prefs(self):
        import json
        try:
            with open(self._prefs_path) as f:
                return json.load(f) or {}
        except (FileNotFoundError, ValueError):
            return {}
        except Exception as ex:
            print(f'Legacy prefs read error: {ex}')
            return {}

    def _write_legacy_prefs(self, prefs):
        """Write back to prefs.json — called only by the migration
        path to drain keys after they've moved to the suite store."""
        import json
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            tmp = self._prefs_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(prefs, f)
            os.replace(tmp, self._prefs_path)
        except Exception as ex:
            print(f'Legacy prefs write error: {ex}')

    def _migrate_prefs_to_suite_store(self):
        """Drain the legacy peer-private prefs.json into $AZT_HOME/config.json.
        Local-file work only — the post-call ``peer_pref('theme', …)``
        read depends on this completing first, so it stays on the main
        thread. Daemon-touching legacy keys (``collab_name`` → daemon
        contributor) are stashed in ``self._pending_legacy_contributor``
        and migrated on the startup worker per
        CLIENT_INTEGRATION.md § 17c Rule 7.

        Idempotent: keys already present in the suite store are left
        alone (a sister app that ran first wins). After drain, the
        keys are popped from prefs.json so subsequent launches do
        nothing."""
        self._pending_legacy_contributor = None
        legacy = self._load_legacy_prefs()
        if not legacy:
            return
        moved = []
        # ui_language and last_lift were drained in 1.30.0 / 1.33.0; if
        # any old install still has them, finish the job here.
        legacy_lang = legacy.pop('ui_language', None)
        if legacy_lang:
            set_language(legacy_lang)
            moved.append('ui_language')
        if legacy.pop('last_lift', None) is not None:
            moved.append('last_lift (dropped)')
        # vernlang, collab_langcode, and image_repo were peer-side
        # caches of daemon-owned data; 1.41.3 dropped all three.
        if legacy.pop('vernlang', None) is not None:
            moved.append('vernlang (dropped)')
        if legacy.pop('collab_langcode', None) is not None:
            moved.append('collab_langcode (dropped)')
        if legacy.pop('image_repo', None) is not None:
            moved.append('image_repo (dropped)')
        # Committer name has a dedicated suite endpoint. Stash the
        # value here (local-only pop); the worker calls the daemon.
        if 'collab_name' in legacy:
            value = legacy.pop('collab_name')
            if value:
                self._pending_legacy_contributor = value
            moved.append('collab_name -> contributor (deferred)')
        # Generic peer-shared keys.
        for key in ('theme', 'show_past_work', 'rec_task'):
            if key in legacy:
                value = legacy.pop(key)
                if peer_pref(key) is None and value not in ('', None):
                    set_peer_pref(key, value)
                moved.append(key)
        # Drop any vernlang / collab_langcode / image_repo entries
        # that an earlier release wrote into the suite store. The keys
        # are no longer consulted; clearing them keeps stale values
        # from confusing an inspector reading config.json.
        for stale in ('vernlang', 'collab_langcode', 'image_repo'):
            if peer_pref(stale) is not None:
                set_peer_pref(stale, None)
                moved.append(f'{stale} (suite-store drop)')
        if moved:
            self._write_legacy_prefs(legacy)
            print(f'[migrate] prefs.json -> $AZT_HOME/config.json: {moved}')

    def _startup_daemon_migrations_worker(self):
        """Daemon-touching half of the startup migrations, spawned
        from ``build`` on a worker thread per CLIENT_INTEGRATION.md
        § 17c Rule 7. Two responsibilities:

        - Promote any legacy ``collab_name`` value stashed by
          ``_migrate_prefs_to_suite_store`` into the daemon's
          contributor slot (only if the daemon currently has no
          contributor — a sister app that ran first wins).
        - Drain legacy credential keys out of prefs.json into
          ``$AZT_HOME/credentials.json`` via
          ``azt_collab_client.migrate_from_prefs``.

        Both calls are idempotent: a second invocation after the
        legacy keys have been drained is a cheap no-op. Failures
        are logged but don't propagate — startup must not block on
        a daemon mid-restart."""
        legacy_contributor = getattr(
            self, '_pending_legacy_contributor', None)
        if legacy_contributor:
            try:
                from azt_collab_client import (
                    get_contributor, set_contributor)
                if not get_contributor():
                    set_contributor(legacy_contributor)
                    print(f'[migrate] collab_name -> contributor: '
                          f'{legacy_contributor!r}')
            except Exception as ex:
                print(f'[migrate] collab_name -> contributor failed: '
                      f'{ex}')
            self._pending_legacy_contributor = None
        try:
            from azt_collab_client import migrate_from_prefs
            summary = migrate_from_prefs(self._prefs_path)
            if summary.get('migrated'):
                print(f'[migrate] credentials: {summary}')
        except Exception as ex:
            print(f'[migrate] credentials: {ex}')

    # ── App lifecycle ─────────────────────────────────────────────────────────

    def build(self):
        try:
            # Detect device RAM tier once at startup. Used to gate
            # prewarm and to size the Kivy image cache (#3 + #5 of
            # the "be eager when you have room to" policy). Float
            # MB; 0 on non-Android (treated as "plenty"). Cached on
            # the app so other heuristics can read it.
            self._total_ram_mb = self._sample_total_ram_mb()
            # #3 Kivy image cache size — pick a limit proportional
            # to device memory. Defaults are Kivy's (large); on tight
            # devices we cap to ~10 decoded images, which is plenty
            # for one-at-a-time entry display without keeping a
            # large LRU of textures alive.
            try:
                from kivy.cache import Cache
                if 0 < self._total_ram_mb <= 3072:
                    Cache.register(
                        'kv.loader', limit=10, timeout=60)
                    print(f'[low-power] image cache limit=10 '
                          f'(total RAM {self._total_ram_mb} MB)')
                elif 0 < self._total_ram_mb <= 6144:
                    Cache.register(
                        'kv.loader', limit=25, timeout=120)
            except Exception as ex:
                print(f'[low-power] image cache size set failed: {ex}')
            # #5 Boot prewarm — auto-skip on tight-RAM devices.
            # Prewarm overlaps daemon spawn with Kivy init but burns
            # ~30MB of native memory during the splash; on a 4GB
            # device under zram pressure that's the worst possible
            # window. Threshold: skip if total RAM ≤ 3GB.
            # Toggleable for measurement runs via
            # $AZT_HOME/_no_prewarm sentinel or AZT_BOOT_PREWARM=0
            # (handled inside prewarm() itself).
            try:
                if 0 < self._total_ram_mb <= 3072:
                    print(f'[prewarm] skipped — low-RAM device '
                          f'({self._total_ram_mb} MB)')
                else:
                    from azt_collab_client.ui.bootstrap import prewarm
                    prewarm()
            except Exception as ex:
                print(f'[prewarm] {ex}', file=sys.stderr)
            # Drain the legacy peer-private prefs.json into the
            # suite-wide $AZT_HOME/config.json. Local-file work only;
            # apply *before* reading the theme below so a fresh
            # upgrade still finds the user's choice. Daemon-touching
            # parts (collab_name → contributor; migrate_from_prefs for
            # credentials) are spawned on the startup worker below
            # per CLIENT_INTEGRATION.md § 17c Rule 7.
            self._migrate_prefs_to_suite_store()
            theme.set_theme(peer_pref('theme', 'Ocean') or 'Ocean')
            self.subtitle = _tr(APP_TAGLINE)
            Builder.load_string(KV)
            self.root = RootScreen()
            # Daemon-touching startup migrations off the main thread.
            # Both calls (set_contributor if legacy collab_name was
            # stashed; migrate_from_prefs for legacy credentials) are
            # idempotent — re-entering after the legacy keys are
            # drained is a cheap no-op. § 17c Rule 7.
            import threading
            threading.Thread(
                target=self._startup_daemon_migrations_worker,
                daemon=True,
                name='startup-migrations').start()
            return self.root
        except Exception:
            traceback.print_exc()
            raise

    def on_start(self):
        try:
            sm = self.root.ids.sm
            self.config_screen = sm.get_screen('config')
            # CAWL Stage 2: per-session tmp dir for image pull-throughs
            # (durable cache moved to the daemon); one-shot purge of any
            # leftover pre-1.41.4 durable cache at user_data_dir/image_cache.
            self._init_session_image_cache()
            self._purge_legacy_image_cache()
            # Bind Android activity result listener
            if platform == 'android':
                try:
                    from android import activity
                    activity.bind(on_activity_result=self._on_activity_result_wrapper)
                    print('[app] activity result listener bound')
                except Exception as ex:
                    print(f'[app] failed to bind activity result: {ex}')
                    traceback.print_exc()
                # Force first-time autoclass lookups onto the main
                # thread before any worker thread can race them. See
                # _warm_jnius_classes.
                self._warm_jnius_classes()
                # Diagnostic: log which presplash variant Android
                # picked. Useful for confirming the multi-density
                # add_resources wiring landed in the APK and that
                # the device's DPI bucket maps to a real variant
                # rather than the .jpg fallback. Shared helper per
                # CLIENT_INTEGRATION.md § "Diagnostic logging —
                # verify which bucket landed".
                from azt_collab_client.lowpower import (
                    log_presplash_variant)
                log_presplash_variant(tag='presplash')
            # The AZTCollabProvider lives in the standalone server APK
            # (org.atoznback.aztcollab). Peers do NOT install provider
            # callbacks here — they reach the provider through the
            # azt_collab_client transport instead.
            # Handle Android back button / ESC key
            Window.bind(on_keyboard=self._on_back_button)
            # #6 Touch-time tracking for prefetch-poll throttling.
            # While the user is actively swiping/tapping, the
            # cache-status poll (1Hz during prefetch) is wasted —
            # the user wouldn't see indicator changes mid-gesture
            # anyway. _tick_cache_status reads _last_touch_time and
            # skips a tick if it's < 1s old.
            self._last_touch_time = 0.0
            Window.bind(on_touch_down=self._on_touch_record)
            # One-shot install/update workflow. bootstrap() runs the
            # server-APK install/update prompts and the recorder's own
            # self-update probe in sequence; on Android only, no-op
            # everywhere else. Schedule for next frame so the UI is
            # up before any popup. See azt_collab_client/CLAUDE.md
            # § Bootstrap and azt_collab_client/ui/bootstrap.py.
            #
            # Auto-load runs as bootstrap's on_done — client 0.28.5+
            # only fires on_done when the daemon is reachable, so
            # the first RPC (last_project) needs no defensive
            # try/except; if on_done didn't fire, bootstrap is still
            # parked on its own popup and the user hasn't yet given
            # us a daemon to talk to.
            Clock.schedule_once(lambda dt: self._run_bootstrap(), 0)
            # Shared decisions watcher (CLIENT_INTEGRATION.md § 20a).
            # The single canonical surface for inbound LAN decisions
            # (share offers, pair requests, adopt-origin, remote-
            # conflict). Hard rule #1: "install exactly once at
            # startup." Idempotent — safe even if Kivy fires on_start
            # twice. on_resolved is informational only; the recorder
            # has no project-list or peer-roster UI to refresh, and
            # the contract forbids auto-loading a newly-received
            # project (hard rule #4).
            from azt_collab_client.ui import install_decision_watcher
            install_decision_watcher(
                on_resolved=self._on_decision_resolved)
            # Periodically refresh the last-sync indicator so background
            # debounced commits (commit_project from swipes) and the
            # daemon's scheduler-drain pushes become visible
            # without waiting for the next manual sync. project_status
            # is a single GET against the daemon — cheap. Retained so
            # on_pause / on_resume can suspend the polling while the
            # app is backgrounded (#4 of the low-power policy).
            # § 17c Rule 4 — 5-15 s is the right range for the active
            # project's sync badge; 10 s keeps the daemon-driven push
            # visible promptly without burning RPCs.
            self._sync_status_event = Clock.schedule_interval(
                lambda dt: self._update_sync_status(), 10)
        except Exception:
            traceback.print_exc()
            raise

    # ── Android lifecycle ────────────────────────────────────────────────────
    # Without on_pause+on_resume, Kivy treats backgrounding as on_stop and the
    # OS may kill us before MediaRecorder.stop() runs. That leaves the M4A
    # without a moov atom — the file exists but is unplayable on next launch.
    # Returning True from on_pause is the documented contract that says "I
    # want to be paused, not stopped"; defining on_resume is required when
    # on_pause is defined or Kivy still routes the event as on_stop.

    def on_pause(self):
        self._finalise_active_recording('on_pause')
        # #4: suspend the sync-status poll while backgrounded.
        # No reason to round-trip the daemon every 10s when the
        # user can't see the result. Resumed in on_resume.
        ev = getattr(self, '_sync_status_event', None)
        if ev:
            try:
                ev.cancel()
            except Exception:
                pass
            self._sync_status_event = None
        # § 17b "subscribe when foregrounded" — drop the
        # ContentObserver subscription while paused; re-subscribed
        # in on_resume.
        self._unsubscribe_project_changes()
        return True

    def on_resume(self):
        # Project-switch reconciliation per CLIENT_INTEGRATION.md
        # § 14a. While we were paused the user may have switched
        # projects via the daemon settings UI's "Switch project"
        # button (or any other path landing in
        # daemon.set_last_langcode). last_project() is the daemon-
        # owned source of truth; if it differs from our in-memory
        # _current_langcode, reload before re-arming any polls so
        # the sync-status indicator that fires next tick reflects
        # the new project, not the old one.
        try:
            from azt_collab_client import last_project
            server_langcode = (last_project() or '').strip()
        except Exception as ex:
            # Transport failure — leave current view alone per
            # contract. Daemon could be down or a URI grant stale.
            print(f'[on_resume] last_project() failed: {ex}')
            server_langcode = ''
        if server_langcode:
            peer_langcode = (
                getattr(self, '_current_langcode', '') or '').strip()
            if server_langcode != peer_langcode:
                print(f'[on_resume] project switched externally: '
                      f'peer={peer_langcode!r} → '
                      f'server={server_langcode!r}; reloading')
                # _auto_load_last_project pulls last_project() itself
                # and runs the full open_project → load_lift path —
                # the same code path the initial load uses, which is
                # what the contract requires ("reuse that path").
                self._auto_load_last_project()
        # Re-arm the sync-status poll suspended in on_pause.
        # Idempotent — if we were never paused, the event will
        # have stayed alive and we skip.
        if not getattr(self, '_sync_status_event', None):
            # § 17c Rule 4 — match the build-time cadence (10 s).
            self._sync_status_event = Clock.schedule_interval(
                lambda dt: self._update_sync_status(), 10)
            # Immediate refresh so the user sees a fresh state on
            # foreground rather than waiting up to 10s.
            Clock.schedule_once(
                lambda dt: self._update_sync_status(), 0)
        # § 17b — re-subscribe to ContentObserver wakeups for the
        # foreground project. Idempotent: the helper tears down
        # any prior token before registering a new one.
        if getattr(self, '_current_langcode', ''):
            self._subscribe_project_changes()
        return True

    def on_stop(self):
        self._finalise_active_recording('on_stop')
        # Release the ContentObserver subscription before the
        # process exits. § 17b notes the leak is non-catastrophic
        # (observers are cheap), but unsubscribe is the disciplined
        # shape.
        self._unsubscribe_project_changes()
        # Stop the cache-progress polling if it's still running.
        event = getattr(self, '_cache_status_event', None)
        if event:
            try:
                event.cancel()
            except Exception:
                pass
            self._cache_status_event = None
        # Best-effort cleanup of the per-session CAWL tmp dir. If the
        # process is killed before this runs (typical on Android),
        # the OS reclaims tempfile.gettempdir() eventually; not a
        # durability concern since nothing here is canonical.
        tmp_dir = getattr(self, '_session_image_cache_dir', '')
        if tmp_dir and os.path.isdir(tmp_dir):
            import shutil
            try:
                shutil.rmtree(tmp_dir)
            except Exception as ex:
                print(f'[on_stop] tmp cleanup skipped: {ex}')

    def _finalise_active_recording(self, where):
        if self.recorder and getattr(self.recorder, '_recording', False):
            try:
                self.recorder.stop_recording()
            except Exception as ex:
                print(f'[{where}] stop_recording failed: {ex}')

    def _warm_jnius_classes(self):
        """Force first-time autoclass lookups onto the Kivy main thread so
        worker threads (image-download, picker callback, sync) don't race
        the documented PyJNI ClassLoader-scope hazard
        (memory: feedback_pyjnius_threading.md). Once autoclass has cached
        the proxy on the main thread, worker-thread lookups are safe."""
        if platform != 'android':
            return
        try:
            from jnius import autoclass
        except Exception:
            return
        for cls in (
                'android.app.ActivityManager',
                'android.app.ActivityManager$MemoryInfo',
                'android.content.Context',
                'android.content.Intent',
                'android.media.MediaPlayer',
                'android.media.MediaRecorder',
                'android.media.MediaRecorder$AudioEncoder',
                'android.media.MediaRecorder$AudioSource',
                'android.media.MediaRecorder$OutputFormat',
                'android.net.ConnectivityManager',
                'android.net.Uri',
                'android.os.ParcelFileDescriptor',
                'android.view.WindowManager$LayoutParams',
                'java.io.ByteArrayOutputStream',
                'java.io.FileOutputStream',
                'org.kivy.android.PythonActivity',
        ):
            try:
                autoclass(cls)
            except Exception as ex:
                print(f'[jnius warm] {cls}: {ex}')

    def _auto_load_last_project(self):
        """Resolve the suite-wide last-opened project's current
        path/URI via the daemon and load it. Falls through to the
        picker (start_over) if no project is recorded or the daemon
        doesn't know it.

        Wired as bootstrap()'s on_done. Client 0.28.5+ guarantees
        on_done only fires when the daemon is reachable, so the
        daemon RPCs below (last_project, open_project) need no
        defensive try/except — if the daemon weren't there,
        on_done wouldn't have fired and we wouldn't be here."""
        from azt_collab_client import last_project, open_project
        langcode = last_project()
        if not langcode:
            self.start_over()
            return
        project = open_project(langcode)
        if project is None or not project.lift_path:
            self.start_over()
            return
        # Cache the langcode so _register_current_project doesn't
        # re-derive — the daemon just told us authoritatively.
        self._current_langcode = langcode
        # Show a loading overlay while the synchronous LIFT parse +
        # queue rebuild runs; without it the recorder screen sits
        # blank (top bar only) for the duration of the parse and
        # users couldn't tell whether the app was busy or wedged.
        # Defer load_lift by one frame so the overlay paints before
        # the parse blocks the main thread.
        self._show_loading_overlay(
            _tr('Loading {name}…').format(name=_lang_display_name(langcode)))
        Clock.schedule_once(
            lambda dt: self.load_lift(project.lift_path), 0)

    def _run_bootstrap(self):
        """Drive the suite-wide install/update workflow.

        Delegates to ``azt_collab_client.ui.bootstrap`` so the
        server-APK install/update prompts and the recorder's own
        self-update probe share one canonical implementation across
        every peer. Status strings land in the collab-screen log so
        the user can see "Checking installation…", "Downloading
        45%…", etc.; popups marshal to the UI thread inside the
        helper. Non-Android hosts no-op (on_done fires immediately).

        on_done is wired to ``_auto_load_last_project`` so the
        recorder's first daemon-touching RPC only fires once
        bootstrap has confirmed the daemon is reachable (client
        0.28.5+ contract). No defensive try/except needed around
        that RPC — if the daemon weren't there, on_done wouldn't
        have fired."""
        from azt_collab_client.ui import bootstrap
        from appinfo import APP_NAME
        bootstrap(
            peer_repo='kent-rasmussen/azt-recorder',
            peer_version=__version__,
            # peer_asset_filename omitted — derived at runtime from
            # the Activity's package name (= aztrecorder.apk). See
            # azt_collab_client.ui.update.default_asset_filename.
            peer_display_name=APP_NAME,
            on_status=self._log_bootstrap_status,
            on_done=self._auto_load_last_project,
            on_error=self._log_bootstrap_status,
            font_name=_FONT_NAME,
        )

    def _log_bootstrap_status(self, message):
        """Surface bootstrap progress / errors. Logs to the collab
        screen's status bar if it's available, and prints to stderr
        so desktop devs and Android logcat see it too."""
        print(f'[bootstrap] {message}', file=sys.stderr)
        try:
            sm = self.root.ids.sm
            collab = sm.get_screen('collab')
            collab._set_log(message)
        except Exception:
            pass

    def _on_decision_resolved(self, kind, action, decision):
        """Inbound LAN decision was resolved by the user through the
        shared watcher's popup (CLIENT_INTEGRATION.md § 20a). The
        recorder has no project-list / peer-roster UI to refresh,
        and the contract forbids auto-loading a newly-received
        project — a passive share-offer accept lands the project in
        the daemon's list and the user opens it explicitly via the
        picker on next start_over."""
        print(f'[decisions] resolved kind={kind} action={action} '
              f'id={decision.get("id", "")}',
              file=sys.stderr)

    def _on_touch_record(self, window, touch):
        """Stamp _last_touch_time on every touch_down. Cheap;
        only used by _tick_cache_status to throttle during user
        interaction (#6). Returns False so the touch event
        continues to its real handler."""
        import time as _time
        self._last_touch_time = _time.monotonic()
        return False

    def _on_back_button(self, window, key, *args):
        """Handle Android back button (keycode 27) to navigate back."""
        if key != 27:
            return False
        sm = self.root.ids.sm
        if sm.current == 'recorder':
            return False  # let the system close the app
        elif sm.current in ('config', 'collab'):
            sm.transition = SlideTransition(direction='right')
            sm.current = 'recorder'
        elif sm.current == 'imagepicker':
            sm.transition = SlideTransition(direction='right')
            sm.current = 'recorder'
        else:
            return False
        return True

    @staticmethod
    def _ssl_context():
        """Return an SSL context that works on Android (missing CA bundle)."""
        import ssl
        # Try certifi — extract cacert.pem if needed (Android zip bundle)
        ca = None
        try:
            import certifi
            ca = certifi.where()
            if not os.path.isfile(ca):
                ca = None
        except ImportError:
            pass
        if ca is None:
            try:
                import certifi
                import importlib.resources as _res
                priv = os.environ.get('ANDROID_PRIVATE', '')
                if priv:
                    dest = os.path.join(priv, 'cacert.pem')
                    data = _res.read_binary('certifi', 'cacert.pem')
                    with open(dest, 'wb') as f:
                        f.write(data)
                    ca = dest
            except Exception:
                pass
        if ca:
            return ssl.create_default_context(cafile=ca)
        # Last resort: unverified
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _show_loading_overlay(self, msg):
        """Show a modal overlay that stays until dismissed."""
        from kivy.uix.label import Label
        from kivy.uix.modalview import ModalView
        from kivy.uix.boxlayout import BoxLayout
        self._dismiss_loading_overlay()
        view = ModalView(
            size_hint=(0.8, None), height=dp(120),
            background_color=theme.OVERLAY_DARK,
            auto_dismiss=False,
        )
        box = BoxLayout(orientation='vertical', padding=dp(8), spacing=dp(4))
        lbl = Label(
            text=msg, font_size=sp(16), font_name=_FONT_NAME,
            color=theme.TEXT, size_hint_y=None, height=dp(30),
        )
        detail = Label(
            text='', font_size=sp(12), font_name=_FONT_NAME,
            color=theme.TEXT_DIM,
            halign='center', valign='top',
        )
        detail.bind(size=lambda w, s: setattr(w, 'text_size', s))
        box.add_widget(lbl)
        box.add_widget(detail)
        view.add_widget(box)
        self._loading_overlay = view
        self._loading_detail = detail
        view.open()

    def _update_loading_detail(self, text):
        """Update the detail line of the loading overlay (thread-safe)."""
        detail = getattr(self, '_loading_detail', None)
        if detail:
            detail.text = text

    def _dismiss_loading_overlay(self):
        """Dismiss the loading overlay if one is showing."""
        overlay = getattr(self, '_loading_overlay', None)
        if overlay:
            overlay.dismiss()
            self._loading_overlay = None
            self._loading_detail = None

    def _show_toast(self, msg, duration=None):
        """Show a brief overlay message that auto-dismisses.

        Width is capped at min(85% of window, dp(320)); the label
        wraps and the box grows vertically to fit. Duration scales
        with message length (~25 chars/sec read speed, 1.5s floor,
        8s cap) so longer status messages (e.g. AUTH_REFRESH_STALE's
        "...current access expires in N minute(s). Open GitHub
        Connect and tap Re-authenticate.") get enough time to read.
        Pass an explicit ``duration`` to override.
        """
        from kivy.uix.label import Label
        from kivy.uix.modalview import ModalView
        if duration is None:
            duration = max(1.5, min(8.0, len(msg) / 25.0))
        pad = dp(20)
        box_w = min(Window.width * 0.85, dp(320))
        lbl = Label(
            text=msg, font_size=sp(14), font_name=_FONT_NAME,
            color=theme.TEXT,
            halign='center', valign='middle',
            text_size=(box_w - pad, None),
            size_hint=(None, None),
        )
        lbl.texture_update()
        lbl.size = (box_w, lbl.texture_size[1] + pad)
        view = ModalView(
            size_hint=(None, None),
            size=(box_w, max(dp(50), lbl.height)),
            background_color=theme.OVERLAY,
            auto_dismiss=True,
        )
        view.add_widget(lbl)
        view.open()
        Clock.schedule_once(lambda dt: view.dismiss(), duration)

    def _show_error(self, msg):
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        popup = Popup(
            title=_tr('Error'),
            content=Label(text=msg, font_size=sp(14)),
            size_hint=(0.8, None), height=dp(180),
        )
        popup.open()

    def _project_has_remote(self):
        """Check whether the daemon has a remote URL stored for the
        current project. Goes through project_status(langcode) per
        azt_collab_client/CLAUDE.md hard rule #2 ("no reading project
        state from the local filesystem"). The previous
        dulwich.Repo(working_dir).get_config() check falsely returned
        False on Android — the daemon's working_dir lives in the
        standalone server APK's private filesDir and peer processes
        have no UID-level read on it — which fired the spurious
        "data isn't being backed up" warning even after a successful
        publish, and (more importantly) gates auto-sync flows that
        check this method silently skipped their work on Android."""
        if not self.recorder:
            return False
        langcode = getattr(self, '_current_langcode', '')
        if not langcode:
            return False
        try:
            from azt_collab_client import project_status
            ps = project_status(langcode)
        except Exception:
            return False
        return bool(ps and (ps.remote_url or '').strip())

    def _show_backup_warning(self):
        """Show overlay warning that data isn't being backed up."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.button import Button
        content = BoxLayout(orientation='vertical', spacing=dp(12), padding=dp(12))
        content.add_widget(Label(
            text=_tr("Your data isn't being backed up! Please set up "
                     "collaboration, so your data can be backed up "
                     "automatically while you work."),
            font_size=sp(14), font_name=_FONT_NAME, color=theme.TEXT,
            size_hint_y=None, height=dp(100),
            halign='center', valign='middle',
            text_size=(dp(260), None),
        ))
        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
        later_btn = Button(
            text=_tr('Later'),
            font_size=sp(14), font_name=_FONT_NAME,
            size_hint_x=0.4,
            background_color=theme.BTN_INACTIVE,
        )
        collab_btn = Button(
            text=_tr('Setup Collaboration'),
            font_size=sp(14), font_name=_FONT_NAME,
            size_hint_x=0.6,
            background_color=theme.ACCENT,
        )
        btn_row.add_widget(later_btn)
        btn_row.add_widget(collab_btn)
        content.add_widget(btn_row)
        popup = Popup(
            title='', separator_height=0,
            content=content,
            size_hint=(0.9, None), height=dp(240),
            auto_dismiss=True,
        )
        later_btn.bind(on_release=popup.dismiss)
        def _go(*a):
            popup.dismiss()
            self.go_collab()
        collab_btn.bind(on_release=_go)
        popup.open()

    def _show_collab_prompt(self):
        """Show overlay prompting user to set up collaboration for credentials."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.button import Button
        content = BoxLayout(orientation='vertical', spacing=dp(12), padding=dp(12))
        content.add_widget(Label(
            text=_tr('You need to set up collaboration to do this.'),
            font_size=sp(15), font_name=_FONT_NAME, color=theme.TEXT,
            size_hint_y=None, height=dp(60),
            halign='center', valign='middle',
            text_size=(dp(260), None),
        ))
        btn = Button(
            text=_tr('Setup Collaboration'),
            font_size=sp(15), font_name=_FONT_NAME,
            size_hint_y=None, height=dp(48),
            background_color=theme.ACCENT,
        )
        content.add_widget(btn)
        popup = Popup(
            title='', separator_height=0,
            content=content,
            size_hint=(0.85, None), height=dp(200),
            auto_dismiss=True,
        )
        def _go(*a):
            popup.dismiss()
            self.go_collab()
        btn.bind(on_release=_go)
        popup.open()

    def _on_activity_result_wrapper(self, request_code, result_code, intent):
        """Wrapper for android.activity.bind — runs off main thread.
        Extract data from intent here (Java refs may not survive thread hop),
        then schedule UI work on main thread."""
        print(f'[activity-result] code={request_code} result={result_code}')
        if result_code != -1:  # not RESULT_OK
            print(f'[activity-result] not RESULT_OK, ignoring')
            return
        if request_code == 1002:
            try:
                uri = intent.getData()
                path = self._uri_to_image_path(uri)
                print(f'[activity-result] image path: {path}')
                if path:
                    Clock.schedule_once(
                        lambda dt: self._deliver_image_to_picker(path), 0)
            except Exception as ex:
                print(f'Image picker result error: {ex}')
                traceback.print_exc()
        elif request_code == 1003:
            try:
                from jnius import autoclass
                extras = intent.getExtras()
                if extras:
                    bitmap = extras.get('data')
                    if bitmap:
                        path = self._save_bitmap(bitmap)
                        print(f'[activity-result] camera saved to: {path}')
                        if path:
                            Clock.schedule_once(
                                lambda dt: self._deliver_image_to_picker(path), 0)
                    else:
                        print('[activity-result] no bitmap in extras')
                else:
                    print('[activity-result] no extras in camera result')
            except Exception as ex:
                print(f'Camera result error: {ex}')
                traceback.print_exc()

    def _deliver_image_to_picker(self, path):
        """Pass a local image path to the image picker screen (main thread)."""
        sm = self.root.ids.sm
        picker = sm.get_screen('imagepicker')
        picker._set_local_image(path)

    def _uri_to_image_path(self, uri):
        """Copy image content from URI to a temp file and return the path."""
        try:
            from jnius import autoclass
            context = autoclass('org.kivy.android.PythonActivity').mActivity
            resolver = context.getContentResolver()
            # Use ParcelFileDescriptor to get a native fd — avoids Java
            # byte array bridging issues through pyjnius
            pfd = resolver.openFileDescriptor(uri, autoclass('java.lang.String')('r'))
            fd = pfd.detachFd()
            with os.fdopen(fd, 'rb') as f:
                data = f.read()
            print(f'[uri-to-image] read {len(data)} bytes from URI')
            if not data:
                print('[uri-to-image] empty data from URI')
                return None
            tmp_dir = os.path.join(self.user_data_dir, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_path = os.path.join(tmp_dir, 'picked_image.png')
            with open(tmp_path, 'wb') as f:
                f.write(data)
            return tmp_path
        except Exception as ex:
            print(f'URI to image error: {ex}')
            traceback.print_exc()
            return None

    def _save_bitmap(self, bitmap):
        """Save an Android Bitmap to a temp PNG file."""
        try:
            from jnius import autoclass
            ByteArrayOutputStream = autoclass('java.io.ByteArrayOutputStream')
            CompressFormat = autoclass('android.graphics.Bitmap$CompressFormat')
            baos = ByteArrayOutputStream()
            bitmap.compress(CompressFormat.PNG, 100, baos)
            data = bytes(baos.toByteArray())
            tmp_dir = os.path.join(self.user_data_dir, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_path = os.path.join(tmp_dir, 'camera_photo.png')
            with open(tmp_path, 'wb') as f:
                f.write(data)
            return tmp_path
        except Exception as ex:
            print(f'Save bitmap error: {ex}')
            return None

    def _get_image_cache_dir(self):
        """Return the per-session ephemeral image tmp dir.

        Set up once at app startup via _init_session_image_cache;
        used for CAWL pull-throughs and URI-project image pulls.
        Nothing here is durable — the daemon owns the canonical
        CAWL cache (`$AZT_HOME/cawl/<owner>/<repo>/images/`) shared
        across peers per CAWL Stage 2."""
        return getattr(self, '_session_image_cache_dir', '') or ''

    def _init_session_image_cache(self):
        """Create the per-session tmp dir on first call. Idempotent."""
        if getattr(self, '_session_image_cache_dir', ''):
            return
        import tempfile
        self._session_image_cache_dir = tempfile.mkdtemp(
            prefix='azt_recorder_imgcache_')

    def _purge_legacy_image_cache(self):
        """One-shot rmtree of the pre-1.41.4 durable image cache at
        <user_data_dir>/image_cache/. Pre-Stage-2 peers wrote CAWL
        bytes here on every prefetch; Stage 2 made that the daemon's
        job, so the directory's contents are dead weight (and on
        Android they're per-app sandboxed, so they don't even survive
        a reinstall). Best-effort; silent on failure."""
        legacy = os.path.join(self.user_data_dir, 'image_cache')
        if not os.path.isdir(legacy):
            return
        import shutil
        try:
            shutil.rmtree(legacy)
            print(f'[cawl] purged legacy image cache: {legacy}')
        except Exception as ex:
            print(f'[cawl] legacy cache purge skipped: {ex}')

    def _start_image_prefetch(self):
        """Hand the daemon this project's CAWL working-set and start
        the cache-progress poll, on a worker thread so the daemon
        round-trips (``all_cawl_paths`` triggers ``cawl_index``;
        ``cawl_prefetch`` itself is a daemon RPC) don't compete
        with ``auto_sync`` ticks on the main thread when multiple
        paired phones open the same project simultaneously.

        Per CLIENT_INTEGRATION.md § 10's daemon-driven prefetch
        update: the peer no longer iterates CAWLHandle on its own.
        It computes the working-set (one variant per CAWL id), hands
        the list to ``cawl_prefetch`` once, and the daemon iterates
        in its own background thread."""
        if not self.recorder:
            print('[image-prefetch] no recorder; skipping')
            return
        langcode = (getattr(self, '_current_langcode', '') or '').strip()
        if not langcode:
            print('[image-prefetch] no langcode; cache-progress '
                  'indicator will not poll')
            return
        # "Be eager when you have room to" — sample is cheap and
        # main-thread-safe, so check before paying the worker-spawn
        # cost. Project switch re-samples naturally via the
        # load_lift → _start_image_prefetch path.
        ok, reason = self._have_room_for_prefetch()
        if not ok:
            print(f'[image-prefetch] skipped — {reason}; '
                  f'on-demand only this session')
            return
        import threading
        threading.Thread(
            target=self._image_prefetch_worker,
            args=(langcode,),
            daemon=True,
        ).start()

    def _image_prefetch_worker(self, langcode):
        try:
            paths = self.recorder.db.all_cawl_paths()
        except Exception as ex:
            print(f'[image-prefetch] all_cawl_paths failed: {ex}')
            return
        if not paths:
            print('[image-prefetch] no paths to warm '
                  '(resolver returned empty)')
            return
        from azt_collab_client import cawl_prefetch
        try:
            resp = cawl_prefetch(langcode, paths)
        except Exception as ex:
            print(f'[image-prefetch] cawl_prefetch failed: {ex}')
            return
        print(f'[image-prefetch] daemon warm started: '
              f'requested={resp.get("requested")} '
              f'completed={resp.get("completed")} '
              f'finished={resp.get("finished")}')
        # Polling cycle drives Kivy properties, schedule onto main.
        Clock.schedule_once(
            lambda dt: self._start_cache_status_poll(langcode), 0)

    def _sample_total_ram_mb(self):
        """One-shot total-RAM read at startup, in MB. Used for
        device-class decisions (image cache size, prewarm gate).
        Returns 0 on non-Android — treated as "plenty" by gates."""
        if platform != 'android':
            return 0
        try:
            from jnius import autoclass
            Context = autoclass('android.content.Context')
            ActivityManager = autoclass('android.app.ActivityManager')
            MemoryInfo = autoclass(
                'android.app.ActivityManager$MemoryInfo')
            PythonActivity = autoclass(
                'org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            am = activity.getSystemService(Context.ACTIVITY_SERVICE)
            mi = MemoryInfo()
            am.getMemoryInfo(mi)
            return int(mi.totalMem / (1024 * 1024))
        except Exception as ex:
            print(f'[low-power] totalMem sample failed: {ex}')
            return 0

    def _sample_memory_state(self):
        """Live memory snapshot. Returns
        ``(low_memory_flag, avail_ratio, avail_mb)`` or
        ``(False, 1.0, 0)`` on non-Android / JNI failure (treated
        as "plenty of headroom" by the kill-switches that read it)."""
        if platform != 'android':
            return False, 1.0, 0
        try:
            from jnius import autoclass
            Context = autoclass('android.content.Context')
            ActivityManager = autoclass('android.app.ActivityManager')
            MemoryInfo = autoclass(
                'android.app.ActivityManager$MemoryInfo')
            PythonActivity = autoclass(
                'org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            am = activity.getSystemService(Context.ACTIVITY_SERVICE)
            mi = MemoryInfo()
            am.getMemoryInfo(mi)
            total = max(mi.totalMem, 1)
            return (bool(mi.lowMemory),
                    mi.availMem / total,
                    int(mi.availMem / (1024 * 1024)))
        except Exception as ex:
            print(f'[low-power] memory sample failed: {ex}')
            return False, 1.0, 0

    def _is_low_memory(self):
        """True iff the OS reports memory pressure right now, or
        free-RAM ratio is below 15%. Single source of truth for
        the runtime "is this a moment to back off?" question."""
        low, ratio, _ = self._sample_memory_state()
        return low or ratio < 0.15

    def _downsample_for_display(self, src_path):
        """If the device reports memory pressure, return a path to a
        downsampled (max 720px) copy of ``src_path`` in a session-
        local side cache. Otherwise return ``src_path`` unchanged.

        Lazy: only downsamples when actually called (on display)
        and only if the side cache doesn't already have an entry.
        The side cache lives under ``<session tmp>/_lowres/`` and
        is GC'd with the rest of the per-session image cache on
        app close.

        Returns ``src_path`` on any failure — the original is
        always a valid fallback."""
        if not src_path or not os.path.exists(src_path):
            return src_path
        if not self._is_low_memory():
            return src_path
        try:
            tmp_root = self._get_image_cache_dir()
            if not tmp_root:
                return src_path
            lowres_dir = os.path.join(tmp_root, '_lowres')
            os.makedirs(lowres_dir, exist_ok=True)
            base = os.path.basename(src_path)
            dest = os.path.join(lowres_dir, base)
            if (os.path.exists(dest)
                    and os.path.getmtime(dest)
                    >= os.path.getmtime(src_path)):
                return dest
            from PIL import Image as PILImage
            with PILImage.open(src_path) as im:
                w, h = im.size
                max_dim = 720
                if max(w, h) <= max_dim:
                    # Already small enough — just point at the
                    # original; no need to copy.
                    return src_path
                ratio = max_dim / max(w, h)
                resized = im.resize(
                    (int(w * ratio), int(h * ratio)),
                    PILImage.LANCZOS)
                # Preserve the source's format; fall back to PNG.
                fmt = im.format or 'PNG'
                resized.save(dest, fmt)
            return dest
        except Exception as ex:
            print(f'[low-power] downsample {src_path!r}: {ex}')
            return src_path

    def _have_room_for_prefetch(self):
        """Return ``(ok, reason)``. ``ok=True`` iff the device has
        headroom to bulk-warm the CAWL cache.

        Three OR'd kill-switches, all peer-side OS signals:

        - ``ActivityManager.MemoryInfo.lowMemory`` — the OS's own
          "I'm under memory pressure" boolean.
        - ``availMem / totalMem < 15%`` — secondary memory check
          (catches just before lowMemory fires).
        - Active network is metered (cellular, typically) — don't
          burn the user's data plan on a background warm.

        Desktop / iOS always return ``(True, '')``."""
        if platform != 'android':
            return True, ''
        low, ratio, _ = self._sample_memory_state()
        if low:
            return False, 'lowMemory flag set by OS'
        if ratio < 0.15:
            return False, f'free RAM {ratio:.1%} < 15%'
        try:
            from jnius import autoclass
            Context = autoclass('android.content.Context')
            PythonActivity = autoclass(
                'org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            cm = activity.getSystemService(
                Context.CONNECTIVITY_SERVICE)
            active = cm.getActiveNetwork()
            if active is not None:
                caps = cm.getNetworkCapabilities(active)
                if caps is not None:
                    # NET_CAPABILITY_NOT_METERED = 11.
                    if not caps.hasCapability(11):
                        return False, 'metered network'
        except Exception as ex:
            print(f'[prefetch] network-sample failed; '
                  f'defaulting eager: {ex}')
        return True, ''

    # ── CAWL cache-progress indicator ────────────────────────────────────
    # Per CLIENT_INTEGRATION.md § 10.5: while a CAWL prefetch is in
    # flight (the daemon is fetching ~1700 image binaries from
    # upstream GitHub, which can take many minutes on a slow link),
    # surface "Caching images: M / N (network in use)" so the user
    # doesn't disconnect Wi-Fi mid-warm and end up with a half-cache.

    def _start_cache_status_poll(self, langcode):
        """Begin a 1-second poll of the daemon's CAWL cache status.
        Idempotent: an already-running poll is replaced (langcode may
        have changed via project switch). Stops on its own once the
        cache catches up to the index."""
        if getattr(self, '_cache_status_event', None):
            try:
                self._cache_status_event.cancel()
            except Exception:
                pass
            self._cache_status_event = None
        self._cache_status_langcode = langcode
        # Reset the state-change tracker so the first poll always
        # logs (subsequent identical polls don't, per the contract).
        # Tuple is (cached, total, offline, circuit_open) — flag flips
        # without count changes still need to produce one log line so
        # logcat shows when the daemon transitioned to
        # offline-skipped / paused state.
        self._cache_status_last = (-1, -1, False, False)
        self._logged_total_zero = False
        # Prevent overlapping daemon round-trips when the worker takes
        # longer than the 1s tick. Without this gate, two workers can
        # race to "cache warm" and double the RPC rate plus the log
        # line. Cleared by the worker once its dispatch lands.
        self._cache_tick_in_flight = False
        # Latches on the first cached>=total dispatch so any late
        # worker (started before cancel ran) becomes a no-op.
        self._cache_status_warmed = False
        print(f'[cache-status] poll starting for langcode={langcode!r}')
        self._cache_status_event = Clock.schedule_interval(
            lambda _dt: self._tick_cache_status(), 1.0)
        self._tick_cache_status()

    def _tick_cache_status(self):
        """One poll iteration. Runs the daemon RPC on a worker so the
        UI thread isn't pinned waiting for the cache_status response;
        marshals the label update back to the main thread.

        #6: skip the tick if the user is actively interacting (touch
        within the last second). The 1Hz poll burns a daemon round-
        trip and an indicator-text re-render — wasted during a
        swipe gesture where the user can't perceive the indicator
        anyway."""
        import time as _time
        if _time.monotonic() - getattr(
                self, '_last_touch_time', 0.0) < 1.0:
            return
        langcode = getattr(self, '_cache_status_langcode', '') or ''
        if not langcode:
            self._hide_cache_indicator()
            return
        # Skip if a worker from a prior tick is still resolving — the
        # cancel that fires on cached>=total runs inside
        # _apply_cache_status on the main thread, so without this gate
        # a slow daemon means tick N+1 spawns a second worker that
        # also reaches "cache warm" and double-fires the log + RPC.
        if getattr(self, '_cache_tick_in_flight', False):
            return
        self._cache_tick_in_flight = True
        import threading

        def _worker():
            try:
                from azt_collab_client import cawl_cache_status
                status = cawl_cache_status(langcode)
            except Exception as ex:
                print(f'[cache-status] poll failed: {ex}')
                self._cache_tick_in_flight = False
                return
            cached = int(status.get('cached') or 0)
            total = int(status.get('total') or 0)
            # `offline` / `circuit_open` are additive flags from
            # daemon 0.41.21 — `.get()` with False default keeps the
            # pre-0.41.21 path working (active-progress branch).
            offline = bool(status.get('offline'))
            circuit_open = bool(status.get('circuit_open'))
            # Per-source telemetry (daemon 0.50.21+, contract § 10
            # "Per-source telemetry"). Default-zero / empty on
            # pre-0.50.21 daemons; `last_source` drives the inline
            # "via LAN" / "via Internet" tag so the user can verify
            # the NOTES #3 LAN-share path is producing hits.
            from_lan = int(status.get('from_lan') or 0)
            from_upstream = int(status.get('from_upstream') or 0)
            from_cache = int(status.get('from_cache') or 0)
            last_source = str(status.get('last_source') or '')
            # Log only on state change so a 1 Hz poll doesn't fill
            # logcat with identical lines (contract § 10). State
            # includes the flags AND last_source so a source flip
            # still produces one log even when the counts didn't
            # move. Source counters get delta-logged separately so
            # the size of the LAN-share win is visible at a glance.
            last = getattr(self, '_cache_status_last',
                           (-1, -1, False, False, ''))
            key = (cached, total, offline, circuit_open, last_source)
            if key != last:
                prev_lan, prev_wan, prev_cache = getattr(
                    self, '_cache_source_last', (0, 0, 0))
                print(f'[cache-status] cached={cached} total={total} '
                      f'offline={offline} circuit_open={circuit_open} '
                      f'last_source={last_source!r} '
                      f'Δlan={from_lan - prev_lan} '
                      f'Δwan={from_upstream - prev_wan} '
                      f'Δcache={from_cache - prev_cache} '
                      f'image_repo={status.get("image_repo")!r}')
                self._cache_status_last = key
                self._cache_source_last = (
                    from_lan, from_upstream, from_cache)
            Clock.schedule_once(
                lambda dt, c=cached, t=total, o=offline, x=circuit_open,
                       src=last_source:
                    self._apply_cache_status(c, t, o, x, src), 0)
            self._cache_tick_in_flight = False
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_cache_status(self, cached, total,
                            offline=False, circuit_open=False,
                            last_source=''):
        """Render the indicator (or hide it) based on the latest
        cached / total counts plus the `offline` / `circuit_open`
        flags and `last_source` (daemon 0.50.21+). Cancels the
        polling Clock event once the cache catches up.

        Polling continues during offline / circuit_open per contract
        § 10 — the daemon's scheduler watcher fires
        `cawl.on_online_edge()` on the offline→online transition
        which re-fires `auto_prefetch`; the running 1 Hz poll is
        what lets the banner flip back to live progress
        automatically when that happens.

        ``last_source`` distinguishes the channel serving bytes
        right now: ``'lan'`` (paired peer's cache via the NOTES #3
        share path — free), ``'upstream'`` (GitHub — metered if
        cellular), ``'cache'`` (local disk — free), or ``''``
        (initial / no successful fetch yet, or pre-0.50.21 daemon).
        Drives the "via LAN" / "via Internet" tag so the user can
        see whether the LAN-share is producing hits."""
        # Defence in depth: if a worker started before cancel ran is
        # arriving late with cached>=total, drop it so we don't print
        # "cache warm" twice or re-hide an already-hidden indicator.
        if getattr(self, '_cache_status_warmed', False):
            return
        if total == 0:
            if not getattr(self, '_logged_total_zero', False):
                self._logged_total_zero = True
                print('[cache-status] total=0 → hiding indicator '
                      '(no index entries: endpoint missing on this '
                      'server APK, no image_repo configured, or the '
                      'index transport is broken)')
            self._hide_cache_indicator()
            return
        if cached >= total:
            self._cache_status_warmed = True
            print(f'[cache-status] cache warm: {cached}/{total}; '
                  'hiding + stopping poll')
            self._hide_cache_indicator()
            event = getattr(self, '_cache_status_event', None)
            if event:
                try:
                    event.cancel()
                except Exception:
                    pass
                self._cache_status_event = None
            return
        # Shared msgids with azt_collab_client/locales (the daemon
        # settings UI uses the same three indicator strings) so the
        # recorder inherits translations via the gettext fallback
        # chain — no peer-side duplicate.
        if offline:
            msg = _tr(
                'Image cache: {cached} / {total} '
                '(offline — will resume when online)'
            ).format(cached=cached, total=total)
        elif circuit_open:
            msg = _tr(
                'Image cache: {cached} / {total} '
                '(paused — connectivity lost)'
            ).format(cached=cached, total=total)
        else:
            # Active-progress branch + per-source tag per
            # CLIENT_INTEGRATION.md § 10 "Per-source telemetry".
            # LAN serves bytes for free; GitHub costs cellular if
            # that's the active link — the user needs to see which
            # one is running. Pre-0.50.21 daemons leave last_source
            # empty and fall through to the no-tag wording.
            if last_source == 'lan':
                msg = _tr(
                    'Caching images: {cached} / {total} · via LAN'
                ).format(cached=cached, total=total)
            elif last_source == 'upstream':
                msg = _tr(
                    'Caching images: {cached} / {total} · via Internet '
                    '(please stay online)'
                ).format(cached=cached, total=total)
            else:
                msg = _tr(
                    'Caching images: {cached} / {total} '
                    '(network in use — please stay online)'
                ).format(cached=cached, total=total)
        self._show_cache_indicator(msg)

    def _show_cache_indicator(self, text):
        try:
            lbl = self.root.ids.sm.get_screen('recorder').ids.get(
                'cache_status_label')
        except Exception:
            return
        if not lbl:
            return
        lbl.text = text
        lbl.height = dp(22)
        lbl.opacity = 1

    def _show_severe_alert(self, text):
        """Show the sticky red banner used for the never-silenced
        sync codes (CLIENT_INTEGRATION.md § 17: DATA_LOSS_RISK +
        COMMIT_REPEATEDLY_FAILED). Tap-to-dismiss via the banner's
        on_release. Replaces the previous _show_toast call for
        these codes — a 1.5s toast wasn't long enough for the user
        to read the multi-line maintainer-contact wording.

        ``text`` is the already-translated message (markup OK)."""
        try:
            btn = self.root.ids.sm.get_screen('recorder').ids.get(
                'severe_alert_banner')
        except Exception:
            return
        if not btn:
            return
        btn.text = text
        # Fixed height fits ~4-5 lines of sp(12) wrapped text;
        # the daemon translations land around 4 lines on a typical
        # phone width. Wrap drops further text on overflow —
        # acceptable for an emergency banner, the action is the
        # same regardless of which file count is shown.
        btn.height = dp(80)
        btn.opacity = 1

    def _dismiss_severe_alert(self):
        """Hide the severe-alert banner. Wired to the banner's
        on_release in KV. The daemon will re-emit the underlying
        status code (DATA_LOSS_RISK / COMMIT_REPEATEDLY_FAILED)
        on the next sync if the condition persists, so dismissing
        doesn't actually silence anything — the next poll-job /
        do_sync cycle will surface it again if still applicable."""
        try:
            btn = self.root.ids.sm.get_screen('recorder').ids.get(
                'severe_alert_banner')
        except Exception:
            return
        if not btn:
            return
        btn.text = ''
        btn.height = 0
        btn.opacity = 0

    def _hide_cache_indicator(self):
        try:
            lbl = self.root.ids.sm.get_screen('recorder').ids.get(
                'cache_status_label')
        except Exception:
            return
        if not lbl:
            return
        lbl.text = ''
        lbl.height = 0
        lbl.opacity = 0

    def load_lift(self, path):
        # Three-phase load:
        #   Phase 1 (this method, main thread): cheap path resolution,
        #       stale-cache sweep, baseline resets, capture authoritative
        #       / pending langcodes, then spawn the worker.
        #   Phase 2 (_load_lift_worker, off main): the expensive bit —
        #       LIFTDatabase XML parse, db setup, RecorderController
        #       construction, initial rebuild_queue. A 4 MB LIFT on a
        #       slow MTK chip is several seconds of CPU; running it
        #       inline blocked the touch dispatcher long enough that
        #       Android could ANR-kill the peer (see the "bee" crash
        #       report in 1.55.12). Loading overlay stays up across
        #       phase 2 — the user sees the modal, not a frozen UI.
        #   Phase 3 (_load_lift_publish, back on main): install
        #       self.recorder, register with the daemon, transition
        #       to the recorder screen, schedule _finish_load (which
        #       flips has_project + dismisses the modal in one tick).
        # The picker can return either a filesystem path (desktop / open
        # file) or a content:// URI (Android server-APK model). Only
        # apply abspath when we genuinely have a filesystem path.
        from azt_collab_client import is_content_uri
        if not is_content_uri(path):
            path = os.path.abspath(path)
            # Sweep the stale `.cawl_image_urls.json` that the desktop
            # AZT app used to drop next to the LIFT. CAWL image URLs
            # are daemon-owned now (CLIENT_INTEGRATION.md § 10); a
            # peer-side mirror is exactly the kind of "just-in-case"
            # cache the suite contract forbids. URI projects (Android)
            # skip — the recorder doesn't own that directory.
            try:
                stale = os.path.join(os.path.dirname(path),
                                     '.cawl_image_urls.json')
                if os.path.exists(stale):
                    os.unlink(stale)
                    print(f'[load_lift] removed stale {stale}')
            except OSError as ex:
                print(f'[load_lift] stale CAWL cache cleanup skipped: '
                      f'{ex}')
        # vernlang priority:
        #   1. _current_langcode — server-authoritative langcode for
        #      this project (set by _handle_pick AND by
        #      _auto_load_last_project before reaching load_lift).
        #      This is what drives db.vernlang so progress_text and
        #      downstream LIFT writes use the same code the daemon
        #      stores under.
        #   2. _pending_vernlang — set only by new-from-template flows;
        #      its real semantic is "also run clean_template()."
        # No third-tier peer-side cache: the recorder never persists
        # daemon-owned state locally. If neither is set we're past a
        # broken load path; let downstream surface that rather than
        # paper over it with a stale cache.
        authoritative = getattr(self, '_current_langcode', '')
        pending = getattr(self, '_pending_vernlang', '')
        self._pending_vernlang = ''
        # Drop any content-advance baselines carried over from a
        # previous project — otherwise the first _update_sync_status
        # tick after this load would compare the new project's
        # head_sha / last_commit against the old project's and fire
        # a spurious _reload_and_restore on top of the load we
        # just did. Both signals reset; whichever the daemon
        # surfaces takes over on the next poll.
        self._last_head_sha = None
        self._last_commit_seen = None
        # New project = new split state. Reset the App-level cache so
        # the transient-zero guard doesn't carry the old project's
        # team_size / my_slot into this one. The first successful
        # apply re-populates the cache.
        self._last_known_team_size = 0
        self._last_known_my_slot = ''
        import threading
        threading.Thread(
            target=self._load_lift_worker,
            args=(path, authoritative, pending),
            daemon=True, name='load-lift').start()

    def _load_lift_worker(self, path, authoritative, pending):
        """Phase 2: XML parse + db setup + RecorderController +
        initial rebuild_queue, all off the main thread. None of these
        touch Kivy widgets or properties, so running them on a worker
        is safe. UI publishes back via Clock.schedule_once."""
        try:
            db = LIFTDatabase(
                path, image_cache_dir=self._get_image_cache_dir())
        except Exception as ex:
            Clock.schedule_once(
                lambda _dt, e=ex: self._load_lift_error(e), 0)
            return
        try:
            if authoritative:
                db.set_vernlang(authoritative)
            elif pending:
                db.set_vernlang(pending)
            if pending:
                try:
                    db.clean_template()
                except Exception as ex:
                    # _save() inside clean_template writes the LIFT
                    # file — on Android URI projects that goes through
                    # the daemon's ContentProvider and can fail with a
                    # stale URI grant or transient daemon state.
                    # Best-effort cleanup of template-stub forms; a
                    # failure here must not abort the load.
                    print(f'[load_lift] clean_template failed: {ex}')
            # Filesystem-side orphan-audio recovery: bind any audio
            # files whose deterministic basename names an entry with
            # no audiolang citation form yet. Covers the case where a
            # previous session recorded the file, queued the LIFT
            # write, then the app was force-killed before the auto-
            # retry tick fired. URI projects handle the same case via
            # the persisted _pending_lift_saves queue.
            try:
                db.bind_orphan_audio()
            except Exception as ex:
                print(f'[load_lift] orphan-audio bind raised: {ex}')
            rec = RecorderController(
                db, langcode=(authoritative or pending or ''))
            # Apply persisted show-past-work preference (default: hide
            # past work). peer_pref is an RPC — fine from a worker.
            show_past = bool(peer_pref('show_past_work', False))
            rec.only_unrecorded = not show_past
            rec.rebuild_queue()
        except Exception as ex:
            Clock.schedule_once(
                lambda _dt, e=ex: self._load_lift_error(e), 0)
            return
        Clock.schedule_once(
            lambda _dt: self._load_lift_publish(rec, pending), 0)

    def _load_lift_error(self, ex):
        """Main-thread error handler for the worker. Dismisses the
        loading overlay and surfaces the error so the user knows the
        load didn't silently fail."""
        self._dismiss_loading_overlay()
        self._show_error(
            _tr('Could not open file:\n{error}').format(error=ex))

    def _load_lift_publish(self, rec, pending):
        """Phase 3: install the freshly-built RecorderController,
        register with the daemon, transition to the recorder screen,
        and schedule _finish_load (the synchronous-era tail that flips
        has_project + dismisses the loading overlay in one tick)."""
        self.recorder = rec
        # Register this project with the sync backend so future ops
        # can be addressed by langcode.
        self._register_current_project()
        # ContentObserver subscription (CLIENT_INTEGRATION.md § 17b,
        # v0.47.0+): per-project URI gives sub-second wakeups on
        # daemon-side HEAD advance (incoming LAN receive-pack from a
        # paired peer, scheduler-driven push, post-receive absorb).
        # Polling stays as a heartbeat floor.
        self._subscribe_project_changes()
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'recorder'

        def _finish_load(_dt):
            # All three effects fire in the same Clock tick so the
            # user sees one transition: modal closes ↔ sync + gear
            # appear ↔ entry data populates. has_project trips the
            # KV bindings for the sync + gear buttons; refresh paints
            # the entry image / glosses / progress / sync status;
            # dismiss closes the modal that's been covering
            # everything.
            self.has_project = True
            self.refresh_recorder_ui()
            self._dismiss_loading_overlay()
        Clock.schedule_once(_finish_load, 0.1)
        # Wordlist-split state per CLIENT_INTEGRATION.md § 21.
        # _populate_split_state already runs its RPC reads on a
        # worker; safe to call from main.
        self._populate_split_state()
        # Silently pre-fetch all CAWL images for offline use
        Clock.schedule_once(lambda dt: self._start_image_prefetch(), 1.0)
        # Auto-publish new project if credentials are already configured
        if pending:
            self._try_auto_publish()
        # Warn if data isn't being backed up (no git remote)
        elif not self._project_has_remote():
            Clock.schedule_once(lambda dt: self._show_backup_warning(), 0.5)

    def _register_current_project(self):
        """Tell the backend about the project currently loaded. Returns the
        langcode the backend is tracking it under (empty string on error).

        The langcode is server-supplied: every load path threads the
        daemon's canonical ``projects.json`` key into
        ``_current_langcode`` before reaching here — the picker emits
        it in its result (``_handle_pick``), and the startup auto-load
        reads it from ``azt_collab_client.recent`` and resolves the
        path via ``open_project`` (``_auto_load_last_project``).
        ``derive_langcode`` is left as a defensive fallback for any
        future load path that doesn't yet wire the langcode through;
        it's a server RPC, but going through it is wasteful when we
        already have the answer."""
        if not self.recorder:
            return ''
        try:
            from azt_collab_client import (
                derive_langcode, register_project)
            working_dir = self.recorder.db.dir
            lift_path = self.recorder.db.path
            langcode = getattr(self, '_current_langcode', '')
            if not langcode:
                langcode = derive_langcode(working_dir, lift_path)
            if not langcode:
                return ''
            register_project(langcode, working_dir, lift_path)
            self._current_langcode = langcode
            return langcode
        except Exception as ex:
            print(f'[register_project] error: {ex}')
            return ''

    def _current_langcode_or_register(self):
        """Return the cached langcode for the current project, registering
        on demand if we haven't yet."""
        code = getattr(self, '_current_langcode', '')
        if code:
            return code
        return self._register_current_project()

    # ── ContentObserver subscription (CLIENT_INTEGRATION.md § 17b v0.47.0+) ───

    def _subscribe_project_changes(self):
        """Subscribe to the daemon's per-project ContentObserver URI
        for the currently-loaded project so HEAD-advance events
        (incoming LAN receive-pack, scheduler push, post-receive
        absorb) wake the peer sub-second instead of waiting for the
        next polling tick. Silent no-op on non-Android / loopback —
        the subscribe call returns None and the polling-floor path
        covers those peers.

        Idempotent: tears down any prior subscription before
        registering a new one (project switch reuses this method
        with the new langcode)."""
        self._unsubscribe_project_changes()
        langcode = (getattr(self, '_current_langcode', '') or '').strip()
        if not langcode:
            return
        try:
            from azt_collab_client import subscribe_project_changes
        except ImportError:
            return  # pre-0.47.0 client
        try:
            self._project_sub_token = subscribe_project_changes(
                langcode, self._on_project_changed)
        except Exception as ex:
            print(f'[content-observer] subscribe failed: {ex}',
                  file=sys.stderr)
            self._project_sub_token = None

    def _unsubscribe_project_changes(self):
        """Release any active per-project subscription. Safe to call
        when no subscription is held."""
        token = getattr(self, '_project_sub_token', None)
        if token is None:
            return
        try:
            from azt_collab_client import unsubscribe
            unsubscribe(token)
        except Exception as ex:
            print(f'[content-observer] unsubscribe failed: {ex}',
                  file=sys.stderr)
        self._project_sub_token = None

    def _on_project_changed(self, uri):
        """ContentObserver callback. Fires on the binder thread that
        delivered the notification — marshal back to the main thread
        before touching Kivy widgets. Wakes the existing sync-status
        path, which already does the head_sha advance detection and
        in-place content reload (§ 14)."""
        Clock.schedule_once(lambda dt: self._update_sync_status(), 0)

    # ── Wordlist-split state (CLIENT_INTEGRATION.md § 21) ──────────────────────

    def _populate_split_state(self):
        """Pull ``team_size`` + this device's slot from the project
        KV (worker thread) and apply on the main thread. Refreshes
        ``recorder.split_team_size`` / ``split_my_slot``; recomputes
        the CAWL range when ``cawl_filter_source == 'split'``;
        opens the slot picker if team_size is set but this device
        isn't in ``list_slots`` (§ 21 hard rule #3).

        Worker-thread fetch keeps the UI responsive when the daemon
        is mid-restart. Queue-at-most-one re-entry: if a fetch is
        already in flight when a new call lands (e.g. user just
        bumped team_size and the panel-expand's earlier fetch
        hasn't returned yet), mark a pending re-fire instead of
        dropping. The worker checks the flag in its finally and
        re-spawns a follow-up fetch — guarantees the latest
        mutation is reflected without piling up parallel workers
        (§ 17c Rule 1 shape)."""
        if not self.recorder:
            return
        langcode = (getattr(self, '_current_langcode', '') or '').strip()
        if not langcode:
            return
        if getattr(self, '_split_state_in_flight', False):
            # Worker already running; mark a follow-up so the
            # mutation that triggered this call still gets
            # reflected when the running worker exits.
            self._split_state_pending = True
            return
        self._split_state_in_flight = True
        self._split_state_pending = False
        import threading
        pending_slot = getattr(self, '_claim_pending_slot', '')
        threading.Thread(
            target=self._populate_split_state_worker,
            args=(langcode, pending_slot),
            daemon=True,
        ).start()

    def _populate_split_state_worker(self, langcode, pending_slot):
        try:
            from azt_collab_client import (
                project_kv_get, list_slots, lan_peer_id)
        except ImportError:
            self._split_state_in_flight = False
            return
        try:
            raw = project_kv_get(langcode, 'team_size', default='') or ''
            try:
                team_size = int(raw) if raw else 0
            except (TypeError, ValueError):
                team_size = 0
            try:
                slots = list_slots(langcode) or {}
            except Exception as ex:
                print(f'[split] worker list_slots failed: {ex}',
                      file=sys.stderr)
                return
            try:
                my_peer_id = (lan_peer_id() or {}).get('peer_id', '') or ''
            except Exception:
                my_peer_id = ''
            Clock.schedule_once(
                lambda dt: self._populate_split_state_apply(
                    langcode, team_size, slots, my_peer_id,
                    pending_slot),
                0)
        except Exception as ex:
            print(f'[split] worker project_kv_get failed: {ex}',
                  file=sys.stderr)
        finally:
            # Clear the guard from the worker (not the apply step)
            # so a slow daemon doesn't strand the flag if the worker
            # raises before scheduling the apply. If another
            # _populate_split_state call landed while we were
            # running, satisfy it now — the queue-at-most-one shape
            # guarantees the mutation that triggered the re-entry
            # still gets reflected.
            self._split_state_in_flight = False
            if getattr(self, '_split_state_pending', False):
                self._split_state_pending = False
                Clock.schedule_once(
                    lambda dt: self._populate_split_state(), 0)

    def _populate_split_state_apply(self, langcode, team_size, slots,
                                    my_peer_id, pending_slot):
        # Project switched while the worker was in flight — drop
        # the apply rather than write a previous project's split
        # state onto the new one.
        if (getattr(self, '_current_langcode', '') or '') != langcode:
            return
        if not self.recorder:
            return
        # Transient-zero guard. The daemon's project_kv_get can
        # return '' for team_size.txt during a commit cycle (the
        # data-loss-risk log entries for `.azt/kv/team_size.txt`
        # are the smoking gun — the file is sometimes uncommittable
        # and the read races the daemon's own file shuffle).
        # Without this, team_size briefly drops 4→0→4 between
        # applies and the modal's ts_row flickers.
        #
        # The "previous" value comes from BOTH the current
        # recorder AND an App-level cache that survives
        # RecorderController swaps (every _reload_and_restore
        # creates a fresh controller with split_team_size=0,
        # which would otherwise blank the guard). The cache is
        # reset on load_lift so a new project starts fresh.
        prev_team_size = max(
            int(getattr(self.recorder, 'split_team_size', 0) or 0),
            int(getattr(self, '_last_known_team_size', 0) or 0))
        if prev_team_size and not team_size:
            print(f'[split] ignoring transient team_size=0 '
                  f'(prev={prev_team_size}) — daemon read race',
                  file=sys.stderr)
            team_size = prev_team_size
        # § 21 Locked semantic #2 (2026-05-28): peer_id is the
        # canonical key; device_name is display-only and can change
        # without invalidating any claim. No device_name fallback —
        # if lan_peer_id() returns '', the daemon's LAN identity
        # wasn't initialised, which is a daemon-side issue; the
        # peer logs and skips matching rather than papering over it.
        my_slot = ''
        if my_peer_id:
            for slot, claim in slots.items():
                if (claim or {}).get('peer_id', '') == my_peer_id:
                    my_slot = str(slot)
                    break
        # S1 claim-pending guard: a just-issued claim_slot may not
        # yet have flushed to list_slots. Adopt the pending value
        # optimistically so the picker doesn't re-fire — AND so
        # the user's selection doesn't flicker in/out as repeated
        # populate cycles run before the daemon commit lands.
        # Persistence rule: pending stays alive across applies
        # until the daemon-side state is observable — either our
        # peer_id appears in list_slots (confirmation, handled in
        # the peer_id loop above) or some claim exists at the
        # pending slot key with a DIFFERENT peer_id (we lost the
        # race; drop pending and let the picker fire). Empty
        # claim at the pending key means the daemon hasn't yet
        # flushed; keep adopting.
        optimistic = False
        lost_race = False
        if not my_slot and pending_slot:
            pending_claim = (slots or {}).get(str(pending_slot)) or {}
            other_peer = pending_claim.get('peer_id', '')
            if other_peer and other_peer != my_peer_id:
                lost_race = True
                self._claim_pending_slot = ''
            else:
                my_slot = str(pending_slot)
                optimistic = True
        # Stale-slot detection. team_size can shrink under us (a
        # teammate dropped a device count, the project switched to
        # a smaller split, etc.) leaving this device's claim
        # pointing at a slot beyond the new team_size — e.g. claim
        # at slot 6, new team_size 4 → "[6/4]" nonsense in the
        # progress label. Treat as displacement: release the stale
        # claim daemon-side so other peers don't see a phantom
        # roster entry, clear my_slot locally, and let the
        # picker-fire branch below prompt the user to pick a slot
        # in the new team_size range.
        stale_slot = False
        if team_size and my_slot:
            try:
                if int(my_slot) > team_size:
                    stale_slot = True
            except (TypeError, ValueError):
                pass
        if stale_slot:
            print(f'[split] stale slot {my_slot} exceeds new '
                  f'team_size {team_size}; releasing and prompting '
                  f'for a fresh slot', file=sys.stderr)
            import threading
            threading.Thread(
                target=self._release_stale_slot_worker,
                args=(langcode,), daemon=True,
                name='release-stale-slot').start()
            my_slot = ''
        if team_size and not my_slot and not stale_slot:
            print(f'[split] no slot matched: peer_id={my_peer_id!r} '
                  f'slots_keys={sorted(slots.keys()) if slots else []!r}',
                  file=sys.stderr)
        self.recorder.split_team_size = team_size
        self.recorder.split_my_slot = my_slot
        # Update the App-level cache so a future
        # _reload_and_restore (fresh controller, fresh
        # recorder.split_*) doesn't lose the last-known-good
        # values to the transient-zero guard.
        if team_size:
            self._last_known_team_size = team_size
        if my_slot:
            self._last_known_my_slot = my_slot
        if peer_pref('cawl_filter_source', None) == 'split':
            if team_size and my_slot:
                sorted_nums = self.recorder.sorted_cawl_numbers()
                try:
                    slot_int = int(my_slot)
                except (TypeError, ValueError):
                    slot_int = 0
                new_range = RecorderController.compute_split_range(
                    sorted_nums, team_size, slot_int)
                if new_range and new_range != self.recorder.cawl_filter:
                    self.recorder.cawl_filter = new_range
                    set_peer_pref('cawl_filter', new_range)
                    self.recorder.rebuild_queue()
            else:
                if self.recorder.cawl_filter:
                    self.recorder.cawl_filter = ''
                    set_peer_pref('cawl_filter', None)
                    self.recorder.rebuild_queue()
        cs = getattr(self, 'config_screen', None)
        if cs is not None:
            try:
                cs._refresh_split_rows()
            except Exception as ex:
                print(f'[split] refresh_split_rows raised: {ex}',
                      file=sys.stderr)
        # Clear the pending token ONLY when the daemon's state is
        # observable — peer_id confirmation, or the lost-race
        # branch above where some other claim now owns the slot.
        # Keep pending alive across optimistic adoption so the
        # next populate cycle (often a fast ContentObserver
        # wakeup after the daemon commits) still adopts the same
        # value — preventing the user's freshly-picked slot from
        # flickering off the row before the daemon commit lands.
        if my_slot and not optimistic:
            self._claim_pending_slot = ''
        if team_size and not my_slot:
            reason = 'team_size_shrunk' if stale_slot else None
            self._show_slot_picker(
                team_size, slots, my_peer_id=my_peer_id, reason=reason)
        self.refresh_recorder_ui()

    def _release_stale_slot_worker(self, langcode):
        """Daemon-side release of this device's claim when team_size
        has shrunk below the claimed slot number. Best-effort; the
        picker that fires in parallel still drives the user toward
        a fresh valid claim regardless of whether the release
        landed. § 17c Rule 7 (daemon RPC off the main thread)."""
        try:
            from azt_collab_client import release_slot
            release_slot(langcode)
        except Exception as ex:
            print(f'[split] release of stale slot raised: {ex}',
                  file=sys.stderr)

    def _show_slot_picker(self, team_size, slots,
                          my_peer_id='', reason=None):
        """Modal "Which device is this?" picker. Both the available
        row and the claimed-by-other list are rendered by the
        shared ``_render_slot_picker_into`` helper — the inline
        filter-modal slot row uses the same renderer so a fix to
        one is a fix to both.

        *my_peer_id* — derives own-slot from ``slots`` (this
        device's existing claim, if any, joins the top row with
        the accent highlight). The recorder's ``split_my_slot``
        is empty when this popup fires (that's why it fires), so
        we derive locally.

        *reason* — ``'team_size_shrunk'`` swaps in the
        "Team configuration changed" wording; ``None`` is the
        first-time-join / displacement case."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        if getattr(self, '_slot_picker_open', False):
            return
        self._slot_picker_open = True

        # Derive my_slot from peer_id match within range. Lets
        # the shared renderer highlight our own claim if it's
        # still in slots (e.g., race between stale-slot release
        # and the popup firing). Stale own-claims outside
        # [1..team_size] are ignored.
        my_slot = ''
        if my_peer_id:
            for s, c in (slots or {}).items():
                if (c or {}).get('peer_id', '') != my_peer_id:
                    continue
                try:
                    if 1 <= int(str(s)) <= team_size:
                        my_slot = str(s)
                        break
                except (TypeError, ValueError):
                    pass

        if reason == 'team_size_shrunk':
            prompt_text = _tr(
                'The team size changed. Pick your new recording slot.')
            prompt_height = dp(56)
            popup_title = _tr('Team configuration changed')
        else:
            prompt_text = _tr('Which device is this?')
            prompt_height = dp(36)
            popup_title = _tr('Pick a recording slot')

        content = BoxLayout(
            orientation='vertical', padding=dp(12), spacing=dp(10))
        prompt = Label(
            text=prompt_text,
            font_size=sp(15), font_name=_FONT_NAME,
            size_hint_y=None, height=prompt_height,
            halign='center', valign='middle')
        prompt.bind(size=lambda w, s: setattr(w, 'text_size', s))
        content.add_widget(prompt)

        available_row = BoxLayout(
            orientation='horizontal', size_hint_y=None,
            height=dp(48), spacing=dp(6))
        content.add_widget(available_row)
        claimed_column = BoxLayout(
            orientation='vertical', size_hint_y=None,
            height=0, spacing=dp(4))
        content.add_widget(claimed_column)

        # Use an Object holder so on_pick can close the popup
        # before it's even constructed (popup ref filled in
        # below after we have height).
        popup_ref = [None]

        def _on_pick(slot_str):
            from azt_collab_client import (
                claim_slot as _claim_slot, get_contributor)
            popup = popup_ref[0]
            if not (get_contributor() or '').strip():
                if popup:
                    popup.dismiss()
                self._slot_picker_open = False
                self._show_contributor_required_for_slot()
                return
            langcode = (getattr(self, '_current_langcode', '')
                        or '')
            if not langcode:
                if popup:
                    popup.dismiss()
                self._slot_picker_open = False
                return
            self._claim_pending_slot = str(slot_str)
            ok = _claim_slot(langcode, slot_str)
            if popup:
                popup.dismiss()
            self._slot_picker_open = False
            if not ok:
                self._claim_pending_slot = ''
                print(f'[split] claim_slot({slot_str}) returned '
                      f'False; will retry on next sync',
                      file=sys.stderr)
                return
            set_peer_pref('cawl_filter_source', 'split')
            # Optimistic mirror — same fix as the inline picker.
            # Worker apply confirms via peer_id; lost-race rolls
            # back to '' and re-fires the picker.
            if self.recorder is not None:
                self.recorder.split_my_slot = str(slot_str)
            self._populate_split_state()

        # Render via the shared renderer. state=None — the popup
        # is single-shot; widgets get GC'd when popup dismisses.
        _render_slot_picker_into(
            available_row, claimed_column,
            team_size, my_slot, slots, _on_pick, state=None)

        # Compute popup height from the now-known row sizes.
        base_h = dp(120) + (prompt_height - dp(36))
        if available_row.height:
            base_h += available_row.height + dp(10)
        if claimed_column.height:
            base_h += claimed_column.height + dp(10)
        popup = Popup(
            title=popup_title,
            content=content,
            size_hint=(0.9, None),
            height=min(base_h, Window.height * 0.92),
            auto_dismiss=False,
        )
        popup_ref[0] = popup
        popup.open()

    def _show_contributor_required_for_slot(self):
        """Route the user to set a contributor name before
        claiming a slot. Uses the canonical
        ``open_server_ui`` deep-link per § 21 hard rule #2."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        from kivy.uix.label import Label
        from azt_collab_client import open_server_ui

        box = BoxLayout(
            orientation='vertical', padding=dp(12), spacing=dp(10))
        msg = Label(
            text=_tr('Set your name in Sync Settings before '
                     'claiming a recording slot. Tap "Open Sync '
                     'Settings" to continue.'),
            font_size=sp(14), font_name=_FONT_NAME,
            halign='center', valign='middle')
        msg.bind(size=lambda w, s: setattr(w, 'text_size', s))
        box.add_widget(msg)
        btn_row = BoxLayout(
            orientation='horizontal', size_hint_y=None,
            height=dp(48), spacing=dp(8))
        cancel_btn = Button(text=_tr('Cancel'), font_size=sp(13))
        open_btn = Button(
            text=_tr('Open Sync Settings'), font_size=sp(13),
            background_color=theme.ACCENT)
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(open_btn)
        box.add_widget(btn_row)
        popup = Popup(
            title=_tr('Name required'),
            content=box,
            size_hint=(0.9, None), height=dp(220),
            auto_dismiss=False)
        cancel_btn.bind(on_release=lambda *_: popup.dismiss())

        def _open(*_):
            popup.dismiss()
            open_server_ui(on_status=self._log_bootstrap_status)
        open_btn.bind(on_release=_open)
        popup.open()

    def _reload_and_restore(self, guid):
        """Reload the LIFT file and restore position to the entry with
        *guid*, refreshing the recorder UI in place per
        CLIENT_INTEGRATION.md § 14. Same anchor (entry guid), fresh
        content (re-parsed from disk).

        If the saved guid would be hidden by a client-side filter
        after the refresh (e.g. only_unrecorded is on and another
        contributor just recorded the entry), suspend the filters
        for this view so the user's anchor stays visible per § 14's
        filter-suspension rule. If the entry is genuinely gone from
        the new model (real upstream deletion), let the natural
        next-entry / empty-queue render so the change is visible."""
        if not self.recorder:
            return
        path = self.recorder.db.path
        try:
            db = LIFTDatabase(path,
                              image_cache_dir=self._get_image_cache_dir())
        except Exception as ex:
            print(f'Reload failed: {ex}')
            return
        # Re-apply the server-authoritative _current_langcode set when
        # the project was loaded. No local fallback — see load_lift's
        # comment on the no-daemon-owned-caches rule.
        authoritative = getattr(self, '_current_langcode', '')
        if authoritative:
            db.set_vernlang(authoritative)
        try:
            db.bind_orphan_audio()
        except Exception as ex:
            print(f'[reload] orphan-audio bind raised: {ex}')
        old_settings = (
            self.recorder.cawl_filter,
            self.recorder.gloss_search,
            self.recorder.only_unrecorded,
            self.recorder.active_gloss_langs[:],
            self.recorder.split_team_size,
            self.recorder.split_my_slot,
        )
        self.recorder = RecorderController(db, langcode=authoritative)
        (self.recorder.cawl_filter, self.recorder.gloss_search,
         self.recorder.only_unrecorded, self.recorder.active_gloss_langs,
         self.recorder.split_team_size,
         self.recorder.split_my_slot) = old_settings
        self.recorder.rebuild_queue()
        if guid:
            for i, e in enumerate(self.recorder.queue):
                if e.get('guid') == guid:
                    self.recorder.index = i
                    self.refresh_recorder_ui()
                    return
            # Anchor not in current queue. If the new model still
            # has the entry, a client-side filter is hiding it; per
            # § 14 drop the filters for this view so the user's
            # anchor stays present even though the data clock moved.
            # ConfigScreen.on_enter re-reads the recorder's filter
            # state into the input fields, so no extra UI sync.
            #
            # Scope: only release ``only_unrecorded``. The common
            # cause of anchor disappearance is "this entry just got
            # recorded (by me or by a paired peer), and the
            # 'unrecorded only' filter now excludes it." Releasing
            # past-work restores the view without disrupting the
            # user's slice. ``cawl_filter`` and ``gloss_search``
            # don't auto-exclude in normal workflow (CAWL numbers
            # don't change under us; gloss text is edited
            # deliberately) — leaving them intact preserves the
            # user's place in a large project.
            in_model = any(
                e.get('guid') == guid for e in self.recorder.db.entries)
            if in_model:
                self.recorder.only_unrecorded = False
                self.recorder.rebuild_queue()
                for i, e in enumerate(self.recorder.queue):
                    if e.get('guid') == guid:
                        self.recorder.index = i
                        break
            # else: real upstream deletion — index is clamped to
            # [0, len(queue)-1] by rebuild_queue, so the user lands
            # on whatever's at the same slot. Per § 14 let the
            # natural propagation render; no toast.
        self.refresh_recorder_ui()
        # § 21: sync may have changed team_size or list_slots
        # under us. Re-pull and re-apply the split filter; trigger
        # the slot picker if this device has been displaced (no
        # peer_id in list_slots) while team_size is still set.
        self._populate_split_state()

    def _clone_via_server(self, clone_url, dest_dir, on_progress=None):
        """Drive a server-side clone job to completion. Schedules
        ``self.load_lift(lift_path)`` on success and ``self._show_error``
        on failure. ``on_progress(line)`` is called on the Kivy main
        thread for each new progress line if provided. Always dismisses
        any loading overlay before reporting."""
        from azt_collab_client import (
            clone_project_start, clone_project_status, translate_result, S)
        import threading
        import time

        def _worker():
            try:
                kicked = clone_project_start(clone_url, dest_dir)
                if not kicked.get('ok'):
                    err = kicked.get('error', 'unknown')
                    Clock.schedule_once(
                        lambda dt: (self._dismiss_loading_overlay(),
                                    self._show_error(err)), 0)
                    return
                job_id = kicked['job_id']
                last_index = 0
                while True:
                    time.sleep(0.5)
                    resp = clone_project_status(job_id, last_index)
                    if not resp.get('ok'):
                        err = resp.get('error', 'server_unavailable')
                        Clock.schedule_once(
                            lambda dt, e=err: (
                                self._dismiss_loading_overlay(),
                                self._show_error(e)), 0)
                        return
                    last_index = resp.get('next_index', last_index)
                    if on_progress:
                        for line in resp.get('progress', []):
                            Clock.schedule_once(
                                lambda dt, ln=line: on_progress(ln), 0)
                    state = resp.get('state', 'CLONING')
                    if state == 'DONE':
                        lift_path = resp.get('lift_path', '')
                        result = resp.get('result')
                        log = (translate_result(result)
                               if result is not None else '')
                        if lift_path:
                            Clock.schedule_once(
                                lambda dt: self.load_lift(lift_path), 0)
                        else:
                            # Daemon attaches CLONE_AUTH_REQUIRED to the
                            # Result when a CLONE_FAILED looks auth-shaped
                            # (server.py:_clone_failed_looks_auth). Branch
                            # on the structured code instead of substring-
                            # matching translated error text.
                            if result and result.has(S.CLONE_AUTH_REQUIRED):
                                Clock.schedule_once(
                                    lambda dt: (
                                        self._dismiss_loading_overlay(),
                                        self._show_collab_prompt()), 0)
                            else:
                                Clock.schedule_once(
                                    lambda dt, e=log: (
                                        self._dismiss_loading_overlay(),
                                        self._show_error(e)), 0)
                        return
                    if state == 'FAILED':
                        err = resp.get('error', 'clone_failed')
                        Clock.schedule_once(
                            lambda dt, e=err: (
                                self._dismiss_loading_overlay(),
                                self._show_error(e)), 0)
                        return
            except Exception as ex:
                # No structured Result on this path (transport blew
                # up before the daemon got to emit one). Auth-shape
                # detection here would have to substring-match a
                # locale-dependent string — skip it; the daemon-emitted
                # CLONE_AUTH_REQUIRED branch above handles real auth
                # failures.
                print(f'[clone] error: {ex}')
                Clock.schedule_once(
                    lambda dt, e=str(ex): (
                        self._dismiss_loading_overlay(),
                        self._show_error(e)), 0)

        threading.Thread(target=_worker, daemon=True).start()

    def _try_auto_publish(self):
        """If git credentials and a project langcode are configured,
        publish automatically. The server runs the publish using its
        own credentials store; the peer just tells it the working
        dir + remote URL.

        Per CLIENT_INTEGRATION.md § 11 the repo name is
        ``project.repo_slug or langcode`` — empty repo_slug falls back
        to the langcode (typical case)."""
        langcode = getattr(self, '_current_langcode', '') or ''
        if not (langcode and self.recorder):
            return
        from azt_collab_client import (
            get_credentials_status, init_project, open_project,
            translate_result)
        slug = ''
        try:
            project = open_project(langcode)
            if project is not None:
                slug = project.repo_slug or ''
        except Exception as ex:
            print(f'[auto-publish] open_project repo_slug read: {ex}')
        repo_name = slug or langcode
        status = get_credentials_status()
        host = status.get('host', 'github')
        if host == 'gitlab':
            gl = status.get('gitlab', {})
            user = gl.get('username', '')
            token_ok = gl.get('connected', False)
            domain = 'gitlab.com'
        else:
            gh = status.get('github', {})
            user = gh.get('username', '')
            token_ok = gh.get('connected', False)
            domain = 'github.com'
        if not (user and token_ok):
            return
        remote_url = f'https://{domain}/{user}/{repo_name}.git'
        import threading

        def _worker():
            try:
                # § 12: contributor is daemon-owned; no longer passed
                # on the wire. init_project resolves it from the store.
                result = init_project(
                    self.recorder.db.dir, remote_url, 'main')
                print(f'[auto-publish] {translate_result(result)}')
            except Exception as ex:
                print(f'[auto-publish] error: {ex}')
        threading.Thread(target=_worker, daemon=True).start()

    def _sync_status_info(self):
        """Return (text, last_sync, last_commit, head_sha) for the
        current project, rendered per CLIENT_INTEGRATION.md § 17b
        v0.47.0 5-state model.

        The label is one of:
          - ``OK``                    — wan=0, lan=0
          - ``WAN-{n}``               — wan>0, lan=0
          - ``LAN-{n}``               — wan=0, lan>0
          - ``WAN-{w}_LAN-{l}``       — wan>0, lan>0, at_risk=0
                                        (rare split-brain)
          - ``WAN-{w} LAN-{l}``       — wan>0, lan>0, at_risk>0
                                        (routine transient right
                                         after a fresh commit)
        Per-channel red coloring rule ("settings allow this to be
        stored but it isn't yet"):
          - ``WAN-{n}`` red iff ``work_offline`` is OFF
          - ``LAN-{n}`` red iff ``lan_allow_sync`` is ON
          - ``+{n}`` always red (auto-commit always runs)
        Suffix: only `` · offline`` surfaces (when work_offline
        is ON and lan_allow_sync is OFF — the "phone in the
        forest" mode). All other toggle states are implied; the
        user can see them elsewhere in the UI.

        Returned tuple's other elements drive non-rendering
        logic: ``last_sync`` for do_sync()'s never-pushed →
        go_collab routing; ``last_commit`` for the legacy
        content-advance fallback; ``head_sha`` for the primary
        HEAD-advance signal in _update_sync_status.
        """
        langcode = getattr(self, '_current_langcode', '')
        if not langcode:
            return ('', 0.0, 0.0, '')
        from azt_collab_client import project_status
        status = project_status(langcode)
        if status is None:
            return ('', 0.0, 0.0, '')
        last_sync = float(getattr(status, 'last_sync', 0.0) or 0.0)
        last_commit = float(getattr(status, 'last_commit', 0.0) or 0.0)
        head_sha = str(getattr(status, 'head_sha', '') or '')
        wan = int(getattr(status, 'wan_unshared', 0) or 0)
        lan = int(getattr(status, 'lan_unshared', 0) or 0)
        at_risk = int(getattr(status, 'at_risk', 0) or 0)
        n_changes = int(getattr(status, 'n_changes', 0) or 0)
        work_offline = bool(getattr(status, 'work_offline', False))
        lan_allow_sync = bool(getattr(status, 'lan_allow_sync', False))
        # Per-channel red rule. Markup tags wrap the count if and
        # only if the channel's toggle currently authorises the
        # sync that hasn't happened yet (transient red = normal
        # automation in flight; persistent red = something's
        # broken). Channel parts with the toggle gating them
        # OFF render black ("you accepted by design").
        RED_OPEN = '[color=ff4444]'
        RED_CLOSE = '[/color]'

        def _maybe_red(text, red):
            return f'{RED_OPEN}{text}{RED_CLOSE}' if red else text

        wan_part = _maybe_red(f'WAN-{wan}', red=not work_offline)
        lan_part = _maybe_red(f'LAN-{lan}', red=lan_allow_sync)
        # State label by the 5-state matrix.
        if wan == 0 and lan == 0:
            label = 'OK'
        elif wan == 0:
            label = lan_part
        elif lan == 0:
            label = wan_part
        elif at_risk == 0:
            # Split-brain (rare): different commits on each
            # channel with no overlap; requires divergent
            # history. Underscore separator distinguishes from
            # the routine both-behind state below.
            label = f'{wan_part}_{lan_part}'
        else:
            # Both behind on the same commits — routine
            # transient right after a fresh commit. Drops to
            # WAN-N or LAN-N as one channel catches up. Space
            # separator (not underscore) so it's visually
            # distinct from split-brain.
            label = f'{wan_part} {lan_part}'
        # Uncommitted-changes badge — always red, separate
        # visual element next to the label. The literal output
        # is just ``+N`` (in red); the ``R(+N)`` form in the
        # contract's design notes is shorthand notation for
        # "red uncommitted badge with value N", not output text.
        badge = (f'{RED_OPEN}+{n_changes}{RED_CLOSE}'
                 if n_changes > 0 else '')
        # Suffix: only · offline surfaces. The · LAN-only and
        # · LAN modes are implied (mode-tag visible elsewhere
        # in the UI; calling them out alongside every status
        # would be noise).
        if work_offline and not lan_allow_sync:
            suffix = f' · {_tr("offline")}'
        else:
            suffix = ''
        text = label
        if badge:
            text = f'{text} {badge}'
        if suffix:
            text = f'{text}{suffix}'
        return (text.strip(), last_sync, last_commit, head_sha)

    def _update_sync_status(self):
        """Push sync status text into the recorder top bar, and
        detect external mutations by watching for HEAD advance
        across polls. On a change, refresh the recorder UI in
        place per CLIENT_INTEGRATION.md § 14 — same anchor entry,
        fresh content.

        Per § 17b "Background refresh obligation — peer MUST
        re-poll AND re-read content on HEAD advance" the primary
        signal is ``project_status.head_sha`` (daemon 0.45.45+),
        which catches incoming LAN receive-packs that
        ``last_commit`` alone misses. Legacy daemons (empty
        ``head_sha``) fall back to the pre-0.45.45 last_commit
        signal — strictly less reliable but covers everything the
        old daemon could surface anyway.

        Also polls the most recent auto-commit job (if any) for
        DATA_LOSS_RISK statuses per § 17 — commit_project is
        fire-and-forget so this is the only seam where the
        eventual Result becomes observable for the auto-commit
        context."""
        self._check_data_loss_risk_async()
        # Retry any LIFT saves stranded by a transient daemon
        # hiccup. Idempotent + silent on success; failures stay
        # queued for the next tick.
        if self.recorder:
            self.recorder.retry_pending_lift_saves()
        text, _last_sync, last_commit, head_sha = self._sync_status_info()
        sm = self.root.ids.sm
        rec_screen = sm.get_screen('recorder')
        lbl = rec_screen.ids.get('sync_status_label')
        if lbl:
            lbl.text = text
        # Content-advance detection. Prefer head_sha (catches
        # LAN receive-packs); fall back to last_commit when the
        # daemon doesn't surface head_sha.
        last_seen_head = getattr(self, '_last_head_sha', None)
        last_seen_commit = getattr(self, '_last_commit_seen', None)
        if head_sha:
            content_advanced = (last_seen_head is not None
                                and head_sha != last_seen_head)
        elif last_commit > 0:
            content_advanced = (last_seen_commit is not None
                                and last_commit > last_seen_commit)
        else:
            content_advanced = False
        mid_record = (self.recorder is not None
                      and getattr(self.recorder, '_recording', False))
        # Defer the reload while the user is mid-recording — re-
        # parsing the LIFT mid-take would yank the model out
        # from under stop_recording's set_audio write. Leaving
        # the seen values stale means the next post-stop tick
        # sees the same diff and reloads then. § 17b
        # "content-reload cost" bullet.
        if content_advanced and mid_record:
            pass  # defer; next post-stop poll handles it
        else:
            # Pin the seen values BEFORE the reload (the reload
            # path eventually re-enters this method via
            # refresh_recorder_ui; pinning first prevents the
            # nested call from re-detecting the same diff and
            # firing another reload — an infinite chain).
            if head_sha:
                self._last_head_sha = head_sha
            self._last_commit_seen = last_commit
        if content_advanced and not mid_record and self.recorder:
            guid = (self.recorder.current.get('guid', '')
                    if self.recorder.queue else '')
            self._reload_and_restore(guid)

    def _mark_gh_app_installed(self):
        """Record that the GitHub App has been installed after a successful push."""
        from azt_collab_client import mark_github_app_installed
        mark_github_app_installed(True)

    def _auto_commit_sync(self):
        """Fire-and-forget: ask the server to commit the current
        group of changes. The peer's job is to mark the boundary
        between meaningful chunks of work (one swipe = one entry's
        worth of changes); the daemon's scheduler-drain loop decides
        when to push.

        Runs on a worker thread per § 17c Rule 7. The daemon's
        ``project_lock`` can be held by an incoming LAN receive-pack
        for seconds; calling ``commit_project`` synchronously on
        the swipe path would freeze the UI until the merge finished.
        With the worker the swipe returns immediately; the commit
        debounces and runs whenever the daemon's lock clears.

        Per CLIENT_INTEGRATION.md § 17b (0.43.0+): this RPC NEVER
        emits PUSHED — push is daemon-driven. Peer surface for
        "are we pushed up?" reads ProjectStatus.wan_unshared /
        ProjectStatus.lan_unshared / ProjectStatus.at_risk
        (v0.47.0+) / ProjectStatus.work_offline."""
        if not self.recorder:
            return
        langcode = self._current_langcode_or_register()
        if not langcode:
            return
        # § 17c Rule 1 — share the in-flight guard with do_sync. While
        # sync_project holds the daemon-side project_lock, a parallel
        # commit_project would hit S.BUSY and the peer would pile up
        # redundant calls. Drop, do NOT queue.
        in_flight = getattr(self, '_sync_in_flight', {})
        if in_flight.get(langcode):
            return
        in_flight[langcode] = True
        self._sync_in_flight = in_flight
        import threading
        threading.Thread(
            target=self._auto_commit_sync_worker,
            args=(langcode,),
            daemon=True,
            name='auto-commit').start()

    def _auto_commit_sync_worker(self, langcode):
        try:
            from azt_collab_client import (
                commit_project, ServerUnavailable)
            job_id = commit_project(langcode)
            # Stash for the data-loss-risk poller in
            # _update_sync_status. commit_project is fire-and-forget,
            # so DATA_LOSS_RISK on this code path can only be
            # observed by poll_job-ing the eventual Result. The
            # daemon's debounce collapses bursts into one run, so
            # only the latest job_id matters — overwriting on each
            # request is the right dedupe.
            if job_id:
                self._latest_sync_job_id = job_id
        except ServerUnavailable as ex:
            print(f'[auto-sync] sync service unavailable: {ex}',
                  file=sys.stderr, flush=True)
            _log_server_crash_if_any('auto_sync')
        except Exception as ex:
            print(f'[auto-sync] error: {ex}',
                  file=sys.stderr, flush=True)
        finally:
            try:
                self._sync_in_flight[langcode] = False
            except Exception:
                pass
            # Refresh the recorder's sync status indicator on the
            # main thread slightly after the debounce window so a
            # successful job's last_sync is in place.
            Clock.schedule_once(
                lambda dt: self._update_sync_status(), 1.5)

    def _check_data_loss_risk_async(self):
        """Poll the most recent ``commit_project`` job for
        never-silenced sync signals (``S.DATA_LOSS_RISK`` and
        ``S.COMMIT_REPEATEDLY_FAILED``) per CLIENT_INTEGRATION.md
        § 17.

        Auto-commit goes through ``commit_project``
        (fire-and-forget, daemon-debounced), which only returns a
        job_id — not a Result. The eventual Result becomes
        observable via ``poll_job(job_id)``, which is what this
        method does. Called from ``_update_sync_status`` (every
        30s tick + on events that already touch the status
        indicator).

        DATA_LOSS_RISK is never silenced per contract — render
        the translated toast as soon as we see it. Drops the
        stashed job_id once we observe a terminal state so we
        don't poll the same finished job forever; the next
        ``commit_project`` will stash a fresh one."""
        job_id = getattr(self, '_latest_sync_job_id', '')
        if not job_id:
            return
        try:
            from azt_collab_client import poll_job, translate_status, S
        except ImportError:
            return
        try:
            info = poll_job(job_id)
        except Exception as ex:
            print(f'[auto-sync] poll_job({job_id!r}) failed: {ex}',
                  file=sys.stderr)
            return
        if info is None:
            # Unknown job — daemon evicted it. Stop polling.
            self._latest_sync_job_id = ''
            return
        state = info.get('state', '')
        if state in ('PENDING', 'RUNNING'):
            return  # check again next tick
        result = info.get('result')
        # Terminal — DONE or otherwise. Drop the job_id so we don't
        # re-observe the same Result on every status tick. Next
        # commit_project will stash a fresh one.
        self._latest_sync_job_id = ''
        if result is None:
            return
        # Surface both never-silenced codes per
        # CLIENT_INTEGRATION.md § 17 routing table. Banner, not
        # toast — see _show_severe_alert for rationale.
        for _severe_code in (S.DATA_LOSS_RISK,
                             S.COMMIT_REPEATEDLY_FAILED):
            _severe = next((s for s in result.statuses
                            if s.code == _severe_code), None)
            if _severe is None:
                continue
            msg = translate_status(_severe)
            print(f'[auto-sync] {_severe_code}: {msg}',
                  file=sys.stderr, flush=True)
            Clock.schedule_once(
                lambda dt, m=msg: self._show_severe_alert(m), 0)

    def start_over(self):
        """Spawn the picker directly. Runs the auto-commit/sync and
        the picker call in the same worker so the tap returns
        immediately — keeping the main thread free for the picker's
        own UI-thread JNI dispatch (Clock.schedule_once'd inside
        pick_project on Android)."""
        from azt_collab_client import pick_project
        import threading

        def _worker():
            try:
                self._auto_commit_sync()
            except Exception as ex:
                print(f'[start_over] auto_commit_sync failed: {ex}')
            result = pick_project()
            Clock.schedule_once(
                lambda dt: self._handle_pick(result), 0)
        threading.Thread(target=_worker, daemon=True).start()

    def _handle_pick(self, result):
        if result.get('ok'):
            langcode = result.get('langcode', '')
            if langcode:
                # Picker emits langcode for both new-from-template and
                # existing-project selections (picker.py post-0.17.x
                # adds the existing-project case). Cache as the daemon
                # registry key so _register_current_project can skip
                # the derive_langcode round-trip.
                #
                # We deliberately do NOT also set _pending_vernlang
                # here. _pending_vernlang is the "this is a fresh
                # template clone, run clean_template" signal — set by
                # the langpicker UI before the new-from-template flow
                # reaches the picker (langpicker.py _on_continue).
                # Overwriting it on every pick caused existing-project
                # opens to run a full clean_template + re-parse + save
                # round-trip on every load.
                self._current_langcode = langcode
                # Suite-wide "last opened project" state: any peer
                # opening next lands here too.
                try:
                    from azt_collab_client import set_last_project
                    set_last_project(langcode)
                except Exception as ex:
                    print(f'[recent] set_last_project: {ex}')
            # Loading overlay while the synchronous LIFT parse runs
            # — same rationale as _auto_load_last_project. Deferred
            # by one frame so the overlay paints before load_lift
            # blocks the main thread.
            name = (_lang_display_name(langcode) if langcode
                    else os.path.basename(result.get('path', '')))
            self._show_loading_overlay(
                _tr('Loading {name}…').format(name=name))
            Clock.schedule_once(
                lambda dt: self.load_lift(result['path']), 0)
            return
        err = result.get('error', 'unknown')
        if err == 'cancelled':
            # CLIENT_INTEGRATION.md § 14a: picker-cancel writes
            # nothing to the daemon, so the discriminator for
            # "first-setup exit" vs "user changed their mind" is
            # peer-side state, NOT the daemon's last_project().
            # The contract phrases this as "_current_langcode is
            # None *and* picker came back without a selection".
            # We're already in the cancelled branch, so the second
            # condition is implicit; check self.recorder (the
            # recorder's peer-specific equivalent of "I have a
            # project loaded").
            #
            # § 14a "Exception — first-boot picker-cancel: App.stop()
            # is correct" carves this out as the only legit stop()
            # in any picker / on_resume flow. Anywhere else, an
            # exit would lose the user's place and look like a
            # crash (Android does NOT auto-restart on App.stop()).
            #
            # Daemon-side invariants since 0.43.5: an empty
            # last_project() is impossible outside first-boot — so
            # the bootstrap-race tiebreaker below should never fire
            # in practice; if it does, log so a daemon regression
            # is visible.
            if self.recorder is not None:
                return
            try:
                from azt_collab_client import last_project
                resume = last_project()
            except Exception:
                resume = ''
            if not resume:
                self.stop()
                return
            print('[peer] picker cancel with last_project='
                  f'{resume!r} but no peer-side recorder; '
                  'falling through — daemon regression suspected '
                  '(§ 14a invariant since 0.43.5)')
            return
        # 'server_apk_not_installed' falls through to the generic
        # error below; bootstrap() owns the install prompt at startup
        # and we don't compete with it.
        self._show_error(_tr(
            'Could not open project picker: {error}')
                .format(error=err))

    def go_config(self):
        self._auto_commit_sync()
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'config'

    def go_collab(self):
        """Open the server's collab settings UI. The recorder no
        longer hosts its own setup screen — there's one canonical
        settings UI in the suite, owned by azt_collabd, reached via
        azt_collab_client.open_server_ui(). _auto_commit_sync runs
        in a worker so a slow commit_project RPC can't freeze the tap
        (same pattern as start_over)."""
        from azt_collab_client import open_server_ui as _open_server_ui
        import threading

        def _worker():
            try:
                self._auto_commit_sync()
            except Exception as ex:
                print(f'[go_collab] auto_commit_sync failed: {ex}')
            result = _open_server_ui(
                on_status=lambda m: print(f'[go_collab] {m}'))
            if result.get('ok') or result.get('prompted'):
                return
            err = result.get('error', 'unknown')
            if err == 'desktop_only':
                msg = _tr('Sync settings UI is desktop-only for now.')
            else:
                # 'server_apk_not_installed' falls through; bootstrap()
                # owns the install prompt at startup.
                msg = _tr(
                    'Could not open sync settings: {error}'
                ).format(error=err)
            # If the failure isn't one of the benign environment
            # cases, the daemon may have a crash worth surfacing.
            # 'spawn_exited' especially — that's exactly the case
            # the daemon's crash.log was written for.
            if err not in ('desktop_only', 'server_apk_not_installed'):
                _log_server_crash_if_any('go_collab')
            Clock.schedule_once(lambda dt: self._show_toast(msg), 0)
        threading.Thread(target=_worker, daemon=True).start()

    def share_apk(self):
        from azt_collab_client.ui import share_running_apk
        share_running_apk(on_error=self._show_error)

    def check_for_update_explicit(self):
        """User-initiated update check (Update button in the
        settings title bar). Always shows a modal with the
        outcome — the user explicitly asked, so a silent no-op
        leaves them guessing whether the tap registered, the
        network is down, or there's just no newer release.

        Distinct from the silent self-update probe in bootstrap,
        which only surfaces UI when a newer version is available.
        Calls azt_collab_client.ui.check_for_update which spawns
        its own worker and marshals callbacks back to the UI
        thread, so the lambdas below can update the modal's
        status label directly."""
        from azt_collab_client.ui import check_for_update
        # Invalidate the per-process release cache before probing.
        # _fetch_latest keeps a 5-minute TTL cache so the bootstrap
        # self-update probe + this explicit tap don't double-pay
        # GitHub. But that cache makes a cache-hit indistinguishable
        # from "we just successfully re-fetched" — so if the user
        # turns off the network between bootstrap and tapping
        # Update, the cached release still satisfies the check and
        # on_no_update fires instead of on_error. Forcing a fresh
        # probe makes the URLError surface honestly.
        from azt_collab_client.ui.update import invalidate_release_cache
        repo = 'kent-rasmussen/azt-recorder'
        invalidate_release_cache(repo)
        from kivy.uix.modalview import ModalView
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.button import Button

        def _friendly_error(err):
            """Prepend a user-readable explanation to
            check_for_update's raw error string. Errno 7
            / -2 / 'no address' / 'getaddrinfo' / 'name
            resolution' all mean DNS couldn't resolve
            api.github.com — almost always "no internet"
            on a phone. Keeps the technical detail
            below so a log / support request still has
            what failed."""
            err_str = str(err)
            low = err_str.lower()
            if ('errno 7' in low or 'errno -2' in low
                    or 'no address' in low
                    or 'name or service' in low
                    or 'name resolution' in low
                    or 'getaddrinfo' in low):
                primary = _tr(
                    'Could not reach GitHub. Check your '
                    'internet connection and try again.')
            elif 'timed out' in low or 'timeout' in low:
                primary = _tr(
                    'Update check timed out. The network may be '
                    'slow or unreachable. Try again later.')
            elif ('ssl' in low or 'certificate' in low
                    or 'cert ' in low):
                primary = _tr(
                    'Update check failed: SSL certificate error. '
                    'Check that your device date and time are '
                    'correct.')
            elif ('connection refused' in low
                    or 'connection reset' in low):
                primary = _tr(
                    'Connection to GitHub was refused or reset. '
                    'The network may be blocking it.')
            else:
                primary = _tr(
                    'Could not check for updates. This usually '
                    'means your device is not connected to the '
                    'internet.')
            return (f'{primary}\n\n'
                    f'{_tr("Detail")}: {err_str}')

        view = ModalView(
            size_hint=(0.9, None), height=dp(320),
            background_color=theme.OVERLAY_DARK,
            auto_dismiss=False,
        )
        box = BoxLayout(
            orientation='vertical',
            padding=dp(14), spacing=dp(8),
        )
        title = Label(
            text=_tr('Check for updates'),
            font_size=sp(16), font_name=_FONT_NAME,
            color=theme.TEXT, bold=True,
            size_hint_y=None, height=dp(28),
        )
        # Status label inside a ScrollView so long error
        # messages (raw URLError text appended by
        # _friendly_error's "Detail:") wrap and stay
        # reachable — without it a multi-line errno string
        # ran off the bottom of the modal under the close
        # button.
        from kivy.uix.scrollview import ScrollView
        scroll = ScrollView(size_hint=(1, 1))
        status = Label(
            text=_tr('Checking for updates…'),
            font_size=sp(14), font_name=_FONT_NAME,
            color=theme.TEXT_DIM,
            halign='left', valign='top',
            size_hint=(1, None),
        )
        status.bind(
            width=lambda _w, v:
                setattr(status, 'text_size', (v - dp(4), None)),
            texture_size=lambda _w, v:
                setattr(status, 'height', v[1] + dp(8)),
        )
        scroll.add_widget(status)
        close = Button(
            text=_tr('Close'),
            font_size=sp(15), font_name=_FONT_NAME,
            size_hint_y=None, height=dp(44),
            background_color=theme.SURFACE,
            background_normal='',
            color=theme.TEXT,
        )
        close.bind(on_release=lambda *_: view.dismiss())
        box.add_widget(title)
        box.add_widget(scroll)
        box.add_widget(close)
        view.add_widget(box)
        view.open()
        check_for_update(
            repo=repo,
            current_version=__version__,
            on_status=lambda msg: setattr(status, 'text', msg),
            on_no_update=lambda: setattr(
                status, 'text',
                _tr('You are running the latest version.')),
            on_error=lambda err: setattr(
                status, 'text', _friendly_error(err)),
        )

    def share_log(self):
        """Share the recorder's log file via Android's share sheet.

        Delegates to ``azt_collab_client.ui.share.share_log_file``
        (shipped daemon-side 0.41.19). The helper bundles the
        current session plus the rotated `<path>.prev` previous
        session into one text/plain blob, inserts via MediaStore
        Downloads, and dispatches ACTION_SEND."""
        # Imported via the module path because share_log_file isn't
        # re-exported from azt_collab_client.ui.__init__ yet — keeps
        # this peer-side call working independent of when the daemon
        # team adds the convenience re-export.
        from azt_collab_client.ui.share import share_log_file
        share_log_file(
            log_path=_LOG_PATH,
            prev_path=(_LOG_PATH + '.prev') if _LOG_PATH else None,
            on_error=self._show_error,
        )

    def do_sync(self):
        if not self.recorder:
            return
        from azt_collab_client import (
            sync_project, translate_result, translate_status, S)
        langcode = self._current_langcode_or_register()
        if not langcode:
            return
        # If nothing has ever been pushed (last_sync == 0), the
        # user's tap on the sync indicator is really asking to set
        # up backup, not to sync. Route to the server's collab UI
        # directly so they land where Publish lives. (Pre-v0.47.0
        # the badge surfaced "not backed up" wording for this
        # state; the v0.47.0 model encodes it as a WAN-N count
        # instead, but the routing trigger is unchanged.)
        _, last_sync, _last_commit, _head_sha = self._sync_status_info()
        if not last_sync:
            self.go_collab()
            return
        # CLIENT_INTEGRATION.md § 17c Rule 3 — peer-side debounce on
        # the Sync button (250 ms). sync_project is NOT debounced
        # server-side, so a fast double-tap fires two parallel calls;
        # Rule 1 below drops the second silently but the debounce
        # avoids even queuing the second worker thread.
        import time as _time
        now = _time.monotonic()
        last_tap = getattr(self, '_last_sync_tap_at', {})
        if now - last_tap.get(langcode, 0.0) < 0.25:
            return
        last_tap[langcode] = now
        self._last_sync_tap_at = last_tap
        # § 17c Rule 1 — single in-flight guard per (RPC, project).
        # Drop, do NOT queue, while a prior mutating RPC for this
        # project is still in flight. Shared with _auto_commit_sync
        # so commit_project also defers while sync_project holds the
        # daemon-side project_lock (would otherwise hit S.BUSY).
        in_flight = getattr(self, '_sync_in_flight', {})
        if in_flight.get(langcode):
            return
        in_flight[langcode] = True
        self._sync_in_flight = in_flight
        import threading

        saved_guid = self.recorder.current.get('guid', '') if self.recorder.queue else ''
        # Mutable container so the retry branch can ask finally NOT to
        # release the flag — the retry worker re-enters _on_sync_done
        # (retried=True) and that invocation's finally does the release.
        _keep_held = [False]

        def _on_sync_done(result, retried=False):
            try:
                _on_sync_done_inner(result, retried)
            finally:
                # § 17c Rule 1 — clear the in-flight flag on the "post"
                # branch unless we just spawned a JOB_INTERRUPTED retry;
                # in that case the retried=True invocation's own finally
                # clears it instead.
                if not _keep_held[0]:
                    self._sync_in_flight[langcode] = False
                _keep_held[0] = False

        def _on_sync_done_inner(result, retried=False):
            print(f'[do_sync] {translate_result(result)}')
            # Structure per azt_collab_client/CLAUDE.md "Peer contract:
            # routing on sync results" (do_sync example, lines 389-430):
            #
            # 0. DATA_LOSS_RISK — never silenced (CLIENT_INTEGRATION.md
            #    § 17, since 0.41.13). The daemon emits this when files
            #    written by this peer aren't reaching git — surface
            #    BEFORE any routing branch consumes the result so the
            #    warning can't be hidden by a benign code that lands
            #    in the same Result. Auto/user distinction does NOT
            #    apply; both contexts must render the translated
            #    toast / banner with the maintainer-contact wording.
            #
            # 1. AUTH_REFRESH_STALE — toast first, no return. It
            #    piggybacks on the primary code, so the deadline
            #    warning must surface before any routing branch
            #    consumes the result.
            # 2. Routing branches (elif chain) on the primary code:
            #    NOT_A_REPO/NO_REMOTE  → publish settings
            #    AUTH_REQUIRED         → GitHub Connect
            #    APP_NOT_INSTALLED /
            #    APP_SUSPENDED /
            #    REPO_NOT_AUTHORIZED   → open params['url']
            #                            (fallback: GitHub Connect)
            #    SERVER_UNAVAILABLE /
            #    SERVER_ERROR          → transient toast
            #    JOB_INTERRUPTED       → silent one-shot retry; toast
            #                            on second failure
            # 3. Success path (else): PUSHED / PULLED / NOTHING_TO_COMMIT
            #    etc. Refresh the sync indicator + recorder-specific
            #    follow-ups (PULLED reload per CLIENT_INTEGRATION § 14,
            #    PUSHED app-installed mark).
            #
            # In this peer the server's one-size-fits-all settings UI
            # (go_collab) hosts both Publish and GitHub Connect — so
            # NOT_A_REPO/NO_REMOTE/AUTH_REQUIRED all route there.
            # DATA_LOSS_RISK + COMMIT_REPEATEDLY_FAILED are the two
            # never-silenced signals — surface BOTH before any
            # routing branch consumes the result. Both are
            # data-loss-class per CLIENT_INTEGRATION.md § 17
            # routing table; COMMIT_REPEATEDLY_FAILED specifically
            # catches the catchup-commit pattern (one fat commit
            # after a streak of local-commit failures) — distinct
            # from network-out, where push fails but commit
            # succeeds.
            for _severe_code in (S.DATA_LOSS_RISK,
                                 S.COMMIT_REPEATEDLY_FAILED):
                _severe = next((s for s in result.statuses
                                if s.code == _severe_code), None)
                if _severe is not None:
                    _severe_msg = translate_status(_severe)
                    # Sticky banner, not toast — these messages
                    # tell the user to share the daemon log and
                    # 1.5s isn't long enough to read the
                    # instruction. The banner persists until tap.
                    Clock.schedule_once(
                        lambda dt, m=_severe_msg:
                            self._show_severe_alert(m), 0)
            stale = next((s for s in result.statuses
                          if s.code == S.AUTH_REFRESH_STALE), None)
            if stale is not None:
                _stale_msg = translate_status(stale)
                Clock.schedule_once(
                    lambda dt, m=_stale_msg: self._show_toast(m), 0)

            if result.has_any(S.NOT_A_REPO, S.NO_REMOTE):
                Clock.schedule_once(lambda dt: self.go_collab(), 0)
                return
            if result.has(S.AUTH_REQUIRED):
                Clock.schedule_once(lambda dt: self.go_collab(), 0)
                return
            if result.has(S.CONTRIBUTOR_UNSET):
                # § 12: daemon refuses commit-issuing endpoints when no
                # contributor name is set. Route through go_collab —
                # same App-level entry every other config-class branch
                # in this table uses (AUTH_REQUIRED above, NOT_A_REPO,
                # NO_REMOTE below). go_collab handles the settings-UI
                # dispatch + the desktop-only fallback message.
                _msg = translate_result(result)
                Clock.schedule_once(
                    lambda dt, m=_msg: self._show_toast(m), 0)
                Clock.schedule_once(lambda dt: self.go_collab(), 0)
                return
            if result.has(S.WORK_OFFLINE_ENABLED):
                # Daemon-wide work-offline toggle is on; sync_project
                # refused without attempting any push. Per
                # CLIENT_INTEGRATION.md § 17 routing, toast + route
                # to the daemon settings UI so the user can flip the
                # toggle. Same shape as AUTH_REQUIRED / NOT_A_REPO.
                _msg = translate_result(result)
                Clock.schedule_once(
                    lambda dt, m=_msg: self._show_toast(m), 0)
                from azt_collab_client import open_server_ui
                Clock.schedule_once(lambda dt: open_server_ui(), 0)
                return
            if result.has_any(S.APP_NOT_INSTALLED, S.APP_SUSPENDED,
                              S.REPO_NOT_AUTHORIZED):
                _url = next(
                    (s.params.get('url', '') for s in result.statuses
                     if s.code in (S.APP_NOT_INSTALLED,
                                   S.APP_SUSPENDED,
                                   S.REPO_NOT_AUTHORIZED)),
                    '')
                if _url:
                    import webbrowser
                    webbrowser.open(_url)
                else:
                    Clock.schedule_once(
                        lambda dt: self.go_collab(), 0)
                return
            if result.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR):
                _unavail_msg = translate_result(result)
                Clock.schedule_once(
                    lambda dt, m=_unavail_msg: self._show_toast(m), 0)
                _log_server_crash_if_any('do_sync')
                return
            if result.has(S.JOB_INTERRUPTED):
                if retried:
                    Clock.schedule_once(lambda dt: self._show_toast(
                        _tr('Sync interrupted, please try again.')), 0)
                    return
                # Silent one-shot retry on a fresh worker thread.
                # Tell the outer finally to hold the in-flight flag —
                # the retry's _on_sync_done(retried=True) clears it.
                _keep_held[0] = True
                def _retry_worker():
                    # § 12: contributor is daemon-owned.
                    try:
                        r = sync_project(langcode)
                        Clock.schedule_once(
                            lambda dt, rr=r:
                                _on_sync_done(rr, retried=True),
                            0)
                    except Exception as ex:
                        # Same belt-and-suspenders as the initial
                        # _worker: don't strand the in-flight flag.
                        print(f'[do_sync] retry worker error: {ex}',
                              file=sys.stderr, flush=True)
                        self._sync_in_flight[langcode] = False
                threading.Thread(
                    target=_retry_worker, daemon=True).start()
                return

            # Success path: PUSHED / PULLED / NOTHING_TO_COMMIT /
            # CONFLICTS / etc. Refresh the sync indicator regardless
            # of outcome — the server may have stamped a new last_sync
            # even on partial success, and a no-op result still
            # benefits from re-reading project_status in case another
            # peer pushed since we loaded.
            self._update_sync_status()
            # Per CLIENT_INTEGRATION.md § 14, only refresh the
            # recorder UI when on-disk bytes actually changed — i.e.
            # remote→local was pulled. Local→remote-only pushes don't
            # invalidate our in-memory model, so skip the reparse.
            if result.has(S.PULLED):
                self._reload_and_restore(saved_guid)
            if result.has(S.PUSHED) or result.has(S.COMMITTED_AND_PUSHED):
                self._mark_gh_app_installed()

        def _worker():
            # § 12: contributor is daemon-owned, no longer on the wire.
            try:
                result = sync_project(langcode)
                Clock.schedule_once(lambda dt: _on_sync_done(result), 0)
            except Exception as ex:
                # Belt-and-suspenders: if sync_project raises something
                # the client didn't wrap into a Result, _on_sync_done
                # never runs and its finally never clears the in-flight
                # flag — the user would be locked out of Sync forever.
                # Clear here and surface the error in the log.
                print(f'[do_sync] worker error: {ex}',
                      file=sys.stderr, flush=True)
                self._sync_in_flight[langcode] = False
        threading.Thread(target=_worker, daemon=True).start()

    def show_image_picker(self):
        if not self.recorder or not self.recorder.current:
            return
        sm = self.root.ids.sm
        picker = sm.get_screen('imagepicker')
        picker.populate(self.recorder.current)
        sm.transition = SlideTransition(direction='left')
        sm.current = 'imagepicker'

    def go_recorder(self):
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='right')
        sm.current = 'recorder'
        Clock.schedule_once(lambda dt: self.refresh_recorder_ui(), 0.1)

    def record_start(self):
        if self.recorder:
            self.recorder.start_recording()

    def record_stop(self):
        if self.recorder:
            self.recorder.stop_recording()

    def nav_prev(self):
        if not self.recorder:
            return
        # Only bake images and trigger a sync if the user actually
        # changed something on the current entry. Pure browse swipes
        # are free.
        if self.recorder._dirty:
            self._save_remote_image()
            self._auto_commit_sync()
            self.recorder._dirty = False
        self.recorder.go_prev()

    def nav_next(self):
        if not self.recorder:
            return
        if self.recorder._dirty:
            self._save_remote_image()
            self._auto_commit_sync()
            self.recorder._dirty = False
        self.recorder.go_next()

    def _save_image_for_entry(self, pil_img, entry, fmt='PNG'):
        """Write *pil_img* as *entry*'s canonical illustration into the
        project. Handles both URI projects (via MediaHandle through the
        daemon's provider, per the contract that shipped in 0.35.2) and
        desktop / iOS (direct filesystem write into db.images_dir).
        Updates the LIFT XML's <illustration href> via set_illustration
        and mirrors the path/href onto the entry dict for immediate
        display. Marks the recorder dirty so the surrounding nav_next /
        nav_prev fires _auto_commit_sync. Returns the local display
        path or '' on failure.

        Safe to call from a background thread — the LIFT write inside
        set_illustration / _save tolerates that on URI projects
        (ContentProvider open is JNI-thread-safe), and the entry dict
        updates here aren't Kivy properties so don't need the main
        thread."""
        if not self.recorder:
            return ''
        db = self.recorder.db
        filename = db.imagename(entry)
        guid = entry.get('guid', '')
        if getattr(db, 'is_uri', False):
            import io
            from azt_collab_client import MediaHandle
            buf = io.BytesIO()
            try:
                pil_img.save(buf, fmt)
            except Exception as ex:
                print(f'[image-save] PIL serialize failed: {ex}')
                return ''
            data = buf.getvalue()
            uri = db.image_target(filename)
            try:
                # atomic_open_write on URI POSTs to the daemon's
                # /v1/projects/<lang>/atomic_commit (since 0.36.0),
                # which serialises against daemon merge-output writes
                # and any concurrent peer atomic_commit — non-torn
                # cross-process semantics, per the contract's
                # "atomic_open_write — when peers need cross-process
                # atomicity" section.
                with MediaHandle(uri, 'image').atomic_open_write() as f:
                    f.write(data)
            except Exception as ex:
                print(f'[image-save] URI write failed: {ex}')
                return ''
            # Prime the local URI-image cache so the next display read
            # doesn't re-fetch through the provider.
            dest = ''
            try:
                cache_dir = self._get_image_cache_dir()
                if cache_dir:
                    sub = os.path.join(cache_dir, '_uri_images')
                    os.makedirs(sub, exist_ok=True)
                    dest = os.path.join(sub, filename)
                    with open(dest, 'wb') as f:
                        f.write(data)
                    db._uri_image_cache[filename] = dest
            except Exception as ex:
                print(f'[image-save] cache prime failed: {ex}')
                dest = ''
        else:
            if not db.images_dir:
                return ''
            try:
                os.makedirs(db.images_dir, exist_ok=True)
                dest = os.path.join(db.images_dir, filename)
                pil_img.save(dest, fmt)
            except Exception as ex:
                print(f'[image-save] filesystem save failed: {ex}')
                return ''
        try:
            db.set_illustration(guid, filename)
        except Exception as ex:
            print(f'[image-save] set_illustration failed: {ex}')
            return ''
        entry['image_path'] = dest
        entry['illustration_href'] = filename
        if self.recorder:
            self.recorder._dirty = True
        print(f'[image-save] wrote {filename} for guid {guid[:8]}')
        return dest

    def _save_remote_image(self):
        """Bake the current entry's displayed image into the project so
        it commits with the surrounding swipe. No-op if no image is
        displayed, if the entry already has its canonical project image
        (idempotency), or if a usable image source can't be obtained.
        Called from nav_next / nav_prev only when the recorder is
        dirty — pure browse never invokes this."""
        if not self.recorder:
            return
        entry = self.recorder.current
        if not entry:
            return
        # Go through the lazy property so an unresolved image_path
        # gets filled in here on first access, not by parse.
        img_path = self.recorder.image_path
        if not img_path:
            return  # no image
        db = self.recorder.db
        filename = db.imagename(entry)
        # Idempotency: if the entry already references the canonical
        # project filename, the image is already in the repo. Skip.
        if entry.get('illustration_href') == filename:
            return
        # On filesystem projects we can also short-circuit on a
        # direct path match.
        if not getattr(db, 'is_uri', False) and db.images_dir:
            dest_fs = os.path.join(db.images_dir, filename)
            if img_path.startswith(db.images_dir) or os.path.exists(dest_fs):
                return
        # Stage 2: img_path is always a local file now (project's own
        # images/, an Android URI-project pull-through, or a CAWL
        # pull-through tmpfile from the daemon). Two paths remain:
        #
        #   - The file is on disk → copy bytes through PIL into the
        #     project's images/ via _save_image_for_entry.
        #   - The file isn't on disk (pull failed, file deleted under
        #     us) → fall back to the displayed texture, which Kivy is
        #     still rendering from earlier.
        import threading
        if os.path.exists(img_path):
            threading.Thread(
                target=self._copy_cached_to_images,
                args=(img_path, filename, entry), daemon=True).start()
            return
        # Texture-fallback for the race where the pull-through file
        # vanished between display and save.
        sm = self.root.ids.sm
        rec_screen = sm.get_screen('recorder')
        img_widget = rec_screen.ids.get('entry_image')
        if img_widget and img_widget.texture:
            tex = img_widget.texture
            pixels = tex.pixels
            w, h = tex.size
            needs_flip = (tex.uvpos[1] == 0)
            threading.Thread(
                target=self._save_texture_to_file,
                args=(pixels, w, h, filename, entry, needs_flip),
                daemon=True).start()

    def _copy_cached_to_images(self, src, filename, entry):
        """Worker: read a cached image from *src* (local file path),
        rescale if needed, route through _save_image_for_entry (which
        handles both URI projects via MediaHandle and desktop direct
        writes). *filename* is informational; the helper recomputes
        it from imagename()."""
        try:
            from PIL import Image as PILImage
            img = PILImage.open(src)
            w, h = img.size
            max_dim = 1284
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
            self._save_image_for_entry(img, entry)
        except Exception as ex:
            print(f'[image-save] cache copy error: {ex}')

    def _save_texture_to_file(self, pixels, w, h, filename, entry,
                              needs_flip=True):
        """Worker: build a PIL image from raw RGBA pixel data and
        route through _save_image_for_entry."""
        try:
            from PIL import Image as PILImage
            img = PILImage.frombytes('RGBA', (w, h), pixels)
            if needs_flip:
                img = img.transpose(PILImage.FLIP_TOP_BOTTOM)
            max_dim = 1284
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
            self._save_image_for_entry(img, entry)
        except Exception as ex:
            print(f'[image-save] texture save error: {ex}')

    def play_audio(self):
        if self.recorder:
            self.recorder.play_audio()

    def redo_recording(self):
        """Clear existing audio and allow re-recording."""
        if self.recorder:
            self.recorder.clear_audio()

    def show_goto_dialog(self):
        """Popup to jump to a specific entry by its list number (the same
        number shown in the progress label, e.g. SILCAWL). Falls back to
        queue position when the current wordlist has no list numbers."""
        if not self.recorder:
            return
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.textinput import TextInput
        from kivy.uix.button import Button

        r = self.recorder
        total = len(r.queue)
        if total == 0:
            return

        # Map list-number → queue index for entries that carry one.
        num_to_idx = {}
        for idx, e in enumerate(r.queue):
            cawl = e.get('cawl', '')
            if not cawl:
                continue
            try:
                num_to_idx.setdefault(int(cawl), idx)
            except ValueError:
                continue

        has_list_numbers = bool(num_to_idx)
        list_label = r.db.list_type or _tr('list')

        if has_list_numbers:
            nums = sorted(num_to_idx)
            lo, hi = nums[0], nums[-1]
            cur_cawl = r.current.get('cawl', '') if r.current else ''
            try:
                initial = str(int(cur_cawl))
            except (TypeError, ValueError):
                initial = str(lo)
            title = _tr('Go to {label} number ({lo}-{hi})').format(
                label=list_label, lo=lo, hi=hi)
            hint = f'{lo}-{hi}'
        else:
            initial = str(r.index + 1)
            title = _tr('Go to entry (1-{total})').format(total=total)
            hint = f'1-{total}'

        from kivy.uix.checkbox import CheckBox
        from kivy.uix.label import Label
        content = BoxLayout(orientation='vertical', spacing=dp(12), padding=dp(12))
        num_input = TextInput(
            text=initial,
            hint_text=hint,
            multiline=False, size_hint_y=None, height=dp(48),
            font_size=sp(18),
            # input_filter rejects non-digit keystrokes at the
            # model layer (covers paste / hardware keyboards).
            input_filter='int',
            # input_type='number' asks Android to bring up the
            # numeric keypad on focus instead of the full
            # alphanumeric soft keyboard. Maps to InputType.
            # TYPE_CLASS_NUMBER. Saves the user a tap on the
            # keyboard's 123 button and reduces fat-finger
            # errors on a button-array task like "go to entry N".
            input_type='number',
        )
        content.add_widget(num_input)

        # Past-work toggle mirrors Settings → show_past_work
        # peer_pref. Lives here so the user can flip it without
        # walking into Settings — it's the only filter they're
        # likely to touch day-to-day.
        show_past_row = BoxLayout(
            orientation='horizontal', size_hint_y=None,
            height=dp(40), spacing=dp(8))
        past_cb = CheckBox(
            size_hint_x=None, width=dp(40),
            active=bool(peer_pref('show_past_work', False)))
        past_label = Label(
            text=_tr('Show past work'), font_size=sp(14),
            font_name=_FONT_NAME, halign='left', valign='middle')
        past_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
        show_past_row.add_widget(past_cb)
        show_past_row.add_widget(past_label)
        content.add_widget(show_past_row)

        # "Go outside filter" — one-shot escape hatch for jumping
        # to an entry beyond the active cawl_filter (e.g. peer in
        # a split team needs to spot-check an entry outside their
        # slot). When checked, OK navigates to the entry whether
        # or not it's in the queue; the next swipe (either
        # direction) returns to the filtered queue, snapped to
        # the closest in-filter entry. Default off — leaving
        # unchecked preserves the existing "closest in current
        # filter" fallback semantics.
        outside_row = BoxLayout(
            orientation='horizontal', size_hint_y=None,
            height=dp(40), spacing=dp(8))
        outside_cb = CheckBox(
            size_hint_x=None, width=dp(40), active=False)
        outside_label = Label(
            text=_tr('Go outside filter'), font_size=sp(14),
            font_name=_FONT_NAME, halign='left', valign='middle')
        outside_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
        outside_row.add_widget(outside_cb)
        outside_row.add_widget(outside_label)
        content.add_widget(outside_row)

        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
        clear_btn = Button(text=_tr('Clear'), font_size=sp(14))
        ok_btn = Button(text=_tr('OK'), font_size=sp(14),
                        background_color=theme.ACCENT)
        btn_row.add_widget(clear_btn)
        btn_row.add_widget(ok_btn)
        content.add_widget(btn_row)

        popup = Popup(
            title=title,
            content=content,
            size_hint=(0.8, None), height=dp(284),
            auto_dismiss=True,
        )

        def _apply_past_toggle(checkbox, value):
            """Flip the show-past-work pref. Since 1.52.x this is
            the only surface for the toggle — the Settings-side
            UnrecordedToggle was pulled in favour of this more
            accessible spot. Invert into only_unrecorded, persist,
            rebuild queue, refresh UI."""
            show_past = bool(value)
            r.only_unrecorded = not show_past
            set_peer_pref('show_past_work', show_past)
            r.rebuild_queue()
            App.get_running_app().refresh_recorder_ui()
        past_cb.bind(active=_apply_past_toggle)

        def _go(*args):
            text = num_input.text.strip()
            if text:
                try:
                    n = int(text)
                except ValueError:
                    popup.dismiss()
                    return
                # Rebuild the cawl→queue-index map against the
                # *current* queue, not the popup-time snapshot —
                # flipping the past-work toggle above rebuilds the
                # queue underneath us, and a stale map would route
                # `n in num_to_idx` to a 'closest' fallback in the
                # old filter.
                live_map = {}
                for idx, e in enumerate(r.queue):
                    cawl = e.get('cawl', '')
                    if not cawl:
                        continue
                    try:
                        live_map.setdefault(int(cawl), idx)
                    except ValueError:
                        continue
                live_total = len(r.queue)
                outside = bool(outside_cb.active)
                # In-filter exact match always wins, regardless of
                # the outside checkbox — the user named a number,
                # if it's in the queue, go to it.
                if n in live_map:
                    r._one_shot_entry = None
                    r.index = live_map[n]
                elif outside:
                    # One-shot outside the filter: look up the
                    # entry in the unfiltered model. r.current
                    # returns the one-shot entry until the next
                    # swipe, which clears it and snaps back to
                    # the closest in-filter entry.
                    target_str = f'{n:04d}'
                    entry = None
                    for e in r.db.entries:
                        if e.get('cawl') == target_str:
                            entry = e
                            break
                    if entry is None:
                        for e in r.db.entries:
                            try:
                                if int(e.get('cawl', '') or 0) == n:
                                    entry = e
                                    break
                            except (TypeError, ValueError):
                                continue
                    if entry is not None:
                        r._one_shot_entry = entry
                    elif live_map:
                        closest = min(live_map,
                                      key=lambda k: abs(k - n))
                        r._one_shot_entry = None
                        r.index = live_map[closest]
                elif live_map:
                    closest = min(live_map,
                                  key=lambda k: abs(k - n))
                    r._one_shot_entry = None
                    r.index = live_map[closest]
                elif live_total:
                    r._one_shot_entry = None
                    n = max(1, min(n, live_total))
                    r.index = n - 1
                r._pending_rerecord = False
                r._notify_ui()
            popup.dismiss()

        def _clear(*args):
            """Jump to the start of the current filtered queue.
            Does NOT clear cawl_filter / gloss_search /
            only_unrecorded — those are project-load-bearing for
            the wordlist-split workflow. Past-work flag is changed
            only via the explicit checkbox above. Clears any
            active one-shot outside-filter entry so the queue
            view is what the user lands on."""
            r._one_shot_entry = None
            if r.queue:
                r.index = 0
                r._pending_rerecord = False
                r._notify_ui()
            popup.dismiss()

        clear_btn.bind(on_release=_clear)
        ok_btn.bind(on_release=_go)
        num_input.bind(on_text_validate=_go)
        popup.open()

    def refresh_recorder_ui(self):
        """Push current recorder state into the RecorderScreen widgets."""
        if not self.recorder:
            return
        sm = self.root.ids.sm
        rec_screen = sm.get_screen('recorder')
        ids = rec_screen.ids
        r = self.recorder

        # Progress
        if 'progress_label' in ids:
            ids.progress_label.text = r.progress_text

        # Sync status
        self._update_sync_status()

        # Image — only update if source changed
        if 'entry_image' in ids:
            img = ids.entry_image
            if r.has_image:
                new_src = r.image_path
                # #2 On lowMemory devices, hand AsyncImage a
                # downsampled side-cache path instead of the full-
                # resolution original. Reduces texture-upload memory
                # and Kivy's decode cost. No-op on devices with
                # headroom — full-res displayed.
                new_src = self._downsample_for_display(new_src)
                if img.source != new_src:
                    img.source = new_src
                    img.opacity = 1
                    # Bind texture size to compute correct height once loaded
                    def _resize_image(img_ref, *args):
                        if img_ref.texture:
                            tw, th = img_ref.texture.size
                            if tw > 0:
                                h = img_ref.width * th / tw
                                img_ref.height = min(h, dp(500))
                        elif img_ref.height == 0:
                            img_ref.height = min(img_ref.width, dp(500))
                    img.bind(texture=lambda *a: _resize_image(img))
                    img.bind(width=lambda *a: _resize_image(img))
                    _resize_image(img)
                elif img.opacity == 0:
                    img.opacity = 1
            else:
                img.source = ''
                img.height = 0
                img.opacity = 0

        # Glosses — one row per language, glosses joined with " / "
        if 'gloss_box' in ids:
            ids.gloss_box.clear_widgets()
            e = r.current
            if e:
                for lang in r.active_gloss_langs:
                    texts = e.get('glosses', {}).get(lang, [])
                    if texts:
                        ids.gloss_box.add_widget(
                            GlossRow(lang=lang, gloss=' / '.join(texts)))

        # Status text + colour
        if 'status_label' in ids:
            ids.status_label.text = r.status_text
            ids.status_label.color = theme.GREEN \
                if r.has_recording else theme.TEXT_DIM

        # Button area: record button OR play+redo pair.
        #
        # CRITICAL: do NOT ``clear_widgets`` on every refresh.
        # Each refresh fires within touch-event windows
        # (refresh runs from _notify_ui, which is scheduled by
        # both record_start and _publish_start_success — both
        # of which run BEFORE the user releases the button).
        # If we destroy the RecordButton mid-press, Kivy's
        # touch.grab_list holds only a *weak* ref to the old
        # widget — Python GC can collect it before touch_up
        # arrives, and the grab dispatch silently skips. The
        # touch_up then routes to the freshly-built button
        # whose grab was never set, returns False (not grabbed
        # by *this* widget), and ``record_stop`` is never
        # called. The recording then runs to the
        # ``_MAX_DURATION_BACKSTOP_S`` ceiling. This was the
        # actual "recorder held itself down for 60 s without
        # me" wedge in the 1.46.43 field logs — the #1 grab
        # fix was correct as far as it went, but defeated by
        # the rebuild underneath it.
        #
        # Fix: only rebuild when the desired widget layout
        # differs from what's currently there. Within a
        # "record button is shown" session, update the
        # button's ``recording`` property in place so the
        # same Kivy widget instance survives every refresh
        # and the grab keeps pointing at a live object.
        if 'btn_area' in ids:
            btn_area = ids.btn_area
            want_play_redo = (r.has_recording and not r._recording
                              and not r._pending_rerecord)
            kids = btn_area.children  # newest-first ordering
            has_play_redo = (
                len(kids) == 2
                and any(isinstance(k, PlayButton) for k in kids)
                and any(isinstance(k, RedoButton) for k in kids))
            has_record_btn = (
                len(kids) == 1 and isinstance(kids[0], RecordButton))
            if want_play_redo:
                if not has_play_redo:
                    btn_area.clear_widgets()
                    play_btn = PlayButton(size_hint_x=2)
                    play_btn.bind(on_touch_up=lambda w, t:
                        self.play_audio() if w.collide_point(*t.pos) else None)
                    redo_btn = RedoButton(size_hint_x=1)
                    redo_btn.bind(on_touch_up=lambda w, t:
                        self.redo_recording() if w.collide_point(*t.pos) else None)
                    btn_area.add_widget(play_btn)
                    btn_area.add_widget(redo_btn)
            elif has_record_btn:
                # Same widget instance survives the refresh —
                # just flip the visual state via its
                # BooleanProperty. The grab from touch_down
                # stays valid; touch_up will find this widget
                # alive in the grab dispatch.
                kids[0].recording = r._recording
            else:
                btn_area.clear_widgets()
                # Record button (push-to-talk).
                #
                # Touch handling uses the Kivy ``grab``
                # pattern: on touch_down, if the press lands
                # inside the button we ``grab`` the touch onto
                # the widget and call record_start. On
                # touch_up, we act when ``touch.grab_current``
                # is this widget regardless of the current
                # touch position, so a finger sliding off the
                # button before release still fires
                # record_stop. (Pre-grab binding silently
                # skipped record_stop when the finger had
                # moved off.)
                rec_btn = RecordButton()
                rec_btn.recording = r._recording
                def _rec_down(w, t, _self=self):
                    if w.collide_point(*t.pos):
                        t.grab(w)
                        _self.record_start()
                        return True
                    return False
                def _rec_up(w, t, _self=self):
                    if t.grab_current is w:
                        t.ungrab(w)
                        _self.record_stop()
                        return True
                    return False
                rec_btn.bind(on_touch_down=_rec_down)
                rec_btn.bind(on_touch_up=_rec_up)
                btn_area.add_widget(rec_btn)


if __name__ == '__main__':
    LIFTRecorderApp().run()
