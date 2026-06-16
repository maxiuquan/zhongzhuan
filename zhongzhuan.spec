# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\zhongzhuan\\__main__.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['aiohttp', 'httpx', 'yaml', 'loguru', 'win32serviceutil', 'win32service', 'win32event'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='zhongzhuan',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
