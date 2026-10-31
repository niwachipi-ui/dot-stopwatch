"""Register and remove an IOProc on one verified public input, without starting IO.

Import is inert. Run this only in an isolated helper with an outer deadline:
Core Audio registration or removal can block inside the OS and cannot reliably
be interrupted by a Python timeout. The helper owns only its IOProc; the caller
retains ownership of the public aggregate and tap.
"""
import ctypes as C
import json

from coreaudio_tap import IOPROC, Native, check, trace
from public_input_check import validate_identity, verify_native_identity


def probe_public_input(identity):
    """Resolve the exact UID locally, register/remove its IOProc, print success.

    Never calls AudioDeviceStart, reads sample buffers, or removes the aggregate
    or tap. A successful result proves registration only, not audio delivery.
    """
    validate_identity(identity)  # Fail before Native initialization.
    native = Native()
    device = verify_native_identity(native, identity)
    io_id = C.c_void_p()

    @IOPROC
    def callback(_device, _now, _input, _input_time, _output, _output_time, _client):
        return 0

    try:
        trace("public_probe_create_ioproc_before", device_id=device,
              publisher_device_id=identity["device_id"], uid=identity["uid"])
        status = native.ca.AudioDeviceCreateIOProcID(device, callback, None, C.byref(io_id))
        trace("public_probe_create_ioproc_after", device_id=device, status=status,
              io_id=io_id.value)
        check(status, "register public-input probe IOProc")
        if not io_id.value:
            raise RuntimeError("Core Audio returned no probe IOProc ID")
    finally:
        # Keep callback referenced until its registration has been removed.
        # Even an error with a returned ID must release only this registration.
        if io_id.value:
            trace("public_probe_destroy_ioproc_before", device_id=device)
            status = native.ca.AudioDeviceDestroyIOProcID(device, io_id)
            trace("public_probe_destroy_ioproc_after", device_id=device, status=status)
            check(status, "remove public-input probe IOProc")

    print(json.dumps({"public_input_probe_complete": True, "device_id": device,
                      "publisher_device_id": identity["device_id"], "uid": identity["uid"],
                      "ioproc_registered": True, "ioproc_destroyed": True,
                      "audio_started": False}), flush=True)
    return 0
