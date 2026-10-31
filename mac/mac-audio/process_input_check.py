"""Read-only snapshot of explicitly selected processes' current audio inputs.

Import is inert. This never opens audio, reads samples, changes a device, or
inspects an application's UI. A match describes only the requested PIDs at
read time; it cannot prove a saved pre-call selection or prevent later fallback.
"""
import argparse
import json
import uuid

from coreaudio_tap import Native, checked_pids


def checked_uid(value):
    if value == "BlackHole2ch_UID":
        return value
    prefix = "local.dot-watch."
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("--uid must be the staged local.dot-watch.<UUID> or exact BlackHole2ch_UID")
    suffix = value[len(prefix):]
    try:
        valid = str(uuid.UUID(suffix)) == suffix
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("--uid must be the staged local.dot-watch.<UUID> or exact BlackHole2ch_UID")
    return value


def check_process_inputs(native, expected_uid, pids):
    """Read public CoreAudio properties only; errors and active mismatches fail."""
    expected_uid = checked_uid(expected_uid)
    pids = checked_pids(pids)
    entries = []
    for pid in pids:
        entry = {"pid": pid}
        try:
            process = native.process(pid)  # Resolve in this observer's namespace.
            if not isinstance(process, int) or process <= 0:
                raise RuntimeError("No CoreAudio process object for the requested PID")
            running = native.uint(process, "piri")
            if running not in (0, 1):
                raise RuntimeError("Unexpected input-running state")
            devices = native.object_ids(process, "pdv#", "inpt")
            inputs = []
            for device in devices:
                device_uid = native.string(device, "uid ")
                if not isinstance(device_uid, str) or not device_uid:
                    raise RuntimeError("An input device has no readable UID")
                inputs.append({"device_id": device, "uid": device_uid})
            entry.update(process_object_id=process, input_running=bool(running),
                         inputs=inputs)
            if not running:
                entry["state"] = "inactive"
            elif len(inputs) == 1 and inputs[0]["uid"] == expected_uid:
                entry["state"] = "matched"
            else:
                entry["state"] = "mismatch"
        except (RuntimeError, ValueError, OSError) as exc:
            entry.update(state="unreadable", error=f"{type(exc).__name__}: {exc}"[:240])
        entries.append(entry)
    states = [entry["state"] for entry in entries]
    if "unreadable" in states:
        reason = "unreadable"
    elif "mismatch" in states:
        reason = "input_mismatch"
    elif "matched" not in states:
        reason = "no_active_input"
    else:
        reason = "verified_active_input"
    return {"ok": reason == "verified_active_input", "reason": reason,
            "expected_uid": expected_uid, "scope": "requested_pids",
            "snapshot_only": True, "audio_started": False, "processes": entries}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True, help="Exact staged Dot Watch Mic UID")
    parser.add_argument("--pids", required=True, type=int, nargs="+",
                        help="Explicit positive process IDs to inspect")
    args = parser.parse_args(argv)
    try:
        expected_uid, pids = checked_uid(args.uid), checked_pids(args.pids)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        result = check_process_inputs(Native(), expected_uid, pids)
    except (RuntimeError, ValueError, OSError) as exc:
        result = {"ok": False, "reason": "native_unavailable", "snapshot_only": True,
                  "audio_started": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
