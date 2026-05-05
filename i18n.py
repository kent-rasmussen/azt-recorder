"""
A-Z+T Recorder — internationalisation helpers.

Recorder catalog: ``locales/<lang>/LC_MESSAGES/aztrecorder.{po,mo}``.

The single source of truth for the active UI language is
``azt_collab_client.i18n`` (persisted to ``$AZT_HOME/config.json``
under ``ui.language``). This module wraps that with the recorder's
own gettext catalog chained as the primary, falling back to the
client catalog. There is no transient mode — one preference, one
store, sticks everywhere until next changed.

Auto-inits on import: the recorder's catalog is loaded for whatever
language the client has already applied (its own auto-init runs
first via the ``import`` order at the call site). Hosts can call
``set_language(lang)`` to change the choice for the whole suite.
"""

import gettext
import os

from azt_collab_client import i18n as _client_i18n

_DOMAIN = 'aztrecorder'
_LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locales')

_current: gettext.GNUTranslations | gettext.NullTranslations = gettext.NullTranslations()


def current_language() -> str:
    """Active UI language code; sourced from the client."""
    return _client_i18n.current_language()


def available_languages() -> list[tuple[str, str]]:
    """Languages that have a catalog in either the recorder or the client."""
    seen = {'en'}
    out = [('en', _client_i18n.display_name('en'))]
    for code, name in _client_i18n.scan_catalog_languages(_LOCALE_DIR, _DOMAIN):
        if code not in seen:
            seen.add(code)
            out.append((code, name))
    for code, name in _client_i18n.available_languages():
        if code not in seen:
            seen.add(code)
            out.append((code, name))
    return out


def _load_recorder_catalog(lang: str):
    """Build the recorder gettext.translation for ``lang``, chained to
    the client catalog as a fallback. Returns a NullTranslations for
    English or when the recorder has no catalog for the language —
    in either case the client catalog still answers via fallback.

    Auto-compiles ``aztrecorder.po`` → ``.mo`` via the client's
    ``ensure_mo`` helper before the lookup, so editing
    ``locales/<lang>/LC_MESSAGES/aztrecorder.po`` in place takes
    effect on next launch with no external ``msgfmt`` step."""
    if lang == 'en':
        cat = gettext.NullTranslations()
    else:
        _client_i18n.ensure_mo(_LOCALE_DIR, _DOMAIN, lang)
        try:
            cat = gettext.translation(
                _DOMAIN, localedir=_LOCALE_DIR, languages=[lang])
        except FileNotFoundError:
            cat = gettext.NullTranslations()
    cat.add_fallback(_client_i18n.gettext_translation())
    return cat


def set_language(lang: str) -> None:
    """Switch the suite-wide UI language. Persists via the client and
    reloads the recorder's catalog (chained to client fallback)."""
    global _current
    _client_i18n.set_language(lang)
    _current = _load_recorder_catalog(_client_i18n.current_language())


def _(message: str) -> str:
    """Translate *message* via the recorder catalog, then the client."""
    return _current.gettext(message)


# ── auto-init ──────────────────────────────────────────────────────────────
# Adopt whatever the client has already applied at import.
_current = _load_recorder_catalog(_client_i18n.current_language())
