"""
populate_images.py — copy canonical illustration images into a LIFT project.

For each entry in a .lift file that has a CAWL number, this script finds the
matching image in an AZT-style image cache (directories named NNNN_concept/)
and copies it into the project's images/ directory, then writes the href back
into the LIFT XML.

Selection rule (fully automatic, no curation needed):
  1. Prefer the file whose name contains '__' (the canonical/default variant).
  2. If no '__' file exists, use the first file alphabetically.
  3. If the directory is empty or missing, skip (entry keeps its existing href).

Usage
-----
    python3 populate_images.py path/to/lexicon.lift path/to/toselect/

    --dry-run   print what would happen without copying or writing XML
    --overwrite replace illustrations that already have an href set
"""

import argparse
import glob
import os
import shutil
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lift import LIFTDatabase


def find_cache_dir(cache_root, cawl):
    """Return the cache directory for a given zero-padded CAWL number, or None."""
    pattern = os.path.join(cache_root, f'{cawl}_*')
    matches = [p for p in glob.glob(pattern) if os.path.isdir(p)]
    return matches[0] if matches else None


def pick_image(cache_dir):
    """
    Return the best image path from cache_dir, or None if directory is empty.
    Preference: file whose basename contains '__'; fallback: first alphabetically.
    """
    files = sorted(
        f for f in os.listdir(cache_dir)
        if os.path.isfile(os.path.join(cache_dir, f))
        and not f.startswith('.')
    )
    if not files:
        return None
    preferred = [f for f in files if '__' in f]
    chosen = preferred[0] if preferred else files[0]
    return os.path.join(cache_dir, chosen)


def populate(lift_path, cache_root, dry_run=False, overwrite=False):
    db = LIFTDatabase(lift_path)
    images_dir = db.images_dir
    if not dry_run:
        os.makedirs(images_dir, exist_ok=True)

    tree = ET.parse(lift_path)
    root = tree.getroot()
    # Build a guid→entry-element map for XML write-back
    entry_map = {el.get('guid'): el for el in root.iter('entry')}

    copied = skipped_no_cawl = skipped_existing = skipped_no_image = 0

    for entry in db.entries:
        cawl = entry.get('cawl', '').strip()
        if not cawl:
            skipped_no_cawl += 1
            continue

        # Skip if already has an illustration and --overwrite not set
        if entry.get('illustration_href') and not overwrite:
            skipped_existing += 1
            continue

        cache_dir = find_cache_dir(cache_root, cawl)
        if cache_dir is None:
            skipped_no_image += 1
            continue

        src = pick_image(cache_dir)
        if src is None:
            skipped_no_image += 1
            continue

        dest_name = f'{cawl}.png'
        dest_path = os.path.join(images_dir, dest_name)

        if dry_run:
            print(f'[dry] {cawl}  {os.path.basename(src)} → images/{dest_name}')
        else:
            shutil.copy2(src, dest_path)

        # Write href into LIFT XML
        guid = entry.get('guid')
        el = entry_map.get(guid)
        if el is not None:
            sense = el.find('sense')
            if sense is None:
                sense = ET.SubElement(el, 'sense')
            illus = sense.find('illustration')
            if illus is None:
                illus = ET.SubElement(sense, 'illustration')
            illus.set('href', dest_name)

        copied += 1

    if not dry_run and copied:
        db._indent(root)
        tree.write(lift_path, encoding='unicode', xml_declaration=True)

    print(f'\nDone.  copied={copied}  '
          f'skipped(existing={skipped_existing}  '
          f'no_cawl={skipped_no_cawl}  '
          f'no_image={skipped_no_image})')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('lift_file',  help='Path to .lift file')
    ap.add_argument('cache_root', help='Path to AZT image cache (toselect/ directory)')
    ap.add_argument('--dry-run',  action='store_true',
                    help='Print plan without copying files or modifying XML')
    ap.add_argument('--overwrite', action='store_true',
                    help='Replace illustrations that already have an href')
    args = ap.parse_args()

    populate(
        os.path.abspath(args.lift_file),
        os.path.abspath(args.cache_root),
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )
