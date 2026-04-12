"""
A-Z+T Recorder — internationalisation helpers.

Uses Python's built-in gettext backed by .po/.mo files under
locales/<lang>/LC_MESSAGES/aztrecorder.{po,mo}.

Usage
-----
    from i18n import _, set_language, current_language

In KV templates the app exposes ``app._('text')`` which resolves at
widget-creation time.

The active language is persisted in the app's prefs.json under the
key ``'ui_language'``.
"""

import gettext
import os

_DOMAIN = 'aztrecorder'
_LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locales')

# Fallback: returns the original string untouched (English)
_current: gettext.GNUTranslations | gettext.NullTranslations = gettext.NullTranslations()
_current_lang: str = 'en'


def current_language() -> str:
    """Return the active language code (e.g. 'en', 'fr')."""
    return _current_lang


def available_languages() -> list[tuple[str, str]]:
    """Return [(code, display_name), ...] for every language with a .mo file,
    plus English (always available as fallback)."""
    langs = [('en', 'English')]
    if os.path.isdir(_LOCALE_DIR):
        for entry in sorted(os.listdir(_LOCALE_DIR)):
            mo = os.path.join(_LOCALE_DIR, entry, 'LC_MESSAGES', f'{_DOMAIN}.mo')
            if os.path.isfile(mo):
                # Read display name from the .mo metadata if possible
                name = _display_name(entry)
                langs.append((entry, name))
    return langs


def _display_name(code: str) -> str:
    """Human-readable name for a language code."""
    _names = {
        'fr': 'Fran\u00e7ais',
        'es': 'Espa\u00f1ol',
        'pt': 'Portugu\u00eas',
        'de': 'Deutsch',
        'sw': 'Kiswahili',
        'zh': '\u4e2d\u6587',
        'ar': '\u0627\u0644\u0639\u0631\u0628\u064a\u0629',
    }
    return _names.get(code, code)


def set_language(lang: str) -> None:
    """Switch the active UI language.  Pass 'en' for English (no translation)."""
    global _current, _current_lang
    _current_lang = lang
    if lang == 'en':
        _current = gettext.NullTranslations()
    else:
        try:
            _current = gettext.translation(
                _DOMAIN, localedir=_LOCALE_DIR, languages=[lang])
        except FileNotFoundError:
            print(f'[i18n] No .mo file for {lang!r}, falling back to English')
            _current = gettext.NullTranslations()


def _(message: str) -> str:
    """Translate *message* using the current language."""
    return _current.gettext(message)
