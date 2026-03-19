"""
lift.py — LIFT XML lexicon database reader/writer for LIFT Recorder.

Provides LIFTDatabase which:
  - Parses a .lift file into entry dicts
  - Exposes Entry.lc semantics: citation form with the audio lang tag
  - Writes audio filenames back to the XML (textvaluebylang pattern)

Key concepts matching kent-rasmussen/azt lift.py:
  - Entry.lc  = <citation> element
  - audiolang  = the lang tag of form {vernlang}-Zxxx-x-audio
  - Entry.lc.textvaluebylang(lang=audiolang) = text of that form
"""

import json
import os
import re
import ssl
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Register namespaces to avoid ns0: prefix on save
ET.register_namespace('', '')

# ── GitHub-hosted CAWL illustration images ───────────────────────────────────

_GITHUB_REPO = 'kent-rasmussen/images_CAWL'
_GITHUB_BRANCH = 'main'
_RAW_BASE = f'https://raw.githubusercontent.com/{_GITHUB_REPO}/{_GITHUB_BRANCH}'


class _CAWLImageResolver:
    """Resolves CAWL numbers to image URLs from kent-rasmussen/images_CAWL.

    Fetches the repo tree once via the GitHub API, caches the CAWL→URL
    mapping to a local JSON file so subsequent runs don't need network.
    """

    def __init__(self, cache_dir: str):
        self._cache_dir = cache_dir
        self._cache_file = os.path.join(cache_dir, '.cawl_image_urls.json')
        self._urls = None          # cawl str → raw URL
        self._lock = threading.Lock()

    def get_url(self, cawl: str) -> str:
        """Return a raw.githubusercontent.com URL for *cawl*, or ''."""
        if self._urls is None:
            self._load()
        return self._urls.get(cawl, '')

    # ── internals ────────────────────────────────────────────────────────

    def _load(self):
        with self._lock:
            if self._urls is not None:
                return

            # Try disk cache first
            if os.path.exists(self._cache_file):
                try:
                    with open(self._cache_file, 'r', encoding='utf-8') as f:
                        self._urls = json.load(f)
                    return
                except Exception:
                    pass

            # Fetch from GitHub
            self._urls = {}
            try:
                api_url = (
                    f'https://api.github.com/repos/{_GITHUB_REPO}'
                    f'/git/trees/{_GITHUB_BRANCH}?recursive=1'
                )
                req = urllib.request.Request(
                    api_url,
                    headers={'Accept': 'application/vnd.github.v3+json'},
                )
                ctx = ssl.create_default_context()
                try:
                    import certifi
                    ctx.load_verify_locations(certifi.where())
                except (ImportError, Exception):
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                    data = json.loads(resp.read())

                for item in data.get('tree', []):
                    if item['type'] != 'blob':
                        continue
                    path = item['path']
                    low = path.lower()
                    if not (low.endswith('.png') or low.endswith('.jpg')
                            or low.endswith('.jpeg')):
                        continue
                    parts = path.split('/')
                    if len(parts) != 2:
                        continue
                    cawl_num = parts[0].split('_')[0]
                    if cawl_num in self._urls:
                        continue  # keep first match per CAWL
                    encoded = '/'.join(
                        urllib.parse.quote(p, safe='') for p in parts
                    )
                    self._urls[cawl_num] = f'{_RAW_BASE}/{encoded}'

                # Persist to disk
                try:
                    os.makedirs(self._cache_dir, exist_ok=True)
                    with open(self._cache_file, 'w', encoding='utf-8') as f:
                        json.dump(self._urls, f)
                except OSError:
                    pass
            except Exception as e:
                print(f'Could not fetch CAWL image index: {e}')


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

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)
        self.images_dir = os.path.join(self.dir, 'images')
        self.audio_dir = os.path.join(self.dir, 'audio')
        self._image_resolver = _CAWLImageResolver(self.dir)

        self._tree = ET.parse(self.path)
        self._root = self._tree.getroot()

        self.vernlang = ''      # e.g. 'lol-x-his30100'
        self.audiolang = ''     # e.g. 'lol-x-his30100-Zxxx-x-audio'
        self.gloss_langs = []
        self.entries = []

        self._parse()

    def set_vernlang(self, code: str):
        """Set vernacular language code externally (e.g. from language picker)."""
        self.vernlang = code
        self.audiolang = code + '-Zxxx-x-audio'

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

            # CAWL
            if not cawl:
                for field_el in sense_el.findall('field'):
                    if field_el.get('type') == 'SILCAWL':
                        for form in field_el.findall('form'):
                            t = self._text(form)
                            if t:
                                cawl = t
                                break

            # Illustration
            if not illustration_href:
                ill_el = sense_el.find('illustration')
                if ill_el is not None:
                    illustration_href = ill_el.get('href', '')

        # Resolve image path: local images/ by href, then GitHub by CAWL
        image_path = ''
        if illustration_href:
            candidate = os.path.join(self.images_dir, illustration_href)
            if os.path.exists(candidate):
                image_path = candidate
        if not image_path and cawl:
            image_path = self._image_resolver.get_url(cawl)

        return {
            'guid': guid,
            'id': entry_id,
            'date_modified': date_modified,
            'headword': display_headword,
            'glosses': glosses,
            'cawl': cawl,
            'illustration_href': illustration_href,
            'image_path': image_path,
            'audio_filename': audio_filename,
            '_el': el,          # live reference for writing back
        }

    @staticmethod
    def _text(el) -> str:
        """Get trimmed text content of a <text> child, or direct text."""
        text_el = el.find('text')
        if text_el is not None:
            return (text_el.text or '').strip()
        return (el.text or '').strip()

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

        # Find or create <form lang=audiolang>
        audio_form = None
        for form in citation_el.findall('form'):
            if form.get('lang') == self.audiolang:
                audio_form = form
                break

        if audio_form is None:
            audio_form = ET.SubElement(citation_el, 'form')
            audio_form.set('lang', self.audiolang)

        # Set <text> child
        text_el = audio_form.find('text')
        if text_el is None:
            text_el = ET.SubElement(audio_form, 'text')
        text_el.text = filename

        # Save
        self._save()

    def _find_entry(self, guid: str):
        for e in self._root.findall('entry'):
            if e.get('guid') == guid:
                return e
        return None

    def _save(self):
        """Write updated XML back to the .lift file, preserving encoding."""
        self._indent(self._root)
        self._tree.write(
            self.path,
            encoding='utf-8',
            xml_declaration=True,
        )

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
