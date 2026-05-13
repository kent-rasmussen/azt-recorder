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

# ── Crash logging — runs before any Kivy import ────────────────────────────────
# On Android: p4a sets $ANDROID_PRIVATE to the app's private files dir (always writable).
#             Also tries /sdcard/ (may need MANAGE_EXTERNAL_STORAGE on API 30+).
# On desktop: ~/azt_recorder.log
def _setup_logging():
    _on_android = os.path.exists('/system/build.prop')
    candidates = []
    if _on_android:
        # ANDROID_PRIVATE is set by p4a bootstrap, e.g. /data/user/0/org.x.y/files
        android_private = os.environ.get('ANDROID_PRIVATE', '')
        if android_private:
            candidates.append(os.path.join(android_private, 'azt_recorder.log'))
        # Fallback using known package name pattern
        candidates += [
            '/data/user/0/org.atoznback.azt_recorder/files/azt_recorder.log',
            '/sdcard/azt_recorder.log',
        ]
    else:
        candidates += [os.path.join(os.path.expanduser('~'), 'azt_recorder.log')]
    fh = None
    for path in candidates:
        try:
            fh = open(path, 'w', buffering=1, encoding='utf-8')
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
            Button:
                size_hint_x: 1
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
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
            Button:
                size_hint_x: None
                width: dp(44)
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
            Label:
                text: app.version_string
                font_size: sp(11)
                font_name: FONT
                color: T.TEXT_DIM
                halign: 'left'
                valign: 'middle'
                text_size: self.size
                padding_x: dp(8)
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
                # ── Share this app ─────────────────────────────────────
                RecBtn:
                    text: _('Share this app')
                    halign: 'left'
                    padding: [dp(52), 0]
                    text_size: self.size
                    valign: 'middle'
                    normal_color: T.SURFACE
                    on_release: app.share_apk()
                    Image:
                        source: 'icons/share_dark.png'
                        size_hint: None, None
                        size: dp(24), dp(24)
                        x: self.parent.x + dp(16)
                        center_y: self.parent.center_y
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
                            text: _('Change gloss languages')
                            normal_color: T.ACCENT
                            font_size: sp(14)
                            on_release: root._show_gloss_overlay()
                        Label:
                            id: gloss_summary_label
                            text: ''
                            font_size: sp(12)
                            font_name: FONT
                            color: T.TEXT_DIM
                            halign: 'left'
                            valign: 'middle'
                            text_size: self.size
                            markup: True
                    BoxLayout:
                        size_hint_y: None
                        height: dp(44)
                        spacing: dp(8)
                        RecBtn:
                            id: filter_toggle_btn
                            text: _('Filter words')
                            normal_color: T.ACCENT
                            font_size: sp(14)
                            on_release: root.toggle_filter_panel()
                        Label:
                            id: filter_summary_label
                            text: ''
                            font_size: sp(12)
                            font_name: FONT
                            color: T.TEXT_DIM
                            halign: 'left'
                            valign: 'middle'
                            text_size: self.size
                            markup: True
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
                            UnrecordedToggle:
                                id: unrecorded_toggle
                                active: False
                                size_hint_x: 3
                                on_active: root.toggle_show_past(self.active)
                            RecBtn:
                                id: filter_ok_btn
                                text: _('OK')
                                size_hint_x: 1
                                height: dp(56)
                                normal_color: T.GREEN
                                font_size: sp(15)
                                on_release: root.toggle_filter_panel()
                # ── UI Language ─────────────────────────────────────────
                SectionLabel:
                    text: _('UI Language')
                BoxLayout:
                    id: lang_selector_row
                    size_hint_y: None
                    height: dp(44)
                    spacing: dp(8)
                # ── Setup Collaboration (always visible) ──────────────
                RecBtn:
                    text: _('Setup Collaboration')
                    normal_color: T.SURFACE
                    on_release: app.go_collab()
                # ── Select Project (rare, kept at bottom) ─────────────
                RecBtn:
                    text: _('Select Project')
                    normal_color: T.BTN_INACTIVE
                    on_release: app.start_over()
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
        Label:
            text: root.gloss
            font_size: sp(30)
            font_name: FONT
            bold: True
            color: T.TEXT
            halign: 'left'
            valign: 'middle'
            text_size: self.size

<UnrecordedToggle>:
    size_hint_y: None
    height: dp(56)
    canvas.before:
        Color:
            rgba: T.GREEN_DARK if self.active else T.SURFACE
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    BoxLayout:
        spacing: dp(12)
        padding: dp(12), dp(8)
        CheckBox:
            id: chk
            size_hint_x: None
            width: dp(48)
            active: root.active
            color: T.ACCENT
            on_active: root.active = self.active
        Label:
            text: _('Show past work')
            font_size: sp(16)
            font_name: FONT
            bold: True
            color: T.GREEN_BRIGHT if root.active else T.TEXT
            halign: 'left'
            valign: 'middle'
            text_size: self.size

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


class UnrecordedToggle(BoxLayout):
    active = BooleanProperty(False)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self.active = not self.active
            return True
        return super().on_touch_down(touch)




class GlossRow(BoxLayout):
    lang = StringProperty('')
    gloss = StringProperty('')


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


class ConfigScreen(Screen):
    only_unrecorded = BooleanProperty(False)

    def on_enter(self):
        app = App.get_running_app()
        self._build_lang_selector()
        has_db = app.recorder is not None
        # Show/hide database-dependent sections.
        # When hiding: zero height on every descendant so nothing has a
        # hit area (disabled=True alone doesn't block touches in Kivy).
        box = self.ids.get('db_settings_box')
        if box:
            if has_db:
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
        # Restore show-past-work toggle (default False)
        show_past = bool(peer_pref('show_past_work', False))
        toggle = self.ids.get('unrecorded_toggle')
        if toggle:
            toggle.active = show_past
        self.only_unrecorded = not show_past
        # All panels start collapsed
        self._update_filter_summary()
        self._update_gloss_summary()
        self._collapse_filter_panel()
        # Build recording options
        self._build_rec_options(app)

    @staticmethod
    def _hide_box_tree(widget):
        """Zero the height/opacity of a widget and all descendants so
        nothing has a touch hit-area when the section is hidden."""
        widget.height = 0
        widget.opacity = 0
        for child in widget.children:
            if hasattr(child, 'height'):
                child.height = 0
                child.opacity = 0
            # Recurse into nested containers
            if hasattr(child, 'children') and child.children:
                ConfigScreen._hide_box_tree(child)

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
        lbl.text = ', '.join(langs) if langs else _tr('(none selected)')

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
        # Restore child heights before expanding
        for cid in ('cawl_label', 'gloss_search_label'):
            w = self.ids.get(cid)
            if w:
                w.height = dp(36)
        for cid in ('cawl_input', 'gloss_search_input'):
            w = self.ids.get(cid)
            if w:
                w.height = dp(48)
                w.disabled = False
        bottom_row = self.ids.get('filter_bottom_row')
        if bottom_row:
            bottom_row.height = dp(56)
            bottom_row.opacity = 1
        toggle = self.ids.get('unrecorded_toggle')
        if toggle:
            toggle.height = dp(56)
        ok_btn = self.ids.get('filter_ok_btn')
        if ok_btn:
            ok_btn.height = dp(56)
            ok_btn.disabled = False
        # dp(36)*2 labels + dp(48)*2 inputs + dp(56) bottom row + dp(8)*4 spacing
        panel.height = dp(256)
        panel.opacity = 1
        self._filter_open = True
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
        # including the Labels — and the dp(56)-tall UnrecordedToggle
        # / OK button inside filter_bottom_row, whose
        # `<UnrecordedToggle>:` / `<RecBtn@Button>:` root rules pin
        # `size_hint_y: None` + an explicit height so they don't
        # shrink with their (zeroed) parent row.
        for cid in ('cawl_label', 'gloss_search_label'):
            w = self.ids.get(cid)
            if w:
                w.height = 0
        for cid in ('cawl_input', 'gloss_search_input'):
            w = self.ids.get(cid)
            if w:
                w.height = 0
                w.disabled = True
                w.focus = False
        bottom_row = self.ids.get('filter_bottom_row')
        if bottom_row:
            bottom_row.height = 0
            bottom_row.opacity = 0
        toggle = self.ids.get('unrecorded_toggle')
        if toggle:
            toggle.height = 0
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
        """Expand or collapse the word filter panel."""
        print(f'[filter] toggle_filter_panel: _filter_open={self._filter_open}')
        if self._filter_open:
            self._collapse_filter_panel()
            self._update_filter_summary()
            # Apply filters immediately
            app = App.get_running_app()
            if app.recorder:
                cawl_in = self.ids.get('cawl_input')
                if cawl_in:
                    text = cawl_in.text.strip()
                    app.recorder.cawl_filter = text
                    set_peer_pref('cawl_filter', text or None)
                gs = self.ids.get('gloss_search_input')
                if gs:
                    app.recorder.gloss_search = gs.text.strip()
        else:
            self._expand_filter_panel()
        # Belt-and-suspenders: force db_settings_box to recompute
        # height after the panel size change. The KV binding
        # `height: self.minimum_height` should handle this on its
        # own, but Kivy's BoxLayout-inside-ScrollView relayout
        # ordering is touchy, so we explicitly Clock.schedule_once
        # the readback.
        box = self.ids.get('db_settings_box')
        if box and box.opacity > 0:
            Clock.schedule_once(
                lambda dt: setattr(box, 'height', box.minimum_height), 0)
        panel = self.ids.get('filter_panel')
        print(f'[filter] post-toggle: panel.height={panel.height if panel else None} '
              f'panel.opacity={panel.opacity if panel else None} '
              f'box.height={box.height if box else None} '
              f'box.minimum_height={box.minimum_height if box else None}')

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

    def toggle_show_past(self, show_past):
        self.only_unrecorded = not show_past
        app = App.get_running_app()
        if app.recorder:
            app.recorder.only_unrecorded = not show_past
        set_peer_pref('show_past_work', bool(show_past))

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
                app.recorder.cawl_filter = text
                set_peer_pref('cawl_filter', text or None)
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

    def __init__(self, db: 'LIFTDatabase'):
        self.db = db
        self.queue = []          # list of entry dicts
        self.index = 0
        # cawl_filter persists across boots/installs: some workflows
        # pin a CAWL range and rely on it sticking. Stored suite-wide
        # so the same scope follows the user across peers.
        self.cawl_filter = peer_pref('cawl_filter', '') or ''
        self.gloss_search = ''
        self.only_unrecorded = False
        self._recording = False
        self._playing = False
        self._pending_rerecord = False
        self._audio_path = None
        self._recorder = None
        self._record_pfd = None
        # Cleared on every start; flipped to True only when the
        # platform start path returns clean. stop_recording gates the
        # LIFT basename write on this so a failed start cannot leave
        # a bogus filename in <citation><form>.
        self._record_ok = False

        # Has the user changed anything on the current entry (audio
        # recorded, image picked, etc.) since the last sync? Set by
        # set_audio's caller and the image-pick / image-bake paths;
        # cleared by nav_prev / nav_next after firing _auto_commit_sync.
        # Pure browse swipes leave it False and skip the commit RPC.
        self._dirty = False

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

    # ── Navigation ─────────────────────────────────────────────────────────────

    def go_next(self):
        if self.index < len(self.queue) - 1:
            self._pending_rerecord = False
            self._playing = False
            self.index += 1
            self._notify_ui()

    def go_prev(self):
        if self.index > 0:
            self._pending_rerecord = False
            self._playing = False
            self.index -= 1
            self._notify_ui()

    @property
    def current(self):
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
        parts = [lang]
        if cawl_num:
            parts.append(cawl_num)
        # Right-hand side: when a CAWL filter is active, show the filter
        # expression itself (e.g. "501-1000") so the displayed number is
        # interpretable. Otherwise fall back to the queue length.
        cf = self.cawl_filter.strip()
        if cf:
            parts.append(f'/ {cf}')
        else:
            parts.append(f'/ {len(self.queue)}')
        return ' '.join(parts)

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

    def start_recording(self):
        if self._recording or not self.current:
            return
        path = self._make_audio_path()
        # Clear before the attempt so a previous successful flag
        # cannot survive into a now-failing start.
        self._record_ok = False
        try:
            actual_path = self._start_native_recording(path)
        except Exception as ex:
            print(f'Recording start failed: {ex}')
            self._notify_ui()
            return
        self._audio_path = actual_path or path
        self._recording = True
        self._record_ok = True
        self._notify_ui()

    def stop_recording(self):
        if not self._recording:
            return
        self._recording = False
        self._pending_rerecord = False
        self._stop_native_recording()
        # Write filename into LIFT XML only if both start and stop
        # ran clean. _record_ok flips False on a stop exception so a
        # half-finalised M4A (no moov atom, etc.) does not get
        # advertised as the entry's canonical recording.
        if self._audio_path and self._record_ok:
            filename = os.path.basename(self._audio_path)
            self.db.set_audio(self.current['guid'], filename)
            self.current['audio_filename'] = filename
            self._dirty = True
        self._notify_ui()
        if self._record_ok:
            Clock.schedule_once(lambda dt: self.play_audio(), 0.5)

    def play_audio(self):
        if self._playing:
            return
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
                mp.start()
                self._player = mp  # keep reference alive
                # Clear _playing after duration elapses
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
                print(f'Android play error: {ex}')
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

    def clear_audio(self):
        """Mark entry for re-recording without deleting the existing file."""
        if not self.current:
            return
        self._pending_rerecord = True
        self._notify_ui()

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

        # Replace .wav with .m4a in either a path or a content:// URI —
        # basename sits at the end of both, so str.replace works.
        aac_path = path.replace('.wav', '.m4a')
        mr = MediaRecorder()
        pfd = None
        try:
            mr.setAudioSource(AudioSource.MIC)
            # MPEG_4/AAC gives broadest compatibility and highest quality on Android
            mr.setOutputFormat(OutputFormat.MPEG_4)
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
            mr.setAudioEncodingBitRate(256000)   # 256 kbps
            mr.setAudioSamplingRate(48000)        # 48 kHz
            mr.setAudioChannels(1)                # mono for voice
            mr.prepare()
            mr.start()
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
                self._recorder.release()
            except Exception as ex:
                # IllegalStateException here typically means the M4A
                # has no moov atom — file is unplayable. Don't let
                # stop_recording advertise the basename.
                print(f'Android stop error: {ex}')
                self._record_ok = False
            finally:
                self._recorder = None
        pfd = self._record_pfd
        if pfd is not None:
            try:
                pfd.close()
            except Exception:
                pass
            self._record_pfd = None

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


# ── Main App ───────────────────────────────────────────────────────────────────

__version__ = '1.41.13'


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
    recorder: RecorderController = None
    config_screen: ConfigScreen = None

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
        """Drain the legacy peer-private prefs.json into $AZT_HOME/config.json
        via azt_collab_client. Idempotent: keys already present in the
        suite store are left alone (a sister app that ran first wins).
        After drain, the keys are popped from prefs.json so subsequent
        launches do nothing — no daemon contact, no repeated work."""
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
        # caches of daemon-owned data; 1.41.3 dropped all three. The
        # daemon's projects.json is now the only source of truth for
        # the project langcode, and Project.cawl_image_repo is the
        # only source for the CAWL image repo — see the no-daemon-
        # owned-caches rule.
        if legacy.pop('vernlang', None) is not None:
            moved.append('vernlang (dropped)')
        if legacy.pop('collab_langcode', None) is not None:
            moved.append('collab_langcode (dropped)')
        if legacy.pop('image_repo', None) is not None:
            moved.append('image_repo (dropped)')
        # Committer name has a dedicated suite endpoint.
        if 'collab_name' in legacy:
            try:
                from azt_collab_client import get_contributor, set_contributor
                value = legacy.pop('collab_name')
                if value and not get_contributor():
                    set_contributor(value)
                moved.append('collab_name -> contributor')
            except Exception as ex:
                print(f'[migrate] collab_name failed: {ex}')
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

    # ── App lifecycle ─────────────────────────────────────────────────────────

    def build(self):
        try:
            # Pre-warm the daemon so its lazy-spawn overlaps with the
            # rest of our Kivy boot. Single check_server_compat probe
            # on a background thread; idempotent; no-op on non-Android.
            # Recorder hits Android Java surfaces first in on_start
            # (_warm_jnius_classes, activity.bind), so build()-time
            # prewarm is a free overlap window. Per
            # CLIENT_INTEGRATION.md § 3 / § 13. Toggleable for
            # measurement runs via $AZT_HOME/_no_prewarm sentinel or
            # AZT_BOOT_PREWARM=0.
            try:
                from azt_collab_client.ui.bootstrap import prewarm
                prewarm()
            except Exception as ex:
                print(f'[prewarm] {ex}', file=sys.stderr)
            # Drain the legacy peer-private prefs.json into the
            # suite-wide $AZT_HOME/config.json. Idempotent — keys
            # already in the suite store are left alone, so a sister
            # app that ran first wins. Apply *before* reading the theme
            # below so a fresh upgrade still finds the user's choice.
            self._migrate_prefs_to_suite_store()
            theme.set_theme(peer_pref('theme', 'Ocean') or 'Ocean')
            self.subtitle = _tr(APP_TAGLINE)
            Builder.load_string(KV)
            self.root = RootScreen()
            # Move any legacy credential keys out of prefs.json into
            # $AZT_HOME/credentials.json. Idempotent.
            try:
                from azt_collab_client import migrate_from_prefs
                summary = migrate_from_prefs(self._prefs_path)
                if summary.get('migrated'):
                    print(f'[migrate] credentials: {summary}')
            except Exception as ex:
                print(f'[migrate] error: {ex}')
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
            # The AZTCollabProvider lives in the standalone server APK
            # (org.atoznback.aztcollab). Peers do NOT install provider
            # callbacks here — they reach the provider through the
            # azt_collab_client transport instead.
            # Handle Android back button / ESC key
            Window.bind(on_keyboard=self._on_back_button)
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
            # Periodically refresh the last-sync indicator so background
            # debounced syncs (request_sync from swipes) become visible
            # without waiting for the next manual sync. project_status
            # is a single GET against the daemon — cheap.
            Clock.schedule_interval(
                lambda dt: self._update_sync_status(), 30)
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
        return True

    def on_resume(self):
        return True

    def on_stop(self):
        self._finalise_active_recording('on_stop')
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
                'android.media.MediaRecorder',
                'android.media.MediaRecorder$AudioSource',
                'android.media.MediaRecorder$OutputFormat',
                'android.media.MediaRecorder$AudioEncoder',
                'android.media.MediaPlayer',
                'android.net.Uri',
                'android.content.Intent',
                'android.os.ParcelFileDescriptor',
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
        self.load_lift(project.lift_path)

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

    def _show_toast(self, msg, duration=1.5):
        """Show a brief overlay message that auto-dismisses."""
        from kivy.uix.label import Label
        from kivy.uix.modalview import ModalView
        view = ModalView(
            size_hint=(None, None), size=(dp(250), dp(50)),
            background_color=theme.OVERLAY,
            auto_dismiss=True,
        )
        view.add_widget(Label(
            text=msg, font_size=sp(14), font_name=_FONT_NAME,
            color=theme.TEXT,
        ))
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
        """Warm the daemon's CAWL cache for entries in this project.

        Triggers CAWLHandle.open_read() for each basename on a worker
        thread; the daemon coalesces concurrent calls and lazily
        fetches missing binaries from raw.githubusercontent.com on its
        own. Once warmed, in-session display reads hit the daemon's
        cache directly (zero-copy FD on Android, loopback HTTP on
        desktop) — no per-peer disk cache involved.

        Per CLIENT_INTEGRATION.md § 10.5 (Cache-progress indicator),
        also starts a 5-second poll of ``cawl_cache_status`` that
        drives the ``cache_status_label`` so the user sees
        "Caching images: M / N (network in use)" while the daemon
        is downloading from upstream. Auto-hides when caching
        catches up."""
        if not self.recorder:
            print('[image-prefetch] no recorder; skipping')
            return
        import threading
        db = self.recorder.db
        threading.Thread(
            target=self._prefetch_images_worker,
            args=(db,), daemon=True).start()
        langcode = getattr(self, '_current_langcode', '') or ''
        print(f'[image-prefetch] started; langcode={langcode!r} for '
              f'cache-progress poll')
        if langcode:
            self._start_cache_status_poll(langcode)
        else:
            print('[image-prefetch] no langcode; cache-progress '
                  'indicator will not poll')

    # ── CAWL cache-progress indicator ────────────────────────────────────
    # Per CLIENT_INTEGRATION.md § 10.5: while a CAWL prefetch is in
    # flight (the daemon is fetching ~1700 image binaries from
    # upstream GitHub, which can take many minutes on a slow link),
    # surface "Caching images: M / N (network in use)" so the user
    # doesn't disconnect Wi-Fi mid-warm and end up with a half-cache.

    def _start_cache_status_poll(self, langcode):
        """Begin a 5-second poll of the daemon's CAWL cache status.
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
        print(f'[cache-status] poll starting for langcode={langcode!r}')
        self._cache_status_event = Clock.schedule_interval(
            lambda _dt: self._tick_cache_status(), 1.0)
        self._tick_cache_status()

    def _tick_cache_status(self):
        """One poll iteration. Runs the daemon RPC on a worker so the
        UI thread isn't pinned waiting for the cache_status response;
        marshals the label update back to the main thread."""
        langcode = getattr(self, '_cache_status_langcode', '') or ''
        if not langcode:
            self._hide_cache_indicator()
            return
        import threading

        def _worker():
            try:
                from azt_collab_client import cawl_cache_status
                status = cawl_cache_status(langcode)
            except Exception as ex:
                print(f'[cache-status] poll failed: {ex}')
                return
            cached = int(status.get('cached') or 0)
            total = int(status.get('total') or 0)
            print(f'[cache-status] daemon reports cached={cached} '
                  f'total={total} image_repo={status.get("image_repo")!r}')
            Clock.schedule_once(
                lambda dt, c=cached, t=total: self._apply_cache_status(
                    c, t), 0)
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_cache_status(self, cached, total):
        """Render the indicator (or hide it) based on the latest
        cached / total counts. Cancels the polling Clock event once
        the cache catches up."""
        if total == 0:
            print('[cache-status] total=0 → hiding indicator '
                  '(daemon has no index entries: endpoint missing on '
                  'this server APK, no image_repo configured, or '
                  'index transport still broken)')
            self._hide_cache_indicator()
            return
        if cached >= total:
            print(f'[cache-status] cached={cached} >= total={total} '
                  '→ cache warm; hiding + stopping poll')
            self._hide_cache_indicator()
            event = getattr(self, '_cache_status_event', None)
            if event:
                try:
                    event.cancel()
                except Exception:
                    pass
                self._cache_status_event = None
            return
        # Shared msgid with azt_collab_client/locales (the daemon
        # settings UI uses the same indicator) so the recorder
        # inherits the French translation via the gettext fallback
        # chain — no peer-side duplicate.
        self._show_cache_indicator(_tr(
            'Caching images: {cached} / {total} '
            '(network in use — please stay online)'
        ).format(cached=cached, total=total))

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

    def _prefetch_images_worker(self, db):
        """Pull each CAWL basename via the resolver so the daemon's
        cache fills (and the local tmp pull-through path is ready).
        Skips on any error — spotty network surfaces as missing
        images on demand, not as a prefetch crash."""
        try:
            basenames = db.all_cawl_basenames()
        except Exception as ex:
            print(f'[image-prefetch] all_cawl_basenames failed: {ex}')
            return
        if not basenames:
            print('[image-prefetch] no basenames to warm (resolver '
                  'has empty CAWL → basename map; see [cawl] _load '
                  'output for why)')
            return
        count = 0
        for cawl in basenames:
            try:
                path = db._image_resolver.get_path(cawl)
                if path:
                    count += 1
            except Exception:
                pass
        print(f'[image-prefetch] warmed {count}/{len(basenames)} '
              f'CAWL images via daemon')

    def load_lift(self, path):
        self._dismiss_loading_overlay()
        # The picker can return either a filesystem path (desktop / open
        # file) or a content:// URI (Android server-APK model). Only
        # apply abspath when we genuinely have a filesystem path.
        from azt_collab_client import is_content_uri
        if not is_content_uri(path):
            path = os.path.abspath(path)
        try:
            db = LIFTDatabase(path,
                              image_cache_dir=self._get_image_cache_dir())
        except Exception as ex:
            self._show_error(_tr('Could not open file:\n{error}').format(error=ex))
            return
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
        if authoritative:
            db.set_vernlang(authoritative)
        elif pending:
            db.set_vernlang(pending)
        if pending:
            try:
                db.clean_template()
            except Exception as ex:
                # _save() inside clean_template writes the LIFT file
                # — on Android URI projects that goes through the
                # daemon's ContentProvider and can fail with a stale
                # URI grant or transient daemon state. clean_template
                # is best-effort cleanup of template-stub forms; a
                # failure here must not abort the load (which would
                # crash the app on the main thread, since Select
                # Project's _handle_pick → load_lift runs there).
                print(f'[load_lift] clean_template failed: {ex}')
        self._pending_vernlang = ''
        # Drop any last_commit_seen baseline carried over from a
        # previous project — otherwise the first _update_sync_status
        # tick after this load would compare the new project's
        # last_commit against the old project's value and fire a
        # spurious _reload_and_restore on top of the load we just did.
        self._last_commit_seen = None
        self.recorder = RecorderController(db)
        # Apply persisted show-past-work preference (default: hide past work)
        show_past = bool(peer_pref('show_past_work', False))
        self.recorder.only_unrecorded = not show_past
        self.recorder.rebuild_queue()
        # Register this project with the sync backend so future ops can
        # be addressed by langcode.
        self._register_current_project()
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'recorder'
        Clock.schedule_once(lambda dt: self.refresh_recorder_ui(), 0.1)
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

    def _reload_and_restore(self, guid):
        """Reload the LIFT file and restore position to the entry with
        *guid*, refreshing the recorder UI in place per
        CLIENT_INTEGRATION.md § 11. Same anchor (entry guid), fresh
        content (re-parsed from disk).

        If the saved guid would be hidden by a client-side filter
        after the refresh (e.g. only_unrecorded is on and another
        contributor just recorded the entry), suspend the filters
        for this view so the user's anchor stays visible per § 11's
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
        old_settings = (
            self.recorder.cawl_filter,
            self.recorder.gloss_search,
            self.recorder.only_unrecorded,
            self.recorder.active_gloss_langs[:],
        )
        self.recorder = RecorderController(db)
        self.recorder.cawl_filter, self.recorder.gloss_search, \
            self.recorder.only_unrecorded, self.recorder.active_gloss_langs = old_settings
        self.recorder.rebuild_queue()
        if guid:
            for i, e in enumerate(self.recorder.queue):
                if e.get('guid') == guid:
                    self.recorder.index = i
                    self.refresh_recorder_ui()
                    return
            # Anchor not in current queue. If the new model still
            # has the entry, a client-side filter is hiding it; per
            # § 11 drop the filters for this view so the user's
            # anchor stays present even though the data clock moved.
            # ConfigScreen.on_enter re-reads the recorder's filter
            # state into the input fields, so no extra UI sync.
            in_model = any(
                e.get('guid') == guid for e in self.recorder.db.entries)
            if in_model:
                self.recorder.cawl_filter = ''
                self.recorder.gloss_search = ''
                self.recorder.only_unrecorded = False
                self.recorder.rebuild_queue()
                for i, e in enumerate(self.recorder.queue):
                    if e.get('guid') == guid:
                        self.recorder.index = i
                        break
            # else: real upstream deletion — index is clamped to
            # [0, len(queue)-1] by rebuild_queue, so the user lands
            # on whatever's at the same slot. Per § 11 let the
            # natural propagation render; no toast.
        self.refresh_recorder_ui()

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
        """Return (text, last_sync, last_commit) for the current project.

        ``text`` is what goes in the sync indicator:
          - ``''`` if no langcode or the server is unreachable
          - ``'not backed up'`` when last_sync is 0 (no successful push
            yet — see ProjectStatus semantics in
            azt_collab_client/projects.py)
          - ``'HH:MM (+n)'`` / ``'HH:MM (OK)'`` otherwise, where *n*
            is the number of commits ahead of the remote that haven't
            been pushed yet (ProjectStatus.commits_ahead). ``(OK)``
            when commits_ahead is 0.

        Date prefixes (``'yesterday HH:MM'`` / ``'N days ago HH:MM'``)
        prepend the time when last_sync is older than today.

        ``last_sync`` (push timestamp) is returned raw so callers
        like do_sync can branch behaviour without re-querying.
        ``last_commit`` (most recent local commit timestamp) lets
        _update_sync_status detect external mutations between polls
        per CLIENT_INTEGRATION.md § 11.
        """
        import datetime
        langcode = getattr(self, '_current_langcode', '')
        if not langcode:
            return ('', 0.0, 0.0)
        from azt_collab_client import project_status
        status = project_status(langcode)
        if status is None:
            return ('', 0.0, 0.0)
        last_sync = float(getattr(status, 'last_sync', 0.0) or 0.0)
        last_commit = float(getattr(status, 'last_commit', 0.0) or 0.0)
        commits_ahead = int(getattr(status, 'commits_ahead', 0) or 0)
        if not last_sync:
            return (_tr('not backed up'), 0.0, last_commit)
        dt_sync = datetime.datetime.fromtimestamp(last_sync)
        now = datetime.datetime.now()
        sync_date = dt_sync.date()
        time_str = dt_sync.strftime('%H:%M')
        days = (now.date() - sync_date).days
        if days == 0:
            base = time_str
        elif days == 1:
            base = f'yesterday {time_str}'
        else:
            base = f'{days} days ago {time_str}'
        if commits_ahead > 0:
            base = f'{base} (+{commits_ahead})'
        else:
            base = f'{base} (OK)'
        return (base, last_sync, last_commit)

    def _update_sync_status(self):
        """Push sync status text into the recorder top bar, and
        detect external mutations (another peer's push, a daemon-
        driven debounced sync) by watching project_status.last_commit
        across polls. On a change, refresh the recorder UI in place
        per CLIENT_INTEGRATION.md § 11 — same anchor entry, fresh
        content."""
        text, _last_sync, last_commit = self._sync_status_info()
        sm = self.root.ids.sm
        rec_screen = sm.get_screen('recorder')
        lbl = rec_screen.ids.get('sync_status_label')
        if lbl:
            lbl.text = text
        seen = getattr(self, '_last_commit_seen', None)
        # Pin the new seen-value BEFORE calling _reload_and_restore.
        # The reload path eventually schedules refresh_recorder_ui,
        # which re-enters this method; if we updated _last_commit_seen
        # only after the reload returned, the nested call would still
        # see the old value, detect last_commit > seen again, and
        # fire another reload — an infinite chain that pegged the UI
        # thread with back-to-back LIFTDatabase reconstructions.
        self._last_commit_seen = last_commit
        if (self.recorder and seen is not None
                and last_commit > seen and last_commit > 0):
            guid = (self.recorder.current.get('guid', '')
                    if self.recorder.queue else '')
            self._reload_and_restore(guid)

    def _mark_gh_app_installed(self):
        """Record that the GitHub App has been installed after a successful push."""
        from azt_collab_client import mark_github_app_installed
        mark_github_app_installed(True)

    def _auto_commit_sync(self):
        """Fire-and-forget: ask the server to schedule a debounced sync.
        Bursts of edits within sync.debounce_ms collapse into one
        commit/push. The server stamps last_sync on success; the UI
        refreshes its sync indicator after a short delay."""
        if not self.recorder:
            return
        langcode = self._current_langcode_or_register()
        if not langcode:
            return
        try:
            from azt_collab_client import request_sync, ServerUnavailable
            # § 12: contributor is daemon-owned, no longer on the wire.
            request_sync(langcode)
        except ServerUnavailable as ex:
            # Per azt_collab_client/CLAUDE.md "Peer contract: routing
            # on sync results", auto-sync on SERVER_UNAVAILABLE /
            # SERVER_ERROR is **silent** — log only, no UI surface.
            # Daemon will be reachable again next time; user is
            # mid-something-else and shouldn't be derailed.
            print(f'[auto-sync] sync service unavailable: {ex}',
                  file=sys.stderr, flush=True)
            return
        except Exception as ex:
            print(f'[auto-sync] error: {ex}', file=sys.stderr, flush=True)
            return
        # Refresh the recorder's sync status indicator slightly after
        # the debounce window so a successful job's last_sync is in
        # place.
        Clock.schedule_once(lambda dt: self._update_sync_status(), 1.5)

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
            self.load_lift(result['path'])
            return
        err = result.get('error', 'unknown')
        if err == 'cancelled':
            # Per CLIENT_INTEGRATION.md § 5: cancel during Start over
            # (a project was already loaded) leaves the previous
            # project up — silent. First-setup cancel (no
            # last_project, brand-new install) closes the app rather
            # than parking the user on an empty window. The picker
            # subprocess only emits 'cancelled' when it has no
            # last_project to auto-resume to, so an empty
            # last_project is the first-setup signal.
            try:
                from azt_collab_client import last_project
                resume = last_project()
            except Exception:
                resume = ''
            if not resume:
                self.stop()
                return
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
        in a worker so a slow request_sync RPC can't freeze the tap
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
            Clock.schedule_once(lambda dt: self._show_toast(msg), 0)
        threading.Thread(target=_worker, daemon=True).start()

    def share_apk(self):
        from azt_collab_client.ui import share_running_apk
        share_running_apk(on_error=self._show_error)

    def do_sync(self):
        if not self.recorder:
            return
        from azt_collab_client import (
            sync_project, translate_result, translate_status, S)
        langcode = self._current_langcode_or_register()
        if not langcode:
            return
        # If nothing has ever been pushed (last_sync == 0), the
        # indicator reads "not backed up" and the user's tap is really
        # asking to set up backup, not to sync. Route to the server's
        # collab UI directly so they land where Publish lives.
        _, last_sync, _last_commit = self._sync_status_info()
        if not last_sync:
            self.go_collab()
            return
        import threading

        saved_guid = self.recorder.current.get('guid', '') if self.recorder.queue else ''

        def _on_sync_done(result, retried=False):
            print(f'[do_sync] {translate_result(result)}')
            # Structure per azt_collab_client/CLAUDE.md "Peer contract:
            # routing on sync results" (do_sync example, lines 389-430):
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
            #    follow-ups (PULLED reload per CLIENT_INTEGRATION § 11,
            #    PUSHED app-installed mark).
            #
            # In this peer the server's one-size-fits-all settings UI
            # (go_collab) hosts both Publish and GitHub Connect — so
            # NOT_A_REPO/NO_REMOTE/AUTH_REQUIRED all route there.
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
                # contributor name is set. Route to the daemon settings
                # UI where set_contributor lives.
                _msg = translate_result(result)
                Clock.schedule_once(
                    lambda dt, m=_msg: self._show_toast(m), 0)
                Clock.schedule_once(
                    lambda dt: self.open_server_ui(), 0)
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
                return
            if result.has(S.JOB_INTERRUPTED):
                if retried:
                    Clock.schedule_once(lambda dt: self._show_toast(
                        _tr('Sync interrupted, please try again.')), 0)
                    return
                # Silent one-shot retry on a fresh worker thread.
                def _retry_worker():
                    # § 12: contributor is daemon-owned.
                    r = sync_project(langcode)
                    Clock.schedule_once(
                        lambda dt, rr=r: _on_sync_done(rr, retried=True),
                        0)
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
            # Per CLIENT_INTEGRATION.md § 11, only refresh the
            # recorder UI when on-disk bytes actually changed — i.e.
            # remote→local was pulled. Local→remote-only pushes don't
            # invalidate our in-memory model, so skip the reparse.
            if result.has(S.PULLED):
                self._reload_and_restore(saved_guid)
            if result.has(S.PUSHED) or result.has(S.COMMITTED_AND_PUSHED):
                self._mark_gh_app_installed()

        def _worker():
            # § 12: contributor is daemon-owned, no longer on the wire.
            result = sync_project(langcode)
            Clock.schedule_once(lambda dt: _on_sync_done(result), 0)
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

        content = BoxLayout(orientation='vertical', spacing=dp(12), padding=dp(12))
        num_input = TextInput(
            text=initial,
            hint_text=hint,
            multiline=False, size_hint_y=None, height=dp(48),
            font_size=sp(18), input_filter='int',
        )
        content.add_widget(num_input)
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
            size_hint=(0.8, None), height=dp(180),
            auto_dismiss=True,
        )

        def _go(*args):
            text = num_input.text.strip()
            if text:
                try:
                    n = int(text)
                except ValueError:
                    popup.dismiss()
                    return
                if has_list_numbers:
                    if n in num_to_idx:
                        r.index = num_to_idx[n]
                    else:
                        closest = min(num_to_idx, key=lambda k: abs(k - n))
                        r.index = num_to_idx[closest]
                else:
                    n = max(1, min(n, total))
                    r.index = n - 1
                r._pending_rerecord = False
                r._notify_ui()
            popup.dismiss()

        def _clear(*args):
            r.cawl_filter = ''
            r.gloss_search = ''
            r.only_unrecorded = False
            set_peer_pref('cawl_filter', None)
            set_peer_pref('show_past_work', True)
            r.rebuild_queue()
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

        # Button area: record button OR play+redo pair
        if 'btn_area' in ids:
            btn_area = ids.btn_area
            btn_area.clear_widgets()
            if r.has_recording and not r._recording and not r._pending_rerecord:
                # Show Play (2/3) and Re-record (1/3) side by side
                play_btn = PlayButton(size_hint_x=2)
                play_btn.bind(on_touch_up=lambda w, t:
                    self.play_audio() if w.collide_point(*t.pos) else None)
                redo_btn = RedoButton(size_hint_x=1)
                redo_btn.bind(on_touch_up=lambda w, t:
                    self.redo_recording() if w.collide_point(*t.pos) else None)
                btn_area.add_widget(play_btn)
                btn_area.add_widget(redo_btn)
            else:
                # Show record button (push-to-talk)
                rec_btn = RecordButton()
                rec_btn.recording = r._recording
                rec_btn.bind(on_touch_down=lambda w, t:
                    self.record_start() if w.collide_point(*t.pos) else None)
                rec_btn.bind(on_touch_up=lambda w, t:
                    self.record_stop() if w.collide_point(*t.pos) else None)
                btn_area.add_widget(rec_btn)


if __name__ == '__main__':
    LIFTRecorderApp().run()
