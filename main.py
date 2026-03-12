"""
LIFT Recorder — field audio recorder for LIFT XML lexicon databases.

Records high-quality audio for dictionary entries, stores WAV files in an
'audio/' directory next to the LIFT file, and writes filenames back into
the LIFT XML via Entry.lc (citation form) with the audiolang tag.

Runs on Android (primary) and iOS/desktop (secondary) via Kivy.
"""

import os
os.environ.setdefault('KIVY_NO_ENV_CONFIG', '1')

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
        spacing: dp(24)
        Label:
            text: '📖  LIFT Recorder'
            font_size: sp(28)
            font_name: FONT
            bold: True
            color: (0.7882, 0.4824, 0.2275, 1)
            size_hint_y: None
            height: dp(60)
        Label:
            text: 'Open a .lift file to begin'
            font_size: sp(16)
            font_name: FONT
            color: (0.5412, 0.4784, 0.4157, 1)
            size_hint_y: None
            height: dp(30)
        Widget:
            size_hint_y: 0.1
        RecBtn:
            text: 'Open LIFT File'
            normal_color: (0.7882, 0.4824, 0.2275, 1)
            on_release: app.open_file()
        Widget:
            size_hint_y: 1
        Label:
            text: 'version 1.0'
            font_size: sp(11)
            font_name: FONT
            color: (0.2902, 0.2275, 0.1647, 1)
            size_hint_y: None
            height: dp(20)

<RecorderScreen>:
    canvas.before:
        Color:
            rgba: (0.102, 0.0863, 0.0706, 1)
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
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
            Label:
                id: progress_label
                text: ''
                font_size: sp(13)
                font_name: FONT
                color: (0.5412, 0.4784, 0.4157, 1)
                halign: 'left'
                valign: 'middle'
                text_size: self.size
            Widget:
            IconBtn:
                text: '⚙'
                size_hint_x: None
                width: dp(44)
                on_release: app.go_config()
        # ── Image ─────────────────────────────────────────────────────────────
        BoxLayout:
            id: image_box
            size_hint_y: None
            height: 0
            opacity: 0
            padding: dp(8)
            AsyncImage:
                id: entry_image
                source: ''
                allow_stretch: True
                keep_ratio: True
        # ── Headword (compact — may be empty) ────────────────────────────────
        Label:
            id: headword_label
            text: ''
            font_size: sp(28)
            font_name: FONT
            bold: True
            color: (0.7882, 0.4824, 0.2275, 1)
            size_hint_y: None
            height: dp(44)
            halign: 'center'
            valign: 'middle'
            text_size: self.width, None
        # ── Glosses (prominent, fill remaining space) ─────────────────────────
        ScrollView:
            size_hint_y: 1
            do_scroll_x: False
            BoxLayout:
                id: gloss_box
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(12), dp(8)
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
            padding: dp(16), dp(8)
            spacing: dp(12)
            # Populated dynamically by refresh_recorder_ui
        # ── Nav arrows ────────────────────────────────────────────────────────
        BoxLayout:
            size_hint_y: None
            height: dp(64)
            padding: dp(12), dp(8)
            spacing: dp(12)
            NavBtn:
                text: '◀  Prev'
                on_release: app.nav_prev()
            NavBtn:
                text: 'Next  ▶'
                on_release: app.nav_next()

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
                text: 'Configuration'
                font_size: sp(17)
                font_name: FONT
                bold: True
                color: (0.7882, 0.4824, 0.2275, 1)
                halign: 'left'
                valign: 'middle'
                text_size: self.size
                padding_x: dp(8)
            IconBtn:
                text: '📂'
                size_hint_x: None
                width: dp(44)
                on_release: app.open_file()
            IconBtn:
                text: '✕'
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
                # Current file
                SectionLabel:
                    text: 'Current file'
                Label:
                    id: current_file_label
                    text: ''
                    font_size: sp(12)
                    font_name: FONT
                    color: (0.5412, 0.4784, 0.4157, 1)
                    size_hint_y: None
                    height: dp(32)
                    halign: 'left'
                    valign: 'middle'
                    text_size: self.width, None
                # Gloss languages
                SectionLabel:
                    text: 'Gloss languages'
                BoxLayout:
                    id: lang_box
                    orientation: 'vertical'
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(8)
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

                        font_name: FONT
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

                        font_name: FONT
                # Only unrecorded
                SectionLabel:
                    text: 'Session options'
                BoxLayout:
                    size_hint_y: None
                    height: dp(48)
                    spacing: dp(12)
                    CheckboxStyled:
                        id: unrecorded_check
                        active: False
                        on_active: root.toggle_unrecorded(self.active)
                    Label:
                        text: 'Only show unrecorded entries'
                        font_size: sp(14)
                        font_name: FONT
                        color: (0.9412, 0.9098, 0.8627, 1)
                        halign: 'left'
                        text_size: self.size
                # Apply button
                Widget:
                    size_hint_y: None
                    height: dp(16)
                RecBtn:
                    text: 'Apply & Go'
                    normal_color: (0.7882, 0.4824, 0.2275, 1)
                    on_release: root.apply_and_go()
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
    Label:
        text: '⏹  Stop' if root.recording else '⏺  Record'
        font_size: sp(22)
        font_name: FONT
        bold: True
        color: (1, 1, 1, 1)
        center: root.center

<PlayButton>:
    size_hint: 1, 1
    canvas:
        Color:
            rgba: (0.1529, 0.6824, 0.3765, 1)
        RoundedRectangle:
            pos: self.x + dp(4), self.y + dp(4)
            size: self.width - dp(8), self.height - dp(8)
            radius: [dp(12)]
    Label:
        text: '▶  Play'
        font_size: sp(22)
        font_name: FONT
        bold: True
        color: (1, 1, 1, 1)
        center: root.center

<RedoButton>:
    size_hint: 1, 1
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
        text: '⏺  Re-record'
        font_size: sp(18)
        font_name: FONT
        bold: True
        color: (0.7882, 0.4824, 0.2275, 1)
        center: root.center

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
    height: dp(48)
    BoxLayout:
        spacing: dp(12)
        padding: dp(4), 0
        CheckboxStyled:
            id: chk
            active: root.active
            on_active: root.on_toggle(self.active)
        Label:
            text: root.lang
            font_size: sp(16)
            font_name: FONT
            color: (0.9412, 0.9098, 0.8627, 1)
            halign: 'left'
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


class GlossRow(BoxLayout):
    lang = StringProperty('')
    gloss = StringProperty('')


class LangToggle(BoxLayout):
    lang = StringProperty('')
    active = BooleanProperty(True)
    callback = ObjectProperty(None)

    def on_toggle(self, value):
        if self.callback:
            self.callback(self.lang, value)


# ── Screens ────────────────────────────────────────────────────────────────────

class RootScreen(Screen):
    pass


class WelcomeScreen(Screen):
    pass


class RecorderScreen(Screen):
    pass


class ConfigScreen(Screen):
    only_unrecorded = BooleanProperty(False)

    def on_enter(self):
        app = App.get_running_app()
        lbl = self.ids.get('current_file_label')
        if lbl:
            lbl.text = os.path.basename(app.recorder.db.path) if app.recorder else 'No file loaded'
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

    def toggle_unrecorded(self, value):
        self.only_unrecorded = value
        App.get_running_app().recorder.only_unrecorded = value

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
            self.index += 1
            self._notify_ui()

    def go_prev(self):
        if self.index > 0:
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
        return bool(e.get('image_path')) and os.path.exists(e['image_path'])

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
            return '🔴  Recording…'
        fn = e.get('audio_filename')
        if fn:
            return f'✓  {fn}'
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
        self._stop_native_recording()
        # Write filename into LIFT XML
        if self._audio_path and os.path.exists(self._audio_path):
            filename = os.path.basename(self._audio_path)
            self.db.set_audio(self.current['guid'], filename)
            self.current['audio_filename'] = filename
        self._notify_ui()

    def play_audio(self):
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

        if platform == 'android':
            try:
                from jnius import autoclass
                MediaPlayer = autoclass('android.media.MediaPlayer')
                mp = MediaPlayer()
                mp.setDataSource(path)
                mp.prepare()
                mp.start()
                self._player = mp  # keep reference alive
            except Exception as ex:
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
            except Exception as ex:
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
                    # SDL2 may load async; bind on_load to guarantee play
                    # fires only after the buffer is ready
                    def _play_when_ready(instance):
                        instance.play()
                    if sound.state == 'stop' and sound.length > 0:
                        # Already loaded synchronously
                        sound.play()
                    else:
                        sound.bind(on_load=lambda *a: sound.play())
                        # Fallback: also schedule a direct play in case
                        # on_load never fires (provider-dependent)
                        Clock.schedule_once(lambda dt: sound.play()
                            if sound.state == 'stop' else None, 0.3)
                else:
                    print(f'SoundLoader could not load: {path}')
            except Exception as ex:
                print(f'Desktop play error: {ex}')

    def clear_audio(self):
        """Remove audio association so the record button reappears."""
        e = self.current
        if not e:
            return
        e['audio_filename'] = ''
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

class LIFTRecorderApp(App):
    recorder: RecorderController = None
    config_screen: ConfigScreen = None

    # ── Preferences (last used file) ──────────────────────────────────────────

    @property
    def _prefs_path(self):
        return os.path.join(self.user_data_dir, 'prefs.json')

    def _save_prefs(self, lift_path):
        import json
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self._prefs_path, 'w') as f:
                json.dump({'last_lift': lift_path}, f)
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
        Builder.load_string(KV)
        self.root = RootScreen()
        return self.root

    def on_start(self):
        sm = self.root.ids.sm
        self.config_screen = sm.get_screen('config')
        # Auto-load last used LIFT file if it still exists
        prefs = self._load_prefs()
        last = prefs.get('last_lift', '')
        if last and os.path.exists(last):
            Clock.schedule_once(lambda dt: self.load_lift(last), 0.3)

    def open_file(self):
        if platform == 'android':
            self._open_file_android()
        elif platform == 'ios':
            self._open_file_ios()
        else:
            self._open_file_desktop()

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
        self._save_prefs(path)
        db = LIFTDatabase(path)
        self.recorder = RecorderController(db)
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'recorder'
        Clock.schedule_once(lambda dt: self.refresh_recorder_ui(), 0.1)

    def go_config(self):
        sm = self.root.ids.sm
        sm.transition = SlideTransition(direction='left')
        sm.current = 'config'

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

        # Image
        if 'image_box' in ids:
            ids.image_box.height = 200 if r.has_image else 0
            ids.image_box.opacity = 1 if r.has_image else 0
        if 'entry_image' in ids:
            ids.entry_image.source = r.image_path if r.has_image else ''

        # Headword — hide row entirely if empty to give glosses more room
        if 'headword_label' in ids:
            hw = r.headword
            ids.headword_label.text = hw
            ids.headword_label.height = dp(44) if hw else 0
            ids.headword_label.opacity = 1 if hw else 0

        # Glosses
        if 'gloss_box' in ids:
            ids.gloss_box.clear_widgets()
            e = r.current
            if e:
                for lang in r.active_gloss_langs:
                    for g in e.get('glosses', {}).get(lang, []):
                        ids.gloss_box.add_widget(GlossRow(lang=lang, gloss=g))

        # Status text + colour
        if 'status_label' in ids:
            ids.status_label.text = r.status_text
            ids.status_label.color = (0.1529, 0.6824, 0.3765, 1) \
                if r.has_recording else (0.5412, 0.4784, 0.4157, 1)

        # Button area: record button OR play+redo pair
        if 'btn_area' in ids:
            btn_area = ids.btn_area
            btn_area.clear_widgets()
            if r.has_recording and not r._recording:
                # Show Play and Re-record side by side
                play_btn = PlayButton()
                play_btn.bind(on_touch_up=lambda w, t:
                    self.play_audio() if w.collide_point(*t.pos) else None)
                redo_btn = RedoButton()
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
