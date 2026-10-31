"""Explicit owned-public-input test helper; import never initializes audio.

This accepts only the random aggregate identity supplied by our publisher. It
never opens a default input. Stream identity is verified before starting it.
"""
import ctypes as C
import json
import os
import signal
import threading
import wave

from coreaudio_tap import Native, float32_to_pcm16, output_clock_uid, trace
from tap_watchdog import valid_owned_resource


NAME = "Dot Watch Mic"
RATE = 48000


def validate_identity(identity):
    if (not valid_owned_resource(identity) or identity.get("public") is not True
            or identity.get("name") != NAME or identity.get("channels") != 1
            or identity.get("sample_rate") != RATE
            or not isinstance(identity.get("clock_output_uid"), str)
            or not identity["clock_output_uid"]):
        raise ValueError("Only our exact public virtual-input identity is accepted")


def cf_strings(native, device, selector):
    """Read owned CFArray<CFString> properties using public CoreFoundation APIs."""
    raw = native.property_bytes(device, selector)
    if len(raw) != C.sizeof(C.c_void_p):
        raise RuntimeError("Unexpected aggregate property layout")
    pointer = C.c_void_p.from_buffer_copy(raw)
    if not pointer.value:
        raise RuntimeError("Aggregate property is missing")
    native.bind(native.cf, "CFArrayGetCount", C.c_long, C.c_void_p)
    native.bind(native.cf, "CFArrayGetValueAtIndex", C.c_void_p, C.c_void_p, C.c_long)
    try:
        count = native.cf.CFArrayGetCount(pointer)
        if not 0 <= count <= 16:
            raise RuntimeError("Unexpected aggregate member count")
        result = []
        for index in range(count):
            value = native.cf.CFArrayGetValueAtIndex(pointer, index)
            length = native.cf.CFStringGetLength(value)
            size = native.cf.CFStringGetMaximumSizeForEncoding(length, 0x08000100) + 1
            if not 0 < size <= 4096:
                raise RuntimeError("Unexpected aggregate member UID")
            buffer = C.create_string_buffer(size)
            if not native.cf.CFStringGetCString(value, buffer, size, 0x08000100):
                raise RuntimeError("Cannot read aggregate member UID")
            result.append(buffer.value.decode("utf-8"))
        return result
    finally:
        if pointer.value:
            native.cf.CFRelease(pointer)


def verify_native_identity(native, identity, read_strings=cf_strings):
    validate_identity(identity)
    devices = native.list_devices()
    matches = [entry for entry in devices if entry.get("name") == NAME]
    uid_matches = [entry for entry in devices if entry.get("uid") == identity["uid"]]
    if (len(matches) != 1 or len(uid_matches) != 1 or matches[0].get("uid") != identity["uid"]
            or len(matches[0].get("input_streams", [])) != 1):
        observed = [{"id": entry.get("object_id"), "uid": entry.get("uid"),
                     "in": entry.get("input_streams")} for entry in matches[:2]]
        detail = ("matches=" + str(len(matches)) + " " + json.dumps(observed, separators=(",", ":")))
        if not matches:
            seen = [f"{entry.get('object_id')}:{str(entry.get('name', entry.get('error', '?')))[:32]}"
                    for entry in devices[:6]]
            detail += " seen=" + json.dumps(seen, separators=(",", ":"))
        raise RuntimeError(f"Owned virtual input mismatch: expected={identity['device_id']}/{identity['uid']}; {detail}")
    # AudioObjectIDs are local handles and have differed between these helpers.
    # Resolve each exact stable UID in this process; transferred IDs are diagnostic.
    device = matches[0]["object_id"]
    tap_matches = [obj for obj in native.object_ids(1, "tps#")
                   if native.string(obj, "tuid") == identity["tap_uid"]]
    if len(tap_matches) != 1:
        raise RuntimeError("Publisher tap UID is missing or ambiguous in this process")
    clock = native.uint(1, "dOut")
    clock_uid = output_clock_uid(native, clock)
    if clock_uid != identity["clock_output_uid"]:
        raise RuntimeError("Output-only clock UID differs from the publisher's clock")
    taps = read_strings(native, device, "tap#")
    subdevices = read_strings(native, device, "grup")
    active = native.object_ids(device, "agrp")
    if taps != [identity["tap_uid"]] or subdevices != [clock_uid] or active != [clock]:
        raise RuntimeError(f"Unexpected tap/subdevice topology: active={active}, clock={clock}, "
                           f"tap_match={taps == [identity['tap_uid']]}, "
                           f"subdevices={subdevices}, expected={clock_uid}, taps={taps}")
    if not native.uint(device, "livn"):
        raise RuntimeError("Virtual input is not alive")
    return device


def portaudio_index(devices, hostapis):
    matches = [(index, value) for index, value in enumerate(devices) if value.get("name") == NAME]
    if len(matches) != 1:
        raise RuntimeError("PortAudio virtual input is missing or ambiguous")
    index, value = matches[0]
    api = value.get("hostapi")
    if (not isinstance(api, int) or not 0 <= api < len(hostapis)
            or hostapis[api].get("name") != "Core Audio"
            or value.get("max_input_channels") != 1):
        raise RuntimeError("PortAudio device is not the expected mono CoreAudio input")
    return index


def capture_public_input(identity, destination):
    validate_identity(identity)  # Reject malformed identity before native imports.
    import numpy as np
    import sounddevice as sd
    native = Native()
    device = verify_native_identity(native, identity)
    index = portaudio_index(sd.query_devices(), sd.query_hostapis())
    # The public PortAudio Mac extension returns the CoreAudio ID of an OPEN,
    # stopped stream. It is exported by our bundled libportaudio.dylib, though
    # sounddevice's CFFI declarations omit it. Verify it before stream.start().
    library = C.CDLL(sd._libname)
    get_device = library.PaMacCore_GetStreamInputDevice
    get_device.argtypes, get_device.restype = [C.c_void_p], C.c_uint32
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stream = None
    try:
        trace("public_input_open_before", device_id=device, portaudio_index=index)
        stream = sd.InputStream(device=index, samplerate=RATE, channels=1,
                                dtype="float32", blocksize=4800)
        trace("public_input_open_after", device_id=device)
        actual = get_device(int(sd._ffi.cast("uintptr_t", stream._ptr)))
        if actual != device:
            raise RuntimeError("Opened stream maps to a different CoreAudio device; not started")
        if verify_native_identity(native, identity) != device:
            raise RuntimeError("Local virtual input handle changed before stream start")
        if stop.is_set():
            raise RuntimeError("Public input check cancelled before start")
        trace("public_input_start_before", device_id=device)
        stream.start()
        trace("public_input_start_after", device_id=device)
        # Player is still silent. Require 300 ms of digital silence before the
        # parent is told it may send the fixed synthetic tone.
        for _ in range(3):
            data, overflow = stream.read(4800)
            if stop.is_set() or overflow or not np.all(np.isfinite(data)) or np.max(np.abs(data)) > 1e-6:
                raise RuntimeError("Virtual input silence baseline could not be verified")
        print(json.dumps({"input_ready": True, "device_id": device, "uid": identity["uid"],
                          "publisher_device_id": identity["device_id"], "reader_device_id": device,
                          "silent_baseline": True}), flush=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file, wave.open(file, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            for _ in range(30):
                if stop.is_set():
                    raise RuntimeError("Public input check cancelled")
                data, overflow = stream.read(4800)
                if overflow or not np.all(np.isfinite(data)):
                    raise RuntimeError("Invalid or dropped virtual input frames")
                wav.writeframesraw(float32_to_pcm16(data.tobytes()))
        print(json.dumps({"public_input_complete": True, "frames": 144000,
                          "device_id": device, "uid": identity["uid"]}), flush=True)
    finally:
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()
