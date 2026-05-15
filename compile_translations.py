#!/usr/bin/env python3
"""
Build-time: compile every `locales/<lang>/LC_MESSAGES/aztrecorder.po`
into a matching `.mo`, so the APK ships with fresh-compiled
translations.

i18n.py *also* calls ``ensure_mo`` at app startup, but its in-place
write only catches edits made AFTER the APK was built. On a first
launch of a fresh install the bundled ``.mo`` is what gettext
actually reads, and on Android the runtime recompile has been
observed to skip silently when APK extraction gives ``.po`` and
``.mo`` matching mtimes — in which case msgids added to ``.po``
since the last ``.mo`` build never translate.

Run before ``buildozer android debug``. Idempotent: skips files
whose ``.mo`` is already at-or-newer than the ``.po``.

Imports ``ensure_mo`` from the symlinked ``azt_collab_client``
package (canonical location, no code duplication).
"""
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
LOCALE_DIR = os.path.join(HERE, 'locales')
DOMAIN = 'aztrecorder'


def main():
    sys.path.insert(0, HERE)
    try:
        from azt_collab_client.i18n import ensure_mo
    except ImportError as ex:
        print(f'[compile_translations] cannot import ensure_mo: {ex}',
              file=sys.stderr)
        print('  did you set up the azt_collab_client symlink? '
              '(see CLAUDE.md / setup_from_nuke.sh)',
              file=sys.stderr)
        return 1

    if not os.path.isdir(LOCALE_DIR):
        print(f'[compile_translations] no {LOCALE_DIR}; nothing to do')
        return 0

    seen = 0
    for entry in sorted(os.listdir(LOCALE_DIR)):
        if entry == 'en':
            continue
        po = os.path.join(LOCALE_DIR, entry, 'LC_MESSAGES',
                          f'{DOMAIN}.po')
        if not os.path.isfile(po):
            continue
        ensure_mo(LOCALE_DIR, DOMAIN, entry)
        mo = po[:-3] + '.mo'
        status = 'ok' if os.path.isfile(mo) else 'MISSING'
        print(f'[compile_translations] {entry}: {status}')
        seen += 1
    if seen == 0:
        print('[compile_translations] no .po files under '
              f'{LOCALE_DIR} (English only is fine)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
