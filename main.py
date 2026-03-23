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

from appinfo import APP_NAME, APP_TAGLINE, APP_USER_AGENT, APP_ICON
import theme

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

from kivy.app import App
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
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
# Place CharisSIL-Regular.ttf (and optionally Bold/Italic/BoldItalic) in a
# 'fonts/' directory next to main.py.  Download from: https://software.sil.org/charis/
# On Linux you can also: sudo apt install fonts-sil-charis
# The font files are also searched in the system font directories.

def _find_font(filename):
    """Search for a font file in several likely locations."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    search = [
        os.path.join(app_dir, 'fonts', filename),
        os.path.join(app_dir, filename),
        os.path.join('/usr/share/fonts/truetype/fonts-sil-charis', filename),
        os.path.join('/usr/share/fonts/opentype/charis', filename),
        os.path.join(os.path.expanduser('~'), '.fonts', filename),
        os.path.join(os.path.expanduser('~'), '.local/share/fonts', filename),
        # Android: Kivy bundles app assets here at runtime
        os.path.join('/data/user/0', 'org.atoznback.azt_recorder', 'files/app/fonts', filename),
    ]
    for path in search:
        if os.path.exists(path):
            return path
    # Broader system search (slower, only if above fails)
    for root, dirs, files in os.walk('/usr/share/fonts'):
        if filename in files:
            return os.path.join(root, filename)
    return None

def _register_charis():
    from kivy.core.text import LabelBase
    regular = _find_font('CharisSIL-Regular.ttf')
    if regular is None:
        # Try alternate naming conventions
        regular = (_find_font('CharisSIL.ttf') or
                   _find_font('charissil.ttf') or
                   _find_font('CharisSIL-R.ttf'))
    if regular is None:
        print('[WARN] Charis SIL font not found. '
              'Place CharisSIL-Regular.ttf in a fonts/ subdirectory next to main.py, '
              'or install with: sudo apt install fonts-sil-charis')
        return False
    bold    = (_find_font('CharisSIL-Bold.ttf') or
               _find_font('CharisSIL-B.ttf') or regular)
    italic  = (_find_font('CharisSIL-Italic.ttf') or
               _find_font('CharisSIL-I.ttf') or regular)
    boldita = (_find_font('CharisSIL-BoldItalic.ttf') or
               _find_font('CharisSIL-BI.ttf') or bold)
    LabelBase.register(
        name='CharisSIL',
        fn_regular=regular,
        fn_bold=bold,
        fn_italic=italic,
        fn_bolditalic=boldita,
    )
    print(f'[INFO] Charis SIL registered: {regular}')
    return True

_CHARIS_AVAILABLE = _register_charis()
_FONT_NAME = 'CharisSIL' if _CHARIS_AVAILABLE else 'Roboto'

# ── KV layout ─────────────────────────────────────────────────────────────────
# Font name is injected at build time so every widget uses Charis SIL if available.
KV_TEMPLATE = '''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp
#:import T theme
#:set FONT '{font_name}'

<RootScreen>:
    ScreenManager:
        id: sm
        WelcomeScreen:
            name: 'welcome'
        RecorderScreen:
            name: 'recorder'
        ConfigScreen:
            name: 'config'
        CollabScreen:
            name: 'collab'
        LangPickerScreen:
            name: 'langpicker'
        ImagePickerScreen:
            name: 'imagepicker'

<WelcomeScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        padding: dp(40)
        spacing: dp(20)
        Widget:
            size_hint_y: 0.05
        Image:
            source: app.icon #'icons/icon.png'
            size_hint: None, None
            size: dp(240), dp(240)
            pos_hint: {{'center_x': 0.5}}
            allow_stretch: True
            keep_ratio: True
        Label:
            text: app.title
            font_size: sp(28)
            font_name: FONT
            bold: True
            color: T.ACCENT
            size_hint_y: None
            height: dp(50)
        Label:
            text: 'Open or create a LIFT lexicon'
            font_size: sp(16)
            font_name: FONT
            color: T.TEXT_DIM
            size_hint_y: None
            height: dp(30)
        Widget:
            size_hint_y: 0.08
        RecBtn:
            text: 'From Phone'
            normal_color: T.ACCENT
            on_release: app.open_file()
        RecBtn:
            text: 'From Internet'
            normal_color: T.BTN_INACTIVE
            on_release: app.open_url_dialog()
        RecBtn:
            text: 'Clone Repository'
            normal_color: T.BTN_INACTIVE
            on_release: app.clone_dialog()
        RecBtn:
            text: 'Start New'
            normal_color: T.BTN_INACTIVE
            on_release: app.show_start_over() #< should be Start a new wordlist
        # ── Existing projects ─────────────────────────────────────
        BoxLayout:
            id: project_list
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            spacing: dp(6)
        Widget:
            size_hint_y: 1
        Label:
            text: app.version_string
            font_size: sp(11)
            font_name: FONT
            color: T.TEXT_FAINT
            size_hint_y: None
            height: dp(20)
            halign: 'center'
            text_size: self.size

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
                    width: dp(100)
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
                        font_size: sp(20)
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
            Button:
                size_hint_x: None
                width: dp(44)
                background_color: T.TRANSPARENT
                background_normal: ''
                on_release: app.share_apk()
                Image:
                    source: 'icons/share_dark.png'
                    size: dp(28), dp(28)
                    size_hint: None, None
                    center: self.parent.center
                    allow_stretch: True
                    keep_ratio: True
            BoxLayout:
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
                spacing: dp(20)
                # Gloss languages — 3-column grid
                SectionLabel:
                    text: 'Gloss languages'
                GridLayout:
                    id: lang_box
                    cols: 3
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(6)
                # CAWL filter
                SectionLabel:
                    text: 'Word filter'
                BoxLayout:
                    orientation: 'vertical'
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(8)
                    Label:
                        text: 'CAWL number or range (e.g. 1-100, 42, leave blank for all)'
                        font_size: sp(13)
                        font_name: FONT
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: dp(36)
                        halign: 'left'
                        text_size: self.width, None
                    TextInput:
                        id: cawl_input
                        hint_text: 'e.g. 1-500'
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
                        text: 'Gloss search (filter by gloss text)'
                        font_size: sp(13)
                        font_name: FONT
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: dp(36)
                        halign: 'left'
                        text_size: self.width, None
                    TextInput:
                        id: gloss_search_input
                        hint_text: 'search gloss text…'
                        font_size: sp(16)
                        font_name: FONT
                        size_hint_y: None
                        height: dp(48)
                        background_color: T.SURFACE
                        foreground_color: T.TEXT
                        cursor_color: T.ACCENT
                        multiline: False
                # Show past work — toggle (logically reversed from only_unrecorded)
                UnrecordedToggle:
                    id: unrecorded_toggle
                    active: False
                    on_active: root.toggle_show_past(self.active)
                # Recording task selector
                BoxLayout:
                    id: rec_task_row
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(8)
                    Label:
                        text: 'Recording:'
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
                # Bottom spacer
                Widget:
                    size_hint_y: None
                    height: dp(16)
                Widget:
                    size_hint_y: None
                    height: dp(8)
                RecBtn:
                    text: 'Setup Collaboration'
                    normal_color: T.SURFACE
                    on_release: app.go_collab()
                Widget:
                    size_hint_y: None
                    height: dp(16)
                SectionLabel:
                    text: 'Image repository'
                TextInput:
                    id: image_repo_input
                    hint_text: 'https://github.com/kent-rasmussen/images_CAWL'
                    font_size: sp(12)
                    font_name: FONT
                    size_hint_y: None
                    height: dp(48)
                    background_color: T.SURFACE
                    foreground_color: T.TEXT
                    cursor_color: T.ACCENT
                    multiline: False
                Widget:
                    size_hint_y: None
                    height: dp(8)
                RecBtn:
                    text: 'Start over'
                    normal_color: T.BTN_INACTIVE
                    on_release: app.go_welcome() #new_from_template < should be "Open or create a LIFT lexicon" (WelcomeScreen)
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
                text: 'Setup'
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
                on_release: app.go_config()
        ScrollView:
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                # ── Your name ─────────────────────────────────────────────
                SectionLabel:
                    text: 'Your name'
                TextInput:
                    id: name_input
                    hint_text: 'Your name (for commit attribution)'
                    font_size: sp(15)
                    font_name: FONT
                    size_hint_y: None
                    height: dp(48)
                    background_color: T.SURFACE
                    foreground_color: T.TEXT
                    cursor_color: T.ACCENT
                    multiline: False
                # ── Host toggle ───────────────────────────────────────────
                BoxLayout:
                    size_hint_y: None
                    height: dp(40)
                    spacing: dp(8)
                    RecBtn:
                        id: host_github_btn
                        text: 'GitHub'
                        font_size: sp(14)
                        normal_color: T.GREEN
                        on_release: root.set_host('github')
                    RecBtn:
                        id: host_gitlab_btn
                        text: 'GitLab'
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
                        text: 'GitHub account'
                    Label:
                        id: gh_status_label
                        text: 'Not connected'
                        font_size: sp(14)
                        font_name: FONT
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: dp(28)
                        halign: 'left'
                        text_size: self.width, None
                    RecBtn:
                        id: gh_connect_btn
                        text: 'Connect to GitHub'
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
                        on_ref_press: root.open_link(args[1])
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
                            text: 'Copy'
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
                        text: 'GitLab account'
                    TextInput:
                        id: gl_token_input
                        hint_text: 'Personal access token'
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
                        hint_text: 'GitLab username'
                        font_size: sp(14)
                        font_name: FONT
                        size_hint_y: None
                        height: dp(48)
                        background_color: T.SURFACE
                        foreground_color: T.TEXT
                        cursor_color: T.ACCENT
                        multiline: False
                    RecBtn:
                        text: 'Save GitLab credentials'
                        normal_color: T.GREEN
                        on_release: root.save_gitlab_credentials()
                # ── Publish ───────────────────────────────────────────────
                SectionLabel:
                    text: 'Publish this project'
                TextInput:
                    id: langcode_input
                    hint_text: 'Language code (repo name)'
                    font_size: sp(14)
                    font_name: FONT
                    size_hint_y: None
                    height: dp(48)
                    background_color: T.SURFACE
                    foreground_color: T.TEXT
                    cursor_color: T.ACCENT
                    multiline: False
                    on_text: root.update_publish_url()
                Label:
                    id: publish_url_label
                    text: ''
                    font_size: sp(12)
                    font_name: FONT
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: dp(28)
                    halign: 'left'
                    text_size: self.width, None
                RecBtn:
                    text: 'Publish'
                    normal_color: T.ACCENT
                    on_release: root.do_publish()
                # ── Log ───────────────────────────────────────────────────
                SectionLabel:
                    text: 'Last operation'
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
                    size_hint_y: None
                    height: dp(40)

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
                text: 'openclipart'
                normal_color: T.GREEN
                font_size: sp(12)
                on_release: root.fetch_openclipart()
            RecBtn:
                text: 'FreeSVG'
                normal_color: T.TEAL
                font_size: sp(12)
                on_release: root.fetch_freesvg()
            RecBtn:
                text: 'Wikimedia'
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
                text: 'Photo'
                normal_color: T.BTN_INACTIVE
                font_size: sp(12)
                on_release: root.take_photo()
            RecBtn:
                text: 'File'
                normal_color: T.BTN_INACTIVE
                font_size: sp(12)
                on_release: root.pick_from_file()
            RecBtn:
                text: 'Cancel'
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

<LangPickerScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        padding: dp(16)
        spacing: dp(10)
        # ── Title ─────────────────────────────────────────────────────────
        Label:
            text: 'Choose your language'
            font_size: sp(22)
            font_name: FONT
            bold: True
            color: T.ACCENT
            size_hint_y: None
            height: dp(44)
        # ── Search ────────────────────────────────────────────────────────
        TextInput:
            id: lang_search
            hint_text: 'Type a language name...'
            font_size: sp(16)
            font_name: FONT
            multiline: False
            size_hint_y: None
            height: dp(44)
            background_color: T.SURFACE
            foreground_color: T.TEXT
            hint_text_color: T.HINT
            cursor_color: T.ACCENT
            padding: [dp(10), dp(10)]
            on_text: root._on_search_text(self.text)
        # ── Results ───────────────────────────────────────────────────────
        ScrollView:
            id: results_scroll
            BoxLayout:
                id: results_box
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(4)
        # ── Selection details (hidden until a language is picked) ─────────
        BoxLayout:
            id: selection_box
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            opacity: 0
            spacing: dp(6)
            Label:
                id: selected_label
                text: ''
                font_size: sp(15)
                font_name: FONT
                color: T.TEXT
                size_hint_y: None
                height: dp(32)
                halign: 'left'
                text_size: self.width, None
            # Region picker (shown if >1 region)
            Label:
                id: region_title
                text: 'Select region:'
                font_size: sp(14)
                font_name: FONT
                color: T.TEXT_DIM
                size_hint_y: None
                height: 0
                opacity: 0
                halign: 'left'
                text_size: self.width, None
            BoxLayout:
                id: region_box
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(4)
            # Dialect toggle
            BoxLayout:
                size_hint_y: None
                height: dp(40)
                spacing: dp(8)
                CheckBox:
                    id: dialect_check
                    size_hint_x: None
                    width: dp(40)
                    active: False
                    on_active: root._toggle_dialect(self.active)
                Label:
                    text: "I'm working on a dialect"
                    font_size: sp(14)
                    font_name: FONT
                    color: T.TEXT
                    halign: 'left'
                    valign: 'middle'
                    text_size: self.size
            TextInput:
                id: dialect_input
                hint_text: 'Variant code (2-8 chars)'
                font_size: sp(14)
                font_name: FONT
                multiline: False
                size_hint_y: None
                height: 0
                opacity: 0
                background_color: T.SURFACE
                foreground_color: T.TEXT
                hint_text_color: T.HINT
                cursor_color: T.ACCENT
                padding: [dp(10), dp(10)]
                on_text: root._update_code()
            # Assembled code display
            Label:
                id: code_label
                text: ''
                font_size: sp(16)
                font_name: FONT
                bold: True
                color: T.GREEN
                size_hint_y: None
                height: dp(28)
                halign: 'left'
                text_size: self.width, None
        # ── Continue button ───────────────────────────────────────────────
        RecBtn:
            id: continue_btn
            text: 'Continue'
            normal_color: T.GREEN
            size_hint_y: None
            height: dp(52)
            opacity: 0.3
            disabled: True
            on_release: root._on_continue()

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
            text: 'Show past work'
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
    pass


class WelcomeScreen(Screen):

    def on_enter(self):
        self._populate_projects()

    def _populate_projects(self):
        box = self.ids.get('project_list')
        if not box:
            return
        box.clear_widgets()
        app = App.get_running_app()
        projects = app.list_projects()
        if not projects:
            return
        for name, path in projects:
            btn = Builder.load_string(
                'RecBtn:\n'
                f'    text: {name!r}\n'
                '    normal_color: T.GREEN\n'
            )
            btn.lift_path = path
            btn.bind(on_release=lambda b: app.load_lift(b.lift_path))
            box.add_widget(btn)


class LangPickerScreen(Screen):
    """Language code picker shown when creating a new project."""
    _langtags = None        # class-level cache: list of dicts
    _search_index = None    # parallel list of lowered searchable strings
    _region_names = None    # region code -> name mapping

    _selected = None        # chosen langtag entry dict
    _selected_region = ''   # chosen region code (or '')
    _dialect_code = ''      # user-entered variant

    def on_enter(self):
        self._selected = None
        self._selected_region = ''
        self._dialect_code = ''
        si = self.ids.get('lang_search')
        if si:
            si.text = ''
        self._hide_selection()
        if LangPickerScreen._langtags is None:
            self._load_langtags()

    @classmethod
    def _load_langtags(cls):
        import gzip, json
        gz_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'langtags_mini.json.gz')
        with open(gz_path, 'rb') as f:
            blob = json.loads(gzip.decompress(f.read()))
        cls._langtags = blob['langs']
        cls._region_names = blob.get('region_names', {})
        # Build search index once
        idx = []
        for entry in cls._langtags:
            parts = [entry.get('n', '').lower()]
            if 'ln' in entry:
                parts.append(entry['ln'].lower())
            if 'ns' in entry:
                parts.extend(n.lower() for n in entry['ns'])
            if 'lns' in entry:
                parts.extend(n.lower() for n in entry['lns'])
            if 't' in entry:
                parts.append(entry['t'].lower())
            if 'i' in entry:
                parts.append(entry['i'].lower())
            idx.append(' '.join(parts))
        cls._search_index = idx

    def _on_search_text(self, text):
        # Debounce: cancel previous, schedule new
        from kivy.clock import Clock
        if hasattr(self, '_search_ev') and self._search_ev:
            self._search_ev.cancel()
        self._search_ev = Clock.schedule_once(
            lambda dt: self._do_search(text), 0.25)

    def _do_search(self, text):
        box = self.ids.get('results_box')
        if not box:
            return
        box.clear_widgets()
        if not text or len(text) < 2 or self._langtags is None:
            return
        q = text.lower()
        matches = []
        for i, searchable in enumerate(self._search_index):
            if q in searchable:
                matches.append(self._langtags[i])
                if len(matches) >= 50:
                    break
        for entry in matches:
            self._add_result_row(box, entry)

    def _add_result_row(self, box, entry):
        from kivy.metrics import dp, sp
        from kivy.uix.button import Button
        btn = Button(
            text=self._format_entry(entry),
            font_size=sp(13),
            font_name=_FONT_NAME,
            size_hint_y=None,
            height=dp(48),
            halign='left',
            valign='middle',
            background_color=theme.SURFACE,
            background_normal='',
            color=theme.TEXT,
            padding=(dp(10), dp(4)),
        )
        btn.text_size = (None, None)
        btn.bind(size=lambda w, s: setattr(w, 'text_size', s))
        btn.bind(on_release=lambda w: self._select_language(entry))
        box.add_widget(btn)

    @staticmethod
    def _format_entry(entry):
        name = entry.get('n', '')
        local = entry.get('ln', '')
        tag = entry.get('t', '')
        region = entry.get('rn', '')
        parts = [name]
        if local and local != name:
            parts[0] += f'  ({local})'
        parts.append(f'[{tag}]')
        if region:
            parts.append(f'- {region}')
        return '  '.join(parts)

    def _select_language(self, entry):
        from kivy.metrics import dp, sp
        from kivy.uix.button import Button
        self._selected = entry
        self._selected_region = ''
        sel_box = self.ids.get('selection_box')
        if sel_box:
            sel_box.opacity = 1
        lbl = self.ids.get('selected_label')
        if lbl:
            lbl.text = self._format_entry(entry)
        # Clear results and search
        box = self.ids.get('results_box')
        if box:
            box.clear_widgets()
        si = self.ids.get('lang_search')
        if si:
            si.text = ''

        # Region picker
        regions = entry.get('rs', [])
        primary = entry.get('r', '')
        all_regions = []
        if primary:
            all_regions.append(primary)
        for r in regions:
            if r not in all_regions:
                all_regions.append(r)

        region_box = self.ids.get('region_box')
        region_title = self.ids.get('region_title')
        if region_box:
            region_box.clear_widgets()
        if len(all_regions) > 1:
            if region_title:
                region_title.height = dp(20)
                region_title.opacity = 1
            rnames = self._region_names or {}
            # "All/multiple" option
            btn = Button(
                text='Multiple / all regions',
                font_size=sp(13),
                font_name=_FONT_NAME,
                size_hint_y=None,
                height=dp(38),
                background_color=theme.SURFACE_ALT,
                background_normal='',
                color=theme.TEXT,
            )
            btn.bind(on_release=lambda w: self._select_region(''))
            region_box.add_widget(btn)
            for rc in all_regions:
                rn = rnames.get(rc, rc)
                btn = Button(
                    text=f'{rn} ({rc})',
                    font_size=sp(13),
                    font_name=_FONT_NAME,
                    size_hint_y=None,
                    height=dp(38),
                    background_color=theme.SURFACE_ALT,
                    background_normal='',
                    color=theme.TEXT,
                )
                btn.bind(on_release=lambda w, c=rc: self._select_region(c))
                region_box.add_widget(btn)
        else:
            if region_title:
                region_title.height = 0
                region_title.opacity = 0

        self._update_code()
        cb = self.ids.get('continue_btn')
        if cb:
            cb.disabled = False
            cb.opacity = 1

    def _select_region(self, region_code):
        from kivy.metrics import dp
        self._selected_region = region_code
        # Highlight selected region in the region box
        region_box = self.ids.get('region_box')
        if region_box:
            for child in region_box.children:
                if region_code and region_code in child.text:
                    child.background_color = theme.ACCENT
                elif not region_code and 'Multiple' in child.text:
                    child.background_color = theme.ACCENT
                else:
                    child.background_color = theme.SURFACE_ALT
        self._update_code()

    def _toggle_dialect(self, active):
        from kivy.metrics import dp
        di = self.ids.get('dialect_input')
        if di:
            di.height = dp(44) if active else 0
            di.opacity = 1 if active else 0
            if not active:
                di.text = ''
                self._dialect_code = ''
        self._update_code()

    def _hide_selection(self):
        from kivy.metrics import dp
        sel_box = self.ids.get('selection_box')
        if sel_box:
            sel_box.opacity = 0
        region_title = self.ids.get('region_title')
        if region_title:
            region_title.height = 0
            region_title.opacity = 0
        region_box = self.ids.get('region_box')
        if region_box:
            region_box.clear_widgets()
        di = self.ids.get('dialect_input')
        if di:
            di.height = 0
            di.opacity = 0
            di.text = ''
        dc = self.ids.get('dialect_check')
        if dc:
            dc.active = False
        cl = self.ids.get('code_label')
        if cl:
            cl.text = ''
        cb = self.ids.get('continue_btn')
        if cb:
            cb.disabled = True
            cb.opacity = 0.3

    def _update_code(self):
        if not self._selected:
            return
        code = self._selected.get('t', '')
        if self._selected_region:
            code += '-' + self._selected_region
        di = self.ids.get('dialect_input')
        if di and di.text.strip():
            variant = di.text.strip().lower()
            # Clamp to 2-8 alphanumeric chars
            variant = ''.join(c for c in variant if c.isalnum())[:8]
            self._dialect_code = variant
            if len(variant) >= 2:
                code += '-x-' + variant
        else:
            self._dialect_code = ''
        cl = self.ids.get('code_label')
        if cl:
            cl.text = f'Language code: {code}'

    def _assembled_code(self):
        if not self._selected:
            return ''
        code = self._selected.get('t', '')
        if self._selected_region:
            code += '-' + self._selected_region
        if self._dialect_code and len(self._dialect_code) >= 2:
            code += '-x-' + self._dialect_code
        return code

    def _on_continue(self):
        app = App.get_running_app()
        code = self._assembled_code()
        app._pending_vernlang = code
        # Show overlay immediately so user knows the button worked
        app._show_loading_overlay(f'Setting up wordlist for {code}...')
        app.new_from_template()


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

        # Gather URLs from CAWL image repo
        db = app.recorder.db if app.recorder else None
        urls = db.all_image_urls(entry) if db else []

        # Each image = ~1/4 of screen in a 2x2 grid
        # Use dp-based size: half screen height minus chrome
        screen_h = Window.height
        self._cell_h = max(int(screen_h / 2.5), dp(200))

        # Use 1 column if ≤2 images, else 2
        grid.cols = 1 if len(urls) <= 2 else 2

        self._add_image_buttons(grid, urls, self._cell_h)

        # Auto-fetch from web sources if internet available and few images
        if len(urls) < 10 and self._glosses:
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

    def _download_and_set(self, url):
        """Background: download image, scale, save, update LIFT XML."""
        import urllib.request
        app = App.get_running_app()
        entry = self._entry
        db = app.recorder.db

        try:
            ctx = app._ssl_context()
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                data = resp.read()

            # Determine filename from azt convention
            filename = db.imagename(entry)
            images_dir = db.images_dir
            os.makedirs(images_dir, exist_ok=True)
            dest = os.path.join(images_dir, filename)

            # Scale down if larger than 1284px in either dimension
            from PIL import Image as PILImage
            import io
            img = PILImage.open(io.BytesIO(data))
            max_dim = 1284
            w, h = img.size
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
            img.save(dest, 'PNG')

            # Update LIFT XML
            guid = entry.get('guid', '')
            db.set_illustration(guid, filename)

            # Update in-memory entry
            entry['image_path'] = dest
            entry['illustration_href'] = filename

            Clock.schedule_once(lambda dt: app.refresh_recorder_ui(), 0)
        except Exception as ex:
            print(f'[image-picker] download error: {ex}')

    def pick_from_file(self):
        """Let user pick an image from device storage or camera."""
        app = App.get_running_app()
        if platform == 'android':
            self._pick_from_file_android()
        else:
            self._pick_from_file_desktop()

    def _pick_from_file_desktop(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root_tk = tk.Tk()
            root_tk.withdraw()
            path = filedialog.askopenfilename(
                title='Select image',
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
        app = App.get_running_app()
        entry = self._entry
        db = app.recorder.db
        try:
            from PIL import Image as PILImage
            filename = db.imagename(entry)
            images_dir = db.images_dir
            os.makedirs(images_dir, exist_ok=True)
            dest = os.path.join(images_dir, filename)

            img = PILImage.open(source_path)
            max_dim = 1284
            w, h = img.size
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
            img.save(dest, 'PNG')
            print(f'[image-picker] saved {img.size[0]}x{img.size[1]} to {dest}')

            guid = entry.get('guid', '')
            db.set_illustration(guid, filename)
            # Bust Kivy image cache so the new file is reloaded
            from kivy.cache import Cache
            Cache.remove('kv.image', dest)
            Cache.remove('kv.texture', dest)
            entry['image_path'] = dest
            entry['illustration_href'] = filename
            def _update_ui(dt):
                app.refresh_recorder_ui()
                app._show_toast('Image updated')
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
        """Background auto-fetch from web sources if internet available."""
        try:
            from collab import _has_internet
            if not _has_internet():
                return
        except Exception:
            return
        self._do_openclipart(cell_h)
        self._do_wikimedia(cell_h)
        self._do_freesvg(cell_h)

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
                    lambda dt: app._show_toast('No images found on openclipart'), 0)
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
                    lambda dt: app._show_toast('No images found on FreeSVG'), 0)
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
                    lambda dt: app._show_toast('No public domain images on Wikimedia'), 0)
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
            app._show_toast(f'{len(new_urls)} images from {source_name}')

    def take_photo(self):
        """Launch camera to take a photo."""
        if platform == 'android':
            self._take_photo_android()
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
        if not app.recorder:
            return
        self.build_lang_toggles()
        cawl_in = self.ids.get('cawl_input')
        if cawl_in:
            cawl_in.text = app.recorder.cawl_filter or ''
        gs = self.ids.get('gloss_search_input')
        if gs:
            gs.text = app.recorder.gloss_search or ''
        ir = self.ids.get('image_repo_input')
        if ir:
            ir.text = app._load_prefs().get('image_repo', '')
        # Restore show-past-work toggle (default False)
        prefs = app._load_prefs()
        show_past = prefs.get('show_past_work', False)
        toggle = self.ids.get('unrecorded_toggle')
        if toggle:
            toggle.active = show_past
        self.only_unrecorded = not show_past
        # Build recording options
        self._build_rec_options(app)

    def build_lang_toggles(self):
        app = App.get_running_app()
        box = self.ids.get('lang_box')
        if box is None:
            return
        box.clear_widgets()
        for lang in app.recorder.all_gloss_langs:
            t = LangToggle(
                lang=lang,
                active=lang in app.recorder.active_gloss_langs,
                callback=self._toggle_lang,
            )
            box.add_widget(t)

    def _toggle_lang(self, lang, active):
        app = App.get_running_app()
        langs = set(app.recorder.active_gloss_langs)
        if active:
            langs.add(lang)
        else:
            langs.discard(lang)
        app.recorder.active_gloss_langs = sorted(langs)

    def apply_cawl(self, text):
        App.get_running_app().recorder.cawl_filter = text.strip()

    def toggle_show_past(self, show_past):
        self.only_unrecorded = not show_past
        app = App.get_running_app()
        if app.recorder:
            app.recorder.only_unrecorded = not show_past
        prefs = app._load_prefs()
        prefs['show_past_work'] = show_past
        app._save_prefs_dict(prefs)

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
        prefs = app._load_prefs()
        saved_key = prefs.get('rec_task', 'citation')
        # If saved key is no longer available, fall back to citation
        if not any(k == saved_key for k, _ in available):
            saved_key = 'citation'
            prefs['rec_task'] = saved_key
            app._save_prefs_dict(prefs)
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

        app = App.get_running_app()
        prefs = app._load_prefs()
        current_key = prefs.get('rec_task', 'citation')

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
                prefs2 = app._load_prefs()
                prefs2['rec_task'] = k
                app._save_prefs_dict(prefs2)
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
        cawl_in = self.ids.get('cawl_input')
        if cawl_in:
            app.recorder.cawl_filter = cawl_in.text.strip()
        gs = self.ids.get('gloss_search_input')
        if gs:
            app.recorder.gloss_search = gs.text.strip()
        ir = self.ids.get('image_repo_input')
        if ir:
            prefs = app._load_prefs()
            prefs['image_repo'] = ir.text.strip()
            app._save_prefs_dict(prefs)
        app.recorder.only_unrecorded = self.only_unrecorded
        app.recorder.rebuild_queue()
        app.go_recorder()


class CollabScreen(Screen):

    def on_enter(self):
        app = App.get_running_app()
        prefs = app._load_prefs()
        w = self.ids.get('name_input')
        if w and not w.text:
            w.text = prefs.get('collab_name', '')
        w = self.ids.get('langcode_input')
        if w and not w.text:
            w.text = prefs.get('collab_langcode', '')
        # Restore host selection
        host = prefs.get('collab_host', 'github')
        self.set_host(host, save=False)
        self._update_gh_status()
        # Restore GitLab fields
        gl_user = self.ids.get('gl_username_input')
        if gl_user and not gl_user.text:
            gl_user.text = prefs.get('gl_username', '')
        self.update_publish_url()

    def set_host(self, host, save=True):
        """Toggle between github and gitlab sections."""
        self._host = host
        gh_btn = self.ids.get('host_github_btn')
        gl_btn = self.ids.get('host_gitlab_btn')
        gh_sec = self.ids.get('gh_section')
        gl_sec = self.ids.get('gl_section')

        active_color = theme.GREEN
        inactive_color = theme.BTN_INACTIVE

        if host == 'gitlab':
            if gh_btn:
                gh_btn.normal_color = inactive_color
            if gl_btn:
                gl_btn.normal_color = active_color
            if gh_sec:
                gh_sec.height = 0
                gh_sec.opacity = 0
            if gl_sec:
                gl_sec.height = gl_sec.minimum_height
                gl_sec.opacity = 1
        else:
            if gh_btn:
                gh_btn.normal_color = active_color
            if gl_btn:
                gl_btn.normal_color = inactive_color
            if gh_sec:
                gh_sec.height = gh_sec.minimum_height
                gh_sec.opacity = 1
            if gl_sec:
                gl_sec.height = 0
                gl_sec.opacity = 0

        if save:
            app = App.get_running_app()
            prefs = app._load_prefs()
            prefs['collab_host'] = host
            app._save_prefs_dict(prefs)
        self.update_publish_url()

    def save_gitlab_credentials(self):
        """Save GitLab PAT and username to prefs."""
        app = App.get_running_app()
        prefs = app._load_prefs()
        token_w = self.ids.get('gl_token_input')
        user_w = self.ids.get('gl_username_input')
        token = token_w.text.strip() if token_w else ''
        username = user_w.text.strip() if user_w else ''
        if not token or not username:
            self._set_log('Enter both GitLab username and token.')
            return
        prefs['gl_token'] = token
        prefs['gl_username'] = username
        app._save_prefs_dict(prefs)
        self._set_log(f'GitLab credentials saved for {username}')
        self.update_publish_url()

    def _update_gh_status(self):
        """Update the GitHub connection status label."""
        app = App.get_running_app()
        prefs = app._load_prefs()
        username = prefs.get('gh_username', '')
        token = prefs.get('gh_access_token', '')
        lbl = self.ids.get('gh_status_label')
        btn = self.ids.get('gh_connect_btn')
        if lbl:
            if username and token:
                lbl.text = f'Connected as {username}'
                lbl.color = theme.GREEN
            else:
                lbl.text = 'Not connected'
                lbl.color = theme.TEXT_DIM
        if btn:
            btn.text = 'Reconnect' if (username and token) else 'Connect to GitHub'

    # ── Internal helpers ───────────────────────────────────────────────────

    def open_link(self, url):
        """Open a URL in the device browser."""
        import webbrowser
        webbrowser.open(str(url))

    def copy_code(self):
        """Copy the device code to clipboard."""
        lbl = self.ids.get('device_code_label')
        if lbl and lbl.text:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(lbl.text)
            self._set_log('Code copied to clipboard')

    def _save_settings(self):
        app = App.get_running_app()
        prefs = app._load_prefs()
        w = self.ids.get('name_input')
        if w:
            prefs['collab_name'] = w.text
        w = self.ids.get('langcode_input')
        if w:
            prefs['collab_langcode'] = w.text
        app._save_prefs_dict(prefs)

    def _set_log(self, text):
        lbl = self.ids.get('log_label')
        if lbl:
            lbl.text = text

    def _run(self, busy_msg, func, *args):
        """Show busy_msg, run func(*args) in a thread, show result."""
        self._set_log(busy_msg)
        from collab import run_async
        run_async(func, *args, on_done=lambda result: (
            self._set_log(result or ''),
        ))

    # ── Device flow ────────────────────────────────────────────────────────

    def start_device_flow(self):
        """Begin GitHub device flow authentication."""
        from collab import GITHUB_APP_CLIENT_ID
        if not GITHUB_APP_CLIENT_ID:
            self._set_log('GitHub App client_id not configured.')
            return
        self._set_log('Starting GitHub authorization...')
        import threading
        threading.Thread(target=self._device_flow_worker, daemon=True).start()

    def _device_flow_worker(self):
        from collab import device_flow_start, device_flow_poll, \
            get_github_username, save_tokens, check_app_installed, \
            app_install_url, GITHUB_APP_INSTALL_URL
        app = App.get_running_app()
        try:
            resp = device_flow_start()
            user_code = resp['user_code']
            device_code = resp['device_code']
            verification_uri = resp.get('verification_uri', 'https://github.com/login/device')
            interval = resp.get('interval', 5)
            expires_in = resp.get('expires_in', 900)

            def _show_code(dt):
                inst = self.ids.get('device_instructions_label')
                if inst:
                    inst.text = (f'Go to [color=5cb3ff][ref={verification_uri}]'
                                 f'{verification_uri}[/ref][/color]\n'
                                 f'and enter this code:')
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
                self._set_log('Code copied to clipboard — paste it on the GitHub page')
            Clock.schedule_once(_show_code, 0)

            # Poll until authorized
            token_data = device_flow_poll(device_code, interval, expires_in)
            access_token = token_data['access_token']

            # Get the GitHub username
            username = get_github_username(access_token)

            # Save tokens
            save_tokens(app._prefs_path, token_data, username)

            # Also save username in old pref keys for backward compat
            prefs = app._load_prefs()
            prefs['collab_username'] = username
            prefs['collab_token'] = access_token
            app._save_prefs_dict(prefs)

            # Check if the app is installed (required for repo access)
            install_info = check_app_installed(access_token)
            installed = install_info['installed']
            install_id = install_info['installation_id']
            url = app_install_url(install_id)

            def _done(dt):
                lbl = self.ids.get('device_code_label')
                if lbl:
                    lbl.text = ''
                box = self.ids.get('device_code_box')
                if box:
                    box.height = 0
                    box.opacity = 0
                inst = self.ids.get('device_instructions_label')
                if inst:
                    if not installed:
                        inst.text = (
                            'Now install the app to grant repository access.\n'
                            'Tap [color=5cb3ff][ref='
                            f'{url}]Install[/ref][/color]'
                            ' and select "All repositories".'
                        )
                        inst.height = dp(50)
                    else:
                        inst.text = ''
                        inst.height = 0
                self._update_gh_status()
                if installed:
                    self._set_log(f'Connected as {username}')
                else:
                    self._set_log(f'Connected as {username} — install app for repo access')
                    import webbrowser
                    webbrowser.open(url)
            Clock.schedule_once(_done, 0)

        except Exception as ex:
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
                self._set_log(f'Authorization failed: {ex}')
            Clock.schedule_once(_err, 0)

    # ── Button handlers ────────────────────────────────────────────────────

    def update_publish_url(self):
        """Auto-generate the publish URL from username and language code."""
        lbl = self.ids.get('publish_url_label')
        if not lbl:
            return
        app = App.get_running_app()
        prefs = app._load_prefs()
        host = getattr(self, '_host', prefs.get('collab_host', 'github'))
        lang_w = self.ids.get('langcode_input')
        lang = lang_w.text.strip() if lang_w else ''
        if host == 'gitlab':
            user = prefs.get('gl_username', '')
            domain = 'gitlab.com'
        else:
            user = prefs.get('gh_username', '')
            domain = 'github.com'
        if not user or not lang:
            lbl.text = ''
            return
        lbl.text = f'https://{domain}/{user}/{lang}.git'

    def _get_credentials_for_host(self):
        """Return (username, token) for the currently selected host."""
        app = App.get_running_app()
        prefs = app._load_prefs()
        host = getattr(self, '_host', prefs.get('collab_host', 'github'))
        if host == 'gitlab':
            return prefs.get('gl_username', ''), prefs.get('gl_token', '')
        return app._get_gh_credentials()

    def do_publish(self):
        app = App.get_running_app()
        if not app.recorder:
            self._set_log('No project loaded.')
            return
        self._save_settings()
        user, token = self._get_credentials_for_host()
        host = getattr(self, '_host', 'github')
        if not token:
            host_name = 'GitLab' if host == 'gitlab' else 'GitHub'
            self._set_log(f'Connect to {host_name} first.')
            return
        name_w = self.ids.get('name_input')
        name = (name_w.text.strip() if name_w else '') or 'Recorder'
        lbl = self.ids.get('publish_url_label')
        remote_url = lbl.text.strip() if lbl else ''
        if not remote_url:
            self._set_log('Enter a language code first.')
            return
        # GitLab uses username + PAT directly; GitHub uses x-access-token
        git_user = user if host == 'gitlab' else 'x-access-token'
        from collab import init_repo
        self._run('Publishing...', init_repo,
                  app.recorder.db.dir, remote_url,
                  git_user, token,
                  'main', name)



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
        self.cawl_filter = ''
        self.gloss_search = ''
        self.only_unrecorded = False
        self._recording = False
        self._playing = False
        self._pending_rerecord = False
        self._audio_path = None
        self._recorder = None

        # Gloss languages
        self.all_gloss_langs = sorted(db.gloss_langs)
        self.active_gloss_langs = self.all_gloss_langs[:] if len(self.all_gloss_langs) <= 3 \
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
        total = len(self.queue)
        parts = [self.list_name]
        if cawl_num:
            parts.append(cawl_num)
        parts.append(f'/ {total}')
        return ' '.join(parts)

    @property
    def has_image(self):
        e = self.current
        if not e:
            return False
        p = e.get('image_path', '')
        if not p:
            return False
        # Accept both local paths and remote URLs (AsyncImage handles both)
        return p.startswith('https://') or p.startswith('http://') or os.path.exists(p)

    @property
    def image_path(self):
        e = self.current
        if not e:
            return ''
        return e.get('image_path', '')

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
            return 'Recording...'
        fn = e.get('audio_filename')
        if fn:
            return fn
        return 'Not yet recorded'

    # ── Recording ──────────────────────────────────────────────────────────────

    def start_recording(self):
        if self._recording or not self.current:
            return
        self._recording = True
        self._audio_path = self._make_audio_path()
        self._start_native_recording(self._audio_path)
        self._notify_ui()

    def stop_recording(self):
        if not self._recording:
            return
        self._recording = False
        self._pending_rerecord = False
        self._stop_native_recording()
        # Write filename into LIFT XML
        if self._audio_path and os.path.exists(self._audio_path):
            filename = os.path.basename(self._audio_path)
            self.db.set_audio(self.current['guid'], filename)
            self.current['audio_filename'] = filename
        self._notify_ui()
        # Auto-play after a short delay
        Clock.schedule_once(lambda dt: self.play_audio(), 0.5)

    def play_audio(self):
        if self._playing:
            return
        e = self.current
        if not e or not e.get('audio_filename'):
            return
        filename = e['audio_filename']
        # Check audio/ subdir first, then the LIFT file's own directory
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
        audio_dir = os.path.join(self.db.dir, 'audio')
        os.makedirs(audio_dir, exist_ok=True)
        # Filename: {cawl}_{guid}_{en_gloss}.wav
        cawl = e.get('cawl', '0000')
        guid = e.get('guid', 'unknown')[:8]
        gloss = e.get('glosses', {}).get('en', [''])[0]
        safe_gloss = ''.join(c if c.isalnum() or c in '_ ' else '_' for c in gloss)[:24].strip().replace(' ', '_')
        filename = f'{cawl}_{guid}_{safe_gloss}.wav'
        return os.path.join(audio_dir, filename)

    def _start_native_recording(self, path):
        if platform == 'android':
            self._start_android_recording(path)
        elif platform == 'ios':
            self._start_ios_recording(path)
        else:
            self._start_desktop_recording(path)

    def _stop_native_recording(self):
        if platform == 'android':
            self._stop_android_recording()
        elif platform == 'ios':
            self._stop_ios_recording()
        else:
            self._stop_desktop_recording()

    # Android: use MediaRecorder via pyjnius for maximum quality (PCM WAV)
    def _start_android_recording(self, path):
        try:
            from jnius import autoclass
            MediaRecorder = autoclass('android.media.MediaRecorder')
            AudioSource = autoclass('android.media.MediaRecorder$AudioSource')
            OutputFormat = autoclass('android.media.MediaRecorder$OutputFormat')
            AudioEncoder = autoclass('android.media.MediaRecorder$AudioEncoder')

            mr = MediaRecorder()
            mr.setAudioSource(AudioSource.MIC)
            # MPEG_4/AAC gives broadest compatibility and highest quality on Android
            mr.setOutputFormat(OutputFormat.MPEG_4)
            # Replace extension for AAC output
            aac_path = path.replace('.wav', '.m4a')
            mr.setOutputFile(aac_path)
            mr.setAudioEncoder(AudioEncoder.AAC)
            mr.setAudioEncodingBitRate(256000)   # 256 kbps
            mr.setAudioSamplingRate(48000)        # 48 kHz
            mr.setAudioChannels(1)                # mono for voice
            mr.prepare()
            mr.start()
            self._recorder = mr
            self._audio_path = aac_path
        except Exception as ex:
            print(f'Android recording error: {ex}')

    def _stop_android_recording(self):
        if self._recorder:
            try:
                self._recorder.stop()
                self._recorder.release()
            except Exception as ex:
                print(f'Android stop error: {ex}')
            finally:
                self._recorder = None

    # iOS: use AVAudioRecorder via pyobjus for maximum quality
    def _start_ios_recording(self, path):
        try:
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
            recorder, err = AVAudioRecorder.alloc().initWithURL_settings_error_(url, settings, None)
            recorder.record()
            self._recorder = recorder
            self._audio_path = flac_path
        except Exception as ex:
            print(f'iOS recording error: {ex}')

    def _stop_ios_recording(self):
        if self._recorder:
            try:
                self._recorder.stop()
            except Exception as ex:
                print(f'iOS stop error: {ex}')
            finally:
                self._recorder = None

    # Desktop fallback: sounddevice → WAV (for development/testing)
    def _start_desktop_recording(self, path):
        try:
            import sounddevice as sd
            import numpy as np
            self._desktop_frames = []
            self._desktop_samplerate = 48000

            def callback(indata, frames, time, status):
                self._desktop_frames.append(indata.copy())

            self._desktop_stream = sd.InputStream(
                samplerate=self._desktop_samplerate,
                channels=1,
                dtype='int16',
                callback=callback,
            )
            self._desktop_stream.start()
        except Exception as ex:
            print(f'Desktop recording error: {ex}')

    def _stop_desktop_recording(self):
        try:
            import soundfile as sf
            import numpy as np
            self._desktop_stream.stop()
            self._desktop_stream.close()
            if self._desktop_frames:
                data = np.concatenate(self._desktop_frames, axis=0)
                sf.write(self._audio_path, data, self._desktop_samplerate, subtype='PCM_16')
        except Exception as ex:
            print(f'Desktop stop error: {ex}')

    # ── UI notification ────────────────────────────────────────────────────────

    def _notify_ui(self):
        """Tell the running app to refresh UI from current state."""
        app = App.get_running_app()
        if app:
            Clock.schedule_once(lambda dt: app.refresh_recorder_ui(), 0)


# ── Main App ───────────────────────────────────────────────────────────────────

__version__ = '1.14.1'


class LIFTRecorderApp(App):
    title = APP_TAGLINE
    icon = APP_ICON
    version_string = StringProperty(f'version {__version__}')
    recorder: RecorderController = None
    config_screen: ConfigScreen = None

    # ── Project discovery ────────────────────────────────────────────────────

    def list_projects(self):
        """Return [(display_name, lift_path), ...] for all projects."""
        projects_dir = os.path.join(self.user_data_dir, 'projects')
        results = []
        if os.path.isdir(projects_dir):
            for name in sorted(os.listdir(projects_dir)):
                d = os.path.join(projects_dir, name)
                if not os.path.isdir(d):
                    continue
                # Find .lift files in this project dir
                lifts = [f for f in os.listdir(d) if f.endswith('.lift')]
                if lifts:
                    # Prefer file matching dir name, else first alphabetically
                    match = f'{name}.lift'
                    lift = match if match in lifts else sorted(lifts)[0]
                    path = os.path.join(d, lift)
                    if os.path.getsize(path) > 50:
                        results.append((name, path))
        # Also check last_lift if it's outside projects/
        prefs = self._load_prefs()
        last = prefs.get('last_lift', '')
        if last and os.path.isfile(last) and os.path.getsize(last) > 50:
            if not any(p == last for _, p in results):
                display = os.path.basename(os.path.dirname(last)) or os.path.basename(last)
                results.append((display, last))
        return results

    # ── Preferences (last used file) ──────────────────────────────────────────

    @property
    def _prefs_path(self):
        return os.path.join(self.user_data_dir, 'prefs.json')

    def _save_prefs(self, lift_path):
        prefs = self._load_prefs()
        prefs['last_lift'] = lift_path
        self._save_prefs_dict(prefs)

    def _save_prefs_dict(self, prefs):
        import json
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self._prefs_path, 'w') as f:
                json.dump(prefs, f)
        except Exception as ex:
            print(f'Prefs save error: {ex}')

    def _load_prefs(self):
        import json
        try:
            with open(self._prefs_path) as f:
                return json.load(f)
        except Exception:
            return {}

    # ── App lifecycle ─────────────────────────────────────────────────────────

    def build(self):
        try:
            Builder.load_string(KV)
            self.root = RootScreen()
            return self.root
        except Exception:
            traceback.print_exc()
            raise

    def on_start(self):
        try:
            sm = self.root.ids.sm
            self.config_screen = sm.get_screen('config')
            # Bind Android activity result listener
            if platform == 'android':
                try:
                    from android import activity
                    activity.bind(on_activity_result=self._on_activity_result_wrapper)
                    print('[app] activity result listener bound')
                except Exception as ex:
                    print(f'[app] failed to bind activity result: {ex}')
                    traceback.print_exc()
            # Auto-load last used LIFT file if it still exists
            prefs = self._load_prefs()
            last = prefs.get('last_lift', '')
            if last and os.path.isfile(last) and os.path.getsize(last) > 50:
                Clock.schedule_once(lambda dt: self.load_lift(last), 0.3)
        except Exception:
            traceback.print_exc()
            raise

    def open_file(self):
        if platform == 'android':
            self._open_file_android()
        elif platform == 'ios':
            self._open_file_ios()
        else:
            self._open_file_desktop()

    # ── Open from URL ──────────────────────────────────────────────────────────

    def open_url_dialog(self):
        """Show a dialog where the user can paste a URL to a .lift file."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.textinput import TextInput
        from kivy.uix.button import Button
        from kivy.uix.label import Label

        content = BoxLayout(orientation='vertical', spacing=dp(12), padding=dp(12))
        content.add_widget(Label(
            text='Paste the URL to a .lift file:',
            size_hint_y=None, height=dp(30),
            font_size=sp(14), color=theme.TEXT,
        ))
        url_input = TextInput(
            text='', hint_text='https://example.com/path/to/file.lift',
            multiline=False, size_hint_y=None, height=dp(44),
            font_size=sp(14),
        )
        content.add_widget(url_input)
        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
        cancel_btn = Button(text='Cancel', font_size=sp(14))
        open_btn = Button(text='Open', font_size=sp(14),
                          background_color=theme.ACCENT)
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(open_btn)
        content.add_widget(btn_row)

        popup = Popup(
            title='Open LIFT from URL',
            content=content,
            size_hint=(0.9, None), height=dp(220),
            auto_dismiss=True,
        )
        cancel_btn.bind(on_release=popup.dismiss)
        open_btn.bind(on_release=lambda *a: self._do_open_url(
            url_input.text.strip(), popup))
        popup.open()

    def _do_open_url(self, url, popup):
        """Download a .lift file from *url* to user_data_dir and open it."""
        popup.dismiss()
        if not url:
            return
        import threading
        threading.Thread(
            target=self._download_and_open, args=(url,), daemon=True).start()

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

    @staticmethod
    def _parse_git_url(url):
        """If url is a raw GitHub/GitLab file URL, return (clone_url, None).
        Returns (None, None) if not recognisable as a git-hosted file."""
        import re
        # GitHub raw: https://raw.githubusercontent.com/OWNER/REPO/BRANCH/path
        m = re.match(
            r'https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/', url)
        if m:
            return f'https://github.com/{m.group(1)}/{m.group(2)}.git', None
        # GitHub blob: https://github.com/OWNER/REPO/blob/BRANCH/path
        m = re.match(
            r'https://github\.com/([^/]+)/([^/]+)/blob/', url)
        if m:
            return f'https://github.com/{m.group(1)}/{m.group(2)}.git', None
        # GitLab raw: https://gitlab.com/OWNER/REPO/-/raw/BRANCH/path
        m = re.match(
            r'https://gitlab\.com/([^/]+)/([^/]+)/-/raw/', url)
        if m:
            return f'https://gitlab.com/{m.group(1)}/{m.group(2)}.git', None
        return None, None

    def _download_and_open(self, url):
        """Background: download a .lift file and schedule load on main thread.
        If the URL points to a file inside a git repo, clone the repo instead."""
        import urllib.request
        try:
            # When creating from template with a vernlang, use it as dir and filename
            vernlang = getattr(self, '_pending_vernlang', '')

            # Check if URL is from a git repo — clone instead of downloading
            clone_url, _ = self._parse_git_url(url)
            if clone_url and not vernlang:
                # Clone the whole repo
                repo_name = clone_url.rstrip('/').split('/')[-1].replace('.git', '')
                dest = os.path.join(self.user_data_dir, 'projects', repo_name)
                git_user, token = self._get_sync_credentials()
                from collab import clone_repo
                lift_path, log = clone_repo(clone_url, dest, git_user, token)
                if lift_path:
                    Clock.schedule_once(lambda dt: self.load_lift(lift_path), 0)
                else:
                    Clock.schedule_once(
                        lambda dt: self._show_error(log), 0)
                return

            if vernlang:
                project_dir = os.path.join(self.user_data_dir, 'projects', vernlang)
                os.makedirs(project_dir, exist_ok=True)
                dest = os.path.join(project_dir, f'{vernlang}.lift')
                # Pre-fill the language code for publish
                prefs = self._load_prefs()
                prefs['collab_langcode'] = vernlang
                self._save_prefs_dict(prefs)
            else:
                filename = url.rstrip('/').split('/')[-1]
                if not filename.endswith('.lift'):
                    filename = 'downloaded.lift'
                name = filename.replace('.lift', '')
                project_dir = os.path.join(self.user_data_dir, 'projects', name)
                os.makedirs(project_dir, exist_ok=True)
                dest = os.path.join(project_dir, filename)
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=self._ssl_context()) as resp:
                content = resp.read()
            if not content or len(content) < 50:
                raise RuntimeError(f'Download returned {len(content)} bytes')
            with open(dest, 'wb') as f:
                f.write(content)
            Clock.schedule_once(lambda dt: self.load_lift(dest), 0)
        except Exception as ex:
            msg = f'Could not download:\n{ex}'
            print(f'URL download error: {ex}')
            Clock.schedule_once(lambda dt: self._dismiss_loading_overlay(), 0)
            Clock.schedule_once(lambda dt: self._show_error(msg), 0)

    # ── New from SILCAWL template ──────────────────────────────────────────────

    _SILCAWL_URL = ('https://raw.githubusercontent.com/'
                    'kent-rasmussen/lift_templates/main/SILCAWL.lift')

    def new_from_template(self):
        """Show language picker first, then download the SILCAWL template."""
        if not getattr(self, '_pending_vernlang', ''):
            # First call: navigate to language picker
            sm = self.root.ids.sm
            sm.transition = SlideTransition(direction='left')
            sm.current = 'langpicker'
            return
        # Called from LangPickerScreen._on_continue with code set
        import threading
        threading.Thread(
            target=self._download_and_open,
            args=(self._SILCAWL_URL,), daemon=True).start()

    def _show_loading_overlay(self, msg):
        """Show a modal overlay that stays until dismissed."""
        from kivy.uix.label import Label
        from kivy.uix.modalview import ModalView
        from kivy.uix.boxlayout import BoxLayout
        self._dismiss_loading_overlay()
        view = ModalView(
            size_hint=(0.8, None), height=dp(100),
            background_color=theme.OVERLAY_DARK,
            auto_dismiss=False,
        )
        box = BoxLayout(orientation='vertical', padding=dp(8), spacing=dp(4))
        lbl = Label(
            text=msg, font_size=sp(16), font_name=_FONT_NAME,
            color=theme.TEXT, size_hint_y=0.6,
        )
        detail = Label(
            text='', font_size=sp(12), font_name=_FONT_NAME,
            color=theme.TEXT_DIM, size_hint_y=0.4,
        )
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
            title='Error',
            content=Label(text=msg, font_size=sp(14)),
            size_hint=(0.8, None), height=dp(180),
        )
        popup.open()

    def _open_file_android(self):
        """Use Android file picker intent."""
        try:
            from android.storage import primary_external_storage_path
            from jnius import autoclass, cast
            Intent = autoclass('android.content.Intent')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Uri = autoclass('android.net.Uri')

            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType('*/*')
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            PythonActivity.mActivity.startActivityForResult(intent, 1001)
            # Result handled in on_activity_result
        except Exception as ex:
            print(f'Android file picker error: {ex}')

    def _on_activity_result_wrapper(self, request_code, result_code, intent):
        """Wrapper for android.activity.bind — runs off main thread.
        Extract data from intent here (Java refs may not survive thread hop),
        then schedule UI work on main thread."""
        print(f'[activity-result] code={request_code} result={result_code}')
        if result_code != -1:  # not RESULT_OK
            print(f'[activity-result] not RESULT_OK, ignoring')
            return
        if request_code == 1001:
            try:
                uri = intent.getData()
                path = self._uri_to_path(uri)
                if path:
                    Clock.schedule_once(lambda dt: self.load_lift(path), 0)
            except Exception as ex:
                print(f'Activity result error: {ex}')
        elif request_code == 1002:
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

    def _uri_to_path(self, uri):
        try:
            from jnius import autoclass
            context = autoclass('org.kivy.android.PythonActivity').mActivity
            cursor = context.getContentResolver().query(uri, None, None, None, None)
            cursor.moveToFirst()
            idx = cursor.getColumnIndex('_data')
            if idx >= 0:
                path = cursor.getString(idx)
                cursor.close()
                return path
            cursor.close()
            # Fallback: copy to cache
            import shutil
            stream = context.getContentResolver().openInputStream(uri)
            cache = os.path.join(self.user_data_dir, 'tmp.lift')
            with open(cache, 'wb') as f:
                buf = stream.read()
                f.write(buf)
            return cache
        except Exception as ex:
            print(f'URI to path error: {ex}')
            return None

    def _open_file_ios(self):
        """Use UIDocumentPickerViewController on iOS."""
        try:
            from pyobjus import autoclass
            from pyobjus.dylib_manager import load_framework
            # Document picker — handled via delegate callback
            pass  # Simplified; in production wire up UIDocumentPickerDelegate
        except Exception as ex:
            print(f'iOS file picker error: {ex}')

    def _open_file_desktop(self):
        """Use tkinter file dialog on desktop."""
        try:
            import tkinter as tk
            from tkinter import filedialog
            root_tk = tk.Tk()
            root_tk.withdraw()
            path = filedialog.askopenfilename(
                title='Open LIFT file',
                filetypes=[('LIFT files', '*.lift'), ('All files', '*.*')],
            )
            root_tk.destroy()
            if path:
                self.load_lift(path)
        except Exception as ex:
            print(f'Desktop file dialog error: {ex}')

    def _image_repo(self):
        """Return the configured image repo, or default."""
        return self._load_prefs().get('image_repo', '')

    def _get_image_cache_dir(self):
        """Return the image cache directory (not in git, for offline use)."""
        return os.path.join(self.user_data_dir, 'image_cache')

    def _start_image_prefetch(self):
        """Silently pre-fetch all CAWL images to cache dir in background."""
        if not self.recorder:
            return
        import threading
        db = self.recorder.db
        cache_dir = self._get_image_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        threading.Thread(
            target=self._prefetch_images_worker,
            args=(db, cache_dir), daemon=True).start()

    def _prefetch_images_worker(self, db, cache_dir):
        """Download all CAWL images to cache_dir (skips existing)."""
        try:
            url_map = db.all_cawl_urls()
        except Exception:
            return  # no internet or resolver failed
        if not url_map:
            return
        import urllib.request
        ctx = self._ssl_context()
        count = 0
        for cawl, url in url_map.items():
            # Skip if already cached
            already = False
            for ext in ('.png', '.jpg', '.jpeg'):
                if os.path.exists(os.path.join(cache_dir, cawl + ext)):
                    already = True
                    break
            if already:
                continue
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                    data = resp.read()
                # Determine extension from URL
                low = url.lower()
                if low.endswith('.jpg') or low.endswith('.jpeg'):
                    ext = '.jpg'
                else:
                    ext = '.png'
                dest = os.path.join(cache_dir, cawl + ext)
                with open(dest, 'wb') as f:
                    f.write(data)
                count += 1
            except Exception:
                pass  # silently skip — spotty internet is expected
        if count:
            print(f'[image-prefetch] cached {count} new images')

    def load_lift(self, path):
        self._dismiss_loading_overlay()
        path = os.path.abspath(path)
        try:
            db = LIFTDatabase(path, image_repo=self._image_repo(),
                              image_cache_dir=self._get_image_cache_dir())
        except Exception as ex:
            self._show_error(f'Could not open file:\n{ex}')
            return
        self._save_prefs(path)
        # Apply language code: from picker, from prefs, or from filename
        pending = getattr(self, '_pending_vernlang', '')
        if pending:
            db.set_vernlang(pending)
            db.clean_template()
            self._pending_vernlang = ''
            prefs = self._load_prefs()
            prefs['vernlang'] = pending
            self._save_prefs_dict(prefs)
        else:
            prefs = self._load_prefs()
            saved_vern = prefs.get('vernlang', '')
            if saved_vern:
                db.set_vernlang(saved_vern)
        self.recorder = RecorderController(db)
        # Apply persisted show-past-work preference (default: hide past work)
        show_past = self._load_prefs().get('show_past_work', False)
        self.recorder.only_unrecorded = not show_past
        self.recorder.rebuild_queue()
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'recorder'
        Clock.schedule_once(lambda dt: self.refresh_recorder_ui(), 0.1)
        # Silently pre-fetch all CAWL images for offline use
        Clock.schedule_once(lambda dt: self._start_image_prefetch(), 1.0)
        # Auto-publish new project if credentials are already configured
        if pending:
            self._try_auto_publish()

    def _reload_and_restore(self, guid):
        """Reload the LIFT file and restore position to the entry with *guid*."""
        if not self.recorder:
            return
        path = self.recorder.db.path
        try:
            db = LIFTDatabase(path, image_repo=self._image_repo(),
                              image_cache_dir=self._get_image_cache_dir())
        except Exception as ex:
            print(f'Reload failed: {ex}')
            return
        # Re-apply saved vernlang
        prefs = self._load_prefs()
        saved_vern = prefs.get('vernlang', '')
        if saved_vern:
            db.set_vernlang(saved_vern)
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
        # Restore position by guid
        if guid:
            for i, e in enumerate(self.recorder.queue):
                if e.get('guid') == guid:
                    self.recorder.index = i
                    break
        self.refresh_recorder_ui()

    def _get_gh_credentials(self):
        """Return (username, access_token) with auto-refresh. Uses device flow tokens."""
        from collab import get_valid_token
        return get_valid_token(self._prefs_path)

    def _try_auto_publish(self):
        """If git credentials and langcode are configured, publish automatically."""
        prefs = self._load_prefs()
        langcode = prefs.get('collab_langcode', '')
        if not (langcode and self.recorder):
            return
        host = prefs.get('collab_host', 'github')
        git_user, token = self._get_sync_credentials()
        if not token:
            return
        if host == 'gitlab':
            user = prefs.get('gl_username', '')
            domain = 'gitlab.com'
        else:
            user = prefs.get('gh_username', '')
            domain = 'github.com'
        if not user:
            return
        remote_url = f'https://{domain}/{user}/{langcode}.git'
        name = prefs.get('collab_name', '') or 'Recorder'
        import threading
        def _worker():
            try:
                from collab import init_repo
                result = init_repo(self.recorder.db.dir, remote_url,
                                   git_user, token, 'main', name)
                print(f'[auto-publish] {result}')
            except Exception as ex:
                print(f'[auto-publish] error: {ex}')
        threading.Thread(target=_worker, daemon=True).start()

    def _record_sync_time(self):
        """Save current time as last successful sync."""
        import time
        prefs = self._load_prefs()
        prefs['last_sync'] = time.time()
        self._save_prefs_dict(prefs)
        Clock.schedule_once(lambda dt: self._update_sync_status(), 0)

    def _sync_status_text(self):
        """Return human-readable sync age: '' if today, 'yesterday', '-N days'."""
        import time, datetime
        prefs = self._load_prefs()
        ts = prefs.get('last_sync', 0)
        if not ts:
            return ''
        dt_sync = datetime.datetime.fromtimestamp(ts)
        now = datetime.datetime.now()
        today = now.date()
        sync_date = dt_sync.date()
        time_str = dt_sync.strftime('%H:%M')
        days = (today - sync_date).days
        if days == 0:
            return time_str
        elif days == 1:
            return f'yesterday {time_str}'
        else:
            return f'-{days}d {time_str}'

    def _update_sync_status(self):
        """Push sync status text into the recorder top bar."""
        sm = self.root.ids.sm
        rec_screen = sm.get_screen('recorder')
        lbl = rec_screen.ids.get('sync_status_label')
        if lbl:
            lbl.text = self._sync_status_text()

    def _get_sync_credentials(self):
        """Return (git_username, token) for sync, respecting host toggle."""
        prefs = self._load_prefs()
        host = prefs.get('collab_host', 'github')
        if host == 'gitlab':
            user = prefs.get('gl_username', '')
            token = prefs.get('gl_token', '')
            return user, token
        from collab import get_valid_token
        _, token = get_valid_token(self._prefs_path)
        return 'x-access-token', token

    def _auto_commit_sync(self):
        """Background: commit new audio and .lift changes, sync if online."""
        if not self.recorder:
            return
        prefs = self._load_prefs()
        name = prefs.get('collab_name', '') or 'Recorder'
        project_dir = self.recorder.db.dir
        git_user, token = self._get_sync_credentials()
        import threading
        def _worker():
            try:
                from collab import commit_audio_and_sync
                result = commit_audio_and_sync(
                    project_dir, name, git_user, token)
                print(f'[auto-sync] {result}')
                if 'pushed' in result.lower() or 'Pushed' in result:
                    Clock.schedule_once(lambda dt: self._record_sync_time(), 0)
            except Exception as ex:
                print(f'[auto-sync] error: {ex}')
        threading.Thread(target=_worker, daemon=True).start()

    def show_start_over(self):
        """Show template/image repo info, then navigate to welcome screen."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.button import Button

        content = BoxLayout(orientation='vertical', spacing=dp(6), padding=dp(6))
        template = ''
        image_repo = ''
        if self.recorder:
            template = self.recorder.db.list_type or '(unknown)'
            image_repo = self.recorder.db.image_repo or 'https://github.com/kent-rasmussen/images_CAWL'
        content.add_widget(Label(
            text=f'Template: {template}\nImage repo: {image_repo}',
            font_size=sp(14), font_name=_FONT_NAME,
            color=theme.TEXT,
            size_hint_y=None, height=dp(60),
            halign='left', valign='top',
            text_size=(dp(280), None),
        ))
        content.add_widget(Label(
            text='Start a new wordlist with this template?',
            font_size=sp(14), font_name=_FONT_NAME,
            color=theme.TEXT_DIM,
            size_hint_y=None, height=dp(30),
        ))
        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
        cancel_btn = Button(text='Cancel', font_size=sp(14))
        go_btn = Button(text='Yes', font_size=sp(14),
                        background_color=theme.ACCENT)
        btn_row.add_widget(go_btn)
        btn_row.add_widget(cancel_btn)
        content.add_widget(btn_row)

        popup = Popup(
            title='Start a new wordlist',
            content=content,
            size_hint=(0.9, None), height=dp(240),
            auto_dismiss=True,
        )
        cancel_btn.bind(on_release=popup.dismiss)
        def _go(*a):
            popup.dismiss()
            self.new_from_template() #was go_welcome() #< should be langpicker
        go_btn.bind(on_release=_go)
        popup.open()

    def go_welcome(self):
        self._auto_commit_sync()
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='right')
        sm.current = 'welcome'

    def go_config(self):
        self._auto_commit_sync()
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'config'

    def go_collab(self):
        self._auto_commit_sync()
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'collab'

    def share_apk(self):
        """Share the running APK via Android's share sheet using MediaStore content:// URI."""
        if platform == 'android':
            try:
                from jnius import autoclass, cast
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                Intent = autoclass('android.content.Intent')
                ContentValues = autoclass('android.content.ContentValues')
                MediaStoreDownloads = autoclass(
                    'android.provider.MediaStore$Downloads')
                activity = PythonActivity.mActivity
                context = cast('android.content.Context', activity)
                pm = context.getPackageManager()
                app_info = pm.getApplicationInfo(
                    context.getPackageName(), 0)
                apk_path = app_info.sourceDir
                # Insert into MediaStore Downloads to get a content:// URI
                values = ContentValues()
                values.put('_display_name', 'azt_recorder.apk')
                values.put('mime_type',
                           'application/vnd.android.package-archive')
                resolver = context.getContentResolver()
                uri = resolver.insert(
                    MediaStoreDownloads.EXTERNAL_CONTENT_URI, values)
                if not uri:
                    self._show_error('Share failed: could not create MediaStore entry')
                    return
                # Copy APK bytes into the MediaStore entry
                fos = resolver.openOutputStream(uri)
                with open(apk_path, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        fos.write(chunk)
                fos.close()
                intent = Intent(Intent.ACTION_SEND)
                intent.setType('application/vnd.android.package-archive')
                intent.putExtra(Intent.EXTRA_STREAM,
                                cast('android.os.Parcelable', uri))
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                chooser = Intent.createChooser(
                    intent,
                    autoclass('java.lang.String')('Share app'))
                activity.startActivity(chooser)
            except Exception as ex:
                print(f'Share APK error: {ex}')
                self._show_error(f'Could not share APK:\n{ex}')
        else:
            self._show_error('APK sharing is only available on Android.')

    def do_sync(self):
        if not self.recorder:
            return
        prefs = self._load_prefs()
        name = prefs.get('collab_name', '') or 'Recorder'
        git_user, token = self._get_sync_credentials()
        from collab import sync_repo, run_async

        saved_guid = self.recorder.current.get('guid', '') if self.recorder.queue else ''
        def _sync_and_reload(project_dir, username, pw, contributor):
            result = sync_repo(project_dir, username, pw, contributor)
            Clock.schedule_once(
                lambda dt: self._reload_and_restore(saved_guid), 0)
            return result

        def _on_sync_done(result):
            print(f'Sync: {result}')
            if 'Pushed' in (result or ''):
                self._record_sync_time()
        run_async(_sync_and_reload,
                  self.recorder.db.dir, git_user, token, name,
                  on_done=_on_sync_done)

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
        if self.recorder:
            self._save_remote_image()
            self.recorder.go_prev()
            self._auto_commit_sync()

    def nav_next(self):
        if self.recorder:
            self._save_remote_image()
            self.recorder.go_next()
            self._auto_commit_sync()

    def _save_remote_image(self):
        """If the current entry's image is from cache or remote URL, copy/save
        it to the git images/ dir so it gets committed on swipe."""
        if not self.recorder:
            return
        entry = self.recorder.current
        if not entry:
            return
        img_path = entry.get('image_path', '')
        if not img_path:
            return  # no image
        # Already in git images/ dir
        db = self.recorder.db
        if img_path.startswith(db.images_dir):
            return
        filename = db.imagename(entry)
        dest = os.path.join(db.images_dir, filename)
        if os.path.exists(dest):
            return  # already saved locally
        # If image is from cache, just copy the file
        cache_dir = self._get_image_cache_dir()
        if img_path.startswith(cache_dir) and os.path.exists(img_path):
            import threading, shutil
            threading.Thread(
                target=self._copy_cached_to_images,
                args=(img_path, dest, entry), daemon=True).start()
            return
        # Remote URL — try texture first, then download
        if not img_path.startswith('http'):
            return
        import threading
        sm = self.root.ids.sm
        rec_screen = sm.get_screen('recorder')
        img_widget = rec_screen.ids.get('entry_image')
        if img_widget and img_widget.texture:
            tex = img_widget.texture
            pixels = tex.pixels
            w, h = tex.size
            # Kivy textures from file/URL loaders may have uvpos[1]==0
            # (already top-down) or uvpos[1]!=0 (OpenGL bottom-up)
            needs_flip = (tex.uvpos[1] == 0)
            threading.Thread(
                target=self._save_texture_to_file,
                args=(pixels, w, h, dest, entry, needs_flip),
                daemon=True).start()
        else:
            threading.Thread(
                target=self._download_remote_image,
                args=(img_path, dest, entry), daemon=True).start()

    def _copy_cached_to_images(self, src, dest, entry):
        """Copy a cached image to images/ dir and update LIFT XML."""
        try:
            import shutil
            from PIL import Image as PILImage
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # Scale if needed
            img = PILImage.open(src)
            w, h = img.size
            max_dim = 1284
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
                img.save(dest, 'PNG')
            else:
                shutil.copy2(src, dest)
            entry['image_path'] = dest
            entry['illustration_href'] = os.path.basename(dest)
            guid = entry.get('guid', '')
            self.recorder.db.set_illustration(guid, os.path.basename(dest))
            print(f'[image-save] copied from cache to {dest}')
        except Exception as ex:
            print(f'[image-save] cache copy error: {ex}')

    def _save_texture_to_file(self, pixels, w, h, dest, entry, needs_flip=True):
        """Save raw RGBA pixel data to a PNG file."""
        try:
            from PIL import Image as PILImage
            img = PILImage.frombytes('RGBA', (w, h), pixels)
            if needs_flip:
                img = img.transpose(PILImage.FLIP_TOP_BOTTOM)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            max_dim = 1284
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
            img.save(dest, 'PNG')
            entry['image_path'] = dest
            entry['illustration_href'] = os.path.basename(dest)
            guid = entry.get('guid', '')
            self.recorder.db.set_illustration(guid, os.path.basename(dest))
            print(f'[image-save] saved from texture to {dest}')
        except Exception as ex:
            print(f'[image-save] texture save error: {ex}')

    def _download_remote_image(self, url, dest, entry):
        """Fallback: download a remote image to local images/ dir."""
        try:
            import urllib.request
            ctx = self._ssl_context()
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                data = resp.read()
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            from PIL import Image as PILImage
            import io
            img = PILImage.open(io.BytesIO(data))
            max_dim = 1284
            w, h = img.size
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                img = img.resize((int(w * ratio), int(h * ratio)),
                                 PILImage.LANCZOS)
            img.save(dest, 'PNG')
            entry['image_path'] = dest
            entry['illustration_href'] = os.path.basename(dest)
            guid = entry.get('guid', '')
            self.recorder.db.set_illustration(guid, os.path.basename(dest))
            print(f'[image-save] downloaded remote image to {dest}')
        except Exception as ex:
            print(f'[image-save] download error: {ex}')

    def play_audio(self):
        if self.recorder:
            self.recorder.play_audio()

    def redo_recording(self):
        """Clear existing audio and allow re-recording."""
        if self.recorder:
            self.recorder.clear_audio()

    def show_goto_dialog(self):
        """Popup to jump to a specific entry number. OK goes, Clear resets filter."""
        if not self.recorder:
            return
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.textinput import TextInput
        from kivy.uix.button import Button

        total = len(self.recorder.queue)
        content = BoxLayout(orientation='vertical', spacing=dp(12), padding=dp(12))
        num_input = TextInput(
            text=str(self.recorder.index + 1),
            hint_text=f'1-{total}',
            multiline=False, size_hint_y=None, height=dp(48),
            font_size=sp(18), input_filter='int',
        )
        content.add_widget(num_input)
        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
        clear_btn = Button(text='Clear', font_size=sp(14))
        ok_btn = Button(text='OK', font_size=sp(14),
                        background_color=theme.ACCENT)
        btn_row.add_widget(clear_btn)
        btn_row.add_widget(ok_btn)
        content.add_widget(btn_row)

        popup = Popup(
            title=f'Go to entry (1-{total})',
            content=content,
            size_hint=(0.8, None), height=dp(180),
            auto_dismiss=True,
        )

        def _go(*args):
            text = num_input.text.strip()
            if text:
                try:
                    n = int(text)
                    n = max(1, min(n, total))
                    self.recorder.index = n - 1
                    self.recorder._pending_rerecord = False
                    self.recorder._notify_ui()
                except ValueError:
                    pass
            popup.dismiss()

        def _clear(*args):
            self.recorder.cawl_filter = ''
            self.recorder.gloss_search = ''
            self.recorder.only_unrecorded = False
            self.recorder.rebuild_queue()
            popup.dismiss()

        clear_btn.bind(on_release=_clear)
        ok_btn.bind(on_release=_go)
        num_input.bind(on_text_validate=_go)
        popup.open()

    def clone_dialog(self):
        """Popup with host/owner/repo component inputs for cloning."""
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.textinput import TextInput
        from kivy.uix.button import Button
        from kivy.uix.label import Label
        from kivy.uix.spinner import Spinner

        prefs = self._load_prefs()
        content = BoxLayout(orientation='vertical', spacing=dp(10), padding=dp(12))
        content.add_widget(Label(
            text='Clone a git repository containing a LIFT file:',
            size_hint_y=None, height=dp(30),
            font_size=sp(13), color=theme.TEXT,
        ))

        # Host + owner row
        host_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        host_spinner = Spinner(
            text=prefs.get('clone_host', 'GitHub'),
            values=['GitHub', 'GitLab'],
            size_hint_x=0.35, font_size=sp(14),
        )
        owner_input = TextInput(
            text=prefs.get('clone_owner', ''),
            hint_text='Owner (username)',
            multiline=False, size_hint_x=0.65, font_size=sp(14),
        )
        host_row.add_widget(host_spinner)
        host_row.add_widget(owner_input)
        content.add_widget(host_row)

        # Repo name
        repo_input = TextInput(
            text='', hint_text='Repository name',
            multiline=False, size_hint_y=None, height=dp(44), font_size=sp(14),
        )
        content.add_widget(repo_input)

        # Live URL preview
        url_label = Label(
            text='', font_size=sp(12),
            color=theme.TEXT_DIM,
            size_hint_y=None, height=dp(24),
            halign='left',
        )
        url_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
        content.add_widget(url_label)

        def _update_url(*args):
            host = host_spinner.text
            owner = owner_input.text.strip()
            repo = repo_input.text.strip()
            if not owner or not repo:
                url_label.text = ''
                return
            domain = 'gitlab.com' if host == 'GitLab' else 'github.com'
            url_label.text = f'https://{domain}/{owner}/{repo}.git'

        host_spinner.bind(text=_update_url)
        owner_input.bind(text=_update_url)
        repo_input.bind(text=_update_url)

        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
        cancel_btn = Button(text='Cancel', font_size=sp(14))
        clone_btn = Button(text='Clone', font_size=sp(14),
                           background_color=theme.ACCENT)
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(clone_btn)
        content.add_widget(btn_row)

        popup = Popup(
            title='Clone Repository',
            content=content,
            size_hint=(0.9, None), height=dp(340),
            auto_dismiss=True,
        )

        def _do_clone(*args):
            clone_url = url_label.text.strip()
            popup.dismiss()
            if not clone_url:
                return
            git_user, token = self._get_sync_credentials()
            # Save clone prefs for future use
            prefs = self._load_prefs()
            prefs['clone_host'] = host_spinner.text
            prefs['clone_owner'] = owner_input.text.strip()
            self._save_prefs_dict(prefs)

            repo_name = clone_url.rstrip('/').split('/')[-1].replace('.git', '')
            dest = os.path.join(self.user_data_dir, 'projects', repo_name)

            self._show_loading_overlay('Cloning repository...')
            import threading
            from collab import clone_repo

            def _on_progress(line):
                Clock.schedule_once(
                    lambda dt, t=line: self._update_loading_detail(t), 0)

            def _worker():
                try:
                    lift_path, log = clone_repo(
                        clone_url, dest, git_user, token,
                        on_progress=_on_progress)
                    print(f'[clone] result: {log}')
                    if lift_path:
                        Clock.schedule_once(lambda dt: self.load_lift(lift_path), 0)
                    else:
                        Clock.schedule_once(
                            lambda dt: (self._dismiss_loading_overlay(),
                                        self._show_error(log)), 0)
                except Exception as ex:
                    print(f'[clone] error: {ex}')
                    Clock.schedule_once(
                        lambda dt: (self._dismiss_loading_overlay(),
                                    self._show_error(str(ex))), 0)

            threading.Thread(target=_worker, daemon=True).start()

        cancel_btn.bind(on_release=popup.dismiss)
        clone_btn.bind(on_release=_do_clone)
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
