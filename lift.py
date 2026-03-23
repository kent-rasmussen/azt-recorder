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

_DEFAULT_IMAGE_REPO = 'kent-rasmussen/images_CAWL'
_GITHUB_BRANCH = 'main'
_IMAGE_CACHE_VERSION = 2  # bump to invalidate stale caches


class _CAWLImageResolver:
    """Resolves CAWL numbers to image URLs from a GitHub image repo.

    Fetches the repo tree once via the GitHub API, caches the CAWL→URL
    mapping to a local JSON file so subsequent runs don't need network.
    """

    def __init__(self, cache_dir: str, repo: str = ''):
        self._cache_dir = cache_dir
        # Accept full URL (https://github.com/owner/repo) or owner/repo shorthand
        raw = repo.strip() if repo else ''
        if raw.startswith('https://github.com/'):
            # Extract owner/repo from URL
            parts = raw.rstrip('/').replace('https://github.com/', '').split('/')
            self._repo = '/'.join(parts[:2]) if len(parts) >= 2 else _DEFAULT_IMAGE_REPO
        elif raw:
            self._repo = raw
        else:
            self._repo = _DEFAULT_IMAGE_REPO
        self._raw_base = f'https://raw.githubusercontent.com/{self._repo}/{_GITHUB_BRANCH}'
        self._cache_file = os.path.join(cache_dir, '.cawl_image_urls.json')
        self._urls = None          # cawl str → first raw URL
        self._all_urls = None      # cawl str → [all raw URLs]
        self._lock = threading.Lock()

    def _normalize_cawl(self, cawl: str) -> str:
        """Return the key form of *cawl* that exists in self._urls, or ''."""
        if cawl in self._urls:
            return cawl
        try:
            padded = str(int(cawl)).zfill(4)
            if padded in self._urls:
                return padded
        except ValueError:
            pass
        stripped = cawl.lstrip('0') or '0'
        if stripped in self._urls:
            return stripped
        return ''

    def get_url(self, cawl: str) -> str:
        """Return a raw.githubusercontent.com URL for *cawl*, or ''."""
        if self._urls is None:
            self._load()
        key = self._normalize_cawl(cawl)
        return self._urls.get(key, '')

    def get_all_urls(self, cawl: str) -> list:
        """Return all raw.githubusercontent.com URLs for *cawl*."""
        if self._all_urls is None:
            self._load()
        key = self._normalize_cawl(cawl)
        return self._all_urls.get(key, [])

    # ── internals ────────────────────────────────────────────────────────

    def _load(self):
        with self._lock:
            if self._urls is not None:
                return

            # Try disk cache first
            if os.path.exists(self._cache_file):
                try:
                    with open(self._cache_file, 'r', encoding='utf-8') as f:
                        cached = json.load(f)
                    if (isinstance(cached, dict) and 'all' in cached
                            and cached.get('v') == _IMAGE_CACHE_VERSION):
                        self._urls = cached['first']
                        self._all_urls = cached['all']
                        return
                except Exception:
                    pass

            # Fetch from GitHub
            self._urls = {}
            self._all_urls = {}
            try:
                api_url = (
                    f'https://api.github.com/repos/{self._repo}'
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
                    parts = path.split('/')
                    if len(parts) == 1:
                        # Root-level file: 0001.png, 0038.jpg
                        filename = parts[0]
                    elif len(parts) == 2:
                        # Subdirectory: 0001_word/image.png
                        filename = parts[1]
                    else:
                        continue
                    # Skip non-image files (e.g. .txt); accept
                    # .png/.jpg/.jpeg and extensionless files (common)
                    low = filename.lower()
                    if '.' in low and not (low.endswith('.png')
                            or low.endswith('.jpg')
                            or low.endswith('.jpeg')):
                        continue
                    # Extract CAWL number from first path component
                    cawl_num = parts[0].split('_')[0]
                    # Strip extension for root-level files (0001.png → 0001)
                    if len(parts) == 1 and '.' in cawl_num:
                        cawl_num = cawl_num.rsplit('.', 1)[0]
                    encoded = '/'.join(
                        urllib.parse.quote(p, safe='') for p in parts
                    )
                    url = f'{self._raw_base}/{encoded}'
                    # Prefer files with __ in filename (generic/default image)
                    is_default = '__' in filename
                    if cawl_num not in self._urls:
                        self._urls[cawl_num] = url
                    elif is_default and '__' not in self._urls[cawl_num].split('/')[-1]:
                        self._urls[cawl_num] = url
                    # Put __ files first in the all-urls list
                    if is_default:
                        self._all_urls.setdefault(cawl_num, []).insert(0, url)
                    else:
                        self._all_urls.setdefault(cawl_num, []).append(url)

                # Persist to disk
                try:
                    os.makedirs(self._cache_dir, exist_ok=True)
                    with open(self._cache_file, 'w', encoding='utf-8') as f:
                        json.dump({'v': _IMAGE_CACHE_VERSION,
                              'first': self._urls, 'all': self._all_urls}, f)
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

    def __init__(self, path: str, image_repo: str = '', image_cache_dir: str = ''):
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)
        self.images_dir = os.path.join(self.dir, 'images')
        self.audio_dir = os.path.join(self.dir, 'audio')
        self.image_repo = image_repo
        self.image_cache_dir = image_cache_dir
        self._image_resolver = _CAWLImageResolver(self.dir, repo=image_repo)

        self._tree = ET.parse(self.path)
        self._root = self._tree.getroot()

        self.vernlang = ''      # e.g. 'lol-x-his30100'
        self.audiolang = ''     # e.g. 'lol-x-his30100-Zxxx-x-audio'
        self.gloss_langs = []
        self.list_type = ''     # e.g. 'SILCAWL' — from entry.field/@type
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

        # Extract list_type from first entry that has a field/@type
        if not self.list_type:
            for e in raw_entries:
                ft = e.get('_field_type', '')
                if ft:
                    self.list_type = ft
                    break

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
        field_type = ''
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

            # CAWL / field type
            if not cawl:
                for field_el in sense_el.findall('field'):
                    ft = field_el.get('type', '')
                    if ft:
                        field_type = ft
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

        # Resolve image path: local images/ → cache → remote URL
        image_path = ''
        if illustration_href:
            candidate = os.path.join(self.images_dir, illustration_href)
            if os.path.exists(candidate):
                image_path = candidate
        if not image_path and cawl:
            # Check cache before falling back to remote URL
            if self.image_cache_dir:
                cached = self._cached_image_path(cawl)
                if cached:
                    image_path = cached
            if not image_path:
                image_path = self._image_resolver.get_url(cawl)

        return {
            'guid': guid,
            'id': entry_id,
            'date_modified': date_modified,
            'headword': display_headword,
            'glosses': glosses,
            'cawl': cawl,
            '_field_type': field_type,
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

    def _cached_image_path(self, cawl):
        """Return path to a cached image for *cawl*, or '' if not cached."""
        if not self.image_cache_dir:
            return ''
        # Try exact cawl, zero-padded, and stripped forms
        candidates = [cawl]
        try:
            candidates.append(str(int(cawl)).zfill(4))
        except ValueError:
            pass
        candidates.append(cawl.lstrip('0') or '0')
        for c in dict.fromkeys(candidates):  # dedup preserving order
            for ext in ('.png', '.jpg', '.jpeg'):
                path = os.path.join(self.image_cache_dir, c + ext)
                if os.path.exists(path):
                    return path
        return ''

    def all_cawl_urls(self):
        """Return dict of cawl → first URL for all entries (for pre-fetching)."""
        if self._image_resolver._urls is None:
            self._image_resolver._load()
        return dict(self._image_resolver._urls) if self._image_resolver._urls else {}

    # ── Image helpers ──────────────────────────────────────────────────────

    def all_image_urls(self, entry):
        """Return list of all CAWL image URLs for *entry*."""
        cawl = entry.get('cawl', '')
        if not cawl:
            return []
        return self._image_resolver.get_all_urls(cawl)

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
