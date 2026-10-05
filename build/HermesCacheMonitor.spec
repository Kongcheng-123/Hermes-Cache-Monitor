# -*- mode: python ; coding: utf-8 -*-
"""HermesCacheMonitor 打包 spec（正式名）

用法（在项目根目录执行）：
    py -m PyInstaller "build/HermesCacheMonitor.spec" ^
       --distpath "dist" --workpath "build/work" --noconfirm

产物：dist/HermesCacheMonitor.exe（单文件，含全部依赖）

⚠️ 必须用装了 pystray / Pillow 的 Python 打包，否则托盘功能静默失效。
   本 spec 会在开头强校验，缺依赖直接报错退出。

关于 proxy_monitor：
    站方采集相关模块（session_join / calib_export / panel / poll_loop 等）
    是运行时从磁盘 import 的，**必须一起打进 exe**，否则：
      · 点「校准」报 No module named 'session_join'
      · 点「打开网页看板」拉不起 panel.py

    做法：打包前把 proxy_monitor/ 复制到一个临时干净目录，只保留代码与
    `.example` 模板，剔除数据库/日志/个人配置/__pycache__，然后把干净目录
    作为 datas 源。这样能保证构建产物里不会混进任何运行时数据。
"""
import os
import shutil
import sys
import tempfile

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT_DIR = os.path.dirname(SPEC_DIR)

# ── 打包前强校验：缺依赖直接失败，不静默跳过 ──────────────────
_REQUIRED = ["pystray", "PIL", "uiautomation", "comtypes"]
_missing = []
for _m in _REQUIRED:
    try:
        __import__(_m)
    except ImportError:
        _missing.append(_m)
if _missing:
    raise SystemExit(
        "\n[打包失败] 当前 Python 缺少依赖：%s\n"
        "  当前解释器：%s\n"
        "  请先 pip install uiautomation comtypes pystray Pillow\n"
        % (", ".join(_missing), sys.executable)
    )

hiddenimports = ['uiautomation', 'comtypes', 'comtypes.client',
                 'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'sqlite3']
hiddenimports += collect_submodules('comtypes')
hiddenimports += collect_submodules('pystray')

# ── proxy_monitor：先做一份"干净副本"，再拿去打包 ────────────────
_PM_SRC = os.path.join(ROOT_DIR, 'proxy_monitor')
# 运行时数据 / 个人凭据 / 缓存：一律不带进分发包
_PACK_EXCLUDE_DIRS = {'__pycache__', 'archive', '对账参考'}
_PACK_EXCLUDE_FILES = {
    'store.db', 'proxy_monitor.db', 'proxy_monitor.db-wal', 'proxy_monitor.db-shm',
    'poll.log', 'proxy_calibration.json', 'dsh_auth.json',
    'last_retention.txt', 'sites.json', 'host_alias.json',
    'cache_prices.json', 'cost_ledger.json', 'calib_samples.json',
    'site_merge.json', 'prices.json',
}


def _make_clean_copy(dst_parent):
    """把 proxy_monitor 复制成只含代码/模板的干净副本，返回路径。"""
    if not os.path.isdir(_PM_SRC):
        return None
    dst = os.path.join(dst_parent, 'proxy_monitor')

    def _ignore(_dir, names):
        drop = []
        for n in names:
            if n in _PACK_EXCLUDE_DIRS:
                drop.append(n)
            elif n in _PACK_EXCLUDE_FILES:
                drop.append(n)
            elif n.endswith(('.pyc', '.pyo', '.log', '.db', '.db-wal', '.db-shm')):
                drop.append(n)
            elif n.startswith('.'):
                drop.append(n)
        return drop

    shutil.copytree(_PM_SRC, dst, ignore=_ignore)
    return dst


_TMP_DIR = None
_PM_CLEAN = None
if os.path.isdir(_PM_SRC):
    _TMP_DIR = tempfile.mkdtemp(prefix='hcm_pack_')
    _PM_CLEAN = _make_clean_copy(_TMP_DIR)
    if _PM_CLEAN:
        _kept = sorted(os.listdir(_PM_CLEAN))
        print("[spec] proxy_monitor 干净副本：保留 %d 项 -> %s"
              % (len(_kept), ', '.join(_kept)))

datas = [(_PM_CLEAN, 'proxy_monitor')] if _PM_CLEAN else []

a = Analysis(
    [os.path.join(ROOT_DIR, 'cache_follow.py')],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=datas,
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

# 清理临时目录（打包完成后）
if _TMP_DIR and os.path.isdir(_TMP_DIR):
    try:
        shutil.rmtree(_TMP_DIR)
    except Exception:
        pass
