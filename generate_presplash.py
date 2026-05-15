"""
Generate presplash.png + multi-density variants.

Usage: python generate_presplash.py

Writes:
- presplash.png — the existing single-bitmap output that
  buildozer.spec's presplash.filename still points at. Kept so
  the current build isn't disturbed; preserved at 480x800 (the
  historical size).
- presplash_variants/drawable-{ldpi,mdpi,hdpi,xhdpi,xxhdpi,xxxhdpi}/
  presplash.png — Android density-bucket variants. Not yet wired
  into the build (android/ is shared across peers; right-way-to-
  ship-multi-density is a separate research item). Generated here
  so the per-bucket bitmaps are ready in the repo when the build
  hook lands.

Each variant renders at a scale relative to mdpi (160 dpi)
baseline: ldpi=0.75x, mdpi=1.0x, hdpi=1.5x, xhdpi=2.0x,
xxhdpi=3.0x, xxxhdpi=4.0x. Android picks the variant matching
the device's DPI at install time from the APK and discards the
rest.
"""

import os
import re
from PIL import Image, ImageDraw, ImageFont

from appinfo import APP_NAME, APP_TAGLINE, APP_ICON, FILE_W_VERSION
from azt_collab_client.ui.theme import BG_RGB, GREEN_RGB, TEXT_RGB

# mdpi baseline dimensions. The historical 480x800 PNG was rendered
# at hdpi (1.5x); 320x533 here is its 1x equivalent. All other
# render parameters (icon size, font sizes, paddings) are stated in
# mdpi units and multiplied by the bucket's scale at render time.
BASE_W, BASE_H = 320, 533

# Design constants in mdpi units (everything scales linearly).
# - ICON_SIZE > BASE_W on purpose: the icon overflows the canvas
#   slightly for the "full feel" the original 480x800 layout has.
ICON_SIZE_MDPI = 341
ICON_TOP_OFFSET_MDPI = 133
ICON_BASELINE_MDPI = 120
NAME_GAP_MDPI = 20
TAGLINE_GAP_MDPI = 37
VERSION_BOTTOM_MDPI = 40
NAME_FONT_PT_MDPI = 24
TAGLINE_FONT_PT_MDPI = 15
VERSION_FONT_PT_MDPI = 9

# Android density buckets: bucket name → scale factor relative to
# mdpi. Android's resource system picks the right bucket at install
# time based on the device's screen density.
DENSITY_BUCKETS = (
    ('ldpi',    0.75),
    ('mdpi',    1.0),
    ('hdpi',    1.5),
    ('xhdpi',   2.0),
    ('xxhdpi',  3.0),
    ('xxxhdpi', 4.0),
)


def read_version():
    """Read __version__ from main.py."""
    try:
        with open(FILE_W_VERSION) as f:
            for line in f:
                m = re.match(r"^__version__\s*=\s*['\"](.+?)['\"]", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return '?'


def _load_font(size, bold=False):
    names = [
        'fonts/CharisSIL-Bold.ttf' if bold else 'fonts/CharisSIL-Regular.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
        else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _render(scale, version):
    """Render one density variant. All sizes are mdpi-units * scale.
    Returns a PIL Image."""
    width = int(BASE_W * scale)
    height = int(BASE_H * scale)
    img = Image.new('RGBA', (width, height), BG_RGB)
    draw = ImageDraw.Draw(img)

    # Icon — paste centered, slightly oversized.
    icon_size = int(ICON_SIZE_MDPI * scale)
    try:
        icon = Image.open(APP_ICON).convert('RGBA').resize(
            (icon_size, icon_size), Image.LANCZOS)
        icon_x = (width - icon_size) // 2
        icon_y = (int(ICON_TOP_OFFSET_MDPI * scale)
                  - (icon_size - int(ICON_BASELINE_MDPI * scale)) // 2)
        img.paste(icon, (icon_x, icon_y), icon)
    except Exception as e:
        print(f'  warn: could not load icon: {e}')
        icon_y = int(ICON_TOP_OFFSET_MDPI * scale)
        icon_size = int(ICON_BASELINE_MDPI * scale)

    name_font = _load_font(int(NAME_FONT_PT_MDPI * scale), bold=True)
    tagline_font = _load_font(int(TAGLINE_FONT_PT_MDPI * scale))
    version_font = _load_font(int(VERSION_FONT_PT_MDPI * scale))

    name_y = icon_y + icon_size + int(NAME_GAP_MDPI * scale)
    bbox = draw.textbbox((0, 0), APP_NAME, font=name_font)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, name_y),
              APP_NAME, fill=GREEN_RGB, font=name_font)

    tag_y = name_y + int(TAGLINE_GAP_MDPI * scale)
    bbox = draw.textbbox((0, 0), APP_TAGLINE, font=tagline_font)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, tag_y),
              APP_TAGLINE, fill=TEXT_RGB, font=tagline_font)

    ver_display = 'v' + version
    version_y = height - int(VERSION_BOTTOM_MDPI * scale)
    bbox = draw.textbbox((0, 0), ver_display, font=version_font)
    vw = bbox[2] - bbox[0]
    draw.text(((width - vw) // 2, version_y), ver_display,
              fill=TEXT_RGB[:3] + (102,),  # off-white at ~40% alpha
              font=version_font)

    return img


def generate_variants(version):
    """Write the six density variants into presplash_variants/."""
    out_root = 'presplash_variants'
    for bucket, scale in DENSITY_BUCKETS:
        out_dir = os.path.join(out_root, f'drawable-{bucket}')
        os.makedirs(out_dir, exist_ok=True)
        img = _render(scale, version)
        path = os.path.join(out_dir, 'presplash.png')
        img.save(path)
        print(f'  {bucket}: {img.size[0]}x{img.size[1]} → {path}')


def generate():
    """Generate the legacy presplash.png (480x800, hdpi-equivalent
    at 1.5x scale) plus all six density variants. Returns version
    string."""
    version = read_version()

    # Legacy single-output target the existing buildozer.spec
    # points at. Same dimensions as the historical file.
    legacy = _render(1.5, version)
    legacy.save('presplash.png')
    print(f'Generated presplash.png ({legacy.size[0]}x'
          f'{legacy.size[1]}) for v{version}')

    print('Generating density variants in presplash_variants/:')
    generate_variants(version)
    return version


if __name__ == '__main__':
    generate()
