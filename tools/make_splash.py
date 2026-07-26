#!/usr/bin/env python3
"""
make_splash.py  –  Convert ui/hobbysprawl.png to a CircuitPython-compatible
                   4-bit indexed BMP for the splash screen.

Transforms orange-on-white → orange-on-black and resizes to 90×90 px.
Run once; output lands in src/splash_logo.bmp.

Requires Pillow:  pip install pillow
"""
import os
import struct
from pathlib import Path
from PIL import Image

SRC   = Path("ui/hobbysprawl.png")
DST   = Path("src/splash_logo.bmp")
SIZE  = 90          # pixels (square)
BLACK = (0, 0, 0)
ORG   = (255, 140, 0)   # close match for hobbysprawl orange


def write_4bit_bmp(img_p: Image.Image, path: Path):
    """Write a 4-bit-per-pixel Windows BMP with up to 16 palette entries."""
    assert img_p.mode == "P", "image must be palette mode"
    w, h = img_p.size
    pal = img_p.getpalette()          # flat RGB list, 256×3 entries
    n_colors = 16

    # BMP stores rows bottom-up, each row padded to 4-byte boundary
    row_size = ((w + 1) // 2 + 3) & ~3   # bytes per row (4-bit pixels, 4-byte aligned)
    pixel_data_size = row_size * h
    palette_size    = n_colors * 4        # RGBX quads
    header_size     = 14 + 40            # BITMAPFILEHEADER + BITMAPINFOHEADER
    file_size       = header_size + palette_size + pixel_data_size
    pixel_offset    = header_size + palette_size

    with open(path, "wb") as f:
        # BITMAPFILEHEADER (14 bytes)
        f.write(b"BM")
        f.write(struct.pack("<I", file_size))
        f.write(struct.pack("<HH", 0, 0))       # reserved
        f.write(struct.pack("<I", pixel_offset))

        # BITMAPINFOHEADER (40 bytes)
        f.write(struct.pack("<I", 40))           # header size
        f.write(struct.pack("<i", w))
        f.write(struct.pack("<i", -h))           # negative = top-down rows
        f.write(struct.pack("<H", 1))            # colour planes
        f.write(struct.pack("<H", 4))            # bits per pixel
        f.write(struct.pack("<I", 0))            # no compression
        f.write(struct.pack("<I", pixel_data_size))
        f.write(struct.pack("<i", 2835))         # X pixels/meter (72 DPI)
        f.write(struct.pack("<i", 2835))
        f.write(struct.pack("<I", n_colors))
        f.write(struct.pack("<I", n_colors))

        # Colour table: 16 BGRA quads
        for i in range(n_colors):
            r = pal[i * 3];  g = pal[i * 3 + 1];  b = pal[i * 3 + 2]
            f.write(bytes([b, g, r, 0]))

        # Pixel data (two 4-bit indices per byte, row-padded)
        px = img_p.load()
        for y in range(h):
            row = bytearray(row_size)
            for x in range(w):
                idx = px[x, y] & 0x0F           # clamp to 16 colours
                byte_pos = x // 2
                if x % 2 == 0:
                    row[byte_pos] = idx << 4
                else:
                    row[byte_pos] |= idx
            f.write(bytes(row))


def main():
    img = Image.open(SRC).convert("RGB")

    # Replace near-white pixels (background) with black
    pix = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b = pix[x, y]
            if r > 180 and g > 180 and b > 180:
                pix[x, y] = BLACK

    # Resize
    img = img.resize((SIZE, SIZE), Image.LANCZOS)

    # Quantise to 16 colours (black + orange shades from anti-aliasing)
    img_p = img.quantize(colors=16)

    DST.parent.mkdir(parents=True, exist_ok=True)
    write_4bit_bmp(img_p, DST)
    kb = DST.stat().st_size / 1024
    print(f"Wrote {DST}  ({SIZE}×{SIZE} px, 16 colours, {kb:.1f} KB)")


if __name__ == "__main__":
    main()
