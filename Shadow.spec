# -*- mode: python ; coding: utf-8 -*-
import os, sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None
src = '/Users/yangxiaohui/Desktop/Claude Shadow'

a = Analysis(
    [os.path.join(src, 'shadow_recorder.py')],
    pathex=[src],
    binaries=[
        (os.path.join(src, 'audio_capture'),      'Shadow'),
        (os.path.join(src, 'subtitle_recognizer'), 'Shadow'),
        (os.path.join(src, 'ocr_frame'),           'Shadow'),
    ],
    datas=[
        *collect_data_files('cv2'),
        *collect_data_files('PyQt6'),
    ],
    hiddenimports=[
        'PyQt6.sip',
        'sounddevice',
        'mss',
        'mss.darwin',
        'cv2',
        'numpy',
        'wave',
        'threading',
        'subprocess',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Shadow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Shadow',
)

app = BUNDLE(
    coll,
    name='Shadow.app',
    icon=None,
    bundle_identifier='com.shadow.recorder',
    info_plist={
        'NSHighResolutionCapable': True,
        'NSMicrophoneUsageDescription': 'Shadow 需要麦克风权限来录制您的声音。',
        'NSScreenCaptureUsageDescription': 'Shadow 需要屏幕录制权限来捕获屏幕内容。',
        'NSSpeechRecognitionUsageDescription': 'Shadow 需要语音识别权限来生成实时字幕。',
        'NSCameraUsageDescription': 'Shadow 需要摄像头权限来录制您的画面。',
        'LSUIElement': False,
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'CFBundleName': 'Shadow',
        'CFBundleDisplayName': 'Shadow',
    },
)
