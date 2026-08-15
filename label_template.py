#!/usr/bin/env python3
"""
Composable label template engine for the Katasymbol E12 (Supvan) printer.

A template is a list of objects, each rendered onto a (width_px x height_px)
canvas in the printer's final orientation (96 = 12 mm printhead, 320 = 40 mm
feed for the default label size).

Object types:

    text      {type:"text", x, y, text, size, rotate, bold, anchor}
    line      {type:"line", x1, y1, x2, y2, width}
    rect      {type:"rect", x, y, w, h, width, fill}
    image     {type:"image", path, x, y, w, h, mode:"fit"|"stretch"}
    barcode   {type:"barcode", x, y, data, module_px, height, text:"auto"|str|None}
    qrcode    {type:"qrcode", x, y, data, module_px, border}

All coordinates are in pixels of the printer canvas. The template can be
described in Python (list of dicts), JSON, or a compact text markup:

    text "S{id:04d}" x=8 y=280 size=22 bold
    barcode data="S{id:04d}" x=2 y=10 module_px=3 height=70
    line x1=0 y1=40 x2=96 y2=40 width=2
    qrcode data="SAMPLE-2024-001" x=8 y=8 module_px=4

Use {id} (or {i}) in any text/data field for series printing.
"""
import re
import lzma  # noqa: F401 (kept for parity with other modules)
from PIL import Image, ImageDraw, ImageFont

from barcode_label import make_barcode_module_pattern
from qr_label import _font as qr_font  # reuse font helper

DOTS_PER_MM = 8

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    cands = FONT_CANDIDATES if not bold else (
        FONT_CANDIDATES[0], FONT_CANDIDATES[2], FONT_CANDIDATES[1],
        FONT_CANDIDATES[3])
    for p in cands:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---- Object renderers -----------------------------------------------------

def _draw_text(d, obj):
    x, y = obj.get("x", 0), obj.get("y", 0)
    size = obj.get("size", 20)
    rotate = obj.get("rotate", 0)
    bold = obj.get("bold", False)
    anchor = obj.get("anchor", "la")
    text = str(obj.get("text", ""))
    if not text:
        return
    font = get_font(size, bold)
    bbox = d.textbbox((0, 0), text, font=font, anchor=anchor)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    if tw <= 0 or th <= 0:
        return
    tmp = Image.new("L", (tw + 8, th + 8), 255)
    td = ImageDraw.Draw(tmp)
    td.text((4 - bbox[0], 4 - bbox[1]), text, font=font, fill=0, anchor=anchor)
    if rotate:
        tmp = tmp.rotate(rotate, expand=True, fillcolor=255)
    # Mask = glyph pixels (black) opaque, background (white) transparent.
    mask = tmp.point(lambda v: 255 if v == 0 else 0)
    if hasattr(d, "_image"):
        d._image.paste(tmp, (x, y), mask=mask)
    else:
        d.bitmap((x, y), tmp, fill=0)


def _draw_line(d, obj):
    fill = obj.get("fill", 0)
    d.line([(obj.get("x1", 0), obj.get("y1", 0)),
            (obj.get("x2", 96), obj.get("y2", 0))],
           fill=fill, width=obj.get("width", 1))


def _draw_rect(d, obj):
    x, y = obj.get("x", 0), obj.get("y", 0)
    w, h = obj.get("w", 10), obj.get("h", 10)
    width = obj.get("width", 1)
    if obj.get("fill"):
        d.rectangle([x, y, x + w, y + h], fill=0)
    else:
        d.rectangle([x, y, x + w, y + h], outline=0, width=width)


def _draw_image(d, obj):
    path = obj.get("path")
    if not path:
        return
    src = Image.open(path).convert("L")
    x, y = obj.get("x", 0), obj.get("y", 0)
    w = obj.get("w")
    h = obj.get("h")
    mode = obj.get("mode", "fit")
    if mode == "stretch" and w and h:
        src = src.resize((w, h))
    elif w or h:
        src.thumbnail((w or 10**6, h or 10**6))
    # paste respecting transparency (use as mask)
    d.bitmap((x, y), src)


def _draw_barcode(d, obj):
    data = str(obj.get("data", ""))
    x = obj.get("x", 2)          # bar start along printhead (width)
    y = obj.get("y", 2)          # bar start along feed (length)
    module_px = obj.get("module_px", 3)
    height = obj.get("height", 70)   # bar length along printhead (width)
    pattern = make_barcode_module_pattern(data)
    cy = y
    for m in pattern:
        if m == "1":
            d.rectangle([x, cy, x + height - 1, cy + module_px - 1], fill=0)
        cy += module_px
    text = obj.get("text")
    if text is None:
        return
    if text == "auto":
        text = data
    if text:
        f = get_font(16)
        d.text((x, y + (cy - y) + 2), text, font=f, fill=0)


def _draw_qrcode(d, obj):
    import qrcode
    data = str(obj.get("data", ""))
    x, y = obj.get("x", 0), obj.get("y", 0)
    module_px = obj.get("module_px", 4)
    border = obj.get("border", 3)
    qr = qrcode.QRCode(version=None,
                       error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=module_px, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    qim = qr.make_image(fill_color="black", back_color="white").convert("L")
    d.bitmap((x, y), qim)


RENDERERS = {
    "text": _draw_text,
    "line": _draw_line,
    "rect": _draw_rect,
    "image": _draw_image,
    "barcode": _draw_barcode,
    "qrcode": _draw_qrcode,
}


# ---- Template rendering ---------------------------------------------------

def render_template(objects, width_px: int = 96, height_px: int = 320) -> Image.Image:
    """Render a list of object dicts onto a (width_px x height_px) canvas."""
    img = Image.new("L", (width_px, height_px), 255)
    d = ImageDraw.Draw(img)
    for obj in objects:
        kind = obj.get("type")
        fn = RENDERERS.get(kind)
        if fn:
            try:
                fn(d, obj)
            except Exception as e:
                print(f"  [render error] {kind}: {e}")
    return img


def substitute(obj, i: int):
    """Return a copy of obj with {id}/{i} replaced in text-like fields."""
    fields = ("text", "data")
    out = dict(obj)
    for f in fields:
        if f in obj and isinstance(obj[f], str):
            try:
                out[f] = obj[f].format(id=i, i=i).replace("{i}", str(i))
            except (KeyError, IndexError, ValueError):
                out[f] = obj[f].replace("{}", str(i))
    return out


# ---- Compact text markup --------------------------------------------------

TOKEN_RE = re.compile(r'(\w+)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s]+)')


def parse_markup(text: str) -> list[dict]:
    """Parse a compact multi-line template description into object dicts.

    Each line:  <type> [key=value ...]
    Bare positional word after the type is the primary field (text/data).
    """
    objects = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        kind = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        obj = {"type": kind}
        # leading bare word (unquoted, NOT followed by '=') is the primary field
        m = re.match(r'^([^\s=]+)\s*(.*)$', rest)
        if m and m.group(1):
            word = m.group(1)
            tail = m.group(2).lstrip()
            # only treat as primary if the next token is not '='
            if not tail.startswith("="):
                rest = tail
                field = "data" if kind in ("barcode", "qrcode") else "text"
                obj[field] = word.strip('"\'')
        for key, val in TOKEN_RE.findall(rest):
            val = val.strip('"\'')
            # numeric
            try:
                obj[key] = int(val)
            except ValueError:
                try:
                    obj[key] = float(val)
                except ValueError:
                    obj[key] = val
        if kind in RENDERERS:
            objects.append(obj)
    return objects


# ---- CLI ------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Render a label template to PNG")
    ap.add_argument("file", help="template file (.json, .py, or .txt markup)")
    ap.add_argument("--id", type=int, default=1, help="series id to render")
    ap.add_argument("--out", default="label.png")
    ap.add_argument("--width", type=int, default=96)
    ap.add_argument("--height", type=int, default=320)
    args = ap.parse_args()

    if args.file.endswith(".json"):
        import json
        objects = json.load(open(args.file))
    elif args.file.endswith(".py"):
        import importlib.util
        spec = importlib.util.spec_from_file_location("tpl", args.file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        objects = mod.OBJECTS
    else:
        objects = parse_markup(open(args.file).read())

    objects = [substitute(o, args.id) for o in objects]
    img = render_template(objects, args.width, args.height)
    img.save(args.out)
    print(f"rendered {args.out} ({img.size})")


if __name__ == "__main__":
    main()
