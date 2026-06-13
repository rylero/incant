"""
Generate the incant logo: a single clean "i" mark on a brand-gradient
rounded square. Flat shapes only (no blur/sparkle) so it stays crisp at 16px.

Run:  uv run --with pillow python assets/make_logo.py
Outputs: assets/incant.png (256), assets/incant_64.png, assets/incant.ico
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
S = 4                      # supersample factor (downscaled at the end)
N = 256 * S               # working size

# brand gradient (light blue)
TOP = (56, 189, 248)      # sky-400
BOT = (59, 130, 246)      # blue-500


def gradient(size: int, top, bot) -> Image.Image:
    base = Image.new("RGB", (size, size))
    px = base.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return base


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def build() -> Image.Image:
    img = gradient(N, TOP, BOT)
    img.putalpha(rounded_mask(N, int(N * 0.235)))

    draw = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)

    cx = N // 2
    bw = int(N * 0.12)            # stem / dot width

    # dot
    dot_cy = int(N * 0.30)
    dr = bw // 2
    draw.ellipse([cx - dr, dot_cy - dr, cx + dr, dot_cy + dr], fill=white)

    # stem (rounded vertical bar)
    stem_top = int(N * 0.44)
    stem_bot = int(N * 0.74)
    draw.rounded_rectangle([cx - bw // 2, stem_top, cx + bw // 2, stem_bot],
                           radius=bw // 2, fill=white)

    return img.resize((256, 256), Image.LANCZOS)


def main() -> None:
    icon = build()
    icon.save(HERE / "incant.png")
    icon.resize((64, 64), Image.LANCZOS).save(HERE / "incant_64.png")
    icon.save(HERE / "incant.ico",
              sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote incant.png, incant_64.png, incant.ico")


if __name__ == "__main__":
    main()
