# SUPVAN / Katasymbol E12 — printing from Linux over Bluetooth LE

Python scripts that print labels on a Katasymbol E12 (a Supvan handheld
thermal label printer) from Linux over Bluetooth LE, without the vendor app:
Code128 barcode series, QR codes, text, images, and multi-object templates.

Status: used with one E12 and 12 × 40 mm labels; printed barcodes were checked
with a barcode scanner (see [REVERSING.md](REVERSING.md) §7–8). Other Supvan
models and label sizes have not been tried.

## How the device works

The E12 has no printer class over USB and no text or barcode commands. It is
driven over a BLE GATT characteristic with the Supvan protocol: `7E 5A`
command frames, the raster as LZMA1-compressed data in 512-byte frames. Every
label is rendered to a bitmap on the host.

| | |
|---|---|
| BLE service | `0000fee7-0000-1000-8000-00805f9b34fb` |
| Characteristic | `0000fec1-0000-1000-8000-00805f9b34fb` (notify + write) |
| Printhead | 96 dots = 12 mm, 8 dots/mm |
| Default label | 12 mm × 40 mm (96 × 320 px) |

The protocol is documented by [`heeen/supvan-cups`](https://github.com/heeen/supvan-cups)
(Rust CUPS/IPP driver); `katasymbol_e12.py` and `e12_reverse_lab.py` build on
[`eteriall/katasymbol-e12-lab`](https://github.com/eteriall/katasymbol-e12-lab).
[REVERSING.md](REVERSING.md) describes how the E12 was got to print and to
produce scannable barcodes (printhead width, data-frame acks, bar rendering,
orientation).

## Install

Requires Linux with BlueZ and Python 3.10+.

```bash
bash install.sh                        # pip install --user: bleak, pillow, python-barcode, qrcode, PyQt6
# or
pip install --user -r requirements.txt
```

PyQt6 is only needed for the two GUI designers.

## Find the printer

The printer advertises only for a short time after waking. Switch it on, then:

```bash
python3 katasymbol_e12.py scan
```

Lines marked `*` advertise one of the known Supvan services or have a name
like `T0197…`:

```
* AA:BB:CC:DD:EE:FF  T0197XXXXXXXXXXX  services=...
```

All print commands take this address as their first argument.

## Usage

### Code128 barcode series

```bash
python3 barcode_label.py AA:BB:CC:DD:EE:FF "S{id:04d}" --from 1 --to 50
python3 barcode_label.py AA:BB:CC:DD:EE:FF "S{id:04d}" --from 1 --to 50 \
  --label-width-mm 12 --length-mm 40 --density 4 --module-px 3 --delay 1.5
python3 barcode_label.py AA:BB:CC:DD:EE:FF "S{id:04d}" --from 1 --to 1 --preview   # PNG only
```

The template is a Python format string with `{id}`. The ID is printed as text
rotated 90° beside the bars. One connection is used for the whole series.

Length limit: barcode plus quiet zones must fit into the 320 px label length.
At the default 3 px per module that is 4 letters or 8 digits (`S0001` fits);
longer IDs drop to 2 px per module (up to 9 letters), which REVERSING.md calls
too thin or borderline. Beyond that the barcode runs off the label without an error. Use
QR codes for longer IDs.

### QR codes

```bash
python3 qr_label.py AA:BB:CC:DD:EE:FF "BOX-{id:04d}" --from 1 --to 20
python3 qr_label.py AA:BB:CC:DD:EE:FF "SAMPLE-2024-001" --from 1 --to 1 --preview
```

Error correction M. A 15-character ID in upper case and digits fits QR
version 1 (21 × 21 modules), printed at 3 px per module, about 8 mm square
without quiet zone.

### Templates

`label_template.py` renders a template of several objects (text, line, rect,
image, barcode, qrcode) to PNG; `multi_designer.py` is a PyQt6 editor for such
templates (markup, object tree, drag with the mouse, live preview, print one
label or a series).

```bash
python3 label_template.py template.txt --id 1 --out label.png
python3 multi_designer.py
```

Markup, one object per line, `#` starts a comment, coordinates in printer
pixels (96 wide × 320 long); a bare first word is the text/data field. The
template can also be JSON or a Python file defining `OBJECTS`:

```
text    "S{id:04d}" x=6 y=4 size=18 bold
barcode data="S{id:04d}" x=2 y=40 module_px=3 height=88
line    x1=0 y1=36 x2=96 y2=36 width=1
rect    x=2 y=2 w=40 h=20 width=1
image   path=logo.png x=4 y=4 w=40 mode=fit
qrcode  data="SAMPLE-{id}" x=8 y=30 module_px=3
```

`{id}` or `{i}` is replaced with the series number.

`label_designer.py` is a simpler PyQt6 GUI for one barcode or QR code per
label: series template with from/to, preview, density/module width/label size,
print over BLE, save/load as JSON. Its last state is kept in
`~/.katasymbol-e12-designer.json`.

### Text, image, diagnostics

```bash
python3 katasymbol_e12.py print-text  AA:BB:CC:DD:EE:FF "HELLO"
python3 katasymbol_e12.py print-image AA:BB:CC:DD:EE:FF label.png
python3 katasymbol_e12.py probe       AA:BB:CC:DD:EE:FF   # characteristics, status, loaded label material
python3 katasymbol_e12.py dry-run "HELLO"                 # build the print job without a printer
```

`barcode_test.py` prints a series of labels with different module widths and
densities, each encoding its own parameters (`MP2D4`, `MP3D5`, …), to find
settings that scan. `e12_reverse_lab.py` sends raw jobs with adjustable
protocol parameters (address via `--address` or `KATASYMBOL_E12_ADDRESS`).

## Python

```python
import asyncio
from barcode_label import print_series, render_barcode_label
from katasymbol_e12 import E12Ble, prepare_print

async def main():
    await print_series("AA:BB:CC:DD:EE:FF", "S{id:04d}", range(1, 5))

    img = render_barcode_label("S0001", label_width_mm=12, length_mm=40)
    compressed, speed = prepare_print(img, density=4)
    async with E12Ble("AA:BB:CC:DD:EE:FF") as printer:
        await printer.print_compressed(compressed, speed)

asyncio.run(main())
```

## Troubleshooting

- `RuntimeError: Could not connect to …`: the printer is asleep. Wake it and
  retry; the connect is retried 12 times.
- Barcode does not scan: check the label size, try `--module-px` and
  `--density`, or print `barcode_test.py`.

For a CUPS queue, use [`heeen/supvan-cups`](https://github.com/heeen/supvan-cups).

## License

MIT, see [LICENSE](LICENSE).
