# -*- mode: python ; coding: utf-8 -*-
"""HermesCacheMonitor 打包 spec（分发给朋友版 / 正式名）
   与旧 spec 的差异：
     - 显式收 comtypes 全子模块
   用法：
     py -m PyInstaller "<项目目录>/build/HermesCacheMonitor.spec" ^
        --distpath "<项目目录>/dist" --workpath "<项目目录>/build/work" --noconfirm
"""
import os
from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT_DIR = os.path.dirname(SPEC_DIR)

hiddenimports = ['uiautomation', 'comtypes', 'comtypes.client', 'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'sqlite3']
hiddenimports += collect_submodules('comtypes')
hiddenimports += collect_submodules('pystray')

a = Analysis(
    [os.path.join(ROOT_DIR, 'cache_follow.py')],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
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
    name='HermesCacheMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(ROOT_DIR, 'assets', 'hermes_cache_monitor.ico')],
)
