#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import time

from katasymbol_e12 import (
    CMD_BUF_FULL,
    CMD_CHECK_DEVICE,
    CMD_INQUIRY_STA,
    CMD_NEXT_ZIPPEDBULK,
    CMD_START_PRINT,
    CMD_STOP_PRINT,
    DEFAULT_MARGIN_DOTS,
    E12Ble,
    PRINT_BUF_HEADER,
    PRINT_BUF_SIZE,
    build_data_frames,
    calc_speed,
    compress_buffers,
    make_cmd,
)


ADDR_DEFAULT = os.environ.get("KATASYMBOL_E12_ADDRESS", "")


def hx(data):
    return None if data is None else data.hex(" ")


def checksum_header(buf, cols, bpl):
    data_end = cols * bpl + PRINT_BUF_HEADER
    chk = sum(buf[2:14])
    for i in range(1, data_end // 256 + 1):
        idx = i * 256 - 1
        if idx < len(buf):
            chk += buf[idx]
    buf[0:2] = (chk & 0xFFFF).to_bytes(2, "little")


def make_buffer(payload, bpl, cols, density=4, mat=1, page0=0x0E, top=8, bottom=8, density_byte=None):
    buf = bytearray(PRINT_BUF_SIZE)
    buf[2] = page0 & 0xFF
    buf[3] = ((density & 0x0F) << 2) | ((mat & 0x03) << 6)
    buf[4:6] = cols.to_bytes(2, "little")
    buf[6] = bpl
    buf[8:10] = top.to_bytes(2, "little")
    buf[10:12] = bottom.to_bytes(2, "little")
    buf[12] = density if density_byte is None else density_byte
    buf[PRINT_BUF_HEADER : PRINT_BUF_HEADER + len(payload)] = payload[: PRINT_BUF_SIZE - PRINT_BUF_HEADER]
    checksum_header(buf, cols, bpl)
    return bytes(buf)


def pattern(width_dots, length_dots, bpl, kind):
    data = bytearray(length_dots * bpl)
    if kind == "full_band":
        for col in range(40, min(80, length_dots)):
            for dot in range(width_dots):
                data[col * bpl + dot // 8] |= 1 << (dot % 8)
    elif kind == "left_band":
        for col in range(40, min(80, length_dots)):
            for dot in range(0, min(24, width_dots)):
                data[col * bpl + dot // 8] |= 1 << (dot % 8)
    elif kind == "right_band":
        for col in range(40, min(80, length_dots)):
            for dot in range(max(0, width_dots - 24), width_dots):
                data[col * bpl + dot // 8] |= 1 << (dot % 8)
    elif kind == "columns":
        for col in range(24, min(160, length_dots), 16):
            for dot in range(width_dots):
                data[col * bpl + dot // 8] |= 1 << (dot % 8)
    elif kind == "bytes":
        for col in range(40, min(80, length_dots)):
            for byte in range(bpl):
                data[col * bpl + byte] = 0xFF
    else:
        raise ValueError(f"unknown pattern {kind}")
    return bytes(data)


def build_job(args):
    width_dots = args.width_dots
    length_dots = args.length_mm * 8
    bpl = args.bpl or ((width_dots + 7) // 8)
    raw = pattern(width_dots, length_dots, bpl, args.pattern)
    cols = length_dots - args.margin_top - args.margin_bottom
    start = args.margin_top * bpl
    payload = raw[start : start + cols * bpl]
    buf = make_buffer(
        payload,
        bpl,
        cols,
        density=args.density,
        mat=args.mat,
        page0=args.page0,
        top=args.margin_top,
        bottom=args.margin_bottom,
        density_byte=args.density_byte,
    )
    compressed, avg = compress_buffers([buf])
    speed = args.speed if args.speed is not None else calc_speed(avg)
    frames = build_data_frames(compressed)
    return {
        "width_dots": width_dots,
        "length_dots": length_dots,
        "bpl": bpl,
        "cols": cols,
        "buffer": buf,
        "compressed": compressed,
        "avg": avg,
        "speed": speed,
        "frames": frames,
    }


async def raw_cmd(printer, log, name, cmd, p1=0, p2=0, require=True):
    tx = make_cmd(cmd, p1, p2)
    log.append({"event": "tx_cmd", "name": name, "cmd": cmd, "param1": p1, "param2": p2, "hex": hx(tx)})
    resp = await printer.command(cmd, p1, p2, require=require)
    log.append({"event": "rx_cmd", "name": name, "hex": hx(resp)})
    print(name, hx(resp), flush=True)
    return resp


async def run_job(args):
    job = build_job(args)
    log = [
        {
            "event": "job",
            "args": vars(args),
            "width_dots": job["width_dots"],
            "length_dots": job["length_dots"],
            "bpl": job["bpl"],
            "cols": job["cols"],
            "buf_header": hx(job["buffer"][:32]),
            "compressed_len": len(job["compressed"]),
            "lzma_header": hx(job["compressed"][:32]),
            "speed": job["speed"],
            "frames": len(job["frames"]),
        }
    ]
    print(json.dumps(log[0], indent=2), flush=True)

    async with E12Ble(args.address) as printer:
        await raw_cmd(printer, log, "CHECK", CMD_CHECK_DEVICE)
        await raw_cmd(printer, log, "STATUS0", CMD_INQUIRY_STA, require=False)
        await raw_cmd(printer, log, "START", CMD_START_PRINT)
        await asyncio.sleep(args.after_start_delay)
        if args.status_after_start:
            await raw_cmd(printer, log, "STATUS1", CMD_INQUIRY_STA, require=False)
        await raw_cmd(printer, log, "NEXT", CMD_NEXT_ZIPPEDBULK, args.block_size, len(job["frames"]))
        if args.buf_full_before:
            await raw_cmd(printer, log, "BUF_FULL_BEFORE", CMD_BUF_FULL, len(job["compressed"]), job["speed"], require=False)
        for i, frame in enumerate(job["frames"]):
            log.append({"event": "tx_data", "idx": i, "hex_prefix": hx(frame[:32])})
            print("DATA_TX", i + 1, frame[:16].hex(" "), flush=True)
            resp = await printer.send_data_frame(frame, read_response=args.data_ack)
            log.append({"event": "rx_data", "idx": i, "hex": hx(resp)})
            print("DATA_RX", i + 1, hx(resp), flush=True)
        if not args.buf_full_before:
            await raw_cmd(printer, log, "BUF_FULL_AFTER", CMD_BUF_FULL, len(job["compressed"]), job["speed"], require=False)
        print(f"observe for {args.observe}s", flush=True)
        await asyncio.sleep(args.observe)
        if args.stop:
            await raw_cmd(printer, log, "STOP", CMD_STOP_PRINT, require=False)

    if args.log:
        with open(args.log, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default=ADDR_DEFAULT)
    parser.add_argument("--width-dots", type=int, default=96)
    parser.add_argument("--length-mm", type=int, default=40)
    parser.add_argument("--bpl", type=int)
    parser.add_argument("--pattern", choices=["full_band", "left_band", "right_band", "columns", "bytes"], default="full_band")
    parser.add_argument("--density", type=int, default=4)
    parser.add_argument("--density-byte", type=int)
    parser.add_argument("--mat", type=int, default=1)
    parser.add_argument("--page0", type=lambda x: int(x, 0), default=0x0E)
    parser.add_argument("--margin-top", type=int, default=8)
    parser.add_argument("--margin-bottom", type=int, default=8)
    parser.add_argument("--speed", type=int)
    parser.add_argument("--block-size", type=int, default=512)
    parser.add_argument("--buf-full-before", action="store_true")
    parser.add_argument("--data-ack", action="store_true")
    parser.add_argument("--status-after-start", action="store_true")
    parser.add_argument("--after-start-delay", type=float, default=0.2)
    parser.add_argument("--observe", type=float, default=2.0)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--log")
    args = parser.parse_args()
    if not args.address:
        parser.error("provide --address or set KATASYMBOL_E12_ADDRESS")
    asyncio.run(run_job(args))


if __name__ == "__main__":
    main()
