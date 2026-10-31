"""Read display-only firmware status and capture its rendered framebuffer."""
from pathlib import Path
import argparse
import serial
import time

parser = argparse.ArgumentParser()
parser.add_argument("--port", default="/dev/cu.usbmodem1101")
parser.add_argument("--capture", type=Path)
args = parser.parse_args()
with serial.Serial(args.port, 115200, timeout=2) as connection:
    connection.reset_input_buffer()
    connection.write(b"status\n")
    deadline = time.monotonic() + 8
    status = b""
    while time.monotonic() < deadline:
        line = connection.readline()
        if line.startswith(b"DOT_READY "):
            status = line
            break
    if not status:
        raise SystemExit("No DOT_READY response; firmware startup is unverified.")
    print(status.decode().strip())
    if args.capture:
        connection.write(b"capture\n")
        header = connection.readline() + connection.readline() + connection.readline()
        if header != b"P6\n466 466\n255\n":
            raise SystemExit("Unexpected framebuffer header")
        data = bytearray()
        expected = 466 * 466 * 3
        deadline = time.monotonic() + 30
        while len(data) < expected and time.monotonic() < deadline:
            data.extend(connection.read(min(8192, expected - len(data))))
        if len(data) != expected:
            raise SystemExit(f"Incomplete capture: {len(data)}/{expected}")
        args.capture.write_bytes(header + data)
        print(f"Captured 466 x 466 framebuffer: {args.capture}")
