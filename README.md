# SUPVAN / Katasymbol E12 — Linux label printer driver

Control the **Katasymbol E12** (a rebadged **Supvan** handheld thermal label
printer) from Linux over Bluetooth LE — no vendor app required.

Print **1D Code128 barcodes for sample tubes** (or plain text/images) directly
from the command line or Python, in series.

```
S0001  S0002  S0003  ...  S0050
```

## What this device actually is

The E12 is **not** a standard printer. It has **no USB printer class, no IPP,
and no native text/barcode commands**. It exposes a Bluetooth LE GATT service
and speaks a **Supvan** wire protocol (`0x7E 0x5A` frames + LZMA-compressed
raster): every label — text, barcode, image — is rendered to a bitmap **on the
host** and sent as raster data.

| Property | Value |
|----------|-------|
| BLE service | `0000fee7-0000-1000-8000-00805f9b34fb` |
| BLE characteristic | `0000fec1-0000-1000-8000-00805f9b34fb` (notify + write-without-response) |
| Protocol | Supvan (`7E 5A` command frames, LZMA1 512‑byte data frames) |
| Printhead | 96 dots / 12 mm @ 8 dots/mm |
| Label | 12 mm × 40 mm (default; change with `--label-width-mm` / `--length-mm`) |

Protocol reverse-engineered from the community projects
[`heeen/supvan-cups`](https://github.com/heeen/supvan-cups) (a Rust CUPS/IPP
driver) and [`eteriall/katasymbol-e12-lab`](https://github.com/eteriall/katasymbol-e12-lab)
(Python BLE tools).

## Requirements

- Linux with Bluetooth (BlueZ)
- Python 3.10+

## Install

```bash
git clone git@github.com:vheinitz/SUPVAN_KS_LM_E12.git
cd SUPVAN_KS_LM_E12
bash install.sh
```

`install.sh` installs the Python dependencies (`bleak`, `pillow`,
`python-barcode`, `qrcode`) into your user site-packages. No root required.

(Manual alternative: `pip install --user -r requirements.txt`)

## Find the printer's Bluetooth address

The printer only advertises BLE for a short time after waking. Power it on
(press the button / open the lid), then immediately:

```bash
python3 katasymbol_e12.py scan
```

Look for a line marked with `*`:

```
* A4:93:40:02:F3:F5  T0197A260312H961  services=00001800...,0000fee7...
```

`A4:93:40:02:F3:F5` is the address. The device name (`T0197…`) is a Supvan-form
Serial. (Alternative: `bluetoothctl scan on` and look for "Beijing Supvan"
/ a `T0xxx` name.)

You can persist it to avoid typing it every time:

```bash
export KATASYMBOL_E12_ADDRESS=A4:93:40:02:F3:F5
```

## Usage

### Barcodes for sample tubes (series)

```bash
# S0001, S0002, S0003, S0004
python3 barcode_label.py A4:93:40:02:F3:F5 "S000{id}" --from 1 --to 4

# zero-padded 4-digit IDs: S0001 … S0050
python3 barcode_label.py A4:93:40:02:F3:F5 "S{id:04d}" --from 1 --to 50

# With a different label size / darkness / pause between labels
python3 barcode_label.py A4:93:40:02:F3:F5 "S{id:04d}" --from 1 --to 50 \
  --label-width-mm 12 --length-mm 40 --density 4 --delay 1.5
```

The barcode is **Code128** (letters + digits + dashes supported), with the
human-readable ID rotated 90° so it runs along the label length (it never
overlaps the barcode and handles longer IDs). It scans from any direction.

Preview the first label as a PNG (no print):

```bash
python3 barcode_label.py A4:93:40:02:F3:F5 "S{id:04d}" --from 1 --to 1 --preview
```

> **Note**: a 1D Code128 barcode only fits ~7–8 characters on a 40 mm label
> at a scannable module width. For longer IDs use the QR-code tool below.

### QR codes for longer IDs / boxes

For 15-char IDs, fridge labels, or tube boxes, a 2D QR code is the right
choice (a 15-char QR is a compact 21×21-module square):

```bash
# single 15-char QR code
python3 qr_label.py A4:93:40:02:F3:F5 "SAMPLE-2024-001" --from 1 --to 1

# series
python3 qr_label.py A4:93:40:02:F3:F5 "BOX-{id:04d}" --from 1 --to 20

# preview
python3 qr_label.py A4:93:40:02:F3:F5 "SAMPLE-2024-001" --from 1 --to 1 --preview
```

The QR code is a square (~11 mm) with the human-readable ID rotated along the
label length beside it.

### Multi-object templates (markup + GUI designer)

For labels combining text, lines, boxes, images, barcodes and QR codes, use
the markup engine or the GUI designer:

```bash
# GUI: edit markup, drag objects, live preview, print series
python3 multi_designer.py

# render a markup template to PNG from the CLI
python3 label_template.py template.txt --id 1 --out label.png
```

Template markup (one object per line; `#` comments; bare first word is the
primary field; positions in printer pixels = 96 wide × 320 long):

```
text   "S{id:04d}" x=6  y=4  size=18 bold
barcode data="S{id:04d}" x=2 y=40 module_px=3 height=88
line   x1=0 y1=36 x2=96 y2=36 width=1
rect   x=2 y=2 w=40 h=20 width=1
image  path=/tmp/logo.png x=4 y=4 w=40 mode=fit
qrcode data="SAMPLE-{id}" x=8 y=30 module_px=3
```

`{id}` (or `{i}`) in any `text`/`data` field is the series number. The GUI
lets you drag objects with the mouse; positions update in the markup and the
preview re-renders live.

### Image / text-only

```bash
python3 katasymbol_e12.py print-text A4:93:40:02:F3:F5 "HELLO" \
  --label-width-mm 12 --length-mm 40

python3 katasymbol_e12.py print-image A4:93:40:02:F3:F5 label.png \
  --label-width-mm 12 --length-mm 40
```

### Diagnostics

```bash
python3 katasymbol_e12.py probe A4:93:40:02:F3:F5     # check device + label material
python3 katasymbol_e12.py dry-run "HELLO" --label-width-mm 12 --length-mm 40
```

## Python API

```python
import asyncio
from barcode_label import print_series, render_barcode_label
from katasymbol_e12 import E12Ble, prepare_print

async def main():
    # Print S0001..S0004
    await print_series("A4:93:40:02:F3:F5", "S{id:04d}", range(1, 5))

    # Or render + print a single label manually
    img = render_barcode_label("S0001", label_width_mm=12, length_mm=40)
    compressed, speed = prepare_print(img, density=4)
    async with E12Ble("A4:93:40:02:F3:F5") as printer:
        await printer.print_compressed(compressed, speed)

asyncio.run(main())
```

## Files

| File | Purpose |
|------|---------|
| `katasymbol_e12.py` | Supvan BLE protocol driver + CLI (scan/probe/text/image/dry-run) |
| `barcode_label.py` | 1D barcode (Code128) label renderer + series printing |
| `qr_label.py` | 2D QR-code label renderer + series printing (long IDs) |
| `label_template.py` | Multi-object template engine (text/line/rect/image/barcode/qr) |
| `multi_designer.py` | PyQt6 GUI designer (markup + drag + live preview + series) |
| `label_designer.py` | PyQt6 GUI designer (simple single-symbology series) |
| `barcode_test.py` | Labelled module-width test series (for tuning) |
| `e12_reverse_lab.py` | Low-level raw send/observe harness (advanced) |
| `install.sh` | Install Python dependencies |
| `requirements.txt` | Python dependencies |

## GUI label designer

A graphical designer for composing and printing serialized label templates:

```bash
python3 label_designer.py
```

Features:
- Choose **1D Code128** or **2D QR code** symbology
- Define a **series template** (`S{id:04d}`, `BOX-{id}`, …) with a from/to range
- **Live preview** of the rendered label
- Adjust density, barcode module width, text strip, label size
- **Print the whole series** over BLE in one connection
- **Save / load** templates as JSON (and auto-restore the last session)

Per-session state is stored in `~/.katasymbol-e12-designer.json`.

## Troubleshooting

- **`bleak.exc.BleakDeviceNotFoundError` / printer not found** — the printer
  has gone to sleep. Wake it (press power / open lid) and retry.
- **Prints but barcode doesn't scan** — ensure the label is 12×40 mm; tune
  `--module-px` (2=tight, 3=default, 4=loose) and `--density`. `barcode_test.py`
  prints labelled variants to help you identify the best setting.
- **Different MAC address** — pass your address on the command line or set
  `KATASYMBOL_E12_ADDRESS`.

## CUPS (optional)

For a full CUPS/IPP queue, use the upstream Rust driver
[`heeen/supvan-cups`](https://github.com/heeen/supvan-cups). It maps the E12 to
its T50 family. This repo's `katasymbol_e12.py` is a lighter, direct-print
alternative that does not need CUPS.

## License

MIT.
