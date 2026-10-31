"""Visible macOS synthetic-audio diagnostic. Launching never opens audio."""
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave


def command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def restore_helper_streams():
    # The native windowed bootloader may clear streams; helpers are deliberately
    # launched with pipes by this app. No terminal, console app, or shell needed.
    for name, descriptor, mode in (("stdin", 0, "r"), ("stdout", 1, "w"), ("stderr", 2, "w")):
        if getattr(sys, name) is None:
            setattr(sys, name, os.fdopen(os.dup(descriptor), mode, buffering=1))


def synthetic_player():
    """Dedicated output-only subprocess. Stays silent until a bounded command."""
    import numpy as np
    import sounddevice as sd
    pending = queue.Queue(maxsize=2)
    current = np.zeros((0, 2), dtype=np.float32)
    position = 0

    def output(data, frames, _timing, _status):
        nonlocal current, position
        data.fill(0)
        written = 0
        while written < frames:
            if position == len(current):
                try:
                    current = pending.get_nowait()
                except queue.Empty:
                    break
                position = 0
            length = min(frames - written, len(current) - position)
            data[written:written + length] = current[position:position + length]
            written += length
            position += length

    with sd.OutputStream(samplerate=48000, channels=2, dtype="float32", callback=output):
        print(json.dumps({"player_ready": True, "pid": os.getpid(), "output_only": True}), flush=True)
        for line in sys.stdin:
            value = json.loads(line)
            if value == {"stop": True}:
                break
            if value != {"synthetic_tone": True}:
                raise ValueError("Only this app's fixed synthetic tone is accepted")
            count = 48000 * 2
            x = np.arange(count, dtype=np.float64)
            envelope = np.minimum(1, np.minimum(x / 480, (count - 1 - x) / 480))
            mono = (.02 * envelope * np.sin(2 * np.pi * 1000 * x / 48000)).astype(np.float32)
            pending.put_nowait(np.repeat(mono[:, None], 2, axis=1))


def stop_process(process, grace=1):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def pump(process, events, kind):
    def forward(pipe, stream, state):
        try:
            for line in pipe:
                events.put((kind, stream, line.rstrip()))
            state["eof"] = True
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()
    threads = []
    for stream in ("stdout", "stderr"):
        state = {"eof": False}
        thread = threading.Thread(target=forward, args=(getattr(process, stream), stream, state), daemon=True)
        thread._dot_pipe_state = state
        threads.append(thread)
        thread.start()
    return threads


def spawn_helper(args, events, kind):
    process = subprocess.Popen(command(*args), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)
    process._dot_pump_threads = pump(process, events, kind)
    return process


def remember_child_error(kind, entry, errors):
    """Keep only bounded structured errors from our own helper roles."""
    if kind not in {"tap", "input", "player", "cleanup"} or not isinstance(entry, dict):
        return None
    value = entry.get("error")
    if not isinstance(value, str) or not value.strip():
        return None
    diagnostic = kind + ": " + " ".join(value.split())[:240]
    if diagnostic not in errors and len(errors) < 4:
        errors.append(diagnostic)
    return diagnostic


def attach_child_errors(result, errors):
    if errors:
        result["ok"] = False
        result["diagnostics"] = list(errors)
        extra = [item for item in errors if item not in result["message"]]
        if extra:
            result["message"] += " 詳細: " + " / ".join(extra)


def finish_recorder_events(process, events, owned, timeout=2, errors=None):
    """After child termination, drain owned IDs through bounded reader EOF.

    No audio/native API is used here. A caller can inspect incomplete teardown
    instead of treating a deadline or an empty queue as proof of cleanup.
    """
    import tap_watchdog
    deadline = time.monotonic() + timeout
    threads = getattr(process, "_dot_pump_threads", ()) if process is not None else ()
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    complete = (process is None or (process.poll() is not None and len(threads) == 2
                and all(not thread.is_alive() and thread._dot_pipe_state["eof"]
                        for thread in threads)))
    # Native tracing is small. Cap malformed/excessive diagnostics as well.
    for _ in range(2048):
        try:
            kind, _stream, text = events.get_nowait()
        except queue.Empty:
            return owned, complete
        try:
            entry = json.loads(text)
        except (ValueError, TypeError):
            continue
        if errors is not None:
            remember_child_error(kind, entry, errors)
        if kind != "tap":
            continue
        if isinstance(entry, dict):
            identity = entry.get("ready", entry)
            if tap_watchdog.valid_owned_resource(identity):
                owned = identity
    return owned, complete and events.empty()


def verify_synthetic_wav(path):
    import numpy as np
    with wave.open(str(path), "rb") as recording:
        if recording.getnchannels() != 1 or recording.getsampwidth() != 2:
            raise RuntimeError("合成音の形式が想定と異なります")
        rate = recording.getframerate()
        samples = np.frombuffer(recording.readframes(recording.getnframes()), dtype="<i2").astype(np.float64)
    if not len(samples) or not np.any(samples):
        raise RuntimeError("音声データを受け取れませんでした（無音または0フレーム）")
    spectrum = np.abs(np.fft.rfft(samples))
    frequency = float(np.argmax(spectrum) * rate / len(samples))
    if abs(frequency - 1000) > 20:
        raise RuntimeError(f"受け取った音は検査用の合成音と一致しません（{frequency:.0f} Hz）")
    return {"frames": int(len(samples)), "sample_rate": rate,
            "peak_frequency": round(frequency, 1), "synthetic_verified": True}


def run_synthetic_test(updates, cancelled, public_input=False):
    """Only called by the visible button. Every capture PID is our own child."""
    import coreaudio_tap as tap
    import tap_watchdog
    player = recorder = publisher = None
    events = queue.Queue()
    owned = None
    child_errors = []
    stage = "準備中"
    result = {"ok": False, "message": "確認できませんでした"}
    try:
        with tempfile.TemporaryDirectory(prefix="dot-watch-synthetic-") as temporary:
            native = tap.Native()
            clock_id = native.uint(1, "dOut")
            clock_uid = tap.output_clock_uid(native, clock_id)  # Fail before spawning if any mic streams exist.
            updates.put(("status", "検査用プレイヤーを準備しています…"))
            player = spawn_helper(["--internal-player"], events, "player")
            deadline = time.monotonic() + 10
            ready = False
            while time.monotonic() < deadline and not cancelled.is_set():
                try:
                    kind, stream, text = events.get(timeout=.1)
                except queue.Empty:
                    if player.poll() is not None:
                        raise RuntimeError("検査用プレイヤーを起動できませんでした")
                    continue
                if kind == "player":
                    try:
                        entry = json.loads(text)
                    except ValueError:
                        continue
                    error = remember_child_error(kind, entry, child_errors)
                    if error:
                        raise RuntimeError(error)
                    if entry.get("player_ready") and entry.get("pid") == player.pid:
                        ready = True
                        break
            if cancelled.is_set():
                raise RuntimeError("確認を中止しました")
            if not ready:
                raise RuntimeError("検査用プレイヤーの準備が時間切れになりました")
            destination = Path(temporary) / "synthetic.wav"
            if public_input:
                from public_input_check import validate_identity
                publisher = spawn_helper(["--internal-tap", "--create-input-for-pid", str(player.pid),
                                          "--seconds", "45", "--clock-output-uid", clock_uid],
                                         events, "tap")
                updates.put(("status", "検査専用の仮想マイクを作っています…"))
                deadline = time.monotonic() + 15
                published = None
                while time.monotonic() < deadline and not cancelled.is_set():
                    try:
                        kind, _stream, text = events.get(timeout=.1)
                    except queue.Empty:
                        if publisher.poll() is not None:
                            break
                        continue
                    if kind != "tap":
                        continue
                    try:
                        entry = json.loads(text)
                    except ValueError:
                        continue
                    error = remember_child_error(kind, entry, child_errors)
                    if error:
                        raise RuntimeError(error)
                    identity = entry.get("ready", entry)
                    if tap_watchdog.valid_owned_resource(identity):
                        owned = identity
                    if isinstance(entry.get("ready"), dict):
                        validate_identity(identity)
                        if identity.get("pids") != [player.pid]:
                            raise RuntimeError("仮想マイクの音源が検査用プレイヤーと一致しません")
                        published = identity
                        break
                if cancelled.is_set():
                    raise RuntimeError("確認を中止しました")
                if published is None:
                    raise RuntimeError("仮想マイクの作成を確認できませんでした")
                recorder = spawn_helper(["--internal-public-input", json.dumps(published), str(destination)],
                                        events, "input")
            else:
                args = ["--internal-tap", "--capture-reply-pids", str(player.pid), "--seconds", "3",
                        "--clock-output-uid", clock_uid, "--output", str(destination)]
                recorder = spawn_helper(args, events, "tap")
            deadline = time.monotonic() + 15
            tone_sent = False
            stage = "仮想入力の作成"
            updates.put(("status", "音声の許可確認中です。macOSの案内が出たら内容を確認してください。"))
            while time.monotonic() < deadline and not cancelled.is_set():
                try:
                    kind, stream, text = events.get(timeout=.1)
                except queue.Empty:
                    if recorder.poll() is not None:
                        break
                    continue
                try:
                    entry = json.loads(text)
                except ValueError:
                    updates.put(("detail", text[-300:]))
                    continue
                error = remember_child_error(kind, entry, child_errors)
                if error:
                    updates.put(("detail", error))
                    raise RuntimeError(error)
                if public_input and publisher.poll() is not None:
                    raise RuntimeError("仮想マイクが検査の途中で終了しました")
                if kind != ("input" if public_input else "tap"):
                    continue
                identity = entry.get("ready", entry)
                if tap_watchdog.valid_owned_resource(identity):
                    owned = identity
                step = entry.get("native_step", "")
                names = {"create_ioproc_before": "音声入力の登録",
                         "create_ioproc_after": "音声入力の準備完了",
                         "start_ioproc_before": "合成音の取得開始",
                         "start_ioproc_after": "合成音を確認中",
                         "capture_stop_after": "音声の検証",
                         "public_input_open_before": "仮想マイクを指定して開く処理",
                         "public_input_open_after": "仮想マイクの機器ID確認",
                         "public_input_start_before": "仮想マイクの入力開始",
                         "public_input_start_after": "仮想マイクの無音確認"}
                if step in names:
                    stage = names[step]
                    updates.put(("detail", stage))
                input_ready = (entry.get("input_ready") is True and entry.get("silent_baseline") is True
                               and entry.get("uid") == owned["uid"]) if public_input else False
                if (input_ready if public_input else step == "start_ioproc_after") and not tone_sent:
                    # Only the already-created player child receives this command.
                    player.stdin.write(json.dumps({"synthetic_tone": True}) + "\n")
                    player.stdin.flush()
                    tone_sent = True
            if cancelled.is_set():
                raise RuntimeError("確認を中止しました")
            if recorder.poll() is None:
                raise RuntimeError(f"「{stage}」で時間切れになりました。接続は未確認です。")
            if recorder.returncode:
                raise RuntimeError(f"「{stage}」を完了できませんでした。接続は未確認です。")
            if not tone_sent:
                raise RuntimeError("合成音の取得を開始できませんでした")
            metrics = verify_synthetic_wav(destination)
            message = ("仮想マイクで合成音を受け取れました。Your dotでの選択は次の確認です。"
                       if public_input else "合成音を受け取れました。Your dotとの接続は次の確認です。")
            result = {"ok": True, "message": message,
                      **metrics}
    except Exception as exc:
        result = {"ok": False, "message": str(exc)}
    finally:
        cleanup_complete = True
        for child in (recorder, publisher):
            try:
                stop_process(child, grace=2)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_complete = False
        if player is not None and player.poll() is None:
            try:
                player.stdin.write(json.dumps({"stop": True}) + "\n")
                player.stdin.flush()
                player.wait(timeout=1)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    stop_process(player)
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_complete = False
        # Pump threads continue reading while the worker exits. Ownership may
        # appear after the UI's timeout/cancel decision, so snapshot it only now.
        for child in (recorder, publisher, player):
            owned, child_finished = finish_recorder_events(child, events, owned, errors=child_errors)
            cleanup_complete = cleanup_complete and child_finished
        if owned:
            try:
                cleanup = subprocess.run(command("--internal-cleanup", json.dumps(owned)),
                                         stdin=subprocess.DEVNULL, capture_output=True,
                                         text=True, timeout=5)
                if cleanup.returncode:
                    cleanup_complete = False
                    for line in cleanup.stderr.splitlines()[-4:]:
                        try:
                            remember_child_error("cleanup", json.loads(line), child_errors)
                        except ValueError:
                            pass
            except (OSError, subprocess.TimeoutExpired):
                cleanup_complete = False
        if not cleanup_complete:
            result["ok"] = False
            result["message"] += " 音声処理の終了と仮想入力の後始末も確認が必要です。"
        attach_child_errors(result, child_errors)
        updates.put(("result", result))


def run_internal(function, *args):
    """Report helper failures to the parent instead of a frozen error dialog."""
    try:
        restore_helper_streams()
        result = function(*args)
        return 0 if result is None else result
    except Exception as exc:
        message = json.dumps({"error": f"{type(exc).__name__}: {exc}"[:240]}) + "\n"
        try:
            os.write(2, message.encode("utf-8"))
        except OSError:
            pass
        return 1


def selfcheck(path):
    # This checks packaging only: no Native(), audio streams, app window or capture.
    from Foundation import NSBundle
    from relay_backend import MAX_PCM_BYTES, MAX_INPUT_SECONDS, MAX_REPLY_SECONDS
    bundle = NSBundle.mainBundle()
    identifier = str(bundle.bundleIdentifier() or "")
    description = str(bundle.objectForInfoDictionaryKey_("NSAudioCaptureUsageDescription") or "")
    result = {"frozen": bool(getattr(sys, "frozen", False)),
              "bundle_identifier": identifier,
              "bundle_path": str(bundle.bundlePath()),
              "audio_usage_description": description,
              "audio_started": False, "ui_started": False,
              "max_input_pcm_bytes": MAX_PCM_BYTES,
              "max_input_seconds": MAX_INPUT_SECONDS,
              "max_reply_seconds": MAX_REPLY_SECONDS,
              "input_playback_guard": True,
              "ok": identifier in {"local.dot-watch.audio", "local.dot-watch.audio.v2"} and bool(description)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    return 0 if result["ok"] else 1


def permission_status(path):
    """Read this bundle's public authorization API; never request or open audio."""
    import ctypes as C
    from coreaudio_tap import Native
    native = Native()
    framework = C.CDLL("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
    cls = native.objc.objc_getClass(b"AVCaptureDevice")
    if not cls:
        raise RuntimeError("AVCaptureDevice is unavailable")
    audio = native.cf_value("soun")
    try:
        status = int(native.msg(cls, "authorizationStatusForMediaType:", C.c_long,
                                (C.c_void_p,), (audio,)))
    finally:
        native.cf.CFRelease(audio)
    labels = {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}
    result = {"microphone_authorization": labels.get(status, "unknown"),
              "status": status, "audio_started": False, "permission_requested": False}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(result, output, indent=2)
    return 0


def gui():
    from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
                        NSButton, NSFont, NSMenu, NSMenuItem, NSMakeRect, NSTextField,
                        NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
                        NSWindowStyleMaskMiniaturizable, NSTerminateCancel, NSTerminateNow)
    from Foundation import NSObject, NSTimer
    import objc

    class Controller(NSObject):
        def applicationDidFinishLaunching_(self, notification):
            self.updates = queue.Queue()
            self.cancelled = threading.Event()
            self.running = False
            self.quitting = False
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 570, 340),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable,
                NSBackingStoreBuffered, False)
            self.window.setTitle_("Dot Watch Audio")
            self.window.setDelegate_(self)
            content = self.window.contentView()
            def label(text, y, height, size):
                view = NSTextField.labelWithString_(text)
                view.setFrame_(NSMakeRect(28, y, 514, height))
                view.setFont_(NSFont.systemFontOfSize_(size))
                view.setMaximumNumberOfLines_(0)
                content.addSubview_(view)
                return view
            label("dotと話すための音声接続", 278, 34, 23)
            label("まず、このアプリで作った短い合成音だけを使って\nMacの音声経路を確認します。", 224, 48, 14)
            label("検査はボタンを押した時だけ始まります。\n周囲のマイクやYour dotの通話は開始しません。", 166, 46, 13)
            self.status = label("待機中", 119, 32, 14)
            self.detail = label("Your dotとの会話接続は、まだ未確認です。", 71, 44, 12)
            self.button = NSButton.alloc().initWithFrame_(NSMakeRect(27, 24, 188, 34))
            self.button.setTitle_("① 合成音の受信を確認")
            self.button.setBezelStyle_(1)
            self.button.setTarget_(self)
            self.button.setAction_("startTest:")
            content.addSubview_(self.button)
            self.public_button = NSButton.alloc().initWithFrame_(NSMakeRect(222, 24, 196, 34))
            self.public_button.setTitle_("② 仮想マイクを確認")
            self.public_button.setBezelStyle_(1)
            self.public_button.setTarget_(self)
            self.public_button.setAction_("startPublicTest:")
            content.addSubview_(self.public_button)
            self.cancel_button = NSButton.alloc().initWithFrame_(NSMakeRect(432, 24, 104, 34))
            self.cancel_button.setTitle_("中止")
            self.cancel_button.setBezelStyle_(1)
            self.cancel_button.setTarget_(self)
            self.cancel_button.setAction_("cancelTest:")
            self.cancel_button.setEnabled_(False)
            content.addSubview_(self.cancel_button)
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                .1, self, "poll:", None, True)
            self.window.center()
            self.window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        def startTest_(self, _sender):
            self.beginTest_(False)

        def startPublicTest_(self, _sender):
            self.beginTest_(True)

        def beginTest_(self, public_input):
            if self.running:
                return
            self.running = True
            self.cancelled.clear()
            self.button.setEnabled_(False)
            self.public_button.setEnabled_(False)
            self.cancel_button.setEnabled_(True)
            self.status.setStringValue_("準備中…")
            self.detail.setStringValue_("macOSの許可画面が表示される場合があります。")
            threading.Thread(target=run_synthetic_test,
                             args=(self.updates, self.cancelled, bool(public_input)), daemon=True).start()

        def cancelTest_(self, _sender):
            self.cancelled.set()
            self.status.setStringValue_("中止しています…")

        def poll_(self, _timer):
            while True:
                try:
                    kind, value = self.updates.get_nowait()
                except queue.Empty:
                    break
                if kind == "status":
                    self.status.setStringValue_(value)
                elif kind == "detail":
                    self.detail.setStringValue_(value)
                else:
                    self.running = False
                    self.button.setEnabled_(True)
                    self.public_button.setEnabled_(True)
                    self.cancel_button.setEnabled_(False)
                    self.status.setStringValue_("検査完了" if value["ok"] else "接続を確認できませんでした")
                    self.detail.setStringValue_(value["message"])
                    if self.quitting:
                        NSApplication.sharedApplication().terminate_(None)

        def applicationShouldTerminate_(self, _sender):
            if getattr(self, "running", False):
                self.quitting = True
                self.cancelTest_(None)
                return NSTerminateCancel
            return NSTerminateNow

        def windowShouldClose_(self, _sender):
            NSApplication.sharedApplication().terminate_(None)
            return False

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    menu = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    menu.addItem_(app_item)
    submenu = NSMenu.alloc().initWithTitle_("Dot Watch Audio")
    submenu.addItemWithTitle_action_keyEquivalent_("Dot Watch Audioを終了", "terminate:", "q")
    app_item.setSubmenu_(submenu)
    app.setMainMenu_(menu)
    controller = Controller.alloc().init()
    app.setDelegate_(controller)
    app.run()


def main():
    args = sys.argv[1:]
    if args[:1] == ["--permission-status"]:
        p = argparse.ArgumentParser()
        p.add_argument("--permission-status", required=True, metavar="OUTPUT")
        return run_internal(permission_status, p.parse_args(args).permission_status)
    if args[:1] == ["--selfcheck"]:
        p = argparse.ArgumentParser()
        p.add_argument("--selfcheck", action="store_true")
        p.add_argument("--selfcheck-output", required=True)
        value = p.parse_args(args)
        return selfcheck(value.selfcheck_output)
    if args[:1] == ["--internal-player"]:
        return run_internal(synthetic_player)
    if args[:1] == ["--internal-tap"]:
        import coreaudio_tap
        return run_internal(coreaudio_tap.main, args[1:])
    if args[:1] == ["--internal-public-input"]:
        import public_input_check
        return run_internal(lambda: public_input_check.capture_public_input(json.loads(args[1]), args[2]))
    if args[:1] == ["--internal-public-probe"]:
        import public_input_probe
        return run_internal(public_input_probe.probe_public_input, json.loads(args[1]))
    if args[:1] == ["--internal-cleanup"]:
        import tap_watchdog
        return run_internal(lambda: tap_watchdog.cleanup_owned(json.loads(args[1])))
    if args[:1] == ["--internal-relay"]:
        import relay_backend
        return run_internal(relay_backend.main, args[1:])
    if args[:1] == ["--internal-relay-cleanup"]:
        import relay_backend
        return run_internal(lambda: relay_backend.main(["--cleanup", args[1]]))
    if args:
        raise ValueError("Unknown application arguments")
    gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
