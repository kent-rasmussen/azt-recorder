"""
LIFT Recorder — field audio recorder for LIFT XML lexicon databases.

Records high-quality audio for dictionary entries, stores WAV files in an
'audio/' directory next to the LIFT file, and writes filenames back into
the LIFT XML via Entry.lc (citation form) with the audiolang tag.

Runs on Android (primary) and iOS/desktop (secondary) via Kivy.
"""

import os
import sys
import traceback
import warnings

os.environ.setdefault('KIVY_NO_ENV_CONFIG', '1')
warnings.filterwarnings('ignore', message='.*olefile.*')

# ── Crash logging — runs before any Kivy import ────────────────────────────────
# On Android: p4a sets $ANDROID_PRIVATE to the app's private files dir (always writable).
#             Also tries /sdcard/ (may need MANAGE_EXTERNAL_STORAGE on API 30+).
# On desktop: ~/liftrecorder.log
def _setup_logging():
    _on_android = os.path.exists('/system/build.prop')
    candidates = []
    if _on_android:
        # ANDROID_PRIVATE is set by p4a bootstrap, e.g. /data/user/0/org.x.y/files
        android_private = os.environ.get('ANDROID_PRIVATE', '')
        if android_private:
            candidates.append(os.path.join(android_private, 'liftrecorder.log'))
        # Fallback using known package name pattern
        candidates += [
            '/data/user/0/org.liftrecorder.liftrecorder/files/liftrecorder.log',
            '/sdcard/liftrecorder.log',
        ]
    else:
        candidates += [os.path.join(os.path.expanduser('~'), 'liftrecorder.log')]
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

    print(f'[LOG] liftrecorder starting — log: {path}', flush=True)

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
        os.path.join('/data/user/0', 'org.liftrecorder', 'files/app/fonts', filename),
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

# ── Colour palette (dark, warm) ────────────────────────────────────────────────
#   bg      #1a1612
#   surface #2a2320
#   accent  #c97b3a
#   text    #f0e8dc
#   dim     #8a7a6a
#   red     #c0392b
#   green   #27ae60

<WelcomeScreen>:
    canvas.before:
        Color:
            rgba: (0.102, 0.0863, 0.0706, 1)
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
            source: 'icons/icon.png'
            size_hint: None, None
            size: dp(120), dp(120)
            pos_hint: {{'center_x': 0.5}}
            allow_stretch: True
            keep_ratio: True
        Label:
            text: 'Record My Wordlist'
            font_size: sp(28)
            font_name: FONT
            bold: True
            color: (0.7882, 0.4824, 0.2275, 1)
            size_hint_y: None
            height: dp(50)
        Label:
            text: 'Open or create a LIFT lexicon'
            font_size: sp(16)
            font_name: FONT
            color: (0.5412, 0.4784, 0.4157, 1)
            size_hint_y: None
            height: dp(30)
        Widget:
            size_hint_y: 0.08
        RecBtn:
            text: 'From Phone'
            normal_color: (0.7882, 0.4824, 0.2275, 1)
            on_release: app.open_file()
        RecBtn:
            text: 'From Internet'
            normal_color: (0.3922, 0.3137, 0.2353, 1)
            on_release: app.open_url_dialog()
        RecBtn:
            text: 'Clone Repository'
            normal_color: (0.3922, 0.3137, 0.2353, 1)
            on_release: app.clone_dialog()
        RecBtn:
            text: 'Start New'
            normal_color: (0.3922, 0.3137, 0.2353, 1)
            on_release: app.new_from_template()
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
            color: (0.2902, 0.2275, 0.1647, 1)
            size_hint_y: None
            height: dp(20)
            halign: 'center'
            text_size: self.size

<RecorderScreen>:
    canvas.before:
        Color:
            rgba: (0.102, 0.0863, 0.0706, 1)
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
                    rgba: (0.1647, 0.1373, 0.1255, 1)
                Rectangle:
                    pos: self.pos
                    size: self.size
            Button:
                id: progress_label
                text: ''
                font_size: sp(13)
                font_name: FONT
                color: (0.5412, 0.4784, 0.4157, 1)
                halign: 'left'
                valign: 'middle'
                text_size: self.size
                background_color: 0, 0, 0, 0
                background_normal: ''
                on_release: app.show_goto_dialog()
                size_hint_x: 1
            Button:
                size_hint_x: None
                width: dp(44)
                background_color: 0, 0, 0, 0
                background_normal: ''
                on_release: app.do_sync()
                Image:
                    source: 'icons/sync_dark.png'
                    size: dp(28), dp(28)
                    size_hint: None, None
                    center: self.parent.center
                    allow_stretch: True
                    keep_ratio: True
            BoxLayout:
                size_hint_x: 1
            Button:
                size_hint_x: None
                width: dp(44)
                background_color: 0, 0, 0, 0
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
        AsyncImage:
            id: entry_image
            source: ''
            size_hint_y: None
            height: 0
            opacity: 0
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
            color: (0.5412, 0.4784, 0.4157, 1)
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
            rgba: (0.102, 0.0863, 0.0706, 1)
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
                    rgba: (0.1647, 0.1373, 0.1255, 1)
                Rectangle:
                    pos: self.pos
                    size: self.size
            Label:
                text: app.version_string
                font_size: sp(11)
                font_name: FONT
                color: (0.5412, 0.4784, 0.4157, 1)
                halign: 'left'
                valign: 'middle'
                text_size: self.size
                padding_x: dp(8)
                size_hint_x: 1
            Button:
                size_hint_x: None
                width: dp(44)
                background_color: 0, 0, 0, 0
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
                on_release: app.go_recorder()
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
                        color: (0.5412, 0.4784, 0.4157, 1)
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
                        background_color: (0.1647, 0.1373, 0.1255, 1)
                        foreground_color: (0.9412, 0.9098, 0.8627, 1)
                        cursor_color: (0.7882, 0.4824, 0.2275, 1)
                        multiline: False
                        on_text_validate: root.apply_cawl(self.text)
                    Label:
                        text: 'Gloss search (filter by gloss text)'
                        font_size: sp(13)
                        font_name: FONT
                        color: (0.5412, 0.4784, 0.4157, 1)
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
                        background_color: (0.1647, 0.1373, 0.1255, 1)
                        foreground_color: (0.9412, 0.9098, 0.8627, 1)
                        cursor_color: (0.7882, 0.4824, 0.2275, 1)
                        multiline: False
                # Show past work — toggle (logically reversed from only_unrecorded)
                UnrecordedToggle:
                    id: unrecorded_toggle
                    active: True
                    on_active: root.toggle_show_past(self.active)
                # Apply button
                Widget:
                    size_hint_y: None
                    height: dp(16)
                RecBtn:
                    text: 'Use these Settings'
                    normal_color: (0.7882, 0.4824, 0.2275, 1)
                    on_release: root.apply_and_go()
                Widget:
                    size_hint_y: None
                    height: dp(8)
                RecBtn:
                    text: 'Setup Collaboration'
                    normal_color: (0.1647, 0.1373, 0.1255, 1)
                    on_release: app.go_collab()
                Widget:
                    size_hint_y: None
                    height: dp(8)
                RecBtn:
                    text: 'Start over'
                    normal_color: (0.3922, 0.3137, 0.2353, 1)
                    on_release: app.go_welcome()
                Widget:
                    size_hint_y: None
                    height: dp(40)

<CollabScreen>:
    canvas.before:
        Color:
            rgba: (0.102, 0.0863, 0.0706, 1)
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
                    rgba: (0.1647, 0.1373, 0.1255, 1)
                Rectangle:
                    pos: self.pos
                    size: self.size
            Label:
                text: 'Setup'
                font_size: sp(17)
                font_name: FONT
                bold: True
                color: (0.7882, 0.4824, 0.2275, 1)
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
                # ── Credentials ───────────────────────────────────────────
                SectionLabel:
                    text: 'Your identity'
                TextInput:
                    id: name_input
                    hint_text: 'Your name'
                    font_size: sp(15)
                    font_name: FONT
                    size_hint_y: None
                    height: dp(48)
                    background_color: (0.1647, 0.1373, 0.1255, 1)
                    foreground_color: (0.9412, 0.9098, 0.8627, 1)
                    cursor_color: (0.7882, 0.4824, 0.2275, 1)
                    multiline: False
                TextInput:
                    id: username_input
                    hint_text: 'Git username'
                    font_size: sp(15)
                    font_name: FONT
                    size_hint_y: None
                    height: dp(48)
                    background_color: (0.1647, 0.1373, 0.1255, 1)
                    foreground_color: (0.9412, 0.9098, 0.8627, 1)
                    cursor_color: (0.7882, 0.4824, 0.2275, 1)
                    multiline: False
                TextInput:
                    id: token_input
                    hint_text: 'Personal access token'
                    password: True
                    font_size: sp(15)
                    font_name: FONT
                    size_hint_y: None
                    height: dp(48)
                    background_color: (0.1647, 0.1373, 0.1255, 1)
                    foreground_color: (0.9412, 0.9098, 0.8627, 1)
                    cursor_color: (0.7882, 0.4824, 0.2275, 1)
                    multiline: False
                # ── Publish ───────────────────────────────────────────────
                SectionLabel:
                    text: 'Publish this project'
                BoxLayout:
                    size_hint_y: None
                    height: dp(48)
                    spacing: dp(8)
                    Spinner:
                        id: host_spinner
                        text: 'GitHub'
                        values: ['GitHub', 'GitLab']
                        size_hint_x: 0.35
                        font_size: sp(14)
                        font_name: FONT
                        on_text: root.update_publish_url()
                    TextInput:
                        id: langcode_input
                        hint_text: 'Language code'
                        font_size: sp(14)
                        font_name: FONT
                        size_hint_x: 0.65
                        background_color: (0.1647, 0.1373, 0.1255, 1)
                        foreground_color: (0.9412, 0.9098, 0.8627, 1)
                        cursor_color: (0.7882, 0.4824, 0.2275, 1)
                        multiline: False
                        on_text: root.update_publish_url()
                Label:
                    id: publish_url_label
                    text: ''
                    font_size: sp(12)
                    font_name: FONT
                    color: (0.5412, 0.4784, 0.4157, 1)
                    size_hint_y: None
                    height: dp(28)
                    halign: 'left'
                    text_size: self.width, None
                RecBtn:
                    text: 'Publish'
                    normal_color: (0.7882, 0.4824, 0.2275, 1)
                    on_release: root.do_publish()
                # ── Log ───────────────────────────────────────────────────
                SectionLabel:
                    text: 'Last operation'
                Label:
                    id: log_label
                    text: ''
                    font_size: sp(12)
                    font_name: FONT
                    color: (0.5412, 0.4784, 0.4157, 1)
                    size_hint_y: None
                    height: self.texture_size[1] + dp(16)
                    halign: 'left'
                    valign: 'top'
                    text_size: self.width, None
                Widget:
                    size_hint_y: None
                    height: dp(40)

# ── Reusable widgets ──────────────────────────────────────────────────────────

<RecordButton>:
    size_hint: 1, 1
    canvas:
        Color:
            rgba: (0.7529, 0.2235, 0.1686, 1) if self.recording else (0.7882, 0.4824, 0.2275, 1)
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
            rgba: (0.1529, 0.6824, 0.3765, 1)
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
            rgba: (0.1647, 0.1373, 0.1255, 1)
        RoundedRectangle:
            pos: self.x + dp(4), self.y + dp(4)
            size: self.width - dp(8), self.height - dp(8)
            radius: [dp(12)]
        Color:
            rgba: (0.7882, 0.4824, 0.2275, 0.6)
        Line:
            rounded_rectangle: self.x + dp(4), self.y + dp(4), self.width - dp(8), self.height - dp(8), dp(12)
            width: dp(1.5)
    Label:
        text: 'X'
        font_size: sp(36)
        font_name: FONT
        bold: True
        color: (0.7882, 0.4824, 0.2275, 1)
        center: root.center

<LangPickerScreen>:
    canvas.before:
        Color:
            rgba: (0.102, 0.0863, 0.0706, 1)
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
            color: (0.7882, 0.4824, 0.2275, 1)
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
            background_color: (0.1647, 0.1373, 0.1255, 1)
            foreground_color: (0.9412, 0.9098, 0.8627, 1)
            hint_text_color: (0.5412, 0.4784, 0.4157, 0.6)
            cursor_color: (0.7882, 0.4824, 0.2275, 1)
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
                color: (0.9412, 0.9098, 0.8627, 1)
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
                color: (0.5412, 0.4784, 0.4157, 1)
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
                    color: (0.9412, 0.9098, 0.8627, 1)
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
                background_color: (0.1647, 0.1373, 0.1255, 1)
                foreground_color: (0.9412, 0.9098, 0.8627, 1)
                hint_text_color: (0.5412, 0.4784, 0.4157, 0.6)
                cursor_color: (0.7882, 0.4824, 0.2275, 1)
                padding: [dp(10), dp(10)]
                on_text: root._update_code()
            # Assembled code display
            Label:
                id: code_label
                text: ''
                font_size: sp(16)
                font_name: FONT
                bold: True
                color: (0.1529, 0.6824, 0.3765, 1)
                size_hint_y: None
                height: dp(28)
                halign: 'left'
                text_size: self.width, None
        # ── Continue button ───────────────────────────────────────────────
        RecBtn:
            id: continue_btn
            text: 'Continue'
            normal_color: (0.1529, 0.6824, 0.3765, 1)
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
            rgba: (0.1647, 0.1373, 0.1255, 1)
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
            color: (0.5412, 0.4784, 0.4157, 1)
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
            color: (0.9412, 0.9098, 0.8627, 1)
            halign: 'left'
            valign: 'middle'
            text_size: self.size

<UnrecordedToggle>:
    size_hint_y: None
    height: dp(56)
    canvas.before:
        Color:
            rgba: (0.15, 0.35, 0.22, 1) if self.active else (0.1647, 0.1373, 0.1255, 1)
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
            color: (0.7882, 0.4824, 0.2275, 1)
            on_active: root.active = self.active
        Label:
            text: 'Show past work'
            font_size: sp(16)
            font_name: FONT
            bold: True
            color: (0.1529, 0.9, 0.3765, 1) if root.active else (0.9412, 0.9098, 0.8627, 1)
            halign: 'left'
            valign: 'middle'
            text_size: self.size

<RecBtn@Button>:
    normal_color: (0.7882, 0.4824, 0.2275, 1)
    size_hint_y: None
    height: dp(52)
    background_color: 0,0,0,0
    background_normal: ''
    canvas.before:
        Color:
            rgba: self.normal_color
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
    background_color: 0,0,0,0
    background_normal: ''
    canvas.before:
        Color:
            rgba: (0.1647, 0.1373, 0.1255, 1)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    color: (0.7882, 0.4824, 0.2275, 1)
    font_size: sp(16)
    font_name: FONT
    bold: True

<IconBtn@Button>:
    background_color: 0,0,0,0
    background_normal: ''
    color: (0.5412, 0.4784, 0.4157, 1)
    font_size: sp(20)
    font_name: FONT

<SectionLabel@Label>:
    size_hint_y: None
    height: dp(32)
    font_size: sp(12)
    font_name: FONT
    bold: True
    color: (0.7882, 0.4824, 0.2275, 1)
    halign: 'left'
    valign: 'middle'
    text_size: self.size
    text: ''

<CheckboxStyled@CheckBox>:
    size_hint_x: None
    width: dp(40)
    color: (0.7882, 0.4824, 0.2275, 1)

<LangToggle>:
    size_hint_y: None
    height: dp(44)
    canvas.before:
        Color:
            rgba: (0.15, 0.35, 0.22, 1) if root.active else (0.1647, 0.1373, 0.1255, 1)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(6)]
    Label:
        text: root.lang
        font_size: sp(15)
        font_name: FONT
        bold: root.active
        color: (0.1529, 0.9, 0.3765, 1) if root.active else (0.9412, 0.9098, 0.8627, 1)
        halign: 'center'
        valign: 'middle'
        text_size: self.size
'''

KV = KV_TEMPLATE.format(font_name=_FONT_NAME)


# ── Widget classes ─────────────────────────────────────────────────────────────

from kivy.uix.widget import Widget
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label


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
    lang = StringProperty('')
    active = BooleanProperty(True)
    callback = ObjectProperty(None)

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
                '    normal_color: (0.1529, 0.6824, 0.3765, 1)\n'
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
            background_color=(0.1647, 0.1373, 0.1255, 1),
            background_normal='',
            color=(0.9412, 0.9098, 0.8627, 1),
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
                background_color=(0.2353, 0.1961, 0.1647, 1),
                background_normal='',
                color=(0.9412, 0.9098, 0.8627, 1),
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
                    background_color=(0.2353, 0.1961, 0.1647, 1),
                    background_normal='',
                    color=(0.9412, 0.9098, 0.8627, 1),
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
                    child.background_color = (0.7882, 0.4824, 0.2275, 1)
                elif not region_code and 'Multiple' in child.text:
                    child.background_color = (0.7882, 0.4824, 0.2275, 1)
                else:
                    child.background_color = (0.2353, 0.1961, 0.1647, 1)
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
        app._pending_vernlang = self._assembled_code()
        app.new_from_template()


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

    def apply_and_go(self):
        app = App.get_running_app()
        cawl_in = self.ids.get('cawl_input')
        if cawl_in:
            app.recorder.cawl_filter = cawl_in.text.strip()
        gs = self.ids.get('gloss_search_input')
        if gs:
            app.recorder.gloss_search = gs.text.strip()
        app.recorder.only_unrecorded = self.only_unrecorded
        app.recorder.rebuild_queue()
        app.go_recorder()


class CollabScreen(Screen):

    def on_enter(self):
        app = App.get_running_app()
        prefs = app._load_prefs()
        for field, key in [
            ('name_input',       'collab_name'),
            ('username_input',   'collab_username'),
            ('token_input',      'collab_token'),
            ('langcode_input',   'collab_langcode'),
        ]:
            w = self.ids.get(field)
            if w and not w.text:
                w.text = prefs.get(key, '')
        # Restore host spinner
        host = prefs.get('collab_host', 'GitHub')
        w = self.ids.get('host_spinner')
        if w:
            w.text = host
        self.update_publish_url()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _save_creds(self):
        app = App.get_running_app()
        prefs = app._load_prefs()
        for field, key in [
            ('name_input',       'collab_name'),
            ('username_input',   'collab_username'),
            ('token_input',      'collab_token'),
            ('langcode_input',   'collab_langcode'),
        ]:
            w = self.ids.get(field)
            if w:
                prefs[key] = w.text
        w = self.ids.get('host_spinner')
        if w:
            prefs['collab_host'] = w.text
        app._save_prefs_dict(prefs)

    def _creds(self):
        def _txt(field):
            w = self.ids.get(field)
            return w.text.strip() if w else ''
        return _txt('name_input') or 'Recorder', _txt('username_input'), _txt('token_input')

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

    # ── Button handlers ────────────────────────────────────────────────────

    def update_publish_url(self):
        """Auto-generate the publish URL from host, username, and language code."""
        lbl = self.ids.get('publish_url_label')
        if not lbl:
            return
        host_w = self.ids.get('host_spinner')
        user_w = self.ids.get('username_input')
        lang_w = self.ids.get('langcode_input')
        host = host_w.text if host_w else 'GitHub'
        user = user_w.text.strip() if user_w else ''
        lang = lang_w.text.strip() if lang_w else ''
        if not user or not lang:
            lbl.text = ''
            return
        if host == 'GitLab':
            lbl.text = f'https://gitlab.com/{user}/{lang}.git'
        else:
            lbl.text = f'https://github.com/{user}/{lang}.git'

    def do_publish(self):
        app = App.get_running_app()
        if not app.recorder:
            self._set_log('No project loaded.')
            return
        self._save_creds()
        name, user, token = self._creds()
        lbl = self.ids.get('publish_url_label')
        remote_url = lbl.text.strip() if lbl else ''
        if not remote_url:
            self._set_log('Enter username and language code first.')
            return
        from collab import init_repo
        self._run('Publishing...', init_repo,
                  app.recorder.db.dir, remote_url, user, token,
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
    def progress_text(self):
        if not self.queue:
            return 'No entries'
        cawl = self.current.get('cawl', '')
        cawl_str = f'  ·  CAWL {cawl}' if cawl else ''
        return f'{self.index + 1} / {len(self.queue)}{cawl_str}'

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

__version__ = '1.6.0'


class LIFTRecorderApp(App):
    title = 'Record My Wordlist'
    icon = 'icons/icon.png'
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
            font_size=sp(14), color=(0.94, 0.91, 0.86, 1),
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
                          background_color=(0.7882, 0.4824, 0.2275, 1))
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
                prefs = self._load_prefs()
                user = prefs.get('collab_username', '')
                token = prefs.get('collab_token', '')
                from collab import clone_repo
                lift_path, log = clone_repo(clone_url, dest, user, token)
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

    def on_activity_result(self, request_code, result_code, intent):
        """Called by Kivy Android bridge when file picker returns."""
        if request_code == 1001 and result_code == -1:  # RESULT_OK
            try:
                from jnius import autoclass
                uri = intent.getData()
                path = self._uri_to_path(uri)
                if path:
                    self.load_lift(path)
            except Exception as ex:
                print(f'Activity result error: {ex}')

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

    def load_lift(self, path):
        path = os.path.abspath(path)
        try:
            db = LIFTDatabase(path)
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
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'recorder'
        Clock.schedule_once(lambda dt: self.refresh_recorder_ui(), 0.1)
        # Auto-publish new project if credentials are already configured
        if pending:
            self._try_auto_publish()

    def _reload_and_restore(self, guid):
        """Reload the LIFT file and restore position to the entry with *guid*."""
        if not self.recorder:
            return
        path = self.recorder.db.path
        try:
            db = LIFTDatabase(path)
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

    def _try_auto_publish(self):
        """If git credentials and langcode are configured, publish automatically."""
        prefs = self._load_prefs()
        user = prefs.get('collab_username', '')
        token = prefs.get('collab_token', '')
        langcode = prefs.get('collab_langcode', '')
        host = prefs.get('collab_host', 'GitHub')
        if not (user and token and langcode and self.recorder):
            return
        domain = 'gitlab.com' if host == 'GitLab' else 'github.com'
        remote_url = f'https://{domain}/{user}/{langcode}.git'
        name = prefs.get('collab_name', '') or 'Recorder'
        import threading
        def _worker():
            try:
                from collab import init_repo
                result = init_repo(self.recorder.db.dir, remote_url,
                                   user, token, 'main', name)
                print(f'[auto-publish] {result}')
            except Exception as ex:
                print(f'[auto-publish] error: {ex}')
        threading.Thread(target=_worker, daemon=True).start()

    def _auto_commit_sync(self):
        """Background: commit new audio and .lift changes, sync if online."""
        if not self.recorder:
            return
        prefs = self._load_prefs()
        name = prefs.get('collab_name', '') or 'Recorder'
        user = prefs.get('collab_username', '')
        token = prefs.get('collab_token', '')
        project_dir = self.recorder.db.dir
        import threading
        def _worker():
            try:
                from collab import commit_audio_and_sync
                result = commit_audio_and_sync(project_dir, name, user, token)
                print(f'[auto-sync] {result}')
            except Exception as ex:
                print(f'[auto-sync] error: {ex}')
        threading.Thread(target=_worker, daemon=True).start()

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
        """Share the running APK via the OS share sheet."""
        if platform == 'android':
            try:
                import shutil
                from jnius import autoclass, cast
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                Intent = autoclass('android.content.Intent')
                Uri = autoclass('android.net.Uri')
                File = autoclass('java.io.File')
                StrictMode = autoclass('android.os.StrictMode')
                activity = PythonActivity.mActivity
                pm = activity.getPackageManager()
                pkg = activity.getPackageName()
                app_info = pm.getApplicationInfo(pkg, 0)
                apk_path = app_info.sourceDir
                # Copy to external cache so other apps can read it
                cache_dir = activity.getExternalCacheDir().getAbsolutePath()
                dest = os.path.join(cache_dir, 'RecordMyWordlist.apk')
                shutil.copy2(apk_path, dest)
                dest_file = File(dest)
                # Temporarily allow file:// URIs (no FileProvider in p4a)
                old_policy = StrictMode.getVmPolicy()
                builder = autoclass('android.os.StrictMode$VmPolicy$Builder')()
                StrictMode.setVmPolicy(builder.build())
                try:
                    uri = Uri.fromFile(dest_file)
                    intent = Intent(Intent.ACTION_SEND)
                    intent.setType('application/vnd.android.package-archive')
                    intent.putExtra(Intent.EXTRA_STREAM, cast(
                        'android.os.Parcelable', uri))
                    intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    chooser = Intent.createChooser(intent, cast(
                        'java.lang.CharSequence',
                        autoclass('java.lang.String')('Share app')))
                    activity.startActivity(chooser)
                finally:
                    StrictMode.setVmPolicy(old_policy)
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
        user = prefs.get('collab_username', '')
        token = prefs.get('collab_token', '')
        from collab import sync_repo, run_async

        saved_guid = self.recorder.current.get('guid', '') if self.recorder.queue else ''
        def _sync_and_reload(project_dir, username, pw, contributor):
            result = sync_repo(project_dir, username, pw, contributor)
            Clock.schedule_once(
                lambda dt: self._reload_and_restore(saved_guid), 0)
            return result

        run_async(_sync_and_reload,
                  self.recorder.db.dir, user, token, name,
                  on_done=lambda result: print(f'Sync: {result}'))

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
            self.recorder.go_prev()

    def nav_next(self):
        if self.recorder:
            self.recorder.go_next()

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
                        background_color=(0.7882, 0.4824, 0.2275, 1))
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
            font_size=sp(13), color=(0.94, 0.91, 0.86, 1),
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
            color=(0.5412, 0.4784, 0.4157, 1),
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

        # Auth credentials
        user_input = TextInput(
            text=prefs.get('collab_username', ''),
            hint_text='Your git username',
            multiline=False, size_hint_y=None, height=dp(44), font_size=sp(14),
        )
        token_input = TextInput(
            text=prefs.get('collab_token', ''),
            hint_text='Personal access token', password=True,
            multiline=False, size_hint_y=None, height=dp(44), font_size=sp(14),
        )
        content.add_widget(user_input)
        content.add_widget(token_input)

        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
        cancel_btn = Button(text='Cancel', font_size=sp(14))
        clone_btn = Button(text='Clone', font_size=sp(14),
                           background_color=(0.7882, 0.4824, 0.2275, 1))
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(clone_btn)
        content.add_widget(btn_row)

        popup = Popup(
            title='Clone Repository',
            content=content,
            size_hint=(0.9, None), height=dp(440),
            auto_dismiss=True,
        )

        def _do_clone(*args):
            clone_url = url_label.text.strip()
            user = user_input.text.strip()
            token = token_input.text.strip()
            popup.dismiss()
            if not clone_url:
                return
            # Save credentials and clone prefs for future use
            prefs = self._load_prefs()
            prefs['collab_username'] = user
            prefs['collab_token'] = token
            prefs['clone_host'] = host_spinner.text
            prefs['clone_owner'] = owner_input.text.strip()
            self._save_prefs_dict(prefs)

            repo_name = clone_url.rstrip('/').split('/')[-1].replace('.git', '')
            dest = os.path.join(self.user_data_dir, 'projects', repo_name)

            import threading
            from collab import clone_repo

            def _worker():
                lift_path, log = clone_repo(clone_url, dest, user, token)
                if lift_path:
                    Clock.schedule_once(lambda dt: self.load_lift(lift_path), 0)
                else:
                    Clock.schedule_once(
                        lambda dt: self._show_error(log), 0)

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

        # Image — size to actual width, maintaining aspect ratio
        if 'entry_image' in ids:
            img = ids.entry_image
            if r.has_image:
                img.source = r.image_path
                img.opacity = 1
                # Bind texture size to compute correct height once loaded
                def _resize_image(img_ref, *args):
                    if img_ref.texture:
                        tw, th = img_ref.texture.size
                        if tw > 0:
                            img_ref.height = img_ref.width * th / tw
                    elif img_ref.height == 0:
                        img_ref.height = img_ref.width  # fallback square
                img.bind(texture=lambda *a: _resize_image(img))
                img.bind(width=lambda *a: _resize_image(img))
                _resize_image(img)
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
            ids.status_label.color = (0.1529, 0.6824, 0.3765, 1) \
                if r.has_recording else (0.5412, 0.4784, 0.4157, 1)

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
