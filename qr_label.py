#!/usr/bin/env python3
"""
QR-code label printing for the Katasymbol E12, for longer IDs (e.g. 15 chars)
that don't fit as a 1D barcode on a 40 mm label.

Layout: a square QR code (fits the 12 mm label width) + the human-readable ID
rotated along the label length beside it.

Usage:
    python3 qr_label.py A4:93:40:02:F3:F5 "SAMPLE-2024-001"
    python3 qr_label.py A4:93:40:02:F3:F5 "S{id:05d}" --from 1 --to 5
"""
import argparse
import asyncio

import qrcode
from PIL import Image, ImageDraw

from katasymbol_e12 import E12Ble, prepare_print
from barcode_label import _font, DOTS_PER_MM


def render_qr_label(code_text: str, label_width_mm: int = 12,
                    length_mm: int = 40, text_strip: int = 20,
                    qr_margin: int = 2) -> Image.Image:
    """Render a QR-code label in the printer's final orientation.

    Final image = (96 x 320) = 12 mm wide x 40 mm long.
    The QR code is a square occupying most of the width; the human text is
    rotated 90 deg and runs along the length beside it (handles long IDs).
    """
    width_px = label_width_mm * DOTS_PER_MM      # 96
    height_px = length_mm * DOTS_PER_MM          # 320

    # QR code sized to fit the width (minus the text strip). Use qrcode's
    # make_image with an integer box_size so each module is a crisp pixel.
    qr = qrcode.QRCode(version=None,
                       error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=1, border=3)
    qr.add_data(code_text)
    qr.make(fit=True)
    qr_size = qr.modules_count          # actual modules incl border
    avail = width_px - text_strip - 2
    module_px = max(1, avail // qr_size)

    qr2 = qrcode.QRCode(version=qr.version,
                        error_correction=qrcode.constants.ERROR_CORRECT_M,
                        box_size=module_px, border=3)
    qr2.add_data(code_text)
    qr2.make()
    qr_img = qr2.make_image(fill_color="black", back_color="white").convert("L")
    box = qr_img.size[0]

    img = Image.new("L", (width_px, height_px), 255)
    # Paste QR centered in the left area.
    qx = (width_px - text_strip - box) // 2
    qy = (height_px - box) // 2
    img.paste(qr_img, (qx, qy))

    # Human text rotated 90 deg along the length.
    d = ImageDraw.Draw(img)
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
                       density: int = 4, delay: float = 1.5):
    async with E12Ble(address) as printer:
        for n, i in enumerate(ids):
            code = template.format(id=i)
            print(f"[{n + 1}] {code}", flush=True)
            img = render_qr_label(code, label_width_mm, length_mm)
            compressed, speed = prepare_print(img, density)
            await printer.print_compressed(
                compressed, speed, poll_completion=False,
                ignore_ribbon_end=True, progress=lambda m: None)
            await asyncio.sleep(delay)
    print("series done")


async def _main():
    ap = argparse.ArgumentParser(description="Print QR-code labels (for long IDs)")
    ap.add_argument("address")
    ap.add_argument("template", help='Python format template, e.g. "S{id:05d}"')
    ap.add_argument("--from", dest="start", type=int, default=1)
    ap.add_argument("--to", dest="end", type=int, required=True)
    ap.add_argument("--label-width-mm", type=int, default=12)
    ap.add_argument("--length-mm", type=int, default=40)
    ap.add_argument("--density", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()

    if args.preview:
        code = args.template.format(id=args.start)
        img = render_qr_label(code, args.label_width_mm, args.length_mm)
        out = f"preview_qr_{code}.png"
        img.save(out)
        print(f"preview saved: {out} ({img.size})")
        return

    ids = range(args.start, args.end + 1)
    await print_series(args.address, args.template, ids,
                       label_width_mm=args.label_width_mm,
                       length_mm=args.length_mm,
                       density=args.density, delay=args.delay)


if __name__ == "__main__":
    asyncio.run(_main())
