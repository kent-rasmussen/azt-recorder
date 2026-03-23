"""
Default colour theme for A-Z+T Recorder.

Every colour used in the UI is defined here so that theming is a single-file
change.  Names follow a role-based convention (what the colour *does*, not
what it *looks like*).

Kivy KV files import this module with ``#:import T theme`` and reference
colours as e.g. ``T.BG``.  Python code uses ``from theme import …`` or
``import theme``.
"""

# ── Backgrounds ───────────────────────────────────────────────────────────────
BG            = (0.102,  0.0863, 0.0706, 1)      # #1a1612  dark earth
SURFACE       = (0.1647, 0.1373, 0.1255, 1)      # #2a2320  raised surface / inputs
SURFACE_ALT   = (0.2353, 0.1961, 0.1647, 1)      # #3c3229  lighter surface variant

# ── Text ──────────────────────────────────────────────────────────────────────
TEXT          = (0.9412, 0.9098, 0.8627, 1)       # #f0e8dc  primary (warm off-white)
TEXT_DIM      = (0.5412, 0.4784, 0.4157, 1)       # #8a7a6a  secondary (muted taupe)
TEXT_MID      = (0.7,    0.62,   0.55,   1)       # #b39e8c  mid-brightness
TEXT_FAINT    = (0.2902, 0.2275, 0.1647, 1)       # #4a3a2a  barely visible
HINT          = (0.5412, 0.4784, 0.4157, 0.6)    # TEXT_DIM at 60 % alpha

# ── Accents ───────────────────────────────────────────────────────────────────
ACCENT        = (0.7882, 0.4824, 0.2275, 1)       # #c97b3a  warm amber
GREEN         = (0.1529, 0.6824, 0.3765, 1)       # #27ae60  success / active
GREEN_BRIGHT  = (0.1529, 0.9,    0.3765, 1)       # #27e660  active-text highlight
GREEN_DARK    = (0.15,   0.35,   0.22,   1)       # #265939  active-toggle bg
RED           = (0.7529, 0.2235, 0.1686, 1)       # #c0392b  recording indicator
TEAL          = (0.1529, 0.5824, 0.5765, 1)       # #279593  teal accent
BLUE          = (0.2,    0.4,    0.7,    1)       # #3366b3  blue accent

# ── Interactive ───────────────────────────────────────────────────────────────
BTN_INACTIVE  = (0.3922, 0.3137, 0.2353, 1)       # #64503c  dim button face

# ── Overlays ──────────────────────────────────────────────────────────────────
TRANSPARENT   = (0, 0, 0, 0)
OVERLAY_DARK  = (0, 0, 0, 0.85)                   # heavy modal scrim
OVERLAY       = (0, 0, 0, 0.7)                    # standard modal scrim

# ── Pillow / integer-RGB helpers (0-255) ──────────────────────────────────────
BG_RGB        = (26,  22,  18,  255)               # BG as RGBA-255
GREEN_RGB     = (39,  174, 96)                     # GREEN as RGB-255
TEXT_RGB      = (240, 232, 220)                    # TEXT as RGB-255
