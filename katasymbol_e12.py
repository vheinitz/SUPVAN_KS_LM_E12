#!/usr/bin/env python3
import argparse
import asyncio
import lzma
import math
import os
import re
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError, BleakGATTProtocolError
from PIL import Image, ImageDraw, ImageFont, ImageOps


SIG_BASE = "0000{short:04x}-0000-1000-8000-00805f9b34fb"
E0FF_SERVICE = "0000e0ff-3c17-d293-8e48-14fe2e4da212"

SERVICE_PATTERNS = {
    SIG_BASE.format(short=0xFEE7): (SIG_BASE.format(short=0xFEC1), SIG_BASE.format(short=0xFEC1)),
    E0FF_SERVICE: (SIG_BASE.format(short=0xFFE1), SIG_BASE.format(short=0xFFE9)),
    SIG_BASE.format(short=0xFF00): (SIG_BASE.format(short=0xFF01), SIG_BASE.format(short=0xFF02)),
}

CMD_BUF_FULL = 0x10
CMD_INQUIRY_STA = 0x11
CMD_CHECK_DEVICE = 0x12
CMD_START_PRINT = 0x13
CMD_STOP_PRINT = 0x14
CMD_RETURN_MAT = 0x30
CMD_NEXT_ZIPPEDBULK = 0x5C

DOTS_PER_MM = 8
# E12 printhead is 96 dots (12 mm); the T50's 48 mm / 384-dot canvas does
# NOT apply to the E10/E11/E12/E16 handheld series.
PRINTHEAD_WIDTH_DOTS = 96
default_printhead_width_dots = PRINTHEAD_WIDTH_DOTS
DEFAULT_MARGIN_DOTS = 8
MAX_BUF_DATA = 4074
PRINT_BUF_SIZE = 4096
PRINT_BUF_HEADER = 14
DATA_PAYLOAD_SIZE = 500
BLE_BULK_CHUNK = 180

# Ported from heeen/supvan-cups `crates/supvan-app/src/dither.rs`.
# The driver uses an 8bpp grayscale canvas, mirrors horizontally, then applies
# this thermal-compensated 4x4 Bayer dither before the Supvan raster packing.
SRGB_TO_LINEAR = [
    0, 50, 58, 63, 67, 70, 72, 74, 76, 78, 80, 82, 83, 85, 86, 88, 89, 90, 92, 93, 95, 96, 97, 98,
    100, 101, 102, 103, 105, 106, 107, 108, 109, 110, 112, 113, 114, 115, 116, 117, 118, 119, 120,
    121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 135, 136, 137, 138,
    139, 140, 141, 142, 142, 143, 144, 145, 146, 147, 148, 148, 149, 150, 151, 152, 152, 153, 154,
    155, 156, 156, 157, 158, 159, 159, 160, 161, 162, 162, 163, 164, 165, 165, 166, 167, 167, 168,
    169, 170, 170, 171, 172, 172, 173, 174, 174, 175, 176, 177, 177, 178, 179, 179, 180, 181, 181,
    182, 183, 183, 184, 184, 185, 186, 186, 187, 188, 188, 189, 190, 190, 191, 191, 192, 193, 193,
    194, 195, 195, 196, 196, 197, 198, 198, 199, 199, 200, 201, 201, 202, 202, 203, 203, 204, 205,
    205, 206, 206, 207, 207, 208, 209, 209, 210, 210, 211, 211, 212, 213, 213, 214, 214, 215, 215,
    216, 216, 217, 217, 218, 219, 219, 220, 220, 221, 221, 222, 222, 223, 223, 224, 224, 225, 225,
    226, 226, 227, 227, 228, 228, 229, 230, 230, 231, 231, 232, 232, 233, 233, 234, 234, 235, 235,
    236, 236, 237, 237, 238, 238, 238, 239, 239, 240, 240, 241, 241, 242, 242, 243, 243, 244, 244,
    245, 245, 246, 246, 247, 247, 248, 248, 249, 249, 249, 250, 250, 251, 251, 252, 252, 253, 253,
    254, 254, 255, 255,
]
BAYER4 = [
    [8, 136, 40, 168],
    [200, 72, 232, 104],
    [56, 184, 24, 152],
    [248, 120, 216, 88],
]


def make_cmd(cmd: int, param: int = 0, param2: int = 0) -> bytes:
    pkt = bytearray(16)
    pkt[0] = 0x7E
    pkt[1] = 0x5A
    pkt[2] = 0x0C
    pkt[4] = 0x10
    pkt[5] = 0x01
    pkt[6] = 0xAA
    pkt[7] = cmd
    pkt[11] = 0x01
    pkt[12:14] = int(param).to_bytes(2, "little")
    pkt[14:16] = int(param2).to_bytes(2, "little")
    checksum = sum(pkt[10:16]) & 0xFFFF
    pkt[8:10] = checksum.to_bytes(2, "little")
    return bytes(pkt)


def validate_response(resp: bytes, expected_cmd: int) -> bool:
    return len(resp) >= 8 and resp[0] == 0x7E and resp[1] == 0x5A and resp[7] == expected_cmd


@dataclass
class PrinterStatus:
    buf_full: bool = False
    label_rw_error: bool = False
    label_end: bool = False
    label_mode_error: bool = False
    ribbon_rw_error: bool = False
    ribbon_end: bool = False
    low_battery: bool = False
    device_busy: bool = False
    head_temp_high: bool = False
    cover_open: bool = False
    printing: bool = False
    label_not_installed: bool = False
    print_count: int = 0

    @property
    def has_error(self) -> bool:
        return any(
            [
                self.label_rw_error,
                self.label_end,
                self.label_mode_error,
                self.ribbon_rw_error,
                self.ribbon_end,
                self.low_battery,
                self.head_temp_high,
                self.cover_open,
                self.label_not_installed,
            ]
        )


def parse_status(resp: bytes) -> PrinterStatus | None:
    if len(resp) < 20 or resp[0] != 0x7E or resp[1] != 0x5A:
        return None
    return PrinterStatus(
        buf_full=bool(resp[14] & 0x01),
        label_rw_error=bool(resp[14] & 0x02),
        label_end=bool(resp[14] & 0x04),
        label_mode_error=bool(resp[14] & 0x08),
        ribbon_rw_error=bool(resp[14] & 0x10),
        ribbon_end=bool(resp[14] & 0x20),
        low_battery=bool(resp[14] & 0x40),
        device_busy=bool(resp[15] & 0x04),
        head_temp_high=bool(resp[15] & 0x08),
        cover_open=bool(resp[16] & 0x08),
        printing=bool(resp[16] & 0x40),
        label_not_installed=bool(resp[17] & 0x01),
        print_count=int.from_bytes(resp[18:20], "little"),
    )


def material_summary(resp: bytes) -> dict:
    out = {"raw_hex": resp.hex(" ")}
    if len(resp) >= 47:
        out["t50_candidate"] = {
            "width_mm": resp[37],
            "height_mm": resp[38],
            "gap_mm": resp[39],
            "label_type": resp[40],
            "remaining": int.from_bytes(resp[43:47], "little"),
        }
    if len(resp) >= 44:
        out["e12_candidate"] = {
            "width_mm": resp[40],
            "height_mm": resp[41],
            "gap_mm": resp[42],
            "remaining": int.from_bytes(resp[43:47], "little") if len(resp) >= 47 else None,
        }
    return out


class E12Ble:
    def __init__(self, address: str):
        self.address = address
        self.client: BleakClient | None = None
        self.notify_char: str | None = None
        self.write_char: str | None = None
        self.notifications: asyncio.Queue[bytes] = asyncio.Queue()

    async def __aenter__(self):
        self.client = BleakClient(self.address, services=list(SERVICE_PATTERNS))
        # The printer only advertises intermittently and sleeps when idle.
        # Re-discover it and retry connecting a few times before giving up.
        last_exc: Exception | None = None
        for attempt in range(12):
            try:
                await self.client.connect()
                break
            except BleakError as exc:
                last_exc = exc
                try:
                    await BleakScanner.find_device_by_address(
                        self.address, timeout=4.0)
                except Exception:
                    pass
                await asyncio.sleep(1.0)
        else:
            raise RuntimeError(
                f"Could not connect to {self.address}: {last_exc}") from last_exc
        await self._find_chars()
        await self.client.start_notify(self.notify_char, self._on_notify)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.client and self.client.is_connected:
            if self.notify_char:
                try:
                    await self.client.stop_notify(self.notify_char)
                except BleakError:
                    pass
            await self.client.disconnect()

    def _on_notify(self, _sender, data: bytearray):
        self.notifications.put_nowait(bytes(data))

    async def _find_chars(self):
        assert self.client
        if hasattr(self.client, "get_services"):
            services = await self.client.get_services()
        else:
            try:
                services = self.client.services
            except BleakError as exc:
                raise RuntimeError(f"service discovery was not available after connect: {exc}") from exc
        service_uuids = {str(s.uuid).lower(): s for s in services}
        for service_uuid, (notify_uuid, write_uuid) in SERVICE_PATTERNS.items():
            if service_uuid not in service_uuids:
                continue
            notify_matches = []
            write_matches = []
            for c in service_uuids[service_uuid].characteristics:
                cu = str(c.uuid).lower()
                props = set(getattr(c, "properties", []) or [])
                if cu == notify_uuid and ({"notify", "indicate"} & props):
                    notify_matches.append(c)
                if cu == write_uuid and ({"write", "write-without-response"} & props):
                    write_matches.append(c)
            if notify_matches and write_matches:
                self.notify_char = notify_matches[0]
                # Prefer write-with-response, but accept write-without-response.
                self.write_char = next(
                    (c for c in write_matches if "write" in set(getattr(c, "properties", []) or [])),
                    write_matches[0],
                )
                return
        detail = []
        for s in services:
            detail.append(f"service {s.uuid}")
            for c in s.characteristics:
                detail.append(f"  char {c.uuid} props={','.join(getattr(c, 'properties', []) or [])}")
        raise RuntimeError("no supported writable/notifiable Supvan GATT service found:\n" + "\n".join(detail))

    async def _next_notification(self, want_cmd: int | None = None, timeout: float = 4.0) -> bytes | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                payload = await asyncio.wait_for(self.notifications.get(), remaining)
            except TimeoutError:
                return None
            if want_cmd is None or (len(payload) > 7 and payload[7] == want_cmd):
                return payload

    async def command(self, cmd: int, param: int = 0, param2: int = 0, require: bool = False) -> bytes | None:
        assert self.client and self.write_char
        frame = make_cmd(cmd, param, param2)
        last_exc: Exception | None = None
        for attempt in range(6):
            try:
                try:
                    await self.client.write_gatt_char(self.write_char, frame, response=True)
                except BleakGATTProtocolError as exc:
                    if "Write Not Permitted" not in str(exc):
                        raise
                    await self.client.write_gatt_char(self.write_char, frame, response=False)
                resp = await self._next_notification(cmd)
                if require and resp is None:
                    raise TimeoutError(f"no response for command 0x{cmd:02x}")
                return resp
            except (BleakGATTProtocolError, BleakError) as exc:
                # The printer is busy mid-print; transient errors are normal.
                last_exc = exc
                await asyncio.sleep(0.15)
        raise last_exc  # type: ignore[misc]


    async def send_data_frame(self, frame: bytes, read_response: bool):
        assert self.client and self.write_char
        for offset in range(0, len(frame), BLE_BULK_CHUNK):
            await self.client.write_gatt_char(self.write_char, frame[offset : offset + BLE_BULK_CHUNK], response=False)
        if read_response:
            return await self._next_notification(None, timeout=1.0)
        return None

    async def check(self) -> bool:
        resp = await self.command(CMD_CHECK_DEVICE, require=True)
        return bool(resp and validate_response(resp, CMD_CHECK_DEVICE))

    async def status(self) -> PrinterStatus | None:
        resp = await self.command(CMD_INQUIRY_STA, require=True)
        return parse_status(resp or b"")

    async def wait_ready(self, attempts: int = 60) -> PrinterStatus:
        for _ in range(attempts):
            st = await self.status()
            if st and not st.device_busy and not st.printing:
                return st
            await asyncio.sleep(0.1)
        raise TimeoutError("printer did not become ready")

    async def wait_printing(self, attempts: int = 60):
        for _ in range(attempts):
            st = await self.status()
            if st and st.has_error:
                raise RuntimeError(f"printer error after start: {st}")
            if st and st.printing:
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("printer did not enter printing state")

    async def wait_buffer_ready(self, attempts: int = 200):
        for _ in range(attempts):
            await asyncio.sleep(0.02)
            st = await self.status()
            if st and st.has_error:
                raise RuntimeError(f"printer error while waiting for buffer: {st}")
            if st and not st.buf_full:
                return
        raise TimeoutError("printer buffer stayed full")

    async def print_compressed(
        self,
        compressed: bytes,
        speed: int,
        final_data_ack: bool = False,
        buf_full_before_data: bool = False,
        wait_after_start: bool = True,
        stop_after_transfer: bool = False,
        stop_after_transfer_delay: float = 0.2,
        poll_completion: bool = True,
        ignore_ribbon_end: bool = False,
        progress=None,
    ):
        def report(message: str):
            if progress:
                progress(message)

        report("CHECK_DEVICE")
        if not await self.check():
            raise RuntimeError("CHECK_DEVICE failed")
        report("wait ready")
        st = await self.wait_ready()
        if st.has_error and not (ignore_ribbon_end and st.ribbon_end and not any([
            st.label_rw_error,
            st.label_end,
            st.label_mode_error,
            st.ribbon_rw_error,
            st.low_battery,
            st.head_temp_high,
            st.cover_open,
            st.label_not_installed,
        ])):
            raise RuntimeError(f"printer error before print: {st}")
        report("START_PRINT")
        await self.command(CMD_START_PRINT, require=True)
        if wait_after_start:
            report("wait printing")
            await self.wait_printing()
            report("wait buffer ready")
            await self.wait_buffer_ready()

        frames = build_data_frames(compressed)
        report(f"NEXT_ZIPPEDBULK frames={len(frames)}")
        await self.command(CMD_NEXT_ZIPPEDBULK, 512, len(frames), require=True)
        if buf_full_before_data:
            report("BUF_FULL before data")
            await asyncio.sleep(0.02)
            await self.command(CMD_BUF_FULL, len(compressed), speed, require=True)
        for i, frame in enumerate(frames):
            report(f"send data frame {i + 1}/{len(frames)}")
            # Read the ack after every data frame (including the last), as the
            # firmware acks each packet and expects us to drain it before the
            # next command — matching the Android reference and test_print.py.
            await self.send_data_frame(frame, read_response=True)
        if not buf_full_before_data:
            report("BUF_FULL after data")
            await asyncio.sleep(0.02)
            await self.command(CMD_BUF_FULL, len(compressed), speed, require=True)

        if stop_after_transfer:
            report(f"STOP_PRINT after transfer delay={stop_after_transfer_delay}")
            await asyncio.sleep(max(0.0, stop_after_transfer_delay))
            await self.command(CMD_STOP_PRINT, require=True)
        if not poll_completion:
            report("skip completion polling")
            return

        report("wait completion")
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                st = await self.status()
            except Exception:
                st = None
            if st and not st.printing:
                return
        # Not all firmware revs report completion cleanly; the job has
        # already been transferred. End the session explicitly.
        report("completion status not reported - ending session")
        try:
            await self.command(CMD_STOP_PRINT, require=False)
        except Exception:
            pass


def render_text(text: str, label_width_mm: int, length_mm: int, font_path: str | None = None, font_size: int | None = None) -> Image.Image:
    # Canvas: width = printhead (12mm), height = feed length (40mm).
    width = label_width_mm * DOTS_PER_MM
    height = length_mm * DOTS_PER_MM

    # To read text along the long (feed) axis, render it in a wide canvas
    # (length x width) and rotate 90 deg into the printer's orientation.
    landscape = Image.new("L", (height, width), 255)
    draw = ImageDraw.Draw(landscape)

    font_size = font_size or max(16, min(height, width) * 2 // 3)
    font = None
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    if font_path:
        font_paths.insert(0, font_path)
    for candidate in font_paths:
        if os.path.exists(candidate):
            try:
                font = ImageFont.truetype(candidate, font_size)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()

    # Auto-shrink the font so the text fits the label length.
    while True:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if (tw <= height - 8 and th <= width) or font_size <= 10:
            break
        font_size -= 2
        try:
            font = ImageFont.truetype(font_paths[0], font_size)
        except Exception:
            font = ImageFont.load_default()
    draw.text(((height - tw) // 2, (width - th) // 2), text, fill=0, font=font)
    # Rotate 90 deg CW so it maps onto the printer's column-major layout and
    # reads correctly along the label length.
    return landscape.transpose(Image.Transpose.ROTATE_270)


def render_image(path: str, label_width_mm: int, length_mm: int) -> Image.Image:
    width = label_width_mm * DOTS_PER_MM
    height = length_mm * DOTS_PER_MM
    src = Image.open(path).convert("L")
    # Landscape canvas (length x width), then rotate into printer orientation,
    # consistent with render_text().
    src.thumbnail((height, width), Image.Resampling.LANCZOS)
    image = Image.new("L", (height, width), 255)
    image.paste(src, ((height - src.width) // 2, (width - src.height) // 2))
    return image.transpose(Image.Transpose.ROTATE_270)


def image_to_row_major_1bpp(image: Image.Image) -> tuple[bytes, int, int]:
    gray = ImageOps.grayscale(image)
    width, height = gray.size
    pixels = gray.load()
    out_bpr = math.ceil(width / 8)
    output = bytearray(out_bpr * height)
    for y in range(height):
        bayer_row = BAYER4[y & 3]
        for x in range(width):
            mx = width - 1 - x
            linear = SRGB_TO_LINEAR[pixels[x, y]]
            if linear < bayer_row[mx & 3]:
                output[y * out_bpr + mx // 8] |= 0x80 >> (mx & 7)
    return bytes(output), width, height


def raster_to_column_major(input_bytes: bytes, width: int, height: int) -> tuple[bytes, int, int]:
    in_bpr = math.ceil(width / 8)
    out_bpl = math.ceil(width / 8)
    output = bytearray(height * out_bpl)
    for y in range(height):
        for x in range(width):
            in_byte = y * in_bpr + x // 8
            if in_byte >= len(input_bytes):
                continue
            pixel = (input_bytes[in_byte] >> (7 - (x % 8))) & 1
            if pixel:
                output[y * out_bpl + x // 8] |= 1 << (x % 8)
    return bytes(output), height, out_bpl


def center_in_printhead(data: bytes, cols: int, input_width_dots: int, canvas_width_dots: int = PRINTHEAD_WIDTH_DOTS) -> tuple[bytes, int]:
    canvas_bpl = math.ceil(canvas_width_dots / 8)
    input_bpl = math.ceil(input_width_dots / 8)
    x_offset = max(0, (canvas_width_dots - input_width_dots) // 2)
    output = bytearray(cols * canvas_bpl)
    for col in range(cols):
        for dot in range(min(input_width_dots, canvas_width_dots)):
            in_byte = col * input_bpl + dot // 8
            if in_byte >= len(data):
                continue
            if (data[in_byte] >> (dot % 8)) & 1:
                out_dot = x_offset + dot
                if out_dot < canvas_width_dots:
                    output[col * canvas_bpl + out_dot // 8] |= 1 << (out_dot % 8)
    return bytes(output), canvas_bpl


def build_print_buffer(image_data: bytes, per_line_byte: int, cols_in_buf: int, first: bool, last: bool, density: int) -> bytes:
    buf = bytearray(PRINT_BUF_SIZE)
    page_bits0 = (0x02 if first else 0) | (0x04 if last else 0) | (0x08 if last else 0)
    page_bits1 = ((density & 0x0F) << 2) | (1 << 6)
    buf[2] = page_bits0
    buf[3] = page_bits1
    buf[4:6] = cols_in_buf.to_bytes(2, "little")
    buf[6] = per_line_byte
    buf[8:10] = DEFAULT_MARGIN_DOTS.to_bytes(2, "little")
    buf[10:12] = DEFAULT_MARGIN_DOTS.to_bytes(2, "little")
    buf[12] = min(density, 15)
    buf[PRINT_BUF_HEADER : PRINT_BUF_HEADER + len(image_data)] = image_data[: PRINT_BUF_SIZE - PRINT_BUF_HEADER]
    data_end = cols_in_buf * per_line_byte + PRINT_BUF_HEADER
    checksum = sum(buf[2:14])
    for i in range(1, data_end // 256 + 1):
        idx = i * 256 - 1
        if idx < len(buf):
            checksum += buf[idx]
    buf[0:2] = (checksum & 0xFFFF).to_bytes(2, "little")
    return bytes(buf)


def split_into_buffers(image_data: bytes, per_line_byte: int, total_cols: int, density: int) -> list[bytes]:
    max_cols = MAX_BUF_DATA // per_line_byte
    image_cols = total_cols - DEFAULT_MARGIN_DOTS - DEFAULT_MARGIN_DOTS
    buffers = []
    current_col = 0
    while current_col < image_cols:
        cols = min(max_cols, image_cols - current_col)
        first = current_col == 0
        last = current_col + cols >= image_cols
        start = (DEFAULT_MARGIN_DOTS + current_col) * per_line_byte
        end = start + cols * per_line_byte
        buffers.append(build_print_buffer(image_data[start:end], per_line_byte, cols, first, last, density))
        current_col += cols
    return buffers


def compress_buffers(buffers: list[bytes]) -> tuple[bytes, int]:
    raw = b"".join(buffers)
    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": 8192, "lc": 3, "lp": 0, "pb": 2, "nice_len": 128}]
    compressed = bytearray(lzma.compress(raw, format=lzma.FORMAT_ALONE, filters=filters))
    compressed[5:13] = len(raw).to_bytes(8, "little")
    return bytes(compressed), len(compressed) // len(buffers)


def calc_speed(avg_compressed_size: int) -> int:
    if avg_compressed_size > 3000:
        return 10
    if avg_compressed_size > 2800:
        return 15
    if avg_compressed_size > 2500:
        return 20
    if avg_compressed_size > 2000:
        return 25
    if avg_compressed_size > 1500:
        return 40
    if avg_compressed_size > 1000:
        return 45
    if avg_compressed_size > 500:
        return 55
    return 60


def build_data_frames(compressed: bytes) -> list[bytes]:
    total = math.ceil(len(compressed) / DATA_PAYLOAD_SIZE)
    if total > 255:
        raise ValueError("too much compressed data for one transfer")
    frames = []
    for idx in range(total):
        chunk = compressed[idx * DATA_PAYLOAD_SIZE : (idx + 1) * DATA_PAYLOAD_SIZE]
        pkt = bytearray(506)
        pkt[0] = 0xAA
        pkt[1] = 0xBB
        pkt[4] = idx
        pkt[5] = total
        pkt[6 : 6 + len(chunk)] = chunk
        pkt[2:4] = (sum(pkt[4:506]) & 0xFFFF).to_bytes(2, "little")
        frame = bytearray(512)
        frame[0] = 0x7E
        frame[1] = 0x5A
        frame[2:4] = (508).to_bytes(2, "little")
        frame[4] = 0x10
        frame[5] = 0x02
        frame[6:512] = pkt
        frames.append(bytes(frame))
    return frames


def prepare_print(image: Image.Image, density: int, printhead_width_dots: int = PRINTHEAD_WIDTH_DOTS) -> tuple[bytes, int]:
    raster, width, height = image_to_row_major_1bpp(image)
    col_data, cols, _input_bpl = raster_to_column_major(raster, width, height)
    canvas, canvas_bpl = center_in_printhead(col_data, cols, width, printhead_width_dots)
    buffers = split_into_buffers(canvas, canvas_bpl, cols, density)
    compressed, avg = compress_buffers(buffers)
    return compressed, calc_speed(avg)


async def scan(_args):
    try:
        devices = await asyncio.wait_for(BleakScanner.discover(timeout=8, return_adv=True), timeout=15)
    except TimeoutError:
        raise RuntimeError(
            "Bluetooth adapter did not become ready. On macOS, run this from Terminal "
            "and allow Bluetooth permission for Terminal/Python in System Settings."
        )
    for device, adv in devices.values():
        uuids = [u.lower() for u in adv.service_uuids]
        matched = any(u in SERVICE_PATTERNS for u in uuids)
        name = device.name or adv.local_name or ""
        looks_like_supvan = bool(re.match(r"^[TGD]\d{2}", name))
        if matched or name:
            mark = "*" if matched or looks_like_supvan else " "
            print(f"{mark} {device.address}  {name}  services={','.join(uuids)}")


async def probe(args):
    async with E12Ble(args.address) as printer:
        print(f"notify={printer.notify_char} write={printer.write_char}")
        print(f"check={await printer.check()}")
        print(f"status={await printer.status()}")
        mat_resp = await printer.command(CMD_RETURN_MAT, require=True)
        print(f"material={material_summary(mat_resp or b'')}")


async def print_text(args):
    image = render_text(args.text, args.label_width_mm, args.length_mm)
    compressed, speed = prepare_print(image, args.density)
    print(f"prepared {len(compressed)} compressed bytes, speed={speed}")
    async with E12Ble(args.address) as printer:
        try:
            await printer.print_compressed(
                compressed,
                speed,
                final_data_ack=args.final_data_ack,
                buf_full_before_data=args.buf_full_before_data,
                wait_after_start=not args.no_wait_after_start,
                stop_after_transfer=args.stop_after_transfer,
                stop_after_transfer_delay=args.stop_delay,
                poll_completion=not args.no_poll_completion,
                ignore_ribbon_end=args.ignore_ribbon_end,
                progress=print,
            )
        except Exception:
            await printer.command(CMD_STOP_PRINT)
            raise
    print("done")


async def print_image(args):
    image = render_image(args.image, args.label_width_mm, args.length_mm)
    compressed, speed = prepare_print(image, args.density)
    print(f"prepared {len(compressed)} compressed bytes, speed={speed}")
    async with E12Ble(args.address) as printer:
        try:
            await printer.print_compressed(
                compressed,
                speed,
                final_data_ack=args.final_data_ack,
                buf_full_before_data=args.buf_full_before_data,
                wait_after_start=not args.no_wait_after_start,
                stop_after_transfer=args.stop_after_transfer,
                stop_after_transfer_delay=args.stop_delay,
                poll_completion=not args.no_poll_completion,
                ignore_ribbon_end=args.ignore_ribbon_end,
                progress=print,
            )
        except Exception:
            await printer.command(CMD_STOP_PRINT)
            raise
    print("done")


async def dry_run(args):
    if args.image:
        image = render_image(args.image, args.label_width_mm, args.length_mm)
    else:
        image = render_text(args.text, args.label_width_mm, args.length_mm)
    compressed, speed = prepare_print(image, args.density)
    frames = build_data_frames(compressed)
    print(
        {
            "image_px": image.size,
            "compressed_bytes": len(compressed),
            "speed": speed,
            "data_frames": len(frames),
            "lzma_header": compressed[:13].hex(" "),
            "first_frame_prefix": frames[0][:16].hex(" ") if frames else "",
        }
    )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("scan")
    p.set_defaults(func=scan)

    p = sub.add_parser("probe")
    p.add_argument("address")
    p.set_defaults(func=probe)

    p = sub.add_parser("print-text")
    p.add_argument("address")
    p.add_argument("text")
    p.add_argument("--label-width-mm", type=int, default=12)
    p.add_argument("--length-mm", type=int, default=40)
    p.add_argument("--density", type=int, default=4)
    p.add_argument("--final-data-ack", action="store_true")
    p.add_argument("--buf-full-before-data", action="store_true")
    p.add_argument("--no-wait-after-start", action="store_true")
    p.add_argument("--stop-after-transfer", action="store_true")
    p.add_argument("--stop-delay", type=float, default=0.2)
    p.add_argument("--no-poll-completion", action="store_true")
    p.add_argument("--ignore-ribbon-end", action="store_true")
    p.set_defaults(func=print_text)

    p = sub.add_parser("print-image")
    p.add_argument("address")
    p.add_argument("image")
    p.add_argument("--label-width-mm", type=int, default=12)
    p.add_argument("--length-mm", type=int, default=40)
    p.add_argument("--density", type=int, default=4)
    p.add_argument("--final-data-ack", action="store_true")
    p.add_argument("--buf-full-before-data", action="store_true")
    p.add_argument("--no-wait-after-start", action="store_true")
    p.add_argument("--stop-after-transfer", action="store_true")
    p.add_argument("--stop-delay", type=float, default=0.2)
    p.add_argument("--no-poll-completion", action="store_true")
    p.add_argument("--ignore-ribbon-end", action="store_true")
    p.set_defaults(func=print_image)

    p = sub.add_parser("dry-run")
    p.add_argument("text", nargs="?", default="HELLO E12")
    p.add_argument("--image")
    p.add_argument("--label-width-mm", type=int, default=12)
    p.add_argument("--length-mm", type=int, default=40)
    p.add_argument("--density", type=int, default=4)
    p.set_defaults(func=dry_run)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
