"""
lift.py — LIFT XML lexicon database reader/writer for A-Z+T Recorder.

Provides LIFTDatabase which:
  - Parses a .lift file into entry dicts
  - Exposes Entry.lc semantics: citation form with the audio lang tag
  - Writes audio filenames back to the XML (textvaluebylang pattern)

Key concepts matching kent-rasmussen/azt lift.py:
  - Entry.lc  = <citation> element
  - audiolang  = the lang tag of form {vernlang}-Zxxx-x-audio
  - Entry.lc.textvaluebylang(lang=audiolang) = text of that form
"""

import os
import re
import threading
import xml.etree.ElementTree as ET

from azt_collab_client import (
    CAWLHandle, LiftHandle, MediaHandle, audio_uri_for, cawl_index,
    image_uri_for, is_content_uri,
)


def _scan_namespaces(handle):
    """Read the source through *handle* once and return its
    {prefix: uri} declarations as encountered. ET strips original
    prefixes during parse, so we capture them up-front and re-register
    each before save — otherwise FLEx-produced LIFT files round-trip
    with ``ns0:flex=...`` mangled prefixes."""
    seen = {}
    try:
        with handle.open_read() as f:
            for event, payload in ET.iterparse(f, events=('start-ns',)):
                prefix, uri = payload
                seen.setdefault(prefix, uri)
    except Exception as ex:
        # A quirky source shouldn't block parsing — we'll just emit
        # ns0:-style prefixes in that case (still well-formed XML).
        print(f'lift namespace scan failed: {ex}')
    return seen

# ── CAWL illustration images ─────────────────────────────────────────────────
# CAWL is *suite-scoped* infrastructure owned by the daemon (azt_collabd
# 0.38+ for the binary half). The peer-side resolver below:
#
#   - Calls ``cawl_index(langcode)`` for the CAWL → basename map
#     (Stage 1; the daemon's index is the source of truth for which
#     image_repo / branch / files exist for this project).
#   - Pulls binaries lazily via ``CAWLHandle(langcode, basename).
#     open_read()`` and pipes them into a per-LIFTDatabase tmp dir so
#     Kivy's path-based AsyncImage can render them (Stage 2; the
#     daemon's $AZT_HOME/cawl/<owner>/<repo>/images/<basename> cache
#     is the durable copy, shared across peers).
#
# The peer never hits ``api.github.com`` or ``raw.githubusercontent.com``
# directly anymore, and never writes a durable peer-side cache of CAWL
# bytes — see azt_collab_client/CLAUDE.md "CAWL image access".


class _CAWLImageResolver:
    """Resolves CAWL numbers to local image paths via the daemon.

    On first access calls ``cawl_index(langcode)`` to learn which
    basenames map to which CAWL numbers, then lazily pulls each
    binary via ``CAWLHandle.open_read()`` into a per-LIFTDatabase
    tmp directory. Both calls fail soft — daemon down / image missing
    / project not registered all yield empty results, which callers
    treat as "no illustration for this entry."
    """

    def __init__(self, get_langcode, tmp_dir: str):
        # *get_langcode* is a zero-arg callable that returns the
        # daemon's project langcode (== LIFT vernlang). Wrapped so
        # construction can happen before set_vernlang fires; the
        # resolver only needs the value lazily on first _load().
        # *tmp_dir* is the per-LIFTDatabase ephemeral directory where
        # pulled-through CAWL bytes land for AsyncImage rendering.
        self._get_langcode = get_langcode
        self._tmp_dir = tmp_dir
        self._basenames = None     # cawl str → first basename (preferred)
        self._all_basenames = None # cawl str → [all basenames]
        self._paths = None         # cawl str → first repo-path (preferred)
        self._path_cache = {}      # basename → local tmp path (post-pull)
        self._lock = threading.Lock()
        self._pull_lock = threading.Lock()
        # One-shot diagnostic: log the first failure of each kind so a
        # systemic pull failure (every basename returning '') surfaces
        # in logcat instead of being silenced as "expected" recoverable
        # errors. Tracked per-resolver-instance so we don't spam.
        self._logged_pull_errors = set()
        # Session-level circuit breaker. If the resolver gets
        # _FAILURE_CAP failures in a row without an intervening success,
        # subsequent _pull calls short-circuit to '' without a Binder
        # round-trip. Saves ~1700 wasted IPC calls during prefetch when
        # something is systemically wrong (transport too large,
        # basenames don't round-trip, daemon CAWL endpoint absent on
        # this server APK, etc.). One fresh attempt happens at the next
        # LIFTDatabase instance — i.e. project switch or app restart.
        self._consecutive_failures = 0
        self._breaker_tripped = False

    def _normalize_cawl(self, cawl: str) -> str:
        """Return the key form of *cawl* that exists in self._basenames, or ''."""
        if cawl in self._basenames:
            return cawl
        try:
            padded = str(int(cawl)).zfill(4)
            if padded in self._basenames:
                return padded
        except ValueError:
            pass
        stripped = cawl.lstrip('0') or '0'
        if stripped in self._basenames:
            return stripped
        return ''

    def get_path(self, cawl: str) -> str:
        """Return a local path for *cawl*'s canonical image, or ''.

        Pulls the binary from the daemon on first request, then
        memoizes the local tmp path. Empty string on any failure —
        callers fall through to no-image rendering."""
        if self._basenames is None:
            self._load()
        key = self._normalize_cawl(cawl)
        basename = self._basenames.get(key, '')
        if not basename:
            return ''
        return self._pull(basename)

    def get_all_paths(self, cawl: str) -> list:
        """Return all local paths for *cawl* (all variants pulled
        through). ``__``-prefixed defaults come first per the
        recorder's naming convention."""
        if self._all_basenames is None:
            self._load()
        key = self._normalize_cawl(cawl)
        basenames = self._all_basenames.get(key, [])
        out = []
        for b in basenames:
            p = self._pull(b)
            if p:
                out.append(p)
        return out

    # ── internals ────────────────────────────────────────────────────────

    def _load(self):
        with self._lock:
            if self._basenames is not None:
                return
            self._basenames = {}
            self._all_basenames = {}
            self._paths = {}
            langcode = ''
            try:
                langcode = self._get_langcode() or ''
            except Exception as ex:
                print(f'[cawl] langcode lookup failed: {ex}')
            if not langcode:
                print('[cawl] _load: no langcode; resolver stays empty')
                return
            try:
                index = cawl_index(langcode) or {}
            except Exception as ex:
                print(f'[cawl] cawl_index({langcode!r}) failed: {ex}')
                return
            files = index.get('files') or []
            print(f'[cawl] _load: langcode={langcode!r} '
                  f'repo={index.get("repo", "")!r} '
                  f'files={len(files)}')
            if not files:
                print('[cawl] _load: index empty — daemon-global '
                      'cawl_image_repo and per-project '
                      'Project.cawl_image_repo are both unset, or the '
                      'daemon could not reach the repo')
                return
            skipped_nested = 0
            skipped_ext = 0
            for item in files:
                path = item.get('path', '')
                if not path:
                    continue
                parts = path.split('/')
                if len(parts) == 1:
                    filename = parts[0]
                elif len(parts) == 2:
                    filename = parts[1]
                else:
                    # CAWLHandle rejects basenames containing '/'.
                    # Deeply nested paths can't round-trip through the
                    # provider's flat <basename> contract; skip.
                    skipped_nested += 1
                    continue
                low = filename.lower()
                if '.' in low and not (low.endswith('.png')
                        or low.endswith('.jpg')
                        or low.endswith('.jpeg')):
                    skipped_ext += 1
                    continue
                cawl_num = parts[0].split('_')[0]
                if len(parts) == 1 and '.' in cawl_num:
                    cawl_num = cawl_num.rsplit('.', 1)[0]
                # CAWLHandle's basename is the daemon-served file
                # identifier (flat, no slashes). For root-level files
                # it's the path as-is; for subdir entries the daemon
                # flattens, so we pass `filename` (the leaf) on the
                # assumption that the index path *is* the basename
                # the provider serves. If a future daemon serves
                # paths with internal slashes here, CAWLHandle will
                # ValueError and the pull will gracefully fail.
                basename = path if '/' not in path else filename
                is_default = '__' in filename
                if cawl_num not in self._basenames:
                    self._basenames[cawl_num] = basename
                    self._paths[cawl_num] = path
                elif is_default and '__' not in self._basenames[cawl_num]:
                    self._basenames[cawl_num] = basename
                    self._paths[cawl_num] = path
                if is_default:
                    self._all_basenames.setdefault(cawl_num, []).insert(0, basename)
                else:
                    self._all_basenames.setdefault(cawl_num, []).append(basename)
            print(f'[cawl] _load: kept {len(self._basenames)} CAWL '
                  f'identifiers (skipped {skipped_nested} nested, '
                  f'{skipped_ext} non-image extensions)')
            sample = list(self._basenames.items())[:2]
            if sample:
                print(f'[cawl] _load: sample basenames: {sample!r}')

    _FAILURE_CAP = 10  # consecutive _pull failures before the breaker trips

    def _pull(self, basename: str) -> str:
        """Pull *basename* via CAWLHandle into tmp_dir; return path.

        Memoized: a second call for the same basename returns the
        cached path without re-reading. ''  on any failure — daemon
        unreachable, file missing in upstream repo, basename rejected
        by CAWLHandle's slash guard.

        After _FAILURE_CAP consecutive failures the resolver trips a
        session-level circuit breaker and stops attempting pulls for
        the life of this LIFTDatabase instance. Project switch / app
        restart gives a fresh resolver and a fresh shot."""
        cached = self._path_cache.get(basename)
        if cached and os.path.exists(cached):
            return cached
        if self._breaker_tripped:
            return ''
        with self._pull_lock:
            cached = self._path_cache.get(basename)
            if cached and os.path.exists(cached):
                return cached
            if self._breaker_tripped:
                return ''
            langcode = ''
            try:
                langcode = self._get_langcode() or ''
            except Exception:
                pass
            if not langcode:
                if 'no_langcode' not in self._logged_pull_errors:
                    self._logged_pull_errors.add('no_langcode')
                    print('[cawl] _pull: no langcode (resolver pulled '
                          'before set_vernlang ran)')
                return ''
            if not self._tmp_dir:
                if 'no_tmp_dir' not in self._logged_pull_errors:
                    self._logged_pull_errors.add('no_tmp_dir')
                    print('[cawl] _pull: no tmp_dir — LIFTDatabase was '
                          'constructed without an image_cache_dir')
                return ''
            try:
                os.makedirs(self._tmp_dir, exist_ok=True)
                dest = os.path.join(self._tmp_dir, basename)
                with CAWLHandle(langcode, basename).open_read() as src, \
                        open(dest, 'wb') as dst:
                    while True:
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                self._path_cache[basename] = dest
                self._consecutive_failures = 0
                return dest
            except FileNotFoundError as ex:
                if 'fnf' not in self._logged_pull_errors:
                    self._logged_pull_errors.add('fnf')
                    print(f'[cawl] _pull: FileNotFoundError on first '
                          f'try (basename={basename!r}, '
                          f'langcode={langcode!r}): {ex}')
                self._note_failure()
                return ''
            except ValueError as ex:
                if 'value' not in self._logged_pull_errors:
                    self._logged_pull_errors.add('value')
                    print(f'[cawl] _pull: ValueError on first try '
                          f'(basename={basename!r}, '
                          f'langcode={langcode!r}): {ex}')
                self._note_failure()
                return ''
            except Exception as ex:
                print(f'[cawl] _pull: {basename!r} failed: '
                      f'{type(ex).__name__}: {ex}')
                self._note_failure()
                return ''

    def _note_failure(self):
        """Increment the consecutive-failure counter; trip the breaker
        once it reaches the cap. Caller holds ``_pull_lock`` so the
        counter mutation is serialised."""
        self._consecutive_failures += 1
        if (not self._breaker_tripped
                and self._consecutive_failures >= self._FAILURE_CAP):
            self._breaker_tripped = True
            print(f'[cawl] _pull: circuit breaker tripped after '
                  f'{self._FAILURE_CAP} consecutive failures — '
                  f'further pulls suppressed for this session. '
                  f'Project switch or app restart resets.')


# ── Private-use tag exclusion (matching renderer.html) ────────────────────────

def _is_meta_lang(lang: str) -> bool:
    """
    Return True for lang tags that are metadata, not headword text.
    Exactly the five named suffixes; other private-use tags (e.g. lol-x-his30100)
    are legitimate headword languages and must NOT be excluded.
    """
    l = lang.lower()
    return bool(
        re.search(r'-x-audio', l) or
        re.search(r'-x-ipa', l) or
        re.search(r'-x-tone', l) or
        re.search(r'-x-cvprofile', l) or
        re.search(r'-x-py', l)
    )


def _is_audio_lang(lang: str) -> bool:
    return bool(re.search(r'-x-audio', lang.lower()))


class LIFTDatabase:
    """
    Reads a .lift file, exposes entries as dicts, and can write
    audio filenames back via the audiolang citation form.
    """

    def __init__(self, path: str, image_cache_dir: str = ''):
        # ``path`` may be a filesystem path (desktop, or any platform's
        # open-file flow) or a ``content://`` URI emitted by the picker
        # on the Android server-APK model. LiftHandle papers over the
        # difference for read/write of the .lift file itself; sibling
        # directory access (audio/, images/) only works when we have a
        # real filesystem path. The Tier-3 follow-up will route those
        # through the provider too — see azt_collab_client/CLAUDE.md
        # "Future: audio + image cross-package access".
        self.is_uri = is_content_uri(path)
        self.path = path if self.is_uri else os.path.abspath(path)
        self.handle = LiftHandle(self.path)
        if self.is_uri:
            self.dir = ''
            self.images_dir = ''
            self.audio_dir = ''
        else:
            self.dir = os.path.dirname(self.path)
            self.images_dir = os.path.join(self.dir, 'images')
            self.audio_dir = os.path.join(self.dir, 'audio')
        # image_cache_dir is a per-LIFTDatabase *ephemeral* tmp dir
        # supplied by the host (main.py creates one per app session and
        # cleans up on close). Two consumers: (1) CAWL pull-throughs
        # from the daemon via _CAWLImageResolver; (2) project-image
        # pull-throughs from the daemon's ContentProvider on URI
        # projects (_resolve_uri_image). Both are tmp copies that Kivy
        # AsyncImage renders by path; nothing here is durable.
        self.image_cache_dir = image_cache_dir
        cawl_tmp = (os.path.join(image_cache_dir, '_cawl')
                    if image_cache_dir else '')
        # The resolver pulls binaries via CAWLHandle and writes them
        # into cawl_tmp. The langcode is set later via set_vernlang()
        # (after _handle_pick / _auto_load_last_project know it); pass
        # a callable so the resolver picks it up on first use rather
        # than on construction.
        self._image_resolver = _CAWLImageResolver(
            lambda: self.vernlang, cawl_tmp)

        # Preserve original xmlns prefixes across save (ET.parse strips
        # them otherwise, so a FLEx file with xmlns:flex=… round-trips
        # with ns0:-style prefixes).
        for prefix, uri in _scan_namespaces(self.handle).items():
            ET.register_namespace(prefix, uri)
        with self.handle.open_read() as f:
            self._tree = ET.parse(f)
        self._root = self._tree.getroot()
        # One-time normalization to our 4-space indent style. Subsequent
        # saves are bit-stable as long as no new elements get added
        # between parse and save (tracked via _indent_dirty below); when
        # set_audio / set_illustration / _clean_forms appends a new
        # element, _indent runs once on the next save to give it
        # whitespace, then _indent_dirty resets.
        self._indent(self._root)
        self._indent_dirty = False

        # Peer-side cache for sibling files that arrive via the daemon's
        # ContentProvider on URI projects (Android server-APK model).
        # Both audio and image writes pass through MediaHandle.open_write
        # (since the 0.35.2 daemon cut); image reads are pulled into this
        # dir as tmpfiles so AsyncImage can render them by path, and
        # image writes prime the same cache entry so the next display
        # doesn't re-fetch through the provider. Per-LIFTDatabase
        # instance, GC'd on project switch; nothing here is durable.
        self._uri_image_cache = {}

        self.vernlang = ''      # e.g. 'lol-x-his30100'
        self.audiolang = ''     # e.g. 'lol-x-his30100-Zxxx-x-audio'
        self.gloss_langs = []
        # Name of the <sense><field type="..."> that holds each entry's
        # wordlist line number. SILCAWL is the only template in use for
        # now; other templates will set this per-project. Pinned per
        # project — never auto-detected across entries.
        self.list_type = 'SILCAWL'
        self.entries = []

        self._parse()

    def set_vernlang(self, code: str):
        """Set vernacular language code externally (e.g. from language picker)."""
        self.vernlang = code
        self.audiolang = code + '-Zxxx-x-audio'

    # ── Sibling-resource access (audio / images) ──────────────────────────────

    def audio_target(self, basename: str) -> str:
        """Return a write/read target for ``audio/<basename>`` — a
        ``content://`` URI on Android URI projects (resolved by the
        daemon's provider) or a filesystem path on desktop / iOS /
        legacy-Android. Callers wrap in ``MediaHandle(target, 'audio')``
        for the write path; the Android MediaRecorder takes the FD
        directly via ``ContentResolver.openFileDescriptor``."""
        if self.is_uri:
            return audio_uri_for(self.path, basename)
        return os.path.join(self.audio_dir, basename)

    def image_target(self, basename: str) -> str:
        """Return a read/write target for ``images/<basename>`` — a
        ``content://`` URI on URI projects (resolved by the daemon's
        provider, which auto-creates the ``images/`` subdir on first
        write per the 0.35.2 contract) or a filesystem path otherwise.
        Callers wrap in ``MediaHandle(target, 'image')`` for writes —
        same shape as audio."""
        if self.is_uri:
            return image_uri_for(self.path, basename)
        return os.path.join(self.images_dir, basename)

    def _resolve_uri_image(self, illustration_href: str) -> str:
        """Pull a sibling image from the daemon's provider into the
        peer's image cache dir so Kivy can display it by path. Returns
        the local cache path on success; ``''`` on failure (caller
        falls back to URL/cache). Memoised per-LIFTDatabase instance."""
        if not (self.is_uri and illustration_href and self.image_cache_dir):
            return ''
        cached = self._uri_image_cache.get(illustration_href)
        if cached and os.path.isfile(cached):
            return cached
        try:
            dest_dir = os.path.join(self.image_cache_dir, '_uri_images')
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, illustration_href)
            uri = image_uri_for(self.path, illustration_href)
            handle = MediaHandle(uri, 'image')
            with handle.open_read() as src, open(dest, 'wb') as dst:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            self._uri_image_cache[illustration_href] = dest
            return dest
        except Exception:
            return ''

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse(self):
        gloss_langs_seen = set()
        vern_langs_seen = set()
        audio_langs_seen = set()

        raw_entries = []
        for entry_el in self._root.findall('entry'):
            e = self._parse_entry(entry_el, gloss_langs_seen,
                                  vern_langs_seen, audio_langs_seen)
            raw_entries.append(e)

        # Determine primary vernlang and audiolang from what we saw
        if vern_langs_seen:
            # Take the most common non-meta lang in lexical-units
            self.vernlang = sorted(vern_langs_seen)[0]
        if audio_langs_seen:
            self.audiolang = sorted(audio_langs_seen)[0]

        self.gloss_langs = sorted(gloss_langs_seen)
        self.entries = raw_entries

    def _parse_entry(self, el, gloss_langs_seen, vern_langs_seen, audio_langs_seen):
        guid = el.get('guid', '')
        entry_id = el.get('id', '')
        date_modified = el.get('dateModified', '')

        # ── Lexical unit (headword) ───────────────────────────────────────────
        headword = ''
        lu_el = el.find('lexical-unit')
        if lu_el is not None:
            for form in lu_el.findall('form'):
                lang = form.get('lang', '')
                text = self._text(form)
                if not _is_meta_lang(lang) and text:
                    vern_langs_seen.add(lang)
                    if not headword:
                        headword = text

        # ── Citation form (Entry.lc) ───────────────────────────────────────────
        # Citation form preferred as display headword; also holds audio filename
        citation_headword = ''
        audio_filename = ''
        citation_el = el.find('citation')
        if citation_el is not None:
            for form in citation_el.findall('form'):
                lang = form.get('lang', '')
                text = self._text(form)
                if _is_audio_lang(lang):
                    audio_langs_seen.add(lang)
                    if text:
                        audio_filename = text
                elif not _is_meta_lang(lang) and text:
                    vern_langs_seen.add(lang)
                    if not citation_headword:
                        citation_headword = text

        display_headword = citation_headword or headword

        # ── Senses ────────────────────────────────────────────────────────────
        glosses = {}        # lang -> [str, ...]
        cawl = ''
        illustration_href = ''

        for sense_el in el.findall('sense'):
            # Glosses
            for gloss_el in sense_el.findall('gloss'):
                lang = gloss_el.get('lang', '')
                text = self._text(gloss_el)
                if lang and text:
                    gloss_langs_seen.add(lang)
                    glosses.setdefault(lang, [])
                    if text not in glosses[lang]:
                        glosses[lang].append(text)

            # Wordlist line number — only the project's pinned
            # list_type (SILCAWL for now) is treated as the line
            # number. Any other <field> (Plural, etc.) is ignored
            # regardless of XML order.
            if not cawl:
                for field_el in sense_el.findall('field'):
                    if field_el.get('type', '') != self.list_type:
                        continue
                    for form in field_el.findall('form'):
                        t = self._text(form)
                        if t:
                            cawl = t
                            break
                    if cawl:
                        break

            # Illustration
            if not illustration_href:
                ill_el = sense_el.find('illustration')
                if ill_el is not None:
                    illustration_href = ill_el.get('href', '')

        return {
            'guid': guid,
            'id': entry_id,
            'date_modified': date_modified,
            'headword': display_headword,
            'glosses': glosses,
            'cawl': cawl,
            '_field_type': self.list_type if cawl else '',
            'illustration_href': illustration_href,
            # Resolved lazily by RecorderController.image_path on first
            # access — _resolve_image_path can do per-entry IPC/HTTP
            # (ContentProvider read on URI projects), and eagerly
            # running it for every entry blocks the UI thread on load
            # (~10ms × N entries). Filled in on demand, cached here.
            'image_path': '',
            'audio_filename': audio_filename,
            '_el': el,          # live reference for writing back
        }

    def _resolve_image_path(self, illustration_href, cawl):
        """Resolve image path: project's images/ → daemon-served CAWL
        binary via CAWLHandle. Returns a local filesystem path (or '').
        No durable peer-side cache — Stage 2 of the CAWL migration
        moved that to the daemon; pull-through tmpfiles live under the
        per-session image_cache_dir."""
        image_path = ''
        if illustration_href:
            if self.is_uri:
                image_path = self._resolve_uri_image(illustration_href)
            else:
                candidate = os.path.join(self.images_dir, illustration_href)
                if os.path.exists(candidate):
                    image_path = candidate
        if not image_path and cawl:
            image_path = self._image_resolver.get_path(cawl)
        return image_path

    @staticmethod
    def _text(el) -> str:
        """Get trimmed text content of a <text> child, or direct text."""
        text_el = el.find('text')
        if text_el is not None:
            return (text_el.text or '').strip()
        return (el.text or '').strip()

    def all_cawl_basenames(self):
        """Return dict of cawl → first basename. The basenames are
        what ``CAWLHandle(langcode, basename).open_read()`` accepts
        for on-demand fetches of individual images."""
        if self._image_resolver._basenames is None:
            self._image_resolver._load()
        return (dict(self._image_resolver._basenames)
                if self._image_resolver._basenames else {})

    def all_cawl_paths(self):
        """Return the list of repo-relative paths (one per CAWL id,
        the preferred-default variant) suitable for handing to
        ``cawl_prefetch(langcode, paths)``. The daemon iterates this
        in its own background thread and reports progress through
        ``cawl_cache_status``. Same shape as ``cawl_index().files[i]
        ['path']`` — preserves subdir layout so the daemon stores
        each variant under its own cache key."""
        if self._image_resolver._paths is None:
            self._image_resolver._load()
        if not self._image_resolver._paths:
            return []
        return sorted(self._image_resolver._paths.values())

    # ── Image helpers ──────────────────────────────────────────────────────

    def all_image_paths(self, entry):
        """Return list of all CAWL image local paths for *entry*.
        Each path is the pull-through tmpfile from the daemon — empty
        list if the daemon couldn't supply any."""
        cawl = entry.get('cawl', '')
        if not cawl:
            return []
        return self._image_resolver.get_all_paths(cawl)

    @staticmethod
    def imagename(entry):
        """Generate image filename following azt convention:
        {cawl}_{glosses_underlined}.png"""
        cawl = entry.get('cawl', '')
        glosses = entry.get('glosses', {})
        # Use English glosses, falling back to first available language
        gloss_texts = glosses.get('en', [])
        if not gloss_texts:
            for v in glosses.values():
                if v:
                    gloss_texts = v
                    break
        # Build underscore-joined, URL-safe name
        words = []
        for g in gloss_texts:
            # Remove parenthetical content, split on commas
            clean = re.sub(r'\([^)]*\)', '', g).strip()
            for part in clean.split(','):
                for w in part.split():
                    w = re.sub(r'[^a-zA-Z0-9_-]', '', w)
                    if w:
                        words.append(w)
        joined = '_'.join(words) if words else 'image'
        if cawl:
            return f'{cawl}_{joined}.png'
        return f'{joined}.png'

    def set_illustration(self, guid, filename):
        """Write illustration href into the first sense of entry *guid*."""
        entry_el = self._find_entry(guid)
        if entry_el is None:
            return
        sense_el = entry_el.find('sense')
        if sense_el is None:
            return
        ill_el = sense_el.find('illustration')
        if ill_el is None:
            ill_el = ET.SubElement(sense_el, 'illustration')
            self._indent_dirty = True
        ill_el.set('href', filename)
        self._save()

    # ── Template cleaning ────────────────────────────────────────────────────

    def clean_template(self):
        """Remove empty non-vernlang forms from citation/definition; ensure vernlang form exists.

        Called once after setting vernlang on a newly-created-from-template file.
        """
        if not self.vernlang:
            return
        for entry_el in self._root.findall('entry'):
            for parent_tag in ('citation', 'sense/definition'):
                for parent_el in entry_el.findall(parent_tag):
                    self._clean_forms(parent_el)
        self._save()
        # Re-parse so in-memory entries reflect the cleaned XML
        self.entries = []
        self._parse()

    def _clean_forms(self, parent_el):
        """In *parent_el*, remove empty forms whose lang != vernlang,
        and ensure a <form lang=vernlang><text/></form> exists."""
        has_vern = False
        to_remove = []
        for form in parent_el.findall('form'):
            lang = form.get('lang', '')
            text = self._text(form)
            if lang == self.vernlang:
                has_vern = True
            elif not text and not _is_audio_lang(lang):
                to_remove.append(form)
        for form in to_remove:
            parent_el.remove(form)
        if not has_vern:
            vern_form = ET.SubElement(parent_el, 'form')
            vern_form.set('lang', self.vernlang)
            ET.SubElement(vern_form, 'text')
            self._indent_dirty = True

    # ── Writing audio filename back to LIFT (Entry.lc.textvaluebylang) ────────

    def set_audio(self, guid: str, filename: str):
        """
        Write filename into Entry.lc (citation) with lang=audiolang.
        This is the equivalent of:
            Entry.lc.textvaluebylang(lang=self.db.audiolang) = filename
        Then saves the updated XML to disk.
        """
        if not self.audiolang:
            # Construct audiolang from vernlang if we haven't seen one yet
            self.audiolang = self.vernlang + '-Zxxx-x-audio'

        entry_el = self._find_entry(guid)
        if entry_el is None:
            print(f'set_audio: entry {guid} not found')
            return

        # Find or create <citation>
        citation_el = entry_el.find('citation')
        if citation_el is None:
            citation_el = ET.SubElement(entry_el, 'citation')
            self._indent_dirty = True

        # Find or create <form lang=audiolang>
        audio_form = None
        for form in citation_el.findall('form'):
            if form.get('lang') == self.audiolang:
                audio_form = form
                break

        if audio_form is None:
            audio_form = ET.SubElement(citation_el, 'form')
            audio_form.set('lang', self.audiolang)
            self._indent_dirty = True

        # Set <text> child
        text_el = audio_form.find('text')
        if text_el is None:
            text_el = ET.SubElement(audio_form, 'text')
            self._indent_dirty = True
        text_el.text = filename

        # Save
        self._save()

    def _find_entry(self, guid: str):
        for e in self._root.findall('entry'):
            if e.get('guid') == guid:
                return e
        return None

    def _save(self):
        """Write updated XML back to the .lift file, preserving encoding.

        Routes through ``LiftHandle.atomic_open_write``, which gives:
        - Filesystem paths: true atomic write via a random-suffixed
          tempfile + ``os.replace``. A crash mid-write leaves the
          destination untouched; concurrent in-process saves use
          distinct tempfiles, so the rename-last-wins guarantee
          holds.
        - URI projects: falls back to ``open_write``, which is now
          process-locally lock-protected (same-process FD races,
          previously responsible for mid-file ``</lift>`` corruption,
          can't happen). Cross-process atomicity on URI awaits the
          daemon-side ``/v1/projects/<lang>/atomic_commit`` RPC
          (filed; not shipped).

        Indentation runs only when ``_indent_dirty`` is set — i.e. a
        new element has been added since the last save. This keeps
        text-only edits (the common case: writing a new audio
        filename into an existing <text>) bit-stable in git, instead
        of re-flowing the whole tree's whitespace on every save."""
        if self._indent_dirty:
            self._indent(self._root)
            self._indent_dirty = False
        with self.handle.atomic_open_write() as f:
            self._tree.write(f, encoding='utf-8', xml_declaration=True)

    @staticmethod
    def _indent(elem, level=0):
        """Add pretty-print indentation in-place (Python < 3.9 compat)."""
        pad = '\n' + '    ' * level
        if len(elem):
            if not elem.text or not elem.text.strip():
                elem.text = pad + '    '
            if not elem.tail or not elem.tail.strip():
                elem.tail = pad
            for child in elem:
                LIFTDatabase._indent(child, level + 1)
            if not child.tail or not child.tail.strip():
                child.tail = pad
        else:
            if level and (not elem.tail or not elem.tail.strip()):
                elem.tail = pad
        if not level:
            elem.tail = '\n'
