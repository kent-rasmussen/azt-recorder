"""
Generate presplash.png with icon, app name, tagline, and version.

Usage: python generate_presplash.py
"""

import re
from PIL import Image, ImageDraw, ImageFont

from appinfo import APP_NAME, APP_TAGLINE

BG_COLOR = (26, 22, 18, 255)          # #1a1612
PRIMARY = (39, 174, 96)               # green accent
TEXT_LIGHT = (240, 232, 220)           # warm off-white

WIDTH, HEIGHT = 480, 800


def read_version():
    """Read __version__ from main.py."""
    try:
        with open('main.py') as f:
            for line in f:
                m = re.match(r"^__version__\s*=\s*['\"](.+?)['\"]", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return '?'


def generate():
    """Generate presplash.png. Returns version string."""
    version = read_version()

    img = Image.new('RGBA', (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Load icon and paste centered
    try:
        icon = Image.open('icons/icon_dark.png').convert('RGBA')
        icon_size = 512
        icon = icon.resize((icon_size, icon_size), Image.LANCZOS)
        icon_x = (WIDTH - icon_size) // 2
        icon_y = 200 - (icon_size - 180 ) //2
        img.paste(icon, (icon_x, icon_y), icon)
    except Exception as e:
        print(f'Warning: could not load icon: {e}')
        icon_y = 200
        icon_size = 180

    # Load fonts
    def load_font(size, bold=False):
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

    name_font = load_font(36, bold=True)
    tagline_font = load_font(22)
    version_font = load_font(14)

    # App name
    name_y = icon_y + icon_size + 30
    bbox = draw.textbbox((0, 0), APP_NAME, font=name_font)
    tw = bbox[2] - bbox[0]
    draw.text(((WIDTH - tw) // 2, name_y), APP_NAME, fill=PRIMARY, font=name_font)

    # Tagline
    tag_y = name_y + 55
    bbox = draw.textbbox((0, 0), APP_TAGLINE, font=tagline_font)
    tw = bbox[2] - bbox[0]
    draw.text(((WIDTH - tw) // 2, tag_y), APP_TAGLINE, fill=TEXT_LIGHT, font=tagline_font)

    # Version
    ver_display = 'v' + version
    version_y = HEIGHT - 60
    bbox = draw.textbbox((0, 0), ver_display, font=version_font)
    vw = bbox[2] - bbox[0]
    draw.text(((WIDTH - vw) // 2, version_y), ver_display,
              fill=TEXT_LIGHT[:3] + (102,), font=version_font)

    img.save('presplash.png')
    print(f'Generated presplash.png for v{version}')
    return version


if __name__ == '__main__':
    generate()
