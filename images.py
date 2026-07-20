"""
Generates the card graphics served to Google Wallet:

  stamp_banner(store, filled) -> PNG bytes  (the hero image that fills with stamps)
  logo_placeholder(store)     -> PNG bytes  (a fallback logo if no real one exists)

Fonts are bundled in assets/ so text renders the same locally and when deployed.
"""

import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

ASSETS = os.path.join(os.path.dirname(__file__), "assets")
WHITE = (255, 255, 255)


def _font(size: int, bold: bool = True):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(os.path.join(ASSETS, name), size)
    except Exception:
        return ImageFont.load_default()


def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(c1, c2, t):
    """Blend two colours; t=0 -> c1, t=1 -> c2."""
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def stamp_banner(store: dict, filled: int) -> bytes:
    """A 3:1 hero banner showing a row of stamps, `filled` of them stamped."""
    goal = store["reward_goal"]
    filled = max(0, min(filled, goal))
    brand = _hex(store["brand_color"])
    ready = filled >= goal

    W, H = 1032, 336
    img = Image.new("RGB", (W, H), _mix(brand, WHITE, 0.93))  # light brand tint
    d = ImageDraw.Draw(img)

    # Caption + subtitle
    title = "Reward ready!" if ready else f"{filled} of {goal}"
    d.text((W / 2, 54), title, font=_font(58, True), fill=brand, anchor="mm")
    sub = store.get("reward_text", "")
    if sub:
        d.text((W / 2, 104), sub, font=_font(28, False),
               fill=_mix(brand, WHITE, 0.4), anchor="mm")

    # Stamp circles (one row up to 6, otherwise two rows)
    if goal <= 6:
        rows = [list(range(goal))]
    else:
        half = math.ceil(goal / 2)
        rows = [list(range(half)), list(range(half, goal))]

    margin, gap, max_d = 90, 26, 118
    per = max(len(r) for r in rows)
    diam = min(max_d, (W - 2 * margin - (per - 1) * gap) / per)
    row_gap = 26
    top, bottom = 150, H - 34
    total_h = len(rows) * diam + (len(rows) - 1) * row_gap
    y0 = top + ((bottom - top) - total_h) / 2

    idx = 0
    for r, row in enumerate(rows):
        rw = len(row) * diam + (len(row) - 1) * gap
        x0 = (W - rw) / 2
        cy = y0 + r * (diam + row_gap) + diam / 2
        for i in range(len(row)):
            cx = x0 + i * (diam + gap) + diam / 2
            box = [cx - diam / 2, cy - diam / 2, cx + diam / 2, cy + diam / 2]
            if idx < filled:
                d.ellipse(box, fill=brand)
                cw = diam * 0.30
                d.line([(cx - cw * 0.6, cy + cw * 0.05),
                        (cx - cw * 0.1, cy + cw * 0.55),
                        (cx + cw * 0.7, cy - cw * 0.5)],
                       fill=WHITE, width=max(6, int(diam * 0.07)), joint="curve")
            else:
                d.ellipse(box, outline=_mix(brand, WHITE, 0.5),
                          width=max(4, int(diam * 0.045)))
            idx += 1

    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def logo_placeholder(store: dict) -> bytes:
    """A simple branded logo (coloured disc + initials) used when no real logo
    file has been provided. Google masks program logos into a circle."""
    brand = _hex(store["brand_color"])
    S = 400
    img = Image.new("RGB", (S, S), brand)
    d = ImageDraw.Draw(img)
    initials = "".join(w[0] for w in store["name"].split()[:2]).upper() or "?"
    d.text((S / 2, S / 2 - 6), initials, font=_font(170, True), fill=WHITE, anchor="mm")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
