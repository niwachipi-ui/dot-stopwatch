#!/usr/bin/env python3
"""Explicit-PID Core Audio tap prototype; no drivers or private APIs.

Import and --help do not load Core Audio. Read-only inventory is separate from
device creation and capture. This file never changes the default audio device.
Native runtime behavior and third-party microphone selection need validation.
"""
import argparse
import array
import ctypes as C
import json
import math
import os
import plistlib
import queue
import signal
import sys
import time
import uuid
import wave
from pathlib import Path


def fourcc(text):
    if len(text) != 4:
        raise ValueError("A selector has exactly four characters")
    return int.from_bytes(text.encode("ascii"), "big")


def checked_pids(values):
    values = list(values)
    if (not values or any(not isinstance(pid, int) or isinstance(pid, bool)
                          or not 0 < pid <= 2147483647 for pid in values)):
        raise ValueError("One or more explicit positive process IDs are required")
    return list(dict.fromkeys(values))


def aggregate_description(tap_uid, public, output_clock_uid=None):
    description = {
        "name": "Dot Watch Mic" if public else "Dot Watch Reply Capture",
        "uid": "local.dot-watch." + str(uuid.uuid4()),
        "private": 0 if public else 1,
        "taps": [{"uid": tap_uid, "drift": 1}],
    }
    # AudioHardware.h requires a private aggregate for tapautostart.
    if not public:
        description["tapautostart"] = 1
    if output_clock_uid:
        description["master"] = output_clock_uid
        description["subdevices"] = [{"uid": output_clock_uid}]
    return description


def output_clock_uid(native, device_id):
    """Only an explicitly selected, current, output-only default can clock a tap."""
    if device_id <= 0 or device_id != native.uint(1, "dOut"):
        raise ValueError("Clock ID must match the current default output device")
    if native.object_ids(device_id, "stm#", "inpt"):
        raise ValueError("Clock device has hardware input streams; refusing to add it")
    if not native.object_ids(device_id, "stm#", "outp"):
        raise ValueError("Clock device has no output streams")
    uid = native.string(device_id, "uid ")
    if not uid:
        raise ValueError("Clock device has no UID")
    return uid


class Address(C.Structure):
    _fields_ = [("selector", C.c_uint32), ("scope", C.c_uint32),
                ("element", C.c_uint32)]


class ASBD(C.Structure):
    _fields_ = [("sample_rate", C.c_double), ("format_id", C.c_uint32),
                ("format_flags", C.c_uint32), ("bytes_per_packet", C.c_uint32),
                ("frames_per_packet", C.c_uint32), ("bytes_per_frame", C.c_uint32),
                ("channels_per_frame", C.c_uint32), ("bits_per_channel", C.c_uint32),
                ("reserved", C.c_uint32)]


class AudioBuffer(C.Structure):
    _fields_ = [("channels", C.c_uint32), ("byte_size", C.c_uint32),
                ("data", C.c_void_p)]


class OneBufferList(C.Structure):
    _fields_ = [("count", C.c_uint32), ("buffers", AudioBuffer * 1)]


IOPROC = C.CFUNCTYPE(C.c_int32, C.c_uint32, C.c_void_p, C.c_void_p,
                    C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p)


def check(status, operation):
    if status:
        code = (status & 0xFFFFFFFF).to_bytes(4, "big")
        label = code.decode("ascii", errors="replace")
        raise RuntimeError(f"{operation}: OSStatus {status} ({label!r})")


def trace(operation, **values):
    print(json.dumps({"native_step": operation, "pid": os.getpid(),
                      "monotonic": round(time.monotonic(), 3), **values}),
          file=sys.stderr, flush=True)


def wait_public_lifetime(seconds, stop, run_loop=None):
    """Keep the owner alive, optionally servicing its main-loop notifications."""
    deadline = time.monotonic() + seconds
    while not stop[0] and time.monotonic() < deadline:
        duration = min(0.1, deadline - time.monotonic())
        if duration <= 0:
            break
        started = time.monotonic()
        if run_loop is not None:
            run_loop(duration)
        # CFRunLoopRunInMode can return immediately when no source is attached.
        # Avoid spinning while retaining the existing short cancellation bound.
        remaining = duration - (time.monotonic() - started)
        if not stop[0] and remaining > 0:
            time.sleep(remaining)


class Native:
    def __init__(self):
        if sys.platform != "darwin":
            raise RuntimeError("This prototype requires macOS 14.2 or newer")
        self.ca = C.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.foundation = C.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        self.objc = C.CDLL("/usr/lib/libobjc.A.dylib")
        self.objc.objc_getClass.argtypes = [C.c_char_p]
        self.objc.objc_getClass.restype = C.c_void_p
        self.objc.sel_registerName.argtypes = [C.c_char_p]
        self.objc.sel_registerName.restype = C.c_void_p
        self.bind(self.ca, "AudioObjectGetPropertyDataSize", C.c_int32,
                  C.c_uint32, C.POINTER(Address), C.c_uint32, C.c_void_p,
                  C.POINTER(C.c_uint32))
        self.bind(self.ca, "AudioObjectGetPropertyData", C.c_int32,
                  C.c_uint32, C.POINTER(Address), C.c_uint32, C.c_void_p,
                  C.POINTER(C.c_uint32), C.c_void_p)
        self.bind(self.cf, "CFRelease", None, C.c_void_p)
        self.bind(self.cf, "CFDataCreate", C.c_void_p,
                  C.c_void_p, C.c_void_p, C.c_long)
        self.bind(self.cf, "CFPropertyListCreateWithData", C.c_void_p,
                  C.c_void_p, C.c_void_p, C.c_ulong, C.c_void_p,
                  C.POINTER(C.c_void_p))
        self.bind(self.cf, "CFStringGetLength", C.c_long, C.c_void_p)
        self.bind(self.cf, "CFStringGetMaximumSizeForEncoding", C.c_long,
                  C.c_long, C.c_uint32)
        self.bind(self.cf, "CFStringGetCString", C.c_bool,
                  C.c_void_p, C.c_void_p, C.c_long, C.c_uint32)
        for name in ["AudioHardwareCreateProcessTap", "AudioHardwareCreateAggregateDevice"]:
            if not hasattr(self.ca, name):
                raise RuntimeError("Core Audio taps require macOS 14.2 or newer")
            self.bind(self.ca, name, C.c_int32, C.c_void_p, C.POINTER(C.c_uint32))
        for name in ["AudioHardwareDestroyProcessTap", "AudioHardwareDestroyAggregateDevice"]:
            self.bind(self.ca, name, C.c_int32, C.c_uint32)
        self.bind(self.ca, "AudioDeviceCreateIOProcID", C.c_int32,
                  C.c_uint32, IOPROC, C.c_void_p, C.POINTER(C.c_void_p))
        for name in ["AudioDeviceDestroyIOProcID", "AudioDeviceStart", "AudioDeviceStop"]:
            self.bind(self.ca, name, C.c_int32, C.c_uint32, C.c_void_p)

    @staticmethod
    def bind(lib, name, returns, *arguments):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = returns, list(arguments)

    def msg(self, obj, selector, returns=C.c_void_p, argtypes=(), args=()):
        fn = C.CFUNCTYPE(returns, C.c_void_p, C.c_void_p, *argtypes)(
            ("objc_msgSend", self.objc))
        return fn(obj, self.objc.sel_registerName(selector.encode()), *args)

    def run_loop_pump(self):
        # AudioHardwareDeprecated.h documents the main CFRunLoop as the HAL's
        # default notification loop. This opt-in diagnostic changes no routing.
        self.bind(self.cf, "CFRunLoopRunInMode", C.c_int32,
                  C.c_void_p, C.c_double, C.c_ubyte)
        mode = C.c_void_p.in_dll(self.cf, "kCFRunLoopDefaultMode")
        return lambda seconds: self.cf.CFRunLoopRunInMode(mode, seconds, True)

    def property_bytes(self, obj, selector, scope="glob", qualifier=None):
        address = Address(fourcc(selector), fourcc(scope), 0)
        size = C.c_uint32()
        qlen = C.sizeof(qualifier) if qualifier is not None else 0
        qptr = C.byref(qualifier) if qualifier is not None else None
        check(self.ca.AudioObjectGetPropertyDataSize(obj, C.byref(address), qlen,
                                                     qptr, C.byref(size)),
              f"read size {selector} for {obj}")
        if size.value > 8 * 1024 * 1024:
            raise RuntimeError("Unexpected Core Audio property size")
        buffer = C.create_string_buffer(size.value)
        check(self.ca.AudioObjectGetPropertyData(obj, C.byref(address), qlen,
                                                 qptr, C.byref(size), buffer),
              f"read {selector} for {obj}")
        return buffer.raw[:size.value]

    def uint(self, obj, selector, scope="glob", qualifier=None):
        raw = self.property_bytes(obj, selector, scope, qualifier)
        if len(raw) != 4:
            raise RuntimeError("Expected UInt32 property")
        return int.from_bytes(raw, sys.byteorder)

    def object_ids(self, obj, selector, scope="glob"):
        raw = self.property_bytes(obj, selector, scope)
        if len(raw) % 4:
            raise RuntimeError("Invalid AudioObjectID array")
        return list((C.c_uint32 * (len(raw) // 4)).from_buffer_copy(raw))

    def string(self, obj, selector):
        raw = self.property_bytes(obj, selector)
        if len(raw) != C.sizeof(C.c_void_p):
            raise RuntimeError("Expected CFString reference")
        pointer = C.c_void_p.from_buffer_copy(raw)
        if not pointer.value:
            return ""
        try:
            length = self.cf.CFStringGetLength(pointer)
            size = self.cf.CFStringGetMaximumSizeForEncoding(length, 0x08000100) + 1
            buffer = C.create_string_buffer(size)
            if not self.cf.CFStringGetCString(pointer, buffer, size, 0x08000100):
                raise RuntimeError("Cannot decode Core Audio string")
            return buffer.value.decode("utf-8")
        finally:
            self.cf.CFRelease(pointer)

    def cf_value(self, value):
        raw = plistlib.dumps(value, fmt=plistlib.FMT_XML)
        buffer = C.create_string_buffer(raw)
        data = self.cf.CFDataCreate(None, buffer, len(raw))
        if not data:
            raise MemoryError("CFDataCreate failed")
        error = C.c_void_p()
        try:
            result = self.cf.CFPropertyListCreateWithData(None, data, 0, None,
                                                        C.byref(error))
            if not result:
                raise RuntimeError("Cannot build Core Foundation property list")
            return result
        finally:
            self.cf.CFRelease(data)
            if error.value:
                self.cf.CFRelease(error)

    def process(self, pid):
        value = C.c_int32(pid)
        obj = self.uint(1, "id2p", qualifier=value)
        if not obj:
            raise RuntimeError(f"PID {pid} has no Core Audio process object. "
                               "Its audio engine must be initialized first.")
        return obj

    def list_processes(self):
        result = []
        for obj in self.object_ids(1, "prs#"):
            try:
                result.append({"object_id": obj, "pid": self.uint(obj, "ppid"),
                               "bundle_id": self.string(obj, "pbid"),
                               "output_running": bool(self.uint(obj, "piro")),
                               "input_running": bool(self.uint(obj, "piri"))})
            except RuntimeError as exc:
                result.append({"object_id": obj, "error": str(exc)})
        return result

    def list_devices(self):
        result = []
        default_input = self.uint(1, "dIn ")
        default_output = self.uint(1, "dOut")
        system_output = self.uint(1, "sOut")
        for obj in self.object_ids(1, "dev#"):
            try:
                result.append({"object_id": obj, "name": self.string(obj, "lnam"),
                               "uid": self.string(obj, "uid "),
                               "default_input": obj == default_input,
                               "default_output": obj == default_output,
                               "system_output": obj == system_output,
                               "input_streams": self.object_ids(obj, "stm#", "inpt"),
                               "output_streams": self.object_ids(obj, "stm#", "outp")})
            except RuntimeError as exc:
                result.append({"object_id": obj, "error": str(exc)})
        return result


class Tap:
    def __init__(self, native, pids, public=False, clock_output_device=None):
        self.native, self.pids, self.public = native, checked_pids(pids), public
        self.tap_id = C.c_uint32()
        self.device_id = C.c_uint32()
        self.io_id = C.c_void_p()
        self.io_started = False
        self.callback = None
        self.description = None
        self.clock_output_device = clock_output_device
        self.tap_uid = None

    def __enter__(self):
        n = self.native
        try:
            ids = [n.process(pid) for pid in self.pids]
            clock_uid = (output_clock_uid(n, self.clock_output_device)
                         if self.clock_output_device is not None else None)
            cls = n.objc.objc_getClass(b"CATapDescription")
            if not cls:
                raise RuntimeError("CATapDescription is unavailable")
            values = n.cf_value(ids)
            description = n.msg(cls, "alloc")
            try:
                description = n.msg(description, "initMonoMixdownOfProcesses:",
                                    argtypes=(C.c_void_p,), args=(values,))
            finally:
                n.cf.CFRelease(values)
            if not description:
                raise RuntimeError("Cannot create process tap description")
            try:
                n.msg(description, "setPrivate:", None, (C.c_bool,), (not self.public,))
                # Public input: suppress player output even when nobody reads the tap.
                # Private reply capture: leave ChatGPT playback untouched in prototype.
                n.msg(description, "setMuteBehavior:", None, (C.c_long,),
                      (1 if self.public else 0,))
                trace("create_tap_before", target_pids=self.pids)
                check(n.ca.AudioHardwareCreateProcessTap(description, C.byref(self.tap_id)),
                      "create explicit-process tap")
                trace("create_tap_after", tap_id=self.tap_id.value)
            finally:
                n.msg(description, "release", None)
            tap_uid = n.string(self.tap_id.value, "tuid")
            self.tap_uid = tap_uid
            self.description = aggregate_description(tap_uid, self.public, clock_uid)
            cf_dict = n.cf_value(self.description)
            try:
                trace("create_aggregate_before", uid=self.description["uid"])
                check(n.ca.AudioHardwareCreateAggregateDevice(cf_dict,
                                                              C.byref(self.device_id)),
                      "create tap aggregate")
                trace("create_aggregate_after", device_id=self.device_id.value,
                      tap_id=self.tap_id.value, uid=self.description["uid"],
                      tap_uid=self.tap_uid, public=self.public)
            finally:
                n.cf.CFRelease(cf_dict)
            deadline = time.monotonic() + 3
            while not n.uint(self.device_id.value, "livn"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Aggregate did not become alive within 3 seconds")
                time.sleep(0.05)
            return self
        except BaseException:
            self.close()
            raise

    def format(self):
        raw = self.native.property_bytes(self.tap_id.value, "tfmt")
        if len(raw) != C.sizeof(ASBD):
            raise RuntimeError("Unexpected tap format size")
        return ASBD.from_buffer_copy(raw)

    def metadata(self):
        fmt = self.format()
        return {"device_id": self.device_id.value, "tap_id": self.tap_id.value,
                "tap_uid": self.tap_uid,
                "name": self.description["name"], "uid": self.description["uid"],
                "pids": self.pids, "public": self.public,
                "clock_output_device": self.clock_output_device,
                "clock_output_uid": self.description.get("master"),
                "alive": bool(self.native.uint(self.device_id.value, "livn")),
                "sample_rate": fmt.sample_rate, "channels": fmt.channels_per_frame,
                "input_streams": self.native.object_ids(self.device_id.value,
                                                         "stm#", "inpt")}

    def start_capture(self, callback):
        if self.public:
            raise RuntimeError("This prototype does not capture the public microphone")
        self.callback = IOPROC(callback)
        trace("create_ioproc_before", device_id=self.device_id.value)
        check(self.native.ca.AudioDeviceCreateIOProcID(self.device_id.value,
                                                      self.callback, None,
                                                      C.byref(self.io_id)), "create input callback")
        trace("create_ioproc_after", io_id=self.io_id.value)
        trace("start_ioproc_before", device_id=self.device_id.value)
        check(self.native.ca.AudioDeviceStart(self.device_id.value, self.io_id),
              "start explicit-process capture (system permission may be required)")
        self.io_started = True
        trace("start_ioproc_after", device_id=self.device_id.value)

    def close(self):
        n = self.native
        errors = []
        if self.io_started:
            trace("stop_ioproc_before", device_id=self.device_id.value)
            status = n.ca.AudioDeviceStop(self.device_id.value, self.io_id)
            trace("stop_ioproc_after", status=status)
            if status:
                errors.append(("stop IO", status))
            self.io_started = False
        if self.io_id.value:
            trace("destroy_ioproc_before", device_id=self.device_id.value)
            status = n.ca.AudioDeviceDestroyIOProcID(self.device_id.value, self.io_id)
            trace("destroy_ioproc_after", status=status)
            if status:
                errors.append(("destroy IO", status))
            self.io_id.value = None
        if self.device_id.value:
            trace("destroy_aggregate_before", device_id=self.device_id.value)
            status = n.ca.AudioHardwareDestroyAggregateDevice(self.device_id.value)
            trace("destroy_aggregate_after", status=status)
            if status:
                errors.append(("destroy aggregate", status))
            self.device_id.value = 0
        if self.tap_id.value:
            trace("destroy_tap_before", tap_id=self.tap_id.value)
            status = n.ca.AudioHardwareDestroyProcessTap(self.tap_id.value)
            trace("destroy_tap_after", status=status)
            if status:
                errors.append(("destroy tap", status))
            self.tap_id.value = 0
        if errors:
            print(json.dumps({"cleanup_errors": errors}), file=sys.stderr)

    def __exit__(self, *_):
        self.close()


def float32_to_pcm16(data):
    values = array.array("f")
    values.frombytes(data)
    result = array.array("h", (max(-32768, min(32767, round(value * 32767)))
                              if math.isfinite(value) else 0
                              for value in values))
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


def capture(tap, destination, seconds, stop):
    fmt = tap.format()
    if not (fmt.format_id == fourcc("lpcm") and fmt.format_flags & 1
            and not fmt.format_flags & 2 and fmt.bits_per_channel == 32
            and fmt.channels_per_frame == 1 and 8000 <= fmt.sample_rate <= 192000):
        raise RuntimeError("Prototype capture requires mono little-endian float32 PCM")
    chunks = queue.Queue(maxsize=128)
    counts = {"frames": 0, "dropped_blocks": 0, "invalid_blocks": 0,
              "nonzero_samples": 0}

    def on_audio(_device, _now, input_data, _input_time, _output, _output_time, _client):
        try:
            if not input_data:
                return 0
            buffers = C.cast(input_data, C.POINTER(OneBufferList)).contents
            if buffers.count != 1:
                counts["invalid_blocks"] += 1
                return 0
            buffer = buffers.buffers[0]
            if not buffer.data or not buffer.byte_size:
                return 0
            if buffer.channels != 1 or buffer.byte_size % 4 or buffer.byte_size > 1024 * 1024:
                counts["invalid_blocks"] += 1
                return 0
            chunk = C.string_at(buffer.data, buffer.byte_size)
            try:
                chunks.put_nowait(chunk)
            except queue.Full:
                counts["dropped_blocks"] += 1
        except Exception:
            counts["invalid_blocks"] += 1
        return 0

    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file, wave.open(file, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(round(fmt.sample_rate))
        tap.start_capture(on_audio)
        deadline = time.monotonic() + seconds
        while not stop[0] and time.monotonic() < deadline:
            try:
                chunk = chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            pcm = float32_to_pcm16(chunk)
            counts["frames"] += len(pcm) // 2
            values = array.array("h", pcm)
            counts["nonzero_samples"] += sum(value != 0 for value in values)
            wav.writeframesraw(pcm)
        # Stop callback before leaving the wave writer and before printing results.
        trace("capture_stop_before", device_id=tap.device_id.value)
        check(tap.native.ca.AudioDeviceStop(tap.device_id.value, tap.io_id), "stop capture")
        trace("capture_stop_after", device_id=tap.device_id.value)
        tap.io_started = False
    print(json.dumps({"capture": str(destination), **counts}), flush=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--list-processes", action="store_true", help="Read-only audio process inventory")
    modes.add_argument("--list-devices", action="store_true", help="Read-only device inventory")
    modes.add_argument("--create-input-for-pid", type=int, metavar="PID",
                       help="Publish Dot Watch Mic from ONLY this player's output; no capture in this process")
    modes.add_argument("--capture-reply-pids", type=int, nargs="+", metavar="PID",
                       help="Capture only these explicitly selected output processes")
    p.add_argument("--seconds", type=float, default=30,
                   help="Bounded lifetime, 1 to 600 seconds (default 30)")
    p.add_argument("--output", type=Path, help="New private WAV file for explicit capture mode")
    clock = p.add_mutually_exclusive_group()
    clock.add_argument("--clock-output-device", type=int, metavar="ID",
                   help="Optional explicit default output-only device ID as aggregate clock; never a mic")
    clock.add_argument("--clock-output-uid", metavar="UID",
                       help="Stable output-only clock UID when crossing process boundaries")
    p.add_argument("--service-run-loop", action="store_true",
                   help="Public publisher diagnostic: service its main CoreFoundation run loop")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not 1 <= args.seconds <= 600:
        p.error("--seconds must be between 1 and 600")
    if bool(args.capture_reply_pids) != bool(args.output):
        p.error("--output is required only with --capture-reply-pids")
    if (args.clock_output_device is not None or args.clock_output_uid is not None) and (args.list_processes or args.list_devices):
        p.error("--clock-output-device requires an explicit tap mode")
    if args.service_run_loop and args.create_input_for_pid is None:
        p.error("--service-run-loop requires --create-input-for-pid")
    if args.create_input_for_pid is not None:
        checked_pids([args.create_input_for_pid])
    if args.capture_reply_pids is not None:
        checked_pids(args.capture_reply_pids)
    n = Native()
    clock_id = args.clock_output_device
    if args.clock_output_uid is not None:
        clock_id = n.uint(1, "dOut")
        if output_clock_uid(n, clock_id) != args.clock_output_uid:
            raise ValueError("Current output-only clock does not match the requested stable UID")
    if args.list_processes:
        print(json.dumps(n.list_processes(), indent=2))
        return
    if args.list_devices:
        print(json.dumps(n.list_devices(), indent=2))
        return
    stop = [False]
    def stop_handler(_signal, _frame):
        stop[0] = True
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    public = args.create_input_for_pid is not None
    pids = [args.create_input_for_pid] if public else args.capture_reply_pids
    with Tap(n, pids, public, clock_id) as tap:
        print(json.dumps({"ready": tap.metadata()}), flush=True)
        if public:
            pump = n.run_loop_pump() if args.service_run_loop else None
            if pump is not None:
                trace("public_run_loop_enabled", device_id=tap.device_id.value)
            wait_public_lifetime(args.seconds, stop, pump)
        else:
            capture(tap, args.output, args.seconds, stop)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Core Audio prototype: {exc}", file=sys.stderr)
        sys.exit(1)
