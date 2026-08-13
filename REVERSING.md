# Reverse-engineering notes: Katasymbol E12 (Supvan) label printer

How we went from "unfamiliar Chinese handheld label printer" to a working
Linux driver, and the dead ends along the way. Written from the actual session
history so the reasoning is preserved.

---

## 1. The device

* **Product**: "Katasymbol E12", a rebadged **Supvan** handheld thermal label
  printer (BLE name `T0197A260312H961`, MAC `A4:93:40:02:F3:F5`). The BLE
  vendor is reported as *"Beijing Supvan Information Technology"*.
* **Label**: 12 mm × 40 mm, 96-dot (12 mm) printhead, 8 dots/mm.
* **Interface**: Bluetooth Low Energy **only** in practice. It *also*
  enumerates over USB as a USB Mass-Storage / SCSI device (`VID 349c : PID
  0418`, `/dev/sg0`) but that interface is inert for printing — it does not
  mount a filesystem and no data path was found there.

---

## 2. First hypothesis: USB mass storage

`lsusb` showed `349c:0418 Generic USB2.0 Device` with `bInterfaceClass = 8
(Mass Storage)`. There is a whole class of "NIIMBOT" / "PeriPage" / Supvan
printers that expose a fake USB drive. We probed:

* `/dev/sg0` existed but needed root (`root:disk`).
* No mounted filesystem, `lsblk` showed `sda` with **0B**.

This route was abandoned — no writable storage to drop a label image on, and
the device is Bluetooth-primary for this model family.

---

## 3. Second hypothesis: NIIMBOT protocol over USB / BLE

The printer's BLE GATT service `0000fee7-…` / characteristic `0000fec1-…`
(notify + write-without-response) is *identical* to the well-known **NIIMBOT
D-series** layout. We installed `niimprint` and wrote a USB + BLE transport
assuming the NIIMBOT packet format:

```
55 55 <type> <len> <data> <checksum> AA AA
```

**Result: commands timed out.** The device connected over BLE (`bluetoothctl
connect` → "Connected: yes") and we could enumerate GATT services, but writes
produced **zero notifications**. This was the first big signal that the device
was *not* speaking NIIMBOT, despite the matching GATT UUIDs. (Shared UUIDs are
a red herring — many Chinese BLE gadgets reuse them without sharing a
protocol.)

---

## 4. The breakthrough: `supvan-cups` and the Supvan protocol

Searching for the vendor name surfaced two authoritative projects:

* **`heeen/supvan-cups`** — a Rust IPP-Everywhere driver for Supvan T-series
  printers, with a `docs/PROTOCOL.md` documenting the real wire protocol,
  reverse-engineered from the vendor's Android app (`Katasymbol v1.4.20`).
* **`eteriall/katasymbol-e12-lab`** — Python BLE tools for this exact E12
  device, built on that protocol.

The real protocol is **entirely different** from NIIMBOT:

| | NIIMBOT (wrong) | Supvan (correct) |
|---|---|---|
| Command frame | `55 55 … AA AA` | `7E 5A <len> 10 01 AA <cmd> <checksum> <param>…` (16 bytes) |
| Raster | raw rows | LZMA1-compressed 4096-byte buffers |
| Data frames | bare | `7E 5A …` 512-byte frames wrapping `AA BB` 506-byte packets |
| Transport (E12) | — | BLE GATT `fee7`/`fec1` |

Key command bytes (from `supvan-cups/crates/supvan-proto/src/cmd.rs`):

```
0x10 BUF_FULL       0x11 INQUIRY_STA    0x12 CHECK_DEVICE
0x13 START_PRINT     0x14 STOP_PRINT     0x16 RD_DEV_NAME
0x17 READ_REV        0x2E PAPER_SKIP     0x30 RETURN_MAT
0x5C NEXT_ZIPPEDBULK 0xC5 READ_FWVER
```

**Confirmed live**: `CHECK_DEVICE` (0x12) and `RETURN_MAT` (0x30) returned
valid responses over BLE. `RETURN_MAT` decoded to `width=12 mm, height=40 mm,
gap=3 mm` — matching the physical cartridge.

**Important finding**: there are **no barcode and no text commands** in the
entire Supvan vocabulary. The vendor app (`key-functions.js`) renders barcodes
and text to a canvas on the phone and sends `ImagePixel` (a grayscale raster).
So "barcode support" is always host-side — which is exactly what our code does.

---

## 5. BLE vs Classic RFCOMM

`supvan-cups` documents two transports: USB HID and Bluetooth **RFCOMM (SPP)**.
Its BLE transport (`ble.rs`) is explicitly marked *"unverified against
hardware"*. This raised the question: does the E12 use BLE or Classic RFCOMM?

Decisive clue from the user: the phone app **does not** show the printer as a
paired Classic Bluetooth device, but finds it *"by a magic address number"*.
Classic pairing would appear in the device list; BLE apps generally don't.
Together with our successful BLE GATT command exchange, this confirmed **BLE
is the operative transport**.

(A Classic RFCOMM path *may* also exist — `supvan-cups/test_print.py` uses
`socket.AF_BLUETOOTH` — but it was not needed and not verified for this unit.)

---

## 6. Making it actually print — two real bugs

Commands worked, but the first print attempts produced **no label output**
(the printer said "Printing…" and did nothing). Two bugs, in order:

### 6.1 Printhead width: 384 → 96

The community `katasymbol_e12.py` carried a leftover from the T50 (48 mm /
384 dots): `PRINTHEAD_WIDTH_DOTS = 384`. The E12's `e12_reverse_lab.py`
reference defaults to `--width-dots 96` (12 mm). With 384, the raster was 4×
too wide and the printhead silently dropped it.

**Fix**: `PRINTHEAD_WIDTH_DOTS = 96`.

### 6.2 Data-frame ACKs

The firmware acknowledges **every** 512-byte data frame and expects the host
to drain that ack before the next command. The original code skipped the ack
on the final frame; the firmware then never committed the raster.

**Fix**: read the response after *every* data frame (matching the Android
reference `BasePrint.transferSplitData` and `supvan-spp`'s `send_bulk_data`,
which reads an ack after each non-final packet).

---

## 7. Making the barcode scan — three more fixes

The first barcodes were unscannable by an industrial reader (a phone could
*just* read them with effort). Three independent problems:

### 7.1 No stretching

`python-barcode`'s `ImageWriter` output was resized with `thumbnail()`, which
destroys Code128's exact bar-width ratios. **Fix**: render from the raw module
bit-string (`barcode.get(…).build()[0]`, `1`=bar `0`=space) and draw each
module at an **exact integer pixel width** — no resampling. Verified by
decoding the output with `zxing-cpp`.

### 7.2 Module width (integer-division bug)

`module_px` was being silently forced down to 2 px (0.25 mm) by an integer
division (`avail // total_modules`). 2-px bars are too thin and merge under
thermal bleed. **Fix**: reserve a fixed 20-px text strip so `module_px=3`
(0.375 mm) fits, giving clean 3/6/9/12-px bar/space runs (proper 1:2:3:4).

### 7.3 Orientation (the actual killer)

This was the real root cause. The working `render_text()` draws text in a
**landscape** canvas (`length × width` = `320 × 96`) and ends with
`transpose(Image.Transpose.ROTATE_270)`. The barcode renderer drew **directly
into `96 × 320` without that rotation**, so on the physical label the bars ran
along the 40 mm feed axis instead of across the 12 mm width, and the
human-readable text was pushed off the label ("text almost outside the label").

**Fix**: build the barcode label exactly like `render_text()` — draw the
Code128 modules in a `320 × 96` landscape canvas (modules along the long axis,
bars spanning the 96-px height), then `ROTATE_270`.

After this, the user reported: *"now it scans fast and clearly from all
directions"*.

---

## 8. Verification method

* **Interactive feedback loop** — the user physically scanned each test label
  with an industrial barcode reader and reported which variant worked.
* **Software decode** — `zxing-cpp` (`read_barcode`) was used to validate the
  rendered label *before* printing, and confirmed `S0001`, `MP2D4`, … decoded
  correctly in software.
* **Labelled test series** (`barcode_test.py`) — each label encodes its own ID
  (`MP2D4`, `MP3D5`, …) so a passing/failing scanner result maps to a specific
  parameter set. This replaced ambiguous "all labels look the same" testing.
* **Live protocol tracing** — temporary instrumentation mirrored every
  `write_gatt_char` payload and notification, revealing the exact bytes of
  `CHECK_DEVICE`, `NEXT_ZIPPEDBULK`, data frames and `BUF_FULL`, and exposing
  the missing data-frame ack.

---

## 9. Summary of root causes

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | No BLE responses | Assumed NIIMBOT protocol | Use Supvan `7E 5A` framing |
| 2 | Commands OK, nothing prints | Printhead width 384 (T50 leftover) | `PRINTHEAD_WIDTH_DOTS = 96` |
| 3 | Still nothing prints | Missing data-frame ACKs | Ack every 512-byte frame |
| 4 | Barcode unscannable | Stretched/resampled bars | Integer-pixel module rendering |
| 5 | Thin bars | Integer-division collapsed module width | Reserve fixed text strip |
| 6 | Bars wrong direction + text off-label | Missing `ROTATE_270` | Match `render_text()` orientation |

---

## 10. References

* <https://github.com/heeen/supvan-cups> — Rust CUPS/IPP driver, authoritative
  `docs/PROTOCOL.md`, `crates/supvan-proto/src/{cmd,data,spp_pipe,rfcomm,ble}.rs`
* <https://github.com/eteriall/katasymbol-e12-lab> — Python BLE tools for the E12
* `python-barcode` (Code128), `zxing-cpp` (decode validation), `bleak` (BLE
  client), `Pillow` (raster rendering)
