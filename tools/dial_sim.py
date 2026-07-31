#!/usr/bin/env python3
"""
dial_sim.py – Render deloop's actual dial_ui.py off-device, as PNGs.

Runs the real src/dial_ui.py unmodified by stubbing just enough of
CircuitPython's board/displayio/terminalio/adafruit_display_text/
adafruit_bitmap_font surface for it to import and draw. Every arc, tick,
pointer and fill comes from dial_ui.py's own drawing code -- deliberately
not registering a `bitmaptools` module means dial_ui.py falls back to its
own pure-Python primitives (the same ones it uses on a device that lacks
bitmaptools), so nothing here reimplements the rendering math.

Text is the one place this can't be pixel-identical to the device: labels
are rendered with the same Inter-Medium.ttf the on-device .pcf bitmap
fonts are generated from (see `make fonts`), via Pillow, at the same point
sizes -- close, not byte-for-byte, since FreeType's rasterizer isn't
otf2bdf's. Good enough to show the actual design; not a pixel-diff tool.

Not simulated: the boot splash (show_splash/flash_power_on pull in
vectorio/adafruit_imageload, which aren't stubbed here) and the mute/power
breathing animations (this renders single frames, not a timeline).

Requires Pillow: pip install pillow
Run from the repo root: python tools/dial_sim.py
"""
import re
import sys
import types
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageEnhance

ROOT      = Path(__file__).resolve().parent.parent
SRC       = ROOT / "src"
# Inter-4.1 (the font family download) and renders/ (this script's output)
# both live in local/ -- gitignored, not shipped with the project. Grab Inter
# v4.1 from https://github.com/rsms/inter/releases and extract it there if
# you need to (re)run this or `make fonts`.
LOCAL     = ROOT / "local"
TTF_PATH  = LOCAL / "Inter-4.1" / "extras" / "ttf" / "Inter-Medium.ttf"
OUT_DIR   = LOCAL / "renders"
SCREEN    = 240


# ---------------------------------------------------------------------------
# Minimal displayio-alike primitives
# ---------------------------------------------------------------------------

class _FakeBitmap:
    """Backs dial_ui.py's `bmp[x, y] = idx` / `bmp[x, y]` pixel access with
    a flat bytearray -- fast to fill, and trivial to hand to Pillow as a
    palette-mode image at export time."""

    def __init__(self, width, height, value_count):
        self.width  = width
        self.height = height
        self._data  = bytearray(width * height)

    def __setitem__(self, key, value):
        x, y = key
        self._data[y * self.width + x] = value & 0xFF

    def __getitem__(self, key):
        x, y = key
        return self._data[y * self.width + x]

    def to_image(self, palette):
        img = Image.frombytes("P", (self.width, self.height), bytes(self._data))
        img.putpalette(palette.flat_rgb())
        return img.convert("RGB")


class _FakePalette:
    def __init__(self, n):
        self._colors = [0] * n

    def __setitem__(self, i, color):
        self._colors[i] = color

    def __getitem__(self, i):
        return self._colors[i]

    def flat_rgb(self):
        flat = []
        for c in self._colors:
            flat.extend([(c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF])
        return flat


class _FakeGroup:
    def __init__(self):
        self._children = []

    def append(self, child):
        self._children.append(child)


class _FakeTileGrid:
    def __init__(self, bitmap, pixel_shader=None, x=0, y=0):
        self.bitmap       = bitmap
        self.pixel_shader = pixel_shader
        self.x, self.y    = x, y


class _FakeLabel:
    def __init__(self, font, text="", color=0xFFFFFF,
                 anchor_point=(0.0, 0.0), anchored_position=(0, 0)):
        self.font              = font
        self.text              = text
        self.color             = color
        self.anchor_point      = anchor_point
        self.anchored_position = anchored_position


class _FakeDisplay:
    def __init__(self, width, height):
        self.width       = width
        self.height      = height
        self.brightness  = 1.0
        self.auto_refresh = True
        self.root_group  = None

    def refresh(self):
        pass   # export() reads state directly; nothing to push anywhere


# ---------------------------------------------------------------------------
# Font shim -- maps on-device .pcf paths to the source Inter TTF
# ---------------------------------------------------------------------------

class _FakeFont:
    def __init__(self, pil_font):
        self.pil_font = pil_font

    def load_glyphs(self, glyphs):
        pass   # TTFs don't need glyph pre-caching


_PCF_SIZE_RE = re.compile(r"_(\d+)\.pcf$")


def _load_font_shim(path):
    m = _PCF_SIZE_RE.search(path)
    size = int(m.group(1)) if m else 20
    return _FakeFont(ImageFont.truetype(str(TTF_PATH), size))


# ---------------------------------------------------------------------------
# Install shims, then import the real dial_ui.py
# ---------------------------------------------------------------------------

def _module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _install_shims():
    sys.modules["board"] = _module("board", DISPLAY=_FakeDisplay(SCREEN, SCREEN))
    sys.modules["displayio"] = _module(
        "displayio",
        Bitmap=_FakeBitmap, Palette=_FakePalette,
        Group=_FakeGroup, TileGrid=_FakeTileGrid,
    )
    sys.modules["terminalio"] = _module("terminalio", FONT=None)

    label_mod = _module("adafruit_display_text.label", Label=_FakeLabel)
    sys.modules["adafruit_display_text"] = _module("adafruit_display_text", label=label_mod)
    sys.modules["adafruit_display_text.label"] = label_mod

    bf_mod = _module("adafruit_bitmap_font.bitmap_font", load_font=_load_font_shim)
    sys.modules["adafruit_bitmap_font"] = _module("adafruit_bitmap_font", bitmap_font=bf_mod)
    sys.modules["adafruit_bitmap_font.bitmap_font"] = bf_mod

    # bitmaptools deliberately NOT registered: dial_ui.py's
    # `import bitmaptools as _bt` raises ImportError -> _BT = False -> its
    # own pure-Python fallback drawing primitives run, unmodified.

    sys.path.insert(0, str(SRC))


_install_shims()
import dial_ui                     # noqa: E402  (must follow shim install)
from state import AVRState         # noqa: E402


# ---------------------------------------------------------------------------
# Compositing: walk the group Label/TileGrid children into one RGB image
# ---------------------------------------------------------------------------

def _draw_label(draw, label):
    if not label.text:
        return
    font = label.font.pil_font
    bbox = draw.multiline_textbbox((0, 0), label.text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    ax, ay = label.anchor_point
    px, py = label.anchored_position
    x = px - ax * w - bbox[0]
    y = py - ay * h - bbox[1]
    c = label.color
    fill = ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)
    draw.multiline_text((x, y), label.text, font=font, fill=fill)


def render(ui):
    """Composite the current ui state into an RGB image, brightness applied."""
    img = Image.new("RGB", (SCREEN, SCREEN), (0, 0, 0))
    for child in ui["display"].root_group._children:
        if isinstance(child, _FakeTileGrid):
            tile = child.bitmap.to_image(child.pixel_shader)
            img.paste(tile, (child.x, child.y))
        elif isinstance(child, _FakeLabel):
            _draw_label(ImageDraw.Draw(img), child)

    brightness = getattr(ui["display"], "brightness", 1.0)
    if brightness < 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    return img


# The M5 Dial's glass is round, not square -- SCREEN x SCREEN is just the
# bounding box dial_ui.py draws into. Mask the square's corners to a neutral
# gray and pad by FRAME_PAD so the round glass reads clearly with a margin
# around it -- white was too much contrast against the mostly-black UI.
FRAME_PAD  = 10
FRAME_GRAY = (40, 40, 40)


def _circle_mask(size):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    return mask


_ROUND_MASK = _circle_mask(SCREEN)


def frame(img):
    canvas = Image.new("RGB", (SCREEN + 2 * FRAME_PAD, SCREEN + 2 * FRAME_PAD), FRAME_GRAY)
    canvas.paste(img, (FRAME_PAD, FRAME_PAD), mask=_ROUND_MASK)
    return canvas


def save(ui, name):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    frame(render(ui)).save(path)
    print(f"  {path.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def _base_state():
    state = AVRState()
    state.power          = "ON"
    state.brightness     = 1.0
    state.input          = "SAT/CBL"
    state.preset         = "2"
    state.preset_names   = [("2", "Movie"), ("3", "Music"), ("4", "Night")]
    # Mirrors driver.get_quick_presets()'s default fallback (reuse the full
    # list) -- denon.py/minidsp.py don't define their own, so this is what
    # they'd actually show on real hardware. Without this, dial_ui.py's
    # button row silently renders empty (state.preset_quick_names defaults
    # to [] in AVRState.__init__) -- a real regression this fixture hit the
    # first time camilladsp.py's separate quick-preset list was added.
    state.preset_quick_names = list(state.preset_names)
    state.preset_enabled = True
    return state


def main():
    print("Rendering deloop screens to", OUT_DIR.relative_to(ROOT))
    ui = dial_ui.init()

    state = _base_state()
    state.volume_db = -20.5
    dial_ui.draw_main(ui, state)
    save(ui, "main_normal")

    state.volume_db = -4.0
    dial_ui.draw_main(ui, state)
    save(ui, "main_loud")

    state.volume_db = -45.0
    dial_ui.draw_main(ui, state)
    save(ui, "main_quiet")

    state.volume_db = -20.5
    state.muted = True
    dial_ui.draw_main(ui, state)          # static muted frame (no live pulse here)
    save(ui, "main_muted")
    state.muted = False

    state.preset_enabled = False          # tapped the active slot to disable it in place
    dial_ui.draw_main(ui, state)
    save(ui, "main_preset_disabled")
    state.preset_enabled = True

    dial_ui.draw_status(ui, "connecting to wifi...")
    save(ui, "status_connecting")

    dial_ui.draw_error(ui, "Reconnecting...")
    save(ui, "error")

    state.power = "STANDBY"
    dial_ui.draw_main(ui, state)          # static standby ring, dimmed brightness
    save(ui, "power_off")
    state.power = "ON"
    # draw_main's power-ON path is what normally restores this; the menu
    # scenarios below don't call draw_main, so restore it explicitly here.
    ui["display"].brightness = state.brightness

    dial_ui.draw_menu(ui, "", ["Input", "Dirac Live", "Device"], 1, clear_bg=True)
    save(ui, "menu_top")

    dial_ui.draw_menu(
        ui, "INPUT",
        ["Apple TV", "Cable Box", "Blu-ray", "Game Console", "PC"],
        1, clear_bg=True,
    )
    save(ui, "menu_input")

    dial_ui.draw_menu(
        ui, "BRIGHTNESS",
        ["            72 %\n(rotate to adjust)"],
        0, clear_bg=True,
    )
    save(ui, "menu_brightness")

    print("Done.")


if __name__ == "__main__":
    main()
