"""
imghdr compatibility shim for Python 3.13+ (module removed in PEP 594).
Provides imghdr.what() used by Kivy 2.3.0's core/image/__init__.py.
"""


def what(file, h=None):
    """Return image type string ('png', 'jpeg', 'gif', 'bmp', 'webp', 'tiff'),
    or None if the type cannot be determined."""
    if h is None:
        if isinstance(file, (str, bytes)):
            with open(file, 'rb') as f:
                h = f.read(32)
        else:
            loc = file.tell()
            h = file.read(32)
            file.seek(loc)

    if len(h) < 4:
        return None
    if h[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if h[:2] == b'\xff\xd8':
        return 'jpeg'
    if h[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if h[:2] == b'BM':
        return 'bmp'
    if h[:4] == b'RIFF' and h[8:12] == b'WEBP':
        return 'webp'
    if h[:4] in (b'MM\x00\x2a', b'II\x2a\x00'):
        return 'tiff'
    return None
