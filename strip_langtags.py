#!/usr/bin/env python3
"""Build-time script: strip langtags.json down to a small gzipped lookup file."""
import gzip
import json
import os
import sys

SRC = os.path.join(os.path.dirname(__file__),
                   '..', '..', 'bin', 'raspy', 'azt', 'data', 'langtags.json')
# Also accept an explicit path as argv[1]
if len(sys.argv) > 1:
    SRC = sys.argv[1]

SRC = os.path.expanduser(SRC)
DEST = os.path.join(os.path.dirname(__file__),
                    'azt_collab_client', 'ui', 'assets',
                    'langtags_mini.json.gz')

KEEP = {
    'tag':       't',
    'name':      'n',
    'names':     'ns',
    'localname': 'ln',
    'localnames':'lns',
    'region':    'r',
    'regionname':'rn',
    'regions':   'rs',
    'iso639_3':  'i',
}

def main():
    with open(SRC, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Also extract region-code -> name mapping from entries
    region_names = {}
    for entry in data:
        if 'full' in entry and 'region' in entry and 'regionname' in entry:
            region_names[entry['region']] = entry['regionname']

    out = []
    for entry in data:
        tag = entry.get('tag', '')
        if tag.startswith('_') or 'full' not in entry:
            continue
        if not entry.get('name'):
            continue
        rec = {}
        for src_key, dst_key in KEEP.items():
            if src_key in entry:
                rec[dst_key] = entry[src_key]
        out.append(rec)

    blob = {
        'langs': out,
        'region_names': region_names,
    }

    raw = json.dumps(blob, ensure_ascii=False, separators=(',', ':'))
    compressed = gzip.compress(raw.encode('utf-8'), compresslevel=9)

    with open(DEST, 'wb') as f:
        f.write(compressed)

    print(f'Wrote {len(out)} language entries to {DEST}')
    print(f'  {len(region_names)} region-code-to-name mappings')
    print(f'  {len(raw)//1024} KB raw, {len(compressed)//1024} KB gzipped')

if __name__ == '__main__':
    main()
