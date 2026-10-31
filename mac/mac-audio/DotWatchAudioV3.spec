# Legitimate v2 app update; separate output preserves the previous executable.
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'dot_watch_audio_app.py')],
    pathex=[str(root)], binaries=[], datas=[],
    hiddenimports=['AppKit', 'Foundation', 'objc', 'coreaudio_tap', 'tap_watchdog', 'relay_backend', 'public_input_check', 'public_input_probe', 'sounddevice', 'numpy'],
    hookspath=[], runtime_hooks=[], excludes=['matplotlib', 'pandas', 'scipy', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='DotWatchAudio',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          argv_emulation=False, target_arch=None, codesign_identity=None,
          entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='DotWatchAudioV3')
app = BUNDLE(coll, name='Dot Watch Audio v3.app', icon=None,
             bundle_identifier='local.dot-watch.audio.v2', version='0.3.0',
             info_plist={
                 'CFBundleDisplayName': 'Dot Watch Audio v3',
                 'NSPrincipalClass': 'NSApplication',
                 'LSMinimumSystemVersion': '14.2',
                 'NSHighResolutionCapable': True,
                 'NSAudioCaptureUsageDescription': '選択したアプリの再生音だけをStopWatchへ中継し、合成音で接続を確認します。Macのマイクは使いません。',
                 'NSMicrophoneUsageDescription': '検査用の仮想マイク「Dot Watch Mic」の音声入力を確認します。Mac内蔵マイクやiPhoneのマイクは選びません。',
             })
