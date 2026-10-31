#!/usr/bin/env python3
"""USB-only audio diagnostics. Nothing is uploaded or recorded automatically."""
import argparse
import json
import math
from pathlib import Path
import struct
import sys
import wave

from transport import RATE, TransportError, load_wav, open_device, save_wav


def tone(duration=0.3, frequency=440.0, volume=0.08):
    if not 0.01 <= duration <= 10 or not 20 <= frequency <= 7000 or not 0 < volume <= 0.2:
        raise ValueError("Tone limits: duration 0.01–10 s, frequency 20–7000 Hz, volume 0–0.2.")
    frames = int(duration * RATE)
    ramp = max(1, min(int(RATE * 0.01), frames // 2))
    return b"".join(struct.pack("<h", round(32767 * volume * min(1, n / ramp, (frames - 1 - n) / ramp)
                                           * math.sin(2 * math.pi * frequency * n / RATE)))
                    for n in range(frames))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/cu.usbmodem1101")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Read display and audio metadata without claiming audio")
    sub.add_parser("lock", help="Lock the microphone and verify that it stopped")
    sub.add_parser("unlock", help="Explicitly allow a fresh physical blue-button press; never start recording")
    capture = sub.add_parser("capture", help="Save one completed blue-button recording to a new WAV")
    capture.add_argument("--output", type=Path, required=True)
    play = sub.add_parser("play", help="Queue a mono PCM16 16 kHz WAV (maximum 10 seconds)")
    play.add_argument("wav", type=Path)
    selftest = sub.add_parser("selftest", help="Play a brief synthetic tone; never start the microphone")
    selftest.add_argument("--duration", type=float, default=0.3)
    selftest.add_argument("--frequency", type=float, default=440)
    args = parser.parse_args(argv)
    try:
        data = None
        if args.command == "play":
            data = load_wav(args.wav)
        elif args.command == "selftest":
            data = tone(args.duration, args.frequency)
        elif args.command == "capture":
            if args.output.exists() or not args.output.parent.is_dir():
                raise ValueError("Use a new output filename inside an existing directory.")
        with open_device(args.port) as client:
            if args.command == "status":
                info = client.info()
                result = {"device": client.status(), "audio": info.fields}
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command == "capture":
                data = client.capture()
                save_wav(args.output, data)
                print(f"Saved {len(data) / (RATE * 2):.2f} seconds to {args.output.resolve()}")
            elif args.command in {"lock", "unlock"}:
                info = client.set_guard(args.command == "lock")
                print(json.dumps({"guard": info.fields["guard"], "mic": info.fields.get("mic"),
                                  "message": "Microphone locked." if args.command == "lock"
                                  else "Physical recording enabled; release blue, then press again. No recording was started remotely."}))
            else:
                if args.command == "selftest":
                    client.set_guard(True)
                client.play(data)
                print(f"Queued {len(data) / (RATE * 2):.2f} seconds for playback (audibility unverified).")
        return 0
    except (TransportError, OSError, ValueError, wave.Error, EOFError) as exc:
        print(f"Audio operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
