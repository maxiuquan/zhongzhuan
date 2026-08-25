# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\zhongzhuan\\__main__.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # 注：不引入 pywin32（项目设计约束，Windows 服务不走 SCM）。此前列出的
    # win32serviceutil/win32service/win32event 从未落地且会让 PyInstaller
    # 构建直接报 ERROR，已移除。
    hiddenimports=['aiohttp', 'httpx', 'yaml', 'loguru'],
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
    # UPX 压缩易触发杀软误报（误报处理成本 > 体积收益），默认关闭。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
