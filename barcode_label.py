#!/usr/bin/env python3
"""
Print 1D barcodes on the Katasymbol E12 for sample-tube IDs, in series.

Command line:
    python3 barcode_label.py A4:93:40:02:F3:F5 "S{id:04d}" --from 1 --to 4
    python3 barcode_label.py <ADDR> "L-{id}" --from 100 --to 110
    python3 barcode_label.py <ADDR> "S{id:04d}" --from 1 --to 1 --preview

Python API:
    from barcode_label import render_barcode_label, print_series
    await print_series(addr, "S{id:04d}", range(1, 5))

Barcode is Code128 (letters, digits, symbols). Human-readable text is drawn
below the barcode. The label is 12 mm x 40 mm; the barcode is rotated so its
bars run along the 40 mm length and it scans when the tube is held upright.
"""
import argparse
import asyncio

from PIL import Image, ImageDraw, ImageFont
import barcode

from katasymbol_e12 import E12Ble, prepare_print

DOTS_PER_MM = 8

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_barcode_module_pattern(code_text: str) -> str:
    """Return the Code128 module pattern (a bit string: 1=bar, 0=space)."""
    bc = barcode.get("code128", code_text)
    return bc.build()[0]


def render_barcode_label(code_text: str, label_width_mm: int = 12,
                         length_mm: int = 40, module_px: int = 3,
                         text_strip: int = 20) -> Image.Image:
    """Compose a pixel-perfect barcode label in the printer's final image
    orientation (96 wide = printhead/12 mm, 320 tall = feed/40 mm).

    - Code128 modules run vertically (along the 320 feed); each bar is a
      HORIZONTAL stripe spanning the barcode band width.
    - The human text is ROTATED 90 deg so it reads top-to-bottom ALONG the
      label length, in a narrow strip beside the bars. This lets long IDs
      (e.g. 15 chars) fit without shrinking.

    Every barcode module is an exact integer number of pixels (no resampling).
    """
    width_px = label_width_mm * DOTS_PER_MM    # 96  (x, printhead)
    height_px = length_mm * DOTS_PER_MM        # 320 (y, feed)

    pattern = make_barcode_module_pattern(code_text)
    modules = len(pattern)
    quiet = 10
    total_modules = modules + 2 * quiet
    module_px = max(2, module_px)
    if total_modules * module_px > height_px:
        module_px = max(2, height_px // total_modules)
    barcode_y = total_modules * module_px

    img = Image.new("L", (width_px, height_px), 255)
    d = ImageDraw.Draw(img)

    # Barcode band: bars span the left portion; a strip on the right holds the
    # rotated text.
    bar_w = width_px - text_strip - 2
    bar_x0 = 2
    y_start = (height_px - barcode_y) // 2
    y = y_start + quiet * module_px
    for m in pattern:
        if m == "1":
            d.rectangle([bar_x0, y, bar_x0 + bar_w - 1, y + module_px - 1], fill=0)
        y += module_px

    # Human text rotated 90 deg to run along the length (reads top-to-bottom).
    txt = code_text
    size = 24
    f = _font(size)
    while size > 8:
        bbox = d.textbbox((0, 0), txt, font=f)
        if bbox[2] - bbox[0] <= height_px - 8:
            break
        size -= 1
        f = _font(size)
    bbox = d.textbbox((0, 0), txt, font=f)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    # Render horizontally, rotate 90: result width = glyph height, height = text
    # length. Shrink glyphs only if the strip is too narrow.
    txt_img = Image.new("L", (tw + 4, th + 4), 255)
    ImageDraw.Draw(txt_img).text((2 - bbox[0], 2 - bbox[1]), txt, fill=0, font=f)
    rot = txt_img.rotate(90, expand=True)
    if rot.width > text_strip - 4:
        s = (text_strip - 4) / rot.width
        rot = rot.resize((text_strip - 4, max(1, int(rot.height * s))),
                         Image.Resampling.LANCZOS)
    ty = (height_px - rot.height) // 2
    tx = width_px - text_strip + (text_strip - rot.width) // 2
    img.paste(rot, (tx, ty))

    return img


async def print_series(address: str, template: str, ids,
                       label_width_mm: int = 12, length_mm: int = 40,
                       density: int = 4, delay: float = 1.5,
                       module_px: int = 3):
    """Connect once, print one label per id."""
    async with E12Ble(address) as printer:
        for n, i in enumerate(ids):
            code = template.format(id=i)
            print(f"[{n + 1}] {code}", flush=True)
            img = render_barcode_label(code, label_width_mm, length_mm,
                                       module_px=module_px)
            compressed, speed = prepare_print(img, density)
            await printer.print_compressed(
                compressed,
                speed,
                poll_completion=False,
                ignore_ribbon_end=True,
                progress=lambda m: None,
            )
            await asyncio.sleep(delay)
    print("series done")


async def _main():
    ap = argparse.ArgumentParser(
        description="Print a series of sample-tube barcodes on the Katasymbol E12")
    ap.add_argument("address")
    ap.add_argument("template", help='Python format template, e.g. "S{id:04d}"')
    ap.add_argument("--from", dest="start", type=int, default=1)
    ap.add_argument("--to", dest="end", type=int, required=True)
    ap.add_argument("--label-width-mm", type=int, default=12)
    ap.add_argument("--length-mm", type=int, default=40)
    ap.add_argument("--density", type=int, default=4)
    ap.add_argument("--module-px", type=int, default=3,
                    help="barcode module width in pixels (2=tight,3=std,4=loose)")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--preview", action="store_true",
                    help="save the first label as PNG and exit")
    args = ap.parse_args()

    if args.preview:
        code = args.template.format(id=args.start)
        img = render_barcode_label(code, args.label_width_mm, args.length_mm,
                                   module_px=args.module_px)
        out = f"preview_{code}.png"
        img.save(out)
        print(f"preview saved: {out}  ({img.size})")
        return

    ids = range(args.start, args.end + 1)
    await print_series(
        args.address, args.template, ids,
        label_width_mm=args.label_width_mm,
        length_mm=args.length_mm,
        density=args.density,
        delay=args.delay,
        module_px=args.module_px,
    )


if __name__ == "__main__":
    asyncio.run(_main())
