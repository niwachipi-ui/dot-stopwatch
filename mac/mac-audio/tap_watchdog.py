#!/usr/bin/env python3
"""Bound a coreaudio_tap.py trial, including potentially blocked native calls.

This runs only the sibling tap helper. It sends TERM, then KILL if necessary.
After a timeout, a separate bounded helper removes only the exact resources
reported by this run, and only if their native UIDs still match.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid


def valid_owned_resource(value):
    if not isinstance(value, dict):
        return False
    try:
        uid = value["uid"]
        prefix = "local.dot-watch."
        if not uid.startswith(prefix) or str(uuid.UUID(uid[len(prefix):])) != uid[len(prefix):]:
            return False
        return (isinstance(value["device_id"], int) and value["device_id"] > 0
                and isinstance(value["tap_id"], int) and value["tap_id"] > 0
                and isinstance(value["tap_uid"], str) and bool(value["tap_uid"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def cleanup_owned(value):
    """After child shutdown, resolve exact run UIDs in THIS native process.

    AudioObjectIDs reported by the child are diagnostic only: Core Audio may
    assign different numerical IDs to the same UID in another process.
    """
    if not valid_owned_resource(value):
        raise ValueError("Refusing cleanup without exact per-run device and tap UIDs")
    from coreaudio_tap import Native, check, trace
    n = Native()
    def exact_uid(selector, property_name, expected, label):
        matches = [object_id for object_id in dict.fromkeys(n.object_ids(1, selector))
                   if n.string(object_id, property_name) == expected]
        if len(matches) > 1:
            raise RuntimeError(f"Ambiguous {label} UID; refusing cleanup")
        return matches[0] if matches else None

    def still_owned(object_id, selector, property_name, expected, label):
        if object_id not in n.object_ids(1, selector):
            return False  # The owner may already have removed this resource.
        try:
            current_uid = n.string(object_id, property_name)
        except RuntimeError:
            if object_id not in n.object_ids(1, selector):
                return False
            raise
        if current_uid != expected:
            raise RuntimeError(f"Local {label} ID changed ownership; refusing cleanup")
        return True

    # Resolve BOTH identities before mutating either. An ambiguous tap must not
    # result in partially deleting the aggregate first. Never match by name.
    device_id = exact_uid("dev#", "uid ", value["uid"], "device")
    exact_uid("tps#", "tuid", value["tap_uid"], "tap")
    if device_id is not None and still_owned(device_id, "dev#", "uid ", value["uid"], "device"):
        trace("watchdog_destroy_owned_aggregate_before", device_id=device_id)
        check(n.ca.AudioHardwareDestroyAggregateDevice(device_id), "watchdog destroy owned aggregate")
        trace("watchdog_destroy_owned_aggregate_after", device_id=device_id)
    # Destroying an aggregate can also remove its tap. Re-resolve, accepting a
    # now-absent exact UID as successful cleanup, never using the child's ID.
    tap_id = exact_uid("tps#", "tuid", value["tap_uid"], "tap")
    if tap_id is not None and still_owned(tap_id, "tps#", "tuid", value["tap_uid"], "tap"):
        trace("watchdog_destroy_owned_tap_before", tap_id=tap_id)
        check(n.ca.AudioHardwareDestroyProcessTap(tap_id), "watchdog destroy owned tap")
        trace("watchdog_destroy_owned_tap_after", tap_id=tap_id)


def bounded_cleanup(resource):
    if not valid_owned_resource(resource):
        print(json.dumps({"watchdog_cleanup": "no complete per-run resource identity received"}),
              file=sys.stderr, flush=True)
        return
    command = [sys.executable, str(Path(__file__).resolve()),
               "--internal-cleanup-owned", json.dumps(resource)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if result.stdout:
            print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)
        print(json.dumps({"watchdog_cleanup_exit": result.returncode}), file=sys.stderr, flush=True)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills/reaps its cleanup subprocess before raising.
        print(json.dumps({"watchdog_cleanup": "native cleanup exceeded 5 seconds; inspect device inventory"}),
              file=sys.stderr, flush=True)
        if exc.stderr:
            text = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            print(text, end="", file=sys.stderr, flush=True)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--internal-cleanup-owned":
        if len(arguments) != 2:
            raise ValueError("Invalid internal cleanup arguments")
        cleanup_owned(json.loads(arguments[1]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=15, help="Total child lifetime, 1 to 660 seconds")
    parser.add_argument("--grace", type=float, default=2, help="TERM grace, 0.1 to 10 seconds")
    parser.add_argument("arguments", nargs=argparse.REMAINDER,
                        help="Use -- followed by coreaudio_tap.py arguments")
    args = parser.parse_args(arguments)
    if not 1 <= args.timeout <= 660 or not 0.1 <= args.grace <= 10:
        parser.error("timeout or grace is out of range")
    child_args = args.arguments
    if child_args[:1] == ["--"]:
        child_args = child_args[1:]
    if not child_args:
        parser.error("An explicit coreaudio_tap.py mode is required after --")
    command = [sys.executable, "-u", str(Path(__file__).with_name("coreaudio_tap.py")), *child_args]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)
    state = {"resource": None, "interrupted": False}

    def forward(source, destination):
        try:
            for line in source:
                print(line, end="", file=destination, flush=True)
                if len(line) > 65536:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                value = entry.get("ready", entry)
                if valid_owned_resource(value):
                    state["resource"] = value
        finally:
            source.close()

    threads = [threading.Thread(target=forward, args=(process.stdout, sys.stdout), daemon=True),
               threading.Thread(target=forward, args=(process.stderr, sys.stderr), daemon=True)]
    for thread in threads:
        thread.start()

    def stop(_signum, _frame):
        state["interrupted"] = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    deadline = time.monotonic() + args.timeout
    while process.poll() is None and not state["interrupted"] and time.monotonic() < deadline:
        time.sleep(0.05)
    timed_out = process.poll() is None
    forced = False
    if timed_out:
        print(json.dumps({"watchdog": "interrupted" if state["interrupted"] else "timeout",
                          "child_pid": process.pid, "action": "SIGTERM"}), file=sys.stderr, flush=True)
        process.terminate()
        try:
            process.wait(timeout=args.grace)
        except subprocess.TimeoutExpired:
            forced = True
            print(json.dumps({"watchdog": "native call still blocked", "action": "SIGKILL",
                              "child_pid": process.pid}), file=sys.stderr, flush=True)
            process.kill()
            process.wait()
    for thread in threads:
        thread.join(timeout=1)
    if timed_out or process.returncode:
        bounded_cleanup(state["resource"])
    print(json.dumps({"watchdog_child_exit": process.returncode,
                      "timed_out": timed_out, "forced_kill": forced}), file=sys.stderr, flush=True)
    return 124 if timed_out else process.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Tap watchdog: {exc}", file=sys.stderr)
        sys.exit(1)
