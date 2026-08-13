#!/usr/bin/env python3
"""
Print a labelled barcode test series — each label carries a UNIQUE human ID
so the operator can report which variants scanned correctly.

Each variant encodes its own test code, e.g.:
    MP2D4  = module_px=2, density=4
    MP3D5  = module_px=3, density=5

The barcode content itself is the unique ID (e.g. "MP2D4"), so the returned
scan value directly tells you which variant worked.
"""
import asyncio
import argparse

from katasymbol_e12 import E12Ble, prepare_print
from barcode_label import render_barcode_label

TEST_VARIANTS = [
    # (label_text, module_px, density)
    ("MP2D4", 2, 4),
    ("MP2D5", 2, 5),
    ("MP3D4", 3, 4),
    ("MP3D5", 3, 5),
    ("MP4D4", 4, 4),
]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--label-width-mm", type=int, default=12)
    ap.add_argument("--length-mm", type=int, default=40)
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    async with E12Ble(args.address) as printer:
        for text, mp, density in TEST_VARIANTS:
            print(f"== printing {text} (module_px={mp}, density={density}) ==",
                  flush=True)
            img = render_barcode_label(text, args.label_width_mm,
                                       args.length_mm, module_px=mp)
            compressed, speed = prepare_print(img, density)
            await printer.print_compressed(
                compressed, speed, poll_completion=False,
                ignore_ribbon_end=True, progress=lambda m: None)
            await asyncio.sleep(args.delay)
    print("TEST SERIES DONE")
    print("Scan each label and report which IDs read correctly, e.g. 'MP3D5'.")


if __name__ == "__main__":
    asyncio.run(main())
