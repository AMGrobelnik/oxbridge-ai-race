"""The 1200x630 share-card PNG (Open Graph / Twitter) for a card dict from growth.market_card / growth.agent_card. Needs Pillow."""

import io
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .growth import NAME, TAGLINE

W, H = 1200, 630
GREEN, INK, MUTED, SOFT, LINE, RED = "#2b8a3e", "#1a1a1a", "#6b6b6b", "#f5f7f4", "#d8ded6", "#c92a2a"
FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/dejavu", "/Library/Fonts")
FONT_FILES = {False: ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf"), True: ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf")}
CACHE_MAX = 128
_cache: "OrderedDict[tuple, bytes]" = OrderedDict()


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        for name in FONT_FILES[bold]:
            if (Path(d) / name).exists():
                return ImageFont.truetype(str(Path(d) / name), size)
    return ImageFont.load_default(size)


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.ImageFont | ImageFont.FreeTypeFont, width: int, max_lines: int) -> list[str]:
    """Greedy word wrap to `width` pixels; the last allowed line ends with an ellipsis if text remains."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=f) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    if len(lines) <= max_lines:
        return lines
    last = lines[max_lines - 1]
    while draw.textlength(last + "…", font=f) > width and " " in last:
        last = last.rsplit(" ", 1)[0]
    return [*lines[: max_lines - 1], last + "…"]


def render(card: dict) -> bytes:
    """PNG bytes for a card; cached by content."""
    key = (card["kind"], card["headline"], card["subtitle"], card.get("p_yes"), tuple(card["stats"]))
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 22, H], fill=GREEN)
    pad = 70
    # brand row
    d.rounded_rectangle([pad, 48, pad + 52, 100], radius=12, fill=GREEN)
    d.text((pad + 26, 74), "C", font=font(34, True), fill="white", anchor="mm")
    d.text((pad + 70, 74), NAME.upper(), font=font(30, True), fill=INK, anchor="lm")
    d.text((W - pad, 74), "play credits only", font=font(22), fill=MUTED, anchor="rm")
    # headline and question
    y = 150
    for line in wrap(d, card["headline"], font(64, True), W - 2 * pad, 3):
        d.text((pad, y), line, font=font(64, True), fill=INK)
        y += 78
    y += 8
    for line in wrap(d, card["subtitle"], font(32), W - 2 * pad, 3):
        d.text((pad, y), line, font=font(32), fill=MUTED)
        y += 42
    # probability bar
    p = card.get("p_yes")
    bar_y = 438
    if p is not None:
        d.rounded_rectangle([pad, bar_y, W - pad, bar_y + 30], radius=15, fill=SOFT, outline=LINE, width=2)
        d.rounded_rectangle([pad, bar_y, pad + max(30, int((W - 2 * pad) * p)), bar_y + 30], radius=15, fill=GREEN)
        d.text((pad, bar_y + 40), "NO", font=font(20, True), fill=RED)
        d.text((W - pad, bar_y + 40), "YES", font=font(20, True), fill=GREEN, anchor="ra")
        d.text((W // 2, bar_y + 40), f"{round(p * 100)}% YES", font=font(22, True), fill=INK, anchor="ma")
    # stats and tagline
    d.text((pad, 530), "  ·  ".join(card["stats"]), font=font(24), fill=INK)
    d.line([pad, 560, W - pad, 560], fill=LINE, width=2)
    d.text((pad, 595), TAGLINE, font=font(28, True), fill=GREEN, anchor="lm")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    _cache[key] = out.getvalue()
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return _cache[key]
