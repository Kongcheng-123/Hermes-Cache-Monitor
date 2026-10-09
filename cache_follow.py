# -*- coding: utf-8 -*-
"""cache_follow.py v4 — 跟随式缓存监控悬浮窗（价格 + 缺账检测版）

跟随 Hermes 桌面端"当前打开的对话"：
- 主信号: UIA「复制会话 ID」按钮（实时，热路径 <1ms）
- 兜底:   WebView2 History 最新 #/tasks/<sid> → state.db 最近活动

v4 新增：
- 价格/成本：cache_prices.json（站×模型匹配）＋ 峰谷价 ＋ 子代理汇总
- 缺账检测：agent.log 真实请求数 vs 记账调用数 → 覆盖率、缺账估算（可点击计入成本）
- 缺账明细弹窗、价格填写弹窗、价格表管理窗口

用法:
  python "<项目目录>\\cache_follow.py"         # 悬浮窗模式
  python "<项目目录>\\cache_follow.py" --once  # 打印一次当前结果（调试）
  python "<项目目录>\\cache_follow.py" --sid <会话ID>        # 指定会话
  python "<项目目录>\\cache_follow.py" --stats all           # 文本打印成本统计（24h/today/7d/30d/all）
  python "<项目目录>\\cache_follow.py" --segments            # 打印价格表的价格段（时间轴）
  python "<项目目录>\\cache_follow.py" --recompute day       # 按「当天生效的价格段」逐日重算（day/missing/all）
  python "<项目目录>\\cache_follow.py" --bill-file bills.json  # 导入实付锚点（单对象或数组）
  python "<项目目录>\\cache_follow.py" --bills --resolve     # 列出锚点并反推倍率/真单价
  python "<项目目录>\\cache_follow.py" --bill-del "host|model|from|to"
"""
import collections
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote, urlparse

try:
    import uiautomation as _uia
except Exception:
    _uia = None

try:
    from icon_data import ICON_B64 as _ICON_B64
except Exception:
    _ICON_B64 = ""

_ICON_CACHE = []

# ── 诊断/调试开关（也可用环境变量）──
#   --no-uia   禁用 UIA 实时跟随（只走 WebView2/SMU）→ 用来二分定位"卡顿是否来自 UIA"
#   --profile  把 tick 里耗时 >30ms 的环节写进数据目录的 tick_profile.log
_NO_UIA = ("--no-uia" in sys.argv) or (os.environ.get("HERMES_MON_NO_UIA") == "1")
_PROFILE = ("--profile" in sys.argv) or (os.environ.get("HERMES_MON_PROFILE") == "1")


def _startup_log(msg):
    """启动诊断日志（排查用）：写到数据目录下的 startup.log。"""
    try:
        base = WORK_DIR or _appdata_dir()
        with open(os.path.join(base, "startup.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def apply_icon(win):
    """给 tkinter 窗口设置图标（内嵌 base64 PNG，无需外部文件）。"""
    if not _ICON_B64:
        _startup_log("icon: 内嵌数据为空（icon_data 模块缺失）")
        return
    try:
        img = tk.PhotoImage(data=_ICON_B64)
        win.iconphoto(True, img)
        _ICON_CACHE.append(img)      # 保持引用，防止图标被 GC
        _startup_log("icon: 已应用 %dx%d（base64 %d 字符）" % (img.width(), img.height(), len(_ICON_B64)))
    except Exception as ex:
        _startup_log("icon: 应用失败 %s" % ex)


def apply_dark_title_bar(win, bg_hex="#1e1e24"):
    """为 Windows 窗口启用沉浸式深色标题栏，并将标题栏底色设置为与 UI 背景完全融为一体。"""
    try:
        import ctypes
        from ctypes import c_int, byref
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            hwnd = win.winfo_id()
        # 1. 沉浸式暗黑模式（Win11/Win10 20H1+ 属性为 20，老版 1809 为 19）
        val = c_int(1)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, byref(val), ctypes.sizeof(val))
        if res != 0:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, byref(val), ctypes.sizeof(val))
        # 2. Windows 11 (22000+) 设置标题栏背景色与文字颜色
        if bg_hex and bg_hex.startswith("#") and len(bg_hex) == 7:
            r = int(bg_hex[1:3], 16)
            g = int(bg_hex[3:5], 16)
            b = int(bg_hex[5:7], 16)
            color_ref = c_int(r | (g << 8) | (b << 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, byref(color_ref), ctypes.sizeof(color_ref))
            text_ref = c_int(0xe0 | (0xe0 << 8) | (0xe0 << 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, byref(text_ref), ctypes.sizeof(text_ref))
    except Exception as e:
        _startup_log("暗黑标题栏应用失败：%s" % e)


# ─────────────── 无边框窗口增强（2026-10-09 界面优化） ───────────────
#  用户拍板：去掉系统标题栏（省 32px 高度），最小化/退出移入右键菜单。
#  实测（tmp/border_corner_probe*.py）：
#    · DWM 圆角属性 33 在 Win11 上对 overrideredirect 窗口有效，
#      且**自带一圈淡阴影**（老式 CS_DROPSHADOW 在半透明无边框窗口上无效）
#    · 圆角是 Win11 专属，Win10 会静默失效退回直角（不影响功能）

def apply_round_corners(win, level=2):
    """给无边框窗口加 Windows 11 原生圆角（自带淡阴影）。

    level: 2=ROUND（默认圆角）| 3=ROUNDSMALL（小圆角）。失败静默返回 False。
    """
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            hwnd = win.winfo_id()
        pref = ctypes.c_int(int(level))
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        return res == 0
    except Exception as e:
        _startup_log("系统圆角应用失败：%s" % e)
        return False


def force_dark_menus():
    """把进程级菜单主题强制为深色。

    ★ 2026-10-09 实测（tmp/dark_menu_probe5.py）：不开时原生菜单是纯白
      (#f9f9f9)，调用 uxtheme 私有 ordinal 135(SetPreferredAppMode, ForceDark)
      + 136(FlushMenuThemes) 后变成系统深灰 #2c2c2c。
      这管的是 **原生 Win32 菜单**（pystray 托盘菜单就是它）——tk 菜单不吃这个，
      tk 菜单靠 MENU_STYLE 直接指定颜色。
      uxtheme 的这两个序号是未文档化 API，失败就静默跳过（不影响功能）。
    """
    try:
        import ctypes
        ux = ctypes.WinDLL("uxtheme")
        for ordinal, args in ((135, (2,)), (136, ())):
            try:
                ux[ordinal](*args)
            except Exception:
                pass
        return True
    except Exception as e:
        _startup_log("深色菜单注入失败（不影响功能）：%s" % e)
        return False


def make_menu(parent, **kw):
    """统一风格的右键菜单（tk.Menu 不吃系统深色，必须显式上色）。"""
    opts = dict(MENU_STYLE)
    opts.update(kw)
    try:
        return tk.Menu(parent, tearoff=0, **opts)
    except Exception:
        # 极少数平台不支持部分选项 → 退回只用颜色
        return tk.Menu(parent, tearoff=0, bg=MENU_BG, fg=MENU_FG)


def bind_window_drag(win, widgets, skip=None, on_click=None, threshold=4,
                     is_edge=None):
    """给无边框窗口做拖动：按下 → 位移超过 threshold 像素才算拖，否则算点击。

    ★ 2026-10-09 修正 1（用户实测：只有「有字的地方」能拖、空白处光标变了却拖不动）：
      Tk 的 <B1-Motion> 是发给**鼠标指针当前所在的控件**，不是按下的那个控件。
      所以只给一堆 Label 绑事件，指针一离开文字（落到窗口空白/Frames）就断掉，
      表现成「只有文字上能拖」。→ 同时绑到顶层窗口上。
    ★ 2026-10-09 修正 2（自测抓出）：连续 Motion 里不能再用 winfo_x() 当基准 ——
      Tk 的 geometry 请求不是立即生效，winfo_x() 会返回旧值，导致快速拖动时
      位移被累加成「25+50+80=155」（实测，拖得比手快）。改为自己维护位置。
    ★ 2026-10-09 修正 3：按在窗口边缘时应交给 enable_border_resize 拉伸，
      否则拖动与拉伸会同时改 geometry 打架（is_edge 回调用于判定）。
    """
    st = {"x": 0, "y": 0, "px": 0, "py": 0, "moved": False, "down": False}
    skip_ids = {str(w) for w in (skip or ())}

    def _press(ev):
        if is_edge and is_edge(ev):
            st["down"] = False          # 边缘 → 让拉伸逻辑接管
            return
        st["x"], st["y"] = ev.x_root, ev.y_root
        st["px"], st["py"] = win.winfo_x(), win.winfo_y()
        st["moved"], st["down"] = False, True

    def _motion(ev):
        if not st["down"]:
            return
        dx, dy = ev.x_root - st["x"], ev.y_root - st["y"]
        if not st["moved"]:
            if abs(dx) < threshold and abs(dy) < threshold:
                return
            st["moved"] = True
        nx, ny = st["px"] + dx, st["py"] + dy
        # ★ 2026-10-09 修：改用「整个虚拟桌面」钳制（原来用主屏尺寸，拖不到副屏）
        nx, ny = clamp_to_desktop(nx, ny, win.winfo_width(), win.winfo_height(), win)
        win.geometry("+%d+%d" % (nx, ny))

    def _release(ev):
        was = st["down"] and not st["moved"]
        st["down"] = False
        if was and on_click:
            try:
                on_click(ev)
            except Exception:
                pass

    targets = [win] + [w for w in widgets if w is not None and str(w) not in skip_ids]
    for w in targets:
        try:
            w.bind("<Button-1>", _press, add="+")
            w.bind("<B1-Motion>", _motion, add="+")
            w.bind("<ButtonRelease-1>", _release, add="+")
        except Exception:
            pass


# ─────────── 无边框窗口「四边拉伸」与「自绘深色菜单」（2026-10-09） ───────────
#  用户反馈：① 去掉系统标题栏后四边不能拉伸了 ② tk 菜单四周有 2px 浅色白边
#  实测（tmp/probe_menu_*.py）：那 2px 是 Tk 在 Windows 上给 menu 窗口画的
#  原生外框（activeBorderWidth=0 / 改类画刷 / 去窗口样式 都无效），
#  所以改用「自绘无边框菜单」（probe_selfmenu.py 验证四边 0 白边 + 可加圆角）。

_RESIZE_EDGE = 6          # 旧边缘热区宽度（已废弃；保留常量避免外部引用报错）
_CORNER_EDGE = 14         # ★ 2026-10-09：角热区（只四角能拉伸，四边=纯拖动）


def style_dark_tree(root):
    """把 ttk 的 Treeview 变深色（子窗口表格统一用这个）。

    ★ 2026-10-09：用户反馈「子窗口和主窗口 UI 对齐一下」——
      实测根因：Treeview 走系统默认 ttk 主题（winnative/vista），是白底黑字的
      浅色表格，跟主窗口的碳黑风格完全割裂。
      做法：切到 clam 主题（只有它允许自由改色），再加一个 Dark.Treeview 样式。
      实测表格区底色 #1e1e24、文字 #f4f4f6。

    ★ 2026-10-09 二次修（用户反馈「价格表有白框」）：
      只设 `st.configure("Dark.Treeview", bordercolor=...)` **不够** ——
      clam 主题的表格边框不是画在 `Treeview` 元素上，而是画在**子元素
      `Treeview.field`** 上，它有三个独立的颜色选项，默认值是浅色的：
          lightcolor  = #eeebe7   （≈ 白，画左边/上边）
          darkcolor   = #cfcdc8   （≈ 浅灰，画右边/下边）
          bordercolor = #9e9a91   （≈ 灰，画外框）
      这三个不设 → 表格外圈会出现一圈白框，和深色背景格格不入。
      实测证据（tmp/probe_tree_cmp.py，像素采样）：
          A 组（只设 Treeview 级）→ 边缘像素 (238,235,231) 纯白 ❌
          B 组（补设 field 级）  → 边缘像素 (30,30,36)  深色 ✅
      修法：把 field 的这三个色一并钉成单元格底色，并把布局里的 border 归零。
      注意：field 级的色要跟着 `fieldbackground` 走（此处同为 #1e1e24），
      否则标题栏与表格之间会留一条异色缝。
    """
    try:
        st = ttk.Style(root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        _cell = "#1e1e24"
        _head = "#26262c"
        st.configure("Dark.Treeview",
                     background=_cell, foreground=FG,
                     fieldbackground=_cell,
                     bordercolor=_cell, borderwidth=0, rowheight=22,
                     # ★ 二次修：field 子元素的三个 3D 边框色（不设就是白的）
                     lightcolor=_cell, darkcolor=_cell)
        st.configure("Dark.Treeview.Heading",
                     background=_head, foreground=FG,
                     relief="flat", borderwidth=0)
        st.map("Dark.Treeview",
               background=[("selected", MENU_ACTIVE_BG)],
               foreground=[("selected", "#ffffff")])
        st.map("Dark.Treeview.Heading", background=[("active", "#33333c")])
        # ★ 二次修：把 field 层的 border 宽度归零（默认 1 → 会画一圈边框）
        #   保留其余子结构不变，只改 field 的 border 与配色
        try:
            st.layout("Dark.Treeview", [
                ("Dark.Treeview.field",
                 {"sticky": "nswe", "border": "0",
                  "children": [("Dark.Treeview.padding",
                                {"sticky": "nswe",
                                 "children": [("Dark.Treeview.treearea",
                                               {"sticky": "nswe"})]})]})])
        except Exception as _e:
            _startup_log("表格 field 布局调整失败（不影响功能）：%s" % _e)
    except Exception as e:
        _startup_log("表格深色化失败：%s" % e)


def style_child_window(win, round_corners=True, tree=False):
    """统一子窗口外观：底色 / 沉浸式标题栏 / Win11 圆角 /（可选）深色表格。

    ★ 2026-10-09 用户要求「子窗口和主窗口 UI 对齐，排版不用变」：
      · 底色统一 BG、沉浸式深色标题栏（各子窗口原本就有）
      · 补上 Win11 圆角 + 淡阴影 —— 主窗口已改无边框圆角，子窗口还停在直角
      · 表格（Treeview）统一深色，避免白底表格最刺眼
      注意：**不改任何排版/控件位置/尺寸**，只动外观属性。
    """
    try:
        win.configure(bg=BG)
    except Exception:
        pass
    try:
        apply_dark_title_bar(win, bg_hex=BG)
    except Exception:
        pass
    if round_corners:
        try:
            win.after(80, lambda: apply_round_corners(win, 2))
        except Exception:
            pass
    # ★ 2026-10-09 修正（用户反馈「价格配置/提示注入的标题栏还是白色」）：
    #   实测（tmp/probe_titlebar.py + probe_titlebar2.py）：建窗时立刻调
    #   apply_dark_title_bar 有 3 个窗口不生效（价格配置/提示注入/实付录入，
    #   标题栏实测 #f3f3f3 白色），而延迟重设（等窗口真正显示出来再设一次）
    #   实测生效（→ #16161a）。
    #   原因：DWM 的深色标题栏属性要在窗口被映射后才认，建窗那一刻设会被吞掉。
    #   所以这里排一次延迟重设；顺带覆盖后续 resizable()/transient() 可能的重建。
    for _delay in (120, 400):
        try:
            win.after(_delay, lambda w=win: apply_dark_title_bar(w, bg_hex=BG))
        except Exception:
            pass
    if tree:
        try:
            style_dark_tree(win)
        except Exception:
            pass


def virtual_screen(win=None):
    """★ 2026-10-09 新增：取「所有显示器拼成的虚拟桌面」范围（支持多屏）。

    ⚠️ 为什么需要它（用户 2026-10-09 报的 bug：主窗口拖不到副屏）：
      Tk 的 `winfo_screenwidth()/screenheight()` **只返回主显示器**的尺寸；
      而副屏可能排在主屏左边/上方（坐标为负），于是
      拖动/拉伸的位置钳制写成 `max(-w+60, min(x, sw-60))` + `y>=0` 时，
      窗口**只能在主屏范围内移动**，往左/往上挪不过去 → 永远上不了副屏。
      实测：主屏 2560x1440 + 左侧竖屏 1080x1920（x 从 -1080 起）时，
      旧逻辑把 (-900, 400) 钳成 (-340, 400)，明显拖不过去。

    修法：改用 Win32 的虚拟桌面度量（会覆盖所有显示器，含负坐标）：
      SM_XVIRTUALSCREEN=76  SM_YVIRTUALSCREEN=77
      SM_CXVIRTUALSCREEN=78 SM_CYVIRTUALSCREEN=79
    返回 (x0, y0, x1, y1) —— 整个虚拟桌面的左上/右下角，坐标系与窗口一致。
    取不到就退回主屏范围（老行为，保证不炸）。
    """
    try:
        import ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        x0 = u.GetSystemMetrics(76)
        y0 = u.GetSystemMetrics(77)
        cx = u.GetSystemMetrics(78)
        cy = u.GetSystemMetrics(79)
        if cx > 0 and cy > 0:
            return x0, y0, x0 + cx, y0 + cy
    except Exception:
        pass
    try:
        return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()
    except Exception:
        return 0, 0, 1920, 1080


def clamp_to_desktop(x, y, w, h, win=None, keep_visible=60):
    """★ 把窗口位置钳制到**整个虚拟桌面**内（多屏安全，允许负坐标）。

    保证至少有 keep_visible 像素留在可见区域，不会整个丢到屏幕外找不回来。
    返回 (x, y)。
    """
    try:
        dx0, dy0, dx1, dy1 = virtual_screen(win)
        x = max(dx0 - w + keep_visible, min(x, dx1 - keep_visible))
        y = max(dy0, min(y, dy1 - 40))
    except Exception:
        pass
    return x, y


def keep_out_of_taskbar(win):
    """★ 2026-10-09 修：让无边框浮窗**不出现在任务栏 / Alt-Tab 里**。

    ⚠️ 为什么需要它（用户 2026-10-09 报的 bug：最小化到托盘再打开，
       任务栏/悬停预览里就多出一个「缓存跟随监控 v1.3.0」窗口）：

    实测根因（tmp/find_toolwin.py 逐步取证）：
      Tk 给窗口设 `-alpha`（透明度）时会切到 `WS_EX_LAYERED` 模式，
      **顺手把 `WS_EX_TOOLWINDOW` 覆盖掉了**：
          ② overrideredirect(True)  → EX=0x00000080  TOOLWINDOW=True  ✅ 不进任务栏
          ④ -alpha=0.90            → EX=0x00080008  TOOLWINDOW=False ❌ 进任务栏了
      而 `WS_EX_TOOLWINDOW` 正是「不进任务栏、不出现在 Alt-Tab」的那个样式位
      （`overrideredirect` 本来会给，被 alpha 一冲就没了）。

    修法：设完 alpha / 建完窗之后，显式把这个样式位补回去。
      用 SetWindowLongW(GWL_EXSTYLE, ...) + SetWindowPos(SWP_FRAMECHANGED) 让它立即生效。

    ⚠️⚠️ 2026-10-09 二次修（exe 里失效的坑）：
      exe（PyInstaller 打包）里 `win.frame()` 返回的 hwnd **不是**真正的那层 ——
      实测 exe 版这样设完，目标窗口的 EXSTYLE 纹丝不动（还是 0x00080008）。
      对策：**三个候选 hwnd 全部设一遍**（frame() / 顶层父窗 / winfo_id()），
      哪个是真窗口就改哪个，一次搞定，不依赖对 Tk 内部结构的假设。
    """
    ok_any = False
    hwnds = []
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        # 候选 1：Tk 的 frame()（源码模式下是对的）
        try:
            hwnds.append(int(win.frame(), 16))
        except Exception:
            pass
        # 候选 2：winfo_id() 的顶层父窗
        try:
            wid = win.winfo_id()
            hwnds.append(wid)
            p = u.GetParent(wid)
            if p:
                hwnds.append(p)
                pp = u.GetParent(p)
                if pp:
                    hwnds.append(pp)
        except Exception:
            pass
        # 候选 3：frame() 的父窗
        try:
            p2 = u.GetParent(int(win.frame(), 16))
            if p2:
                hwnds.append(p2)
        except Exception:
            pass

        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        done = set()
        for h in hwnds:
            if not h or h in done:
                continue
            done.add(h)
            try:
                ex = u.GetWindowLongW(h, GWL_EXSTYLE) & 0xFFFFFFFF
                want = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
                if want != ex:
                    u.SetWindowLongW(h, GWL_EXSTYLE, want)
                    u.SetWindowPos(wintypes.HWND(h), None, 0, 0, 0, 0,
                                   0x0002 | 0x0001 | 0x0004 | 0x0010 | 0x0020)
                after = u.GetWindowLongW(h, GWL_EXSTYLE) & 0xFFFFFFFF
                if after & WS_EX_TOOLWINDOW:
                    ok_any = True
            except Exception:
                continue
        return ok_any
    except Exception as e:
        try:
            _startup_log("保持不进任务栏失败（不影响主要功能）：%s" % e)
        except Exception:
            pass
        return False


def enable_border_resize(win, min_w=240, min_h=80, edge=_CORNER_EDGE):
    """给无边框窗口恢复「**四角**拖拽改大小」的能力。

    ★ 2026-10-09 用户要求（第三次调整）：**只要四角能拉伸，四条边不要**。
      原因：原先四边各留 6px 热区，整圈边框都是「拉伸感应区」，
      而浮窗很小、内容区紧贴边缘 → 用户想拖动窗口时经常按到边上变成拉伸，误触频繁。
      现在四条边 = 纯拖动区，只有四个角（默认 14px 见方）能拉伸。

    返回给调用方的字典，附带 bind_tree() 用于把事件绑到所有子控件
    （Tk 的 Motion 事件发给指针所在控件，不绑子控件就会出现"某些区域拉不动"）。
    """
    st = {"mode": None, "x": 0, "y": 0, "px": 0, "py": 0, "w": 0, "h": 0}
    CURSORS = {
        "ne": "size_ne_sw", "sw": "size_ne_sw",
        "nw": "size_nw_se", "se": "size_nw_se",
    }

    def _which(ev):
        """★ 只返回四个角；四条边一律返回 None（= 交给拖动逻辑）。"""
        try:
            w = win.winfo_width()
            h = win.winfo_height()
        except Exception:
            return None
        x, y = ev.x, ev.y
        e = edge
        # 角热区要够大才好按（默认 14px），但不能大到吃掉小窗口
        e = max(8, min(e, w // 3, h // 3))
        west, east = x <= e, x >= w - e
        north, south = y <= e, y >= h - e
        if north and west:
            return "nw"
        if north and east:
            return "ne"
        if south and west:
            return "sw"
        if south and east:
            return "se"
        return None               # ← 四条边不再拉伸

    def _press(ev):
        m = _which(ev)
        st["mode"] = m
        if not m:
            return
        st["x"], st["y"] = ev.x_root, ev.y_root
        # ★ 2026-10-09 修正：起点位置必须**在按下时记一次**。
        #   原来每次 Motion 都读 winfo_x()/winfo_y() —— Tk 的 geometry 请求不是
        #   立即生效，连续快速拖动时读到的是旧值，于是每帧都把整个位移量再加一遍，
        #   窗口越拉越偏、最后飞出屏幕（用户实测）。
        st["px"], st["py"] = win.winfo_x(), win.winfo_y()
        st["w"], st["h"] = win.winfo_width(), win.winfo_height()

    def _motion(ev):
        m = st.get("mode")
        if not m:
            return
        dx, dy = ev.x_root - st["x"], ev.y_root - st["y"]
        x, y, w, h = st["px"], st["py"], st["w"], st["h"]
        # ★ 2026-10-09 修：用「整个虚拟桌面」的尺寸来钳制（原来只拿主屏，
        #   副屏上的窗口一拉伸就被拽回主屏范围）。虚拟桌面见 virtual_screen()。
        _dx0, _dy0, _dx1, _dy1 = virtual_screen(win)
        sw, sh = _dx1 - _dx0, _dy1 - _dy0
        if "e" in m:
            w = max(min_w, st["w"] + dx)
        if "s" in m:
            h = max(min_h, st["h"] + dy)
        if "w" in m:
            w = max(min_w, st["w"] - dx)
            x = st["px"] + (st["w"] - w)
        if "n" in m:
            h = max(min_h, st["h"] - dy)
            y = st["py"] + (st["h"] - h)
        # ★ 2026-10-09 修正（用户实测"四角拖动直接飞出屏幕外"）：
        #   ① 起点位置在按下时记一次（见 _press）—— 原来每帧读 winfo_x() 旧值，
        #      整个位移量被反复叠加，越拉越偏。
        #   ② 尺寸也要钳制：原来只夹位置，尺寸能涨到比屏幕还大（实测 2420x1486
        #      在 2560x1440 屏上），视觉上就是"窗口跑没了"。
        #   ③ 再修（多屏）：钳制范围改用整个虚拟桌面，副屏上拉伸不会被拽回主屏。
        w = min(w, sw)
        h = min(h, sh)
        x, y = clamp_to_desktop(x, y, w, h, win)
        win.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _release(_ev=None):
        st["mode"] = None

    def _hover(ev):
        m = _which(ev)
        try:
            win.configure(cursor=CURSORS.get(m, ""))
        except Exception:
            pass

    def _leave(_ev=None):
        try:
            win.configure(cursor="")
        except Exception:
            pass

    def bind_tree(widgets=()):
        for w in [win] + [x for x in widgets if x is not None]:
            try:
                w.bind("<Motion>", _hover, add="+")
                w.bind("<Button-1>", _press, add="+")
                w.bind("<B1-Motion>", _motion, add="+")
                w.bind("<ButtonRelease-1>", _release, add="+")
            except Exception:
                pass

    return {"bind_tree": bind_tree, "which": _which}


class DarkMenu:
    """自绘深色菜单（替代 tk.Menu，消掉 Windows 上那 2px 浅色白边）。

    特性：无边框 Toplevel + Win11 圆角 + 悬停高亮 + 分隔线 + 子菜单 + 失焦自动关。
    用法：
        m = DarkMenu(root)
        m.add(label, cmd)
        m.add_sep()
        m.add_sub("思考档位…", [("档位A", cb), ...])
        m.popup(x_root, y_root)
    """

    def __init__(self, master, width=210, item_dy=5):
        self.master = master
        self.width = width
        self.item_dy = item_dy
        self.items = []            # (kind, label, cmd, sub)  kind: item/sep
        self._pop = None
        self._sub_pop = None
        self._bind_all_id = None
        self._labels = []
        self._sliders = []

    # ---------- 构建 ----------
    def add(self, label, cmd=None):
        self.items.append(("item", label, cmd, None))
        return self

    def add_sep(self):
        self.items.append(("sep", None, None, None))
        return self

    def add_sub(self, label, sub_items):
        self.items.append(("item", label + "  ▸", None, sub_items))
        return self

    def add_slider(self, label, vmin, vmax, init, cmd, fmt="%d%%"):
        """加一行「标签 + 滑块」（★ 2026-10-09 用户要求透明度改滑块）。

        cmd 收到的是滑块当前值（float）。拖动时实时回调，菜单不关。
        """
        self.items.append(("slider", label, (vmin, vmax, init, cmd, fmt), None))
        return self

    def clear(self):
        self.items = []

    # ---------- 弹出 ----------
    def popup(self, x_root, y_root, parent_pop=None):
        self.close()
        pop = tk.Toplevel(self.master)
        self._pop = pop
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=MENU_BG)
        pop.geometry("+%d+%d" % (x_root, y_root))

        self._labels = []
        for kind, label, cmd, sub in self.items:
            if kind == "sep":
                fr = tk.Frame(pop, bg=MENU_BG, height=1)
                fr.pack(fill="x", pady=4, padx=6)
                sep = tk.Frame(fr, bg="#45454f", height=1)
                sep.pack(fill="x")
                continue
            if kind == "slider":
                # ★ 2026-10-09：滑块行（透明度用）——「标签  值」+ 下方滑块
                vmin, vmax, init, scmd, sfmt = cmd
                # ★ 修正（用户反馈「改完透明度后再按右键会回到 100%」）：
                #   init 支持传 callable → 每次弹出时**现取当前值**。
                #   原来 init 是程序启动时算死的，菜单每次重建都拿旧值。
                try:
                    cur_val = init() if callable(init) else init
                except Exception:
                    cur_val = init
                cur_val = min(vmax, max(vmin, float(cur_val)))

                row = tk.Frame(pop, bg=MENU_BG)
                row.pack(fill="x", padx=10, pady=(4, 0))
                tk.Label(row, text=label, bg=MENU_BG, fg=MENU_FG, anchor="w",
                         font=FONT).pack(side="left")
                val_lbl = tk.Label(row, text=sfmt % cur_val, bg=MENU_BG, fg=YELLOW,
                                   anchor="e", font=FONT_CODE_S)
                val_lbl.pack(side="right")

                def _on_scale(v, lb=val_lbl, c=scmd, fmt=sfmt):
                    try:
                        lb.config(text=fmt % float(v))
                    except Exception:
                        pass
                    if c:
                        try:
                            c(float(v))
                        except Exception:
                            pass

                # ★ 关键：先建 Scale（**不带 command**）→ set 初值 → 再挂 command。
                #   否则 set() 会立刻触发 command，把透明度重置成初值
                #   （用户实测：改完再右键就弹回 100%）。
                sc = tk.Scale(pop, from_=vmin, to=vmax, orient="horizontal",
                              showvalue=0, resolution=1, length=self.width - 40,
                              bg=MENU_BG, fg=MENU_FG, troughcolor="#1b1b21",
                              activebackground=MENU_ACTIVE_BG,
                              highlightthickness=0, bd=0, sliderrelief="flat")
                sc.set(cur_val)
                sc.configure(command=_on_scale)
                sc.pack(fill="x", padx=12, pady=(0, 4))
                self._sliders.append(sc)
                continue
            lb = tk.Label(pop, text=label, bg=MENU_BG, fg=MENU_FG,
                          anchor="w", justify="left", padx=14, pady=self.item_dy,
                          font=FONT)
            lb.pack(fill="x")
            self._labels.append(lb)
            if sub:
                lb.bind("<Enter>", lambda e, it=sub, w=lb: self._open_sub(it, w))
            else:
                lb.bind("<Enter>", lambda e, w=lb: self._close_sub(w))
                lb.bind("<Button-1>", lambda e, c=cmd: self._invoke(c))
            lb.bind("<Enter>", self._hl_on, add="+")
            lb.bind("<Leave>", self._hl_off, add="+")

        pop.update_idletasks()
        w = self.width
        h = pop.winfo_reqheight()
        # 屏幕边界处理（★ 2026-10-09 多屏：改用虚拟桌面，副屏上菜单不再跑到主屏）
        try:
            dx0, dy0, dx1, dy1 = virtual_screen(pop)
            sw, sh = dx1, dy1
            x, y = x_root, y_root
            if x + w > sw:
                x = max(dx0, sw - w - 4)
            if y + h > sh:
                y = max(dy0, sh - h - 4)
            pop.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            pop.geometry("%dx%d" % (w, h))

        try:
            apply_round_corners(pop, 2)
        except Exception:
            pass
        # 失焦/点外面 → 关闭
        pop.bind("<FocusOut>", lambda e: self.close())
        pop.focus_force()
        self._bind_all_id = self.master.bind_all(
            "<Button-1>", self._on_global_click, add="+")
        return pop

    def _hl_on(self, ev):
        try:
            ev.widget.configure(bg=MENU_ACTIVE_BG, fg=MENU_ACTIVE_FG)
        except Exception:
            pass

    def _hl_off(self, ev):
        try:
            ev.widget.configure(bg=MENU_BG, fg=MENU_FG)
        except Exception:
            pass

    def _invoke(self, cmd):
        self.close()
        if cmd:
            try:
                self.master.after(10, cmd)
            except Exception:
                try:
                    cmd()
                except Exception:
                    pass

    def _on_global_click(self, ev):
        """点到菜单外面 → 关闭（点菜单里面不关，交给条目自己的绑定处理）。"""
        try:
            if self._pop and str(ev.widget).startswith(str(self._pop)):
                return
            if self._sub_pop and str(ev.widget).startswith(str(self._sub_pop)):
                return
        except Exception:
            pass
        self.close()

    # ---------- 子菜单 ----------
    def _open_sub(self, sub_items, host_label):
        self._close_sub()
        pop = tk.Toplevel(self.master)
        self._sub_pop = pop
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=MENU_BG)
        labels = []
        for it in sub_items:
            if it is None:
                fr = tk.Frame(pop, bg=MENU_BG, height=1)
                fr.pack(fill="x", pady=4, padx=6)
                tk.Frame(fr, bg="#45454f", height=1).pack(fill="x")
                continue
            lbl, cb = it
            lb = tk.Label(pop, text=lbl, bg=MENU_BG, fg=MENU_FG, anchor="w",
                          justify="left", padx=14, pady=self.item_dy, font=FONT)
            lb.pack(fill="x")
            labels.append(lb)
            lb.bind("<Button-1>", lambda e, c=cb: self._invoke(c))
            lb.bind("<Enter>", self._hl_on, add="+")
            lb.bind("<Leave>", self._hl_off, add="+")
        pop.update_idletasks()
        try:
            # ★ 2026-10-09 多屏：子菜单也用虚拟桌面边界
            dx0, dy0, dx1, dy1 = virtual_screen(pop)
            sw, sh = dx1, dy1
            hx = host_label.winfo_rootx()
            hy = host_label.winfo_rooty()
            w = self.width + 20
            h = pop.winfo_reqheight()
            x = hx + host_label.winfo_width() - 6
            y = hy
            if x + w > sw:
                x = max(dx0, hx - w + 6)
            if y + h > sh:
                y = max(dy0, sh - h - 4)
            pop.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            pass
        try:
            apply_round_corners(pop, 2)
        except Exception:
            pass

    def _close_sub(self, _ev=None):
        if self._sub_pop is not None:
            try:
                self._sub_pop.destroy()
            except Exception:
                pass
            self._sub_pop = None

    def close(self, _ev=None):
        self._close_sub()
        if self._bind_all_id:
            try:
                self.master.unbind_all("<Button-1>")
            except Exception:
                pass
            self._bind_all_id = None
        if self._pop is not None:
            try:
                self._pop.destroy()
            except Exception:
                pass
            self._pop = None
        self._labels = []
        self._sliders = []


_TODAY_SITE_CACHE = {}


def today_site_costs(day=None):
    """今日各站的站方实扣 {站点: {"calls": n, "cost": 元}}（带缓存）。

    权威源 = 站点监控 store.db 的 usage_flows（与面板同口径、同去重规则）。
    ★ 2026-10-09：用户要求悬浮窗显示「今日站点消费」。
    缓存 key 必须带 store.db 的 mtime —— 库是后台持续采集的，只看 day 会
    永远返回首次读到的旧值（同 _site_calls_cached 踩过的坑）。
    """
    if not day:
        day = datetime.now().strftime("%Y-%m-%d")
    p = os.path.join(_get_proxy_monitor_dir(), "store.db")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    key = (day, round(mt, 3))
    hit = _TODAY_SITE_CACHE.get(key)
    if hit is not None:
        return hit
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        for s, n, c in con.execute(
                "SELECT site, COUNT(*), SUM(cost) FROM usage_flows "
                "WHERE day = ? AND day != '' AND %s GROUP BY site" % _FLOW_NON_DUP,
                (day,)):
            k = canon_host(str(s or ""))
            a = out.setdefault(k, {"calls": 0, "cost": 0.0})
            a["calls"] += int(n or 0)
            a["cost"] += float(c or 0)
        con.close()
    except Exception:
        return {}
    if len(_TODAY_SITE_CACHE) >= 4:
        _TODAY_SITE_CACHE.clear()
    _TODAY_SITE_CACHE[key] = out
    return out


def today_site_total(day=None):
    """今日所有站点的消费合计（元）。返回 (总额, 站点数, 调用数)。"""
    d = today_site_costs(day)
    if not d:
        return (None, 0, 0)
    return (sum(v["cost"] for v in d.values()), len(d),
            sum(v["calls"] for v in d.values()))


APP_NAME = "HermesCacheMonitor"
APP_VERSION = "1.3.1"

# 数据目录分裂检测结果（由 _appdata_dir() 填充）：非空 = 发现 MSIX 虚拟化影子目录
_DATA_SPLIT = ""

# 路径全部由 init_paths() 在运行期解析（支持任意 CN 桌面版安装位置 / exe 分发）
HERMES_HOME = ""
DB = ""
UI_DB = ""
LOGS_DIR = ""
HISTORY = ""
WORK_DIR = ""
PRICES_PATH = ""
LEDGER_PATH = ""
SAMPLES_PATH = ""
CONFIG_PATH = ""
REFRESH_MS = 1000
SITE_INFO_TTL = 5.0          # state.db 会话信息缓存秒数


# ─────────────────────── 路径解析（可移植） ───────────────────────

def _appdata_dir():
    """配置/数据目录：优先 %APPDATA%\\HermesCacheMonitor，不可写则退到程序旁。

    ⚠ MSIX 文件虚拟化（2026-09-16 实测澄清）：
      * 打包后的 exe（PyInstaller）非 MSIX 包 → 不虚拟化，写真实 %APPDATA%
      * Store 版 python（WindowsApps\\python.exe）→ 会被重定向到
        %LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation.Python.3.x_<hash>\\LocalCache\\Roaming\\
      * 官方 python（py 启动器）→ 也写真实 %APPDATA%
    所以混用 `python` 和 `py` 跑命令行会出现「两份数据」。这里做一次自检并记日志，
    并把结果放在 _DATA_SPLIT 里供 UI 提示。
    """
    global _DATA_SPLIT
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        try:
            return os.path.dirname(os.path.abspath(sys.argv[0] or "."))
        except Exception:
            return os.getcwd()
    _DATA_SPLIT = _check_data_split(d)
    return d


def _check_data_split(real_dir):
    """检查是否存在「正在被写入的」MSIX 虚拟化影子目录。

    ⚠ 判据是**影子目录比正式目录更新**，而不是「影子目录存在」——
    只要用过一次 Store 版 python 就会留下历史残留目录，若按「存在即报警」，
    这具尸体躺在那儿会让警告永远消不掉（2026-09-16 踩过这个坑）。

    返回影子目录路径；没有「更新的」影子则返回 ""。
    """
    try:
        pk = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Packages")
        if not os.path.isdir(pk):
            return ""
        try:
            real_m = max(os.path.getmtime(os.path.join(real_dir, f))
                         for f in os.listdir(real_dir))
        except Exception:
            real_m = 0.0
        stale = ""
        for name in os.listdir(pk):
            if "PythonSoftwareFoundation" not in name:
                continue
            cand = os.path.join(pk, name, "LocalCache", "Roaming", APP_NAME)
            if not os.path.isdir(cand):
                continue
            files = [f for f in ("cost_ledger.json", "cache_prices.json", "calib_samples.json")
                     if os.path.isfile(os.path.join(cand, f))]
            if not files:
                continue
            try:
                cand_m = max(os.path.getmtime(os.path.join(cand, f)) for f in files)
            except Exception:
                continue
            # 影子比正式新 → 有进程正往那边写，才算真分裂
            if cand_m > real_m:
                return cand
            stale = cand
        if stale:
            _startup_log("数据自检: 发现历史残留影子目录（未更新，仅提示不报警）: %s" % stale)
        return ""
    except Exception:
        return ""


def _valid_home(p):
    return bool(p) and os.path.isfile(os.path.join(p, "state.db"))


def scan_hermes_homes():
    """扫描候选 hermes-home 目录（含 state.db 的才算）。"""
    cands = []
    env = os.environ.get("HERMES_HOME")
    if env:
        cands.append(env)
    roots = []
    for name in ("LOCALAPPDATA", "APPDATA", "ProgramData"):
        b = os.environ.get(name)
        if b:
            roots += [os.path.join(b, "Hermes Agent CN Desktop"), os.path.join(b, "Hermes")]
    roots += [r"D:\Hermes Agent CN Desktop", r"D:\Hermes",
              r"C:\Hermes Agent CN Desktop", r"C:\Hermes"]
    # 各盘根目录里的 *Hermes* 目录（浅层）
    for drive in ("C", "D", "E", "F", "G"):
        root = drive + ":\\"
        if not os.path.isdir(root):
            continue
        try:
            for name in os.listdir(root):
                p = os.path.join(root, name)
                if os.path.isdir(p) and "hermes" in name.lower():
                    roots.append(p)
        except Exception:
            pass
    for r in roots:
        if r:
            cands.append(os.path.join(r, "data", "hermes-home"))
            cands.append(r)
    # 最后手段：从运行中的 Hermes 进程命令行里捞路径
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'hermes-home' } "
             "| Select-Object -First 3 -ExpandProperty CommandLine"],
            capture_output=True, timeout=6)      # ⚠ 不要 text=True：默认按 utf-8 解码，
        raw = out.stdout or b""                  # 中文 Windows 的 PowerShell 输出是 GBK →
        txt = ""                                 # 每次都会抛 UnicodeDecodeError 打在屏幕上
        for enc in ("utf-8", "gbk"):
            try:
                txt = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            txt = raw.decode("utf-8", "replace")
        for m in re.finditer(r"([A-Za-z]:\\[^\"'<>|]*?hermes-home)", txt):
            cands.append(m.group(1).rstrip("\\"))
    except Exception:
        pass
    out = []
    for c in cands:
        if c and c not in out and _valid_home(c):
            out.append(c)

    # ⚠ 排序（2026-09-22 修）：原来按「发现顺序」返回第一个，而候选里
    # 先排 %LOCALAPPDATA% 下的目录、再排硬编码的 D:\... —— 只要有一个残留/空壳
    # hermes-home（例如官方版留下的小库）就会盖过真正在用的库，导致悬浮窗读错库、
    # 连「切档位 / 注入提示」都会改到那份废弃 config 上（实测踩过）。
    # 用 state.db 文件大小排序即可判别：真实在用库 899MB vs 残留空壳 0.3MB（差 3000 倍），
    # 且 getsize 不用开库（不会踩锁）。
    def _weight(p):
        try:
            return os.path.getsize(os.path.join(p, "state.db"))
        except OSError:
            return 0

    out.sort(key=_weight, reverse=True)
    if len(out) > 1:
        _startup_log("探测到 %d 个 hermes-home，按库大小选：%s（其余：%s）" % (
            len(out), out[0], "; ".join(out[1:4])))
    return out


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _ensure_output():
    """windowed（无控制台）exe 下 sys.stdout 是 None，print 会失效。

    这里把输出重定向到数据目录的 cli_output.log，保证用 exe 跑
    --stats / --bills / --segments 等命令行模式也能看到结果。
    返回日志路径（无需重定向时返回 None）。
    """
    need = False
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            need = True
            break
        try:
            s.write("")
        except Exception:
            need = True
            break
    if not need:
        return None
    try:
        path = os.path.join(WORK_DIR, "cli_output.log")
        f = open(path, "a", encoding="utf-8")
        f.write("\n===== %s  %s =====\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), " ".join(sys.argv[1:])))
        f.flush()
        if getattr(sys, "stdout", None) is None:
            sys.stdout = f
        if getattr(sys, "stderr", None) is None:
            sys.stderr = f
        return path
    except Exception:
        try:                       # 兜底：至少别让 print 崩
            if getattr(sys, "stdout", None) is None:
                sys.stdout = open(os.devnull, "w", encoding="utf-8")
            if getattr(sys, "stderr", None) is None:
                sys.stderr = sys.stdout
        except Exception:
            pass
        return None


def init_paths(cfg=None):
    """解析全部路径并填充全局量。返回 (ok, message)。"""
    global HERMES_HOME, DB, UI_DB, LOGS_DIR, HISTORY, WORK_DIR
    global PRICES_PATH, LEDGER_PATH, SAMPLES_PATH, CONFIG_PATH
    global CALIB_PATH, SITE_MERGE_PATH
    CONFIG_PATH = os.path.join(_appdata_dir(), "config.json")
    cfg = cfg if cfg is not None else load_config()

    hh = cfg.get("hermes_home") or ""
    if not _valid_home(hh):
        found = scan_hermes_homes()
        hh = found[0] if found else ""
    HERMES_HOME = hh
    DB = os.path.join(hh, "state.db") if hh else ""
    UI_DB = os.path.join(hh, "desktop-ui.sqlite") if hh else ""
    LOGS_DIR = os.path.join(hh, "logs") if hh else ""

    # WebView2 History（兜底信号，找不到就停用该信号）
    hist = cfg.get("webview2_history") or ""
    if not (hist and os.path.isfile(hist)):
        hist = ""
        envv = os.environ.get("WEBVIEW2_USER_DATA_FOLDER")
        if envv:
            c = os.path.join(envv, "EBWebView", "Default", "History")
            if os.path.isfile(c):
                hist = c
        if not hist and hh:
            # hermes-home 通常是 <root>\data\hermes-home，webview2 在 <root>\data\webview2
            for root in (os.path.dirname(hh), os.path.dirname(os.path.dirname(hh))):
                c = os.path.join(root, "webview2", "EBWebView", "Default", "History")
                if os.path.isfile(c):
                    hist = c
                    break
    HISTORY = hist

    # 数据目录（价格表/账本/样本）
    dd = cfg.get("data_dir") or ""
    if not dd or not os.path.isdir(dd):
        dd = _appdata_dir()
    try:
        os.makedirs(dd, exist_ok=True)
    except Exception:
        pass
    WORK_DIR = dd
    PRICES_PATH = os.path.join(dd, "cache_prices.json")
    LEDGER_PATH = os.path.join(dd, "cost_ledger.json")
    SAMPLES_PATH = os.path.join(dd, "calib_samples.json")
    CALIB_PATH = os.path.join(dd, "proxy_calibration.json")
    SITE_MERGE_PATH = os.path.join(dd, "site_merge.json")

    # 冻结版（PyInstaller）自愈：exe 同级的 proxy_monitor 缺失时补一份。
    # ⚠ 不补的话：_get_proxy_monitor_dir() 返回不存在的路径 → import session_join 失败 →
    #   「校准」报 No module named 'session_join'，「打开网页看板」子进程也拉不起来
    #   （2026-10-05 实测定位，见 handoffs）
    # ⚠ 但「直接释放」有坑：真正的流水库在主工作区 proxy_monitor\store.db，
    #   盲目释放会造出一个空库副本，校准读到它算出来全是 0。
    #   → 策略：先复用本机已有的真实 proxy_monitor（按库大小挑最全的），
    #     实在没有才从 exe 内释放一份。
    if getattr(sys, "frozen", False):
        try:
            _exe_dir = os.path.dirname(sys.executable)
            _pm_target = os.path.join(_exe_dir, "proxy_monitor")

            def _pm_score(p):
                """候选目录打分：能 import session_join 才有意义，流水多少决定优先级。"""
                if not os.path.isfile(os.path.join(p, "session_join.py")):
                    return -1
                db = os.path.join(p, "store.db")
                try:
                    return os.path.getsize(db)
                except OSError:
                    return 0

            if not os.path.isfile(os.path.join(_pm_target, "session_join.py")):
                _cands = []
                # ① 打包内置（保底：模块代码一定是新的）
                _meipass = getattr(sys, "_MEIPASS", "") or ""
                if _meipass:
                    _cands.append(os.path.join(_meipass, "proxy_monitor"))
                # ② ★真实工作区（有流水库，优先复用）
                _ws = os.environ.get("HERMES_CACHE_PM_DIR", "") or r"<项目目录>\proxy_monitor"
                _cands.append(_ws)
                # ③ 上游 release 布局
                _cands.append(os.path.join(_exe_dir, "HermesCacheMonitor_v5_Release", "proxy_monitor"))

                _real = [c for c in _cands[1:] if _pm_score(c) > 0]
                _seed = max(_real, key=_pm_score) if _real else None
                if _seed:
                    _startup_log("proxy_monitor：复用 %s（store.db %d 字节）" % (_seed, _pm_score(_seed)))
                else:
                    _seed = next((c for c in _cands if _pm_score(c) >= 0), None)
                    if _seed:
                        shutil.copytree(_seed, _pm_target)
                        _startup_log("已释放 proxy_monitor 到 %s（源 %s，无现成流水库）" % (_pm_target, _seed))
        except Exception as _ex_pm:
            _startup_log("准备 proxy_monitor 失败：%s" % _ex_pm)

    # 老版本把数据放在工作区根目录 → 平滑迁移一次
    #   （2026-10-04 改造：路径改从环境变量 HERMES_CACHE_LEGACY_DIR 读。）
    #   本机数据早已迁完（工作区根目录下这三个文件都不存在了），
    #   所以这段对你来说已是空转；保留是为了老用户升级时能自动搬家。
    legacy = os.environ.get("HERMES_CACHE_LEGACY_DIR", "")
    if os.path.isdir(legacy) and os.path.abspath(legacy) != os.path.abspath(dd):
        for name in ("cache_prices.json", "cost_ledger.json", "calib_samples.json"):
            src = os.path.join(legacy, name)
            dst = os.path.join(dd, name)
            if os.path.isfile(src) and not os.path.isfile(dst):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass

    if not hh:
        return False, "未找到 Hermes 数据目录（需要含 state.db 的 hermes-home 文件夹）"
    return True, "已连接 %s" % hh


def check_schema():
    """兼容性检查：state.db 是否含所需表。返回 (ok, msg)。"""
    if not DB or not os.path.isfile(DB):
        return False, "未找到 state.db"
    try:
        con = sqlite3.connect(DB)
        tabs = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
    except Exception as ex:
        return False, "state.db 读取失败：%s" % ex
    miss = [t for t in ("sessions", "session_model_usage") if t not in tabs]
    if miss:
        return False, "state.db 缺少表 %s（Hermes 版本可能不兼容）" % ", ".join(miss)
    return True, ""


def first_run_dialog(msg):
    """探测失败时让用户手选 hermes-home。返回路径或 None。

    给非本人使用（分发给朋友）留了足够线索：常见路径示例 + 「先开 Hermes」提示。
    """
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        cands = []
        try:
            cands = scan_hermes_homes()
        except Exception:
            cands = []
        if cands:
            sample = "已扫描到以下候选目录（可直接选它）：\n" + "\n".join("  " + c for c in cands[:3])
        else:
            sample = ("通常长这样（把「安装盘」换成 D/E/F 等实际盘符）：\n"
                      "  安装盘:\\Hermes Agent CN Desktop\\data\\hermes-home\n"
                      "关键：那个文件夹里【直接】有一个 state.db 文件")
        while True:
            retry = messagebox.askretrycancel(
                "Hermes 缓存监控 · 首次配置",
                "%s\n\n"
                "★ 请先确认 Hermes 桌面端已经【打开着】，再点「重试」手动选择目录。\n\n"
                "%s\n\n"
                "点「取消」退出程序。" % (msg, sample))
            if not retry:
                break
            d = filedialog.askdirectory(title="选择 hermes-home 目录（含 state.db 的那个文件夹）")
            if d and _valid_home(d):
                root.destroy()
                return d
            messagebox.showwarning(
                "目录无效",
                "这个目录下没有找到 state.db，不是 hermes-home。\n\n"
                "请往上/往下找一层，选到那个【直接包含 state.db】的文件夹。\n"
                "提示：它通常在 Hermes 安装目录下的 data\\hermes-home\\")
        root.destroy()
    except Exception:
        pass
    return None

# 字符 → token 系数（2026-09-16 用 calib_samples.json 的 17 个干净会话双变量最小二乘标定）
#   标定前 1.0000 / 0.2800 → 聚合比值 0.9337（系统性高估 7%）
#   标定后 1.0714 / 0.2343 → 聚合比值 1.0000（零偏差）
# 重标定方法：攒够 ≥10 个 calib_samples 后，解 min Σ(cjk*a + non*b − out_recorded)²
CHAR_PER_TOK_CJK = 1.0714
CHAR_PER_TOK_OTHER = 0.2343

# 现代极简暗黑配色系统（Zinc 暗调风格，柔和耐看）
BG = "#16161a"          # 深度碳黑背景
FG = "#f4f4f6"          # 主高亮文字
DIM = "#94949e"         # 辅助浅灰
GREEN = "#4ade80"       # 现代柔和荧光翠绿（不刺眼）
YELLOW = "#fbbf24"      # 浅暖琥珀金
RED = "#f87171"         # 柔和浅珊瑚红
BLUE = "#38bdf8"        # 浅天青蓝

# 右键菜单配色（2026-10-09）：与 Windows 系统深色原生菜单观感一致
MENU_BG = "#2c2c2c"
MENU_FG = "#e8e8ee"
MENU_ACTIVE_BG = "#3a3a44"
MENU_ACTIVE_FG = "#ffffff"
MENU_STYLE = dict(bg=MENU_BG, fg=MENU_FG, activebackground=MENU_ACTIVE_BG,
                  activeforeground=MENU_ACTIVE_FG, bd=0, relief="flat")

# 透明度下限（★ 2026-10-09 用户要求：最低 30%）——再低就看不清字了
MIN_ALPHA = 0.30

# ★ 2026-10-09 窗口尺寸下限（统一出口）
#   教训：这些值原来散落在切换模式处写死（max(300,…)、max(280,…)），而拉伸热区用的是
#   另一套（min_w=240/min_h=80）→ 用户手动能缩到 240，一切换模式就被抬回 300。
#   现在两边共用同一组常量，绝不允许再出现两套标准。
_MIN_W = 240
_MIN_MINI_H = 80
_MIN_FULL_H = 80

# 字体系统：中文用微软雅黑色（UI版圆润），数字与英文字符用 Windows 原生现代等宽 Cascadia Code
FONT = ("Microsoft YaHei UI", 9)
FONT_S = ("Microsoft YaHei UI", 8)
FONT_L = ("Cascadia Code", 22, "bold")       # 大命中率：等宽精密仪表质感
FONT_CODE = ("Cascadia Code", 9)            # 关键数字、Token与价格行
FONT_CODE_S = ("Cascadia Code", 8)          # 紧凑等宽小数字


# ─────────────────── 思考档位（reasoning effort，可移植） ───────────────────
# 写的是 <HERMES_HOME>/config.yaml 里的 agent.reasoning_effort。
# 只做「行级替换」：只动目标那一行，其余字节原样保留（注释/空行/CRLF 全不动）。
EFFORT_LEVELS = [
    ("",        "未设置（跟随服务端默认）"),
    ("none",    "关闭思考"),
    ("minimal", "最低"),
    ("low",     "低"),
    ("medium",  "中"),
    ("high",    "高"),
    ("xhigh",   "很高"),
    ("max",     "最高"),
]
# 这些档位在部分中转站会被拒（HTTP 400）：选中时给提示，但不禁用（换模型就合法）
EFFORT_RISKY = {"none", "minimal"}
EFFORT_BACKUP_KEEP = 5

_EFFORT_CACHE = {"t": 0.0, "v": "", "p": ""}


def effort_label(level):
    """档位值 -> 中文名"""
    for v, name in EFFORT_LEVELS:
        if v == (level or ""):
            return name
    return level or "未设置"


def effort_config_path():
    """config.yaml 路径。优先用运行期解析的 HERMES_HOME，没有就现扫一次。"""
    hh = HERMES_HOME
    if not hh:
        try:
            found = scan_hermes_homes()
            hh = found[0] if found else ""
        except Exception:
            hh = ""
    return os.path.join(hh, "config.yaml") if hh else ""


def read_effort():
    """读当前档位。返回 (level, config_path)；读不到时 level 是空串。"""
    p = effort_config_path()
    if not p or not os.path.isfile(p):
        return "", p
    try:
        raw = open(p, "r", encoding="utf-8", newline="").read()
    except Exception:
        return "", p
    sep = "\r\n" if "\r\n" in raw else "\n"
    in_agent = False
    for ln in raw.split(sep):
        if ln.startswith("agent:"):
            in_agent = True
            continue
        if in_agent:
            if ln and not ln.startswith((" ", "\t")):
                break
            m = re.match(r"^\s+reasoning_effort:\s*(\S*)\s*$", ln)
            if m:
                return m.group(1).strip().strip('"').strip("'"), p
    return "", p


def read_effort_cached(ttl=5.0):
    """带缓存的读（给每秒刷新的状态行用，避免频繁读盘）。"""
    now = time.time()
    if now - _EFFORT_CACHE["t"] < ttl:
        return _EFFORT_CACHE["v"], _EFFORT_CACHE["p"]
    v, p = read_effort()
    _EFFORT_CACHE.update({"t": now, "v": v, "p": p})
    return v, p


def _backup_config(path, raw):
    """把 config.yaml 备份到数据目录 config_bak/，只留最近几份。"""
    try:
        d = os.path.join(_appdata_dir(), "config_bak")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "config.yaml.%s.bak" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        with open(f, "w", encoding="utf-8", newline="") as fh:
            fh.write(raw)
        olds = sorted([x for x in os.listdir(d) if x.startswith("config.yaml.")])
        for x in olds[:-EFFORT_BACKUP_KEEP]:
            try:
                os.remove(os.path.join(d, x))
            except Exception:
                pass
        return f
    except Exception:
        return ""


def _atomic_write_text(path, text):
    """原子写文本：同目录 tmp → fsync → os.replace。

    ⚠ 为什么必须这样写（原实现是 open(p, "w") 直接截断写）：
    进程在写入中途被杀 / 磁盘满 / 被杀软拦截时，原文件会留下**半截 YAML**，
    Hermes 随后启动会因为解析失败而报错。原子替换保证「要么全新内容、要么原样」。

    tmp 必须与目标同目录，os.replace 才是同盘原子替换（跨盘会退化成复制+删除）。
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".cfgwrite_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:                    # 尽量沿用原文件权限（Windows 上通常无效，忽略）
            os.chmod(tmp, os.stat(path).st_mode)
        except Exception:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def write_effort(level):
    """行级替换写 agent.reasoning_effort。返回 (ok, message)。

    level == "" 表示删除该行（回到未设置）。
    写前备份、写后读回校验；校验不过自动还原。
    """
    p = effort_config_path()
    if not p:
        return False, "没找到 config.yaml\n（先确认能识别到 hermes-home）"
    if not os.path.isfile(p):
        return False, "config.yaml 不存在：\n%s" % p
    try:
        raw = open(p, "r", encoding="utf-8", newline="").read()
    except Exception as e:
        return False, "读取失败：%s" % e

    sep = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(sep)

    ai = None
    for i, ln in enumerate(lines):
        if ln.startswith("agent:"):
            ai = i
            break
    if ai is None:
        return False, "config.yaml 里没有 agent: 段，未做修改"

    end = len(lines)
    for j in range(ai + 1, len(lines)):
        if lines[j] and not lines[j].startswith((" ", "\t")):
            end = j
            break

    target = None
    for j in range(ai + 1, end):
        if re.match(r"^\s+reasoning_effort:", lines[j]):
            target = j
            break

    new_lines = list(lines)
    if level:
        ind = "  "
        if target is not None:
            m = re.match(r"^(\s+)", new_lines[target])
            if m:
                ind = m.group(1)
            new_lines[target] = "%sreasoning_effort: %s" % (ind, level)
        else:
            for j in range(ai + 1, end):
                m = re.match(r"^(\s+)\S", new_lines[j])
                if m:
                    ind = m.group(1)
                    break
            new_lines.insert(ai + 1, "%sreasoning_effort: %s" % (ind, level))
    else:
        if target is None:
            return True, "本来就是「未设置」，没有改动"
        del new_lines[target]

    bak = _backup_config(p, raw)
    try:
        _atomic_write_text(p, sep.join(new_lines))
    except Exception as e:
        return False, "写入失败：%s" % e

    _EFFORT_CACHE["t"] = 0.0            # 让缓存失效
    got, _ = read_effort()
    if (got or "") != (level or ""):
        try:
            _atomic_write_text(p, raw)          # 原子还原
        except Exception:
            pass
        return False, "写回校验失败（已还原）\n备份：%s" % bak
    return True, "已切到「%s」" % effort_label(level)


# ─────────────── 提示注入（environment_hint，可移植） ───────────────
# 写的是 <HERMES_HOME>/config.yaml 里的 agent.environment_hint。
# 与 reasoning_effort 的关键差异：它是**多行块标量**（| 或 >），所以要先定位块边界。
# 仍然只做「行级替换」：只动目标行区间，其余字节级不变（注释/空行/CRLF 全不动）。
# ⚠ 禁止 import yaml：spec 未打包 pyyaml，exe 会崩。全部手写正则。
HINT_KEY = "environment_hint"
HINT_BLOCK_STEP = 4          # 多行块相对键的缩进格数
_HINT_CACHE = {"t": 0.0, "v": "", "p": "", "present": False}


def _ydent(ln):
    """一行的前导空白格数。"""
    return len(ln) - len(ln.lstrip(" \t"))


def _find_yaml_block(lines, key, section="agent"):
    """在顶层段 section 内定位 key 的子块。

    返回 (key_idx, block_end, section_end, forced)：
      key_idx     key 行下标；键不存在时 None
      block_end   块内容结束位置（不含）；键不存在时 None
      section_end 段结束位置（不含），用于插入定位；没有该段时 None
      forced      True = 块边界定位失败，已按「方案 B」强行吃到段尾
    """
    si = None
    for i, ln in enumerate(lines):
        if re.match(r"^%s:\s*(#.*)?$" % re.escape(section), ln):
            si = i
            break
    if si is None:
        return None, None, None, False

    sec_end = len(lines)
    for j in range(si + 1, len(lines)):
        if lines[j] and not lines[j].startswith((" ", "\t")):
            sec_end = j
            break

    ki = None
    for j in range(si + 1, sec_end):
        if re.match(r"^(\s+)%s\s*:" % re.escape(key), lines[j]):
            ki = j
            break
    if ki is None:
        return None, None, sec_end, False

    kind = _ydent(lines[ki])
    ke = last = ki + 1          # last = 最后一个确认为块内容的行 + 1（尾随空行不算）
    while ke < sec_end:
        ln = lines[ke]
        if not ln.strip():      # 空行：先乐观跳过，若后续判定不在块内则回退
            ke += 1
            continue
        if _ydent(ln) > kind:
            ke += 1
            last = ke
            continue
        break
    ke = last

    head_val = lines[ki].split(":", 1)[1].strip()
    forced = False
    # 方案 B：声明了块标量却一行内容都没圈到 → 缩进错乱，强行截断到段尾
    if ke == ki + 1 and head_val[:1] in ("|", ">"):
        ke = sec_end
        forced = True
    return ki, ke, sec_end, forced


def _hint_block_base(head_val, body, key_indent):
    """块内容的基准缩进列数：带数字指示符（|4）按指示符，否则取首个非空行缩进。"""
    m = re.match(r"^[|>]\s*(\d+)", (head_val or "").strip())
    if m:
        return len(key_indent) + int(m.group(1))
    for ln in body:
        if ln.strip():
            return _ydent(ln)
    return len(key_indent) + 2


def _parse_hint_text(raw):
    """从 config.yaml 原文解析 environment_hint。返回 (text, present)。"""
    if not raw:
        return "", False
    sep = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(sep)
    ki, ke, _, _ = _find_yaml_block(lines, HINT_KEY)
    if ki is None:
        return "", False
    m = re.match(r"^(\s+)%s\s*:(.*)$" % re.escape(HINT_KEY), lines[ki])
    if not m:
        return "", False
    key_indent, head_val = m.group(1), m.group(2).strip()
    body = lines[ki + 1:ke]
    if body:
        base = _hint_block_base(head_val, body, key_indent)
        out = []
        for ln in body:
            if not ln.strip():
                out.append("")
            elif ln[:base].strip() == "":
                out.append(ln[base:])
            else:
                out.append(ln)      # 缩进不足：畸形，原样保留交给上层判断
        return "\n".join(out).rstrip("\n"), True
    if head_val[:1] in ("|", ">"):
        return "", True             # 声明了块标量却没内容
    if len(head_val) >= 2 and head_val[0] == '"' and head_val[-1] == '"':
        return head_val[1:-1].replace('\\"', '"').replace("\\\\", "\\"), True
    if len(head_val) >= 2 and head_val[0] == "'" and head_val[-1] == "'":
        return head_val[1:-1].replace("''", "'"), True
    return head_val, True


def _format_hint_lines(text, key_indent):
    """把文本渲染成 environment_hint 的行列表（单行用引号 / 多行用块标量）。"""
    t = (text or "").replace("\r\n", "\n").rstrip()
    if "\n" not in t:
        esc = t.replace("\\", "\\\\").replace('"', '\\"')
        return ['%s%s: "%s"' % (key_indent, HINT_KEY, esc)]
    body_indent = key_indent + (" " * HINT_BLOCK_STEP)
    # 两个指示符都要：
    #   ① 首行以空格开头时，YAML 会把首行空白当缩进基准 → 必须用显式指示符 |4
    #   ② 一律加 "-"（strip chomping）：裸 | 是 clip，解析值会多一个尾换行，
    #      与写入文本对不上（实测踩过），|- 才能让两边精确相等
    mark = "|4-" if t[:1] in (" ", "\t") else "|-"
    out = ["%s%s: %s" % (key_indent, HINT_KEY, mark)]
    for ln in t.split("\n"):
        out.append((body_indent + ln) if ln.strip() else "")
    return out


def read_hint():
    """读当前 environment_hint。返回 (text, present, config_path)。"""
    p = effort_config_path()
    if not p or not os.path.isfile(p):
        return "", False, p
    try:
        raw = open(p, "r", encoding="utf-8", newline="").read()
    except Exception:
        return "", False, p
    text, present = _parse_hint_text(raw)
    return text, present, p


def read_hint_cached(ttl=5.0):
    """带缓存的读（给每秒刷新的状态行用，避免频繁读盘）。"""
    now = time.time()
    if now - _HINT_CACHE["t"] < ttl:
        return _HINT_CACHE["v"], _HINT_CACHE["present"], _HINT_CACHE["p"]
    v, present, p = read_hint()
    _HINT_CACHE.update({"t": now, "v": v, "present": present, "p": p})
    return v, present, p


def _new_raw_with_hint(raw, text, remove=False):
    """纯函数：算出改完后的新原文。返回 (new_raw, err, forced)；new_raw=None 表示失败。"""
    sep = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(sep)
    ki, ke, sec_end, forced = _find_yaml_block(lines, HINT_KEY)
    if sec_end is None:
        return None, "config.yaml 里没有 agent: 段，未做修改", False

    new_lines = list(lines)
    if ki is not None:
        del new_lines[ki:ke]
        key_indent = re.match(r"^(\s+)", lines[ki]).group(1)
    else:
        key_indent = "  "
        for j in range(0, sec_end):
            m = re.match(r"^(\s+)\S", lines[j])
            if m:
                key_indent = m.group(1)
                break

    if not remove:
        at = ki if ki is not None else sec_end
        for k, ln in enumerate(_format_hint_lines(text, key_indent)):
            new_lines.insert(at + k, ln)
    return sep.join(new_lines), "", forced


def write_hint(text, remove=False):
    """行级写入 agent.environment_hint。返回 (ok, message)。

    remove=True → 删键（含整块）。写前备份、写后读回校验；校验不过自动还原。
    """
    p = effort_config_path()
    if not p:
        return False, "没找到 config.yaml\n（先确认能识别到 hermes-home）"
    if not os.path.isfile(p):
        return False, "config.yaml 不存在：\n%s" % p
    try:
        raw = open(p, "r", encoding="utf-8", newline="").read()
    except Exception as e:
        return False, "读取失败：%s" % e

    want = "" if remove else (text or "").replace("\r\n", "\n").rstrip()
    new_raw, err, forced = _new_raw_with_hint(raw, want, remove=remove)
    if new_raw is None:
        return False, err
    if new_raw == raw:
        return True, "内容没变，未改动"

    bak = _backup_config(p, raw)
    try:
        _atomic_write_text(p, new_raw)
    except Exception as e:
        return False, "写入失败：%s" % e

    _HINT_CACHE["t"] = 0.0              # 让缓存失效
    got, present, _ = read_hint()
    ok = (not present) if remove else (present and got == want)
    if not ok:
        try:
            _atomic_write_text(p, raw)          # 原子还原
        except Exception:
            pass
        return False, "写回校验失败（已还原）\n备份：%s" % bak

    if remove:
        msg = "已删除 environment_hint（回到无注入）"
    else:
        msg = "已注入 environment_hint（%d 字符 / %d 行）" % (
            len(want), (want.count("\n") + 1) if want else 0)
    if forced:
        msg += ("\n\n⚠ 原有内容的缩进异常，已按「到段尾」方式强行替换，\n"
                "可能连带移除了 agent: 段里后面的键（备份可还原）。")
    return True, msg


# ─────────────────────────── helpers ───────────────────────────

def fmt_vol(n):
    n = n or 0
    if n >= 1_000_000_000:
        return "%.2fB" % (n / 1e9)
    if n >= 1_000_000:
        return "%.2fM" % (n / 1e6)
    if n >= 1_000:
        return "%.1fK" % (n / 1e3)
    return str(int(n))


def fmt_money(v, cur="¥"):
    """金额格式化。

    ★ 2026-10-09 修：价格表里 `cur` 可能是 "CNY"（「从中转站实扣同步价格」
      自动写入的就是这个），直接输出会显示成 `CNY0.0035` 这种半洋半土的写法。
      这里统一把常见的币种代码映射成符号，显示才干净。
    """
    if v is None:
        return "--"
    # 币种代码 → 符号（用户看到的应该是符号，不是代码）
    _SYM = {"CNY": "¥", "RMB": "¥", "元": "¥", "USD": "$", "US$": "$",
            "EUR": "€", "JPY": "¥", "": "¥", None: "¥"}
    cur = _SYM.get((cur or "").strip().upper() if isinstance(cur, str) else cur,
                   cur or "¥")
    if abs(v) < 1:
        return "%s%.4f" % (cur, v)
    if abs(v) < 10:
        return "%s%.3f" % (cur, v)      # 1~10 元：保留 3 位，避免跨 1 元时精度视觉跳变
    return "%s%.2f" % (cur, v)


def is_peak(now_hm, segments, day=None, weekend_off=False):
    """峰谷判定：HH:MM 字符串可直接比大小；支持跨午夜段。

    区间语义统一为**左闭右开 [start, end)**（2026-09-16 与 _overlap 对齐）：
    这样「08:00-12:00 + 12:00-18:00」在 12:00 这个点只算进后一段，不会双重命中。

    *weekend_off* = 周六日全天按闲时（DeepSeek 2026-08-23 起的规则），
    需要 *day*（"YYYY-MM-DD"）才能判星期。
    """
    if weekend_off and day:
        try:
            if datetime.strptime(day, "%Y-%m-%d").weekday() >= 5:
                return False
        except ValueError:
            pass
    for seg in segments or []:
        try:
            s, e = seg[0], seg[1]
        except Exception:
            continue
        if not s or not e or s == e:
            continue
        if s < e:
            if s <= now_hm < e:
                return True
        else:                                   # 跨午夜：[s,24:00) ∪ [00:00,e)
            if now_hm >= s or now_hm < e:
                return True
    return False


DEFAULT_MODEL_MERGE = {}
_SITE_MERGE_CACHE = None    # 归并配置缓存（站点表 + 模型表一起）
_SITE_MERGE_MTIME = 0.0


def _self_dir():
    """本程序所在目录（打包后是 exe 目录，否则是脚本目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_proxy_monitor_dir():
    """动态自适应解析 proxy_monitor 目录（支持分发与便携运行）。

    候选顺序（2026-10-05 起，按数据价值排）：
      ① 环境变量 HERMES_CACHE_PM_DIR（显式指定，留给分发给朋友时用）
      ② 本机真实工作区 proxy_monitor（有流水库，优先复用）
      ③ exe/脚本同级 proxy_monitor（自愈释放出来的那份）
      ④ WORK_DIR / %APPDATA%\\HermesCacheMonitor 下的副本
    ⚠ ②排③前面的原因：dist 下那份是 exe 自愈释放的，库是空的；
      真实流水在主工作区，先用它校准才不会算出 0。
    """
    cands = []
    env_pm = os.environ.get("HERMES_CACHE_PM_DIR", "")
    if env_pm:
        cands.append(env_pm)
    cands.append(r"<项目目录>\proxy_monitor")
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        cands.append(os.path.join(exe_dir, "proxy_monitor"))
    else:
        cands.append(os.path.join(_self_dir(), "proxy_monitor"))
    if "WORK_DIR" in globals() and WORK_DIR:
        cands.append(os.path.join(WORK_DIR, "proxy_monitor"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append(os.path.join(appdata, "HermesCacheMonitor", "proxy_monitor"))
    # 兜底：脚本自身所在目录下的 proxy_monitor（分发/便携场景最可靠）
    cands.append(os.path.join(_self_dir(), "proxy_monitor"))
    for p in cands:
        if p and os.path.isdir(p):
            return p
    return cands[0]


def _host_of(url):
    """从 URL / base_url 里抠出纯域名（小写、去端口与路径）。"""
    s = str(url or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0].split("@")[-1]
    return s.split(":")[0].strip()


def _sites_json_path():
    return os.path.join(_get_proxy_monitor_dir(), "sites.json")


# 常见的两段式后缀（用于取「可注册主域名」）
_TWO_PART_SUFFIX = ("com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "com.hk",
                    "com.tw", "co.uk", "co.jp", "co.kr", "com.au", "ne.jp",
                    "com.sg", "com.br", "com.ru", "org.uk")


def _base_domain(host):
    """取可注册主域名：apivip.dshapi.icu -> dshapi.icu。

    只用来「认出同一家的新入口」，不做任何改写；拿不准就返回空串。
    """
    h = str(host or "").strip().lower().strip(".")
    if not h or h in ("?", "localhost") or ":" in h or "/" in h:
        return ""
    if re.match(r"^[\d.]+$", h):          # 裸 IP 不参与
        return ""
    parts = [p for p in h.split(".") if p]
    if len(parts) < 2:
        return ""
    last2 = ".".join(parts[-2:])
    if last2 in _TWO_PART_SUFFIX and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def _db_hosts_models():
    """Hermes 库里现用的 (原始主机, 原始模型)。**不做 canon**（避免递归）。"""
    out = []
    try:
        for host, model in db_query(
                "SELECT DISTINCT billing_base_url, model FROM session_model_usage"):
            h = ""
            try:
                h = urlparse(host or "").hostname or (host or "")
            except Exception:
                h = host or ""
            out.append((str(h).strip().lower(), str(model or "").strip()))
    except Exception:
        pass
    return out


def _known_ledger_names():
    """账本里出现过的 (站点集合, 模型集合)。"""
    hosts, models = set(), set()
    p = LEDGER_PATH if "LEDGER_PATH" in globals() else ""
    if not p or not os.path.isfile(p):
        return hosts, models
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        for blob in (d.get("days") or {}).values():
            for k in (blob or {}):
                parts = str(k).split("|", 1)
                if parts[0].strip():
                    hosts.add(parts[0].strip())
                if len(parts) == 2 and parts[1].strip():
                    models.add(parts[1].strip())
    except Exception:
        pass
    return hosts, models


def _auto_site_merge():
    """从站点监控自动派生站点归并表 —— 「对齐站点」靠的就是这一步。

    两条规则：
      ① 域名池：读 proxy_monitor/sites.json，把每个站的 `bases` / `base_url` 域名
         并入该站的 `host`（`host` 是库内站点身份，历史流水都挂它名下）。
      ② ★同主域名：新域名（如 apivip.dshapi.icu）只要和站点监控里某个站
         共享同一个主域名（dshapi.icu），就自动并入那个站 —— 这样「新加的入口」
         不用手填也能被认出来。同一主域名下挂多个站时**不动**（不敢猜）。

    返回 {别名域名: 主站名}
    """
    out = {}
    p = _sites_json_path()
    if not os.path.isfile(p):
        return out
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return out

    sites_hosts = set()
    for s in (d.get("sites") or []):
        if not isinstance(s, dict):
            continue
        target = _host_of(s.get("host"))
        if not target:
            continue
        sites_hosts.add(target)
        # ① 域名池
        cands = [_host_of(s.get("host")), _host_of(s.get("base_url"))]
        cands += [_host_of(b) for b in (s.get("bases") or [])]
        for c in cands:
            if c and c != target:
                out[c] = target

    # ② 同主域名自动识别新入口
    bd2host = {}
    for t in sites_hosts:
        bd = _base_domain(t)
        if bd:
            bd2host.setdefault(bd, set()).add(t)
    if bd2host:
        seen = set(sites_hosts)
        lh, _lm = _known_ledger_names()
        seen |= lh
        seen |= {h for h, _m in _db_hosts_models()}
        for h in sorted(seen):
            if not h or h == "?" or h in out or h in sites_hosts:
                continue
            cands = bd2host.get(_base_domain(h)) or set()
            if len(cands) == 1:
                t = next(iter(cands))
                if t != h:
                    out[h] = t
    return out


def _file_mtime(p):
    try:
        return round(os.path.getmtime(p), 6) if (p and os.path.isfile(p)) else 0.0
    except OSError:
        return 0.0


def _site_monitor_aliases():
    """站点归一的**唯一权威源**：读站点监控侧的 `host_alias.py`。

    ⚠️ 为什么改成这样（2026-10-03 清理）：
      这里原本内置一份 `DEFAULT_SITE_MERGE`（api/api2/api3/api4 → api.dshapi.icu），
      而站点监控侧 `proxy_monitor/host_alias.py` 里有一份**一模一样的** `DEFAULT_ALIAS`。
      同一件事两处各写一份 → 以后改一家站的域名归并，只改一边就会对不上账。
      现在统一从站点监控侧读，那边改一处、两边同时生效。

    读不到（文件缺失/语法错）→ 返回空表，其余规则（自动识别 + 手写配置）照常生效。
    """
    out = {}
    p = os.path.join(_get_proxy_monitor_dir(), "host_alias.py")
    if not os.path.isfile(p):
        return out
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_pm_host_alias", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        d = getattr(m, "DEFAULT_ALIAS", None)
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    out[k.strip().lower()] = v.strip().lower()
    except Exception:
        return {}
    return out


def _merge_sig():
    """归并配置的缓存签名：归并文件 / sites.json / store.db / 账本 / Hermes 库
    任一变动就重建（新站一冒头就自动认出来，不用手点刷新）。"""
    p = SITE_MERGE_PATH if "SITE_MERGE_PATH" in globals() else ""
    lp = LEDGER_PATH if "LEDGER_PATH" in globals() else ""
    hp = DB if "DB" in globals() else ""
    return (_file_mtime(p), _file_mtime(_sites_json_path()),
            _file_mtime(os.path.join(_get_proxy_monitor_dir(), "store.db")),
            _file_mtime(lp), _file_mtime(hp))


def _known_ledger_models():
    """账本里出现过的模型名（自动归并的候选来源）。"""
    return _known_ledger_names()[1]


def _site_monitor_models():
    """站点监控库里用过的模型名 —— 这些就是「标准名」，模型归并拿它们对齐。"""
    out = set()
    p = os.path.join(_get_proxy_monitor_dir(), "store.db")
    if not os.path.isfile(p):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        for (m,) in con.execute("SELECT DISTINCT model FROM usage_flows"):
            m = str(m or "").strip()
            if m and m != "*":
                out.add(m)
        con.close()
    except Exception:
        pass
    return out


# ⚠️ 站点监控库的**唯一权威过滤口径**，必须与 proxy_monitor/panel.py 的 NON_FLOW
#    保持一致（区别只是表别名用 u. 前缀，便于在 JOIN 查询里引用）。
#    sub2api(dshapi) 站同一笔用量会留下 client: 逐条流水 + sub2api-daily/model: 汇总行，
#    token 与金额互相重叠，全表 SUM 会重复计费。策略：有逐条就只认逐条。
_FLOW_NON_DUP = (
    "(request_id NOT LIKE 'sub2api-model:%' "
    " AND (request_id NOT LIKE 'sub2api-daily:%' "
    "      OR NOT EXISTS (SELECT 1 FROM usage_flows x "
    "                     WHERE x.site = usage_flows.site "
    "                       AND x.request_id LIKE 'client:%' "
    "                       AND x.day = usage_flows.day)))")

_FLOW_NON_DUP_U = (
    "(u.request_id NOT LIKE 'sub2api-model:%' "
    " AND (u.request_id NOT LIKE 'sub2api-daily:%' "
    "      OR NOT EXISTS (SELECT 1 FROM usage_flows x "
    "                     WHERE x.site = u.site "
    "                       AND x.request_id LIKE 'client:%' "
    "                       AND x.day = u.day)))")

_SITE_TRUTH_CACHE = {}


def site_truth_agg(since_ts=None, until_ts=None):
    """站方真值：从站点监控库读「站方实际收的钱」，按 站×模型 聚合。

    为什么以它为准：站方是收钱的一方，它的数字就是实付。Hermes 侧靠
    session_model_usage 的水位差估算，会因上游不回缓存明细等原因偏离。

    返回 {"host|model": {"calls","in","hit","out","cost"}}；读不到返回 {}。
    """
    p = os.path.join(_get_proxy_monitor_dir(), "store.db")
    if not os.path.isfile(p):
        return {}
    sig = (round(since_ts, 3) if since_ts else None,
           round(until_ts, 3) if until_ts else None, _file_mtime(p))
    hit = _SITE_TRUTH_CACHE.get(sig)
    if hit is not None:
        return hit
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        sql = ("SELECT site, model, COUNT(*), SUM(in_tokens), SUM(cache_read), "
               "SUM(out_tokens), SUM(cost), "
               "SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) FROM usage_flows "
               "WHERE day != '' AND %s" % _FLOW_NON_DUP)
        args = []
        if since_ts:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        if until_ts:
            sql += " AND ts <= ?"
            args.append(int(until_ts))
        sql += " GROUP BY site, model"
        for s, m, n, i, cr, o, c, miss in con.execute(sql, args):
            h = canon_host(str(s or "").strip())
            mm = canon_model(str(m or "").strip() or "*")
            k = "%s|%s" % (h, mm)
            a = out.setdefault(k, {"calls": 0, "in": 0, "hit": 0, "out": 0,
                                   "cost": 0.0, "cost_missing": 0})
            a["calls"] += n or 0
            a["in"] += i or 0
            a["hit"] += cr or 0
            a["out"] += o or 0
            a["cost"] += float(c or 0)
            a["cost_missing"] += miss or 0
        con.close()
    except Exception:
        return {}
    if len(_SITE_TRUTH_CACHE) > 32:
        _SITE_TRUTH_CACHE.clear()
    _SITE_TRUTH_CACHE[sig] = out
    return out


_SITE_TRUTH_DAY_CACHE = {}


def _day_in_scope(d, day_set):
    """这一天该不该参与「站方真值」替换（防未来日期 / 脏日期污染）。

    ⚠️ 为什么必须有（2026-10-03 自查实测）：
      站点监控库的 day 直接取站方返回的 created_at 日期。一旦站方后台时区偏移、
      或出现按日汇总之类的脏行，day 就可能落到「明天」甚至更远。
      `day_set` 只由窗口内**过去的天**构成，所以未来日期天然不在集合里；
      但 `all` / 不限窗口时 day_set 是 None —— 那种写法会把未来日期也算进总额。
      这里统一收口：必须是合法 YYYY-MM-DD、且不晚于今天，才允许参与替换。

    实测（合成未来日期 ¥77 的行）：修前 7d 合计被推到 ¥101.02，修后回到正常值。
    """
    if not d:
        return False
    try:
        dd = datetime.strptime(str(d), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    if dd > datetime.now().date():
        return False
    if day_set is not None and str(d) not in day_set:
        return False
    return True


def site_truth_days():
    """按天读站方真值：{day: {"host|model": {calls,in,hit,out,cost}}}。

    ⚠️ 覆盖替换必须**按天**做：站方库只保留 7 天，账本有几十天 —— 按整个窗口
       替换会把站方没覆盖到的天一起抹掉（会少算）。
    口径与 site_truth_agg 完全一致（NON_FLOW 去重）。
    """
    p = os.path.join(_get_proxy_monitor_dir(), "store.db")
    if not os.path.isfile(p):
        return {}
    sig = _file_mtime(p)
    hit = _SITE_TRUTH_DAY_CACHE.get(sig)
    if hit is not None:
        return hit
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        for s, m, d, n, i, cr, o, c, miss in con.execute(
                "SELECT site, model, day, COUNT(*), SUM(in_tokens), SUM(cache_read), "
                "SUM(out_tokens), SUM(cost), "
                "SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) FROM usage_flows "
                "WHERE day != '' AND %s GROUP BY site, model, day" % _FLOW_NON_DUP):
            h = canon_host(str(s or "").strip())
            mm = canon_model(str(m or "").strip() or "*")
            day = str(d or "").strip()
            if not day:
                continue
            a = out.setdefault(day, {}).setdefault(
                "%s|%s" % (h, mm),
                {"calls": 0, "in": 0, "hit": 0, "out": 0, "cost": 0.0, "cost_missing": 0})
            a["calls"] += n or 0
            a["in"] += i or 0
            a["hit"] += cr or 0
            a["out"] += o or 0
            a["cost"] += float(c or 0)
            a["cost_missing"] += miss or 0
        con.close()
    except Exception:
        return {}
    if len(_SITE_TRUTH_DAY_CACHE) > 8:
        _SITE_TRUTH_DAY_CACHE.clear()
    _SITE_TRUTH_DAY_CACHE[sig] = out
    return out


def site_truth_session(sid):
    """实时查库：某个会话归集到的全部站方流水（站方实付口径）。

    ⚠️ 为什么要它（2026-10-03 实测）：
      原来悬浮窗读 `proxy_calibration.json` 那份**快照**。但 `flow_sessions`
      的归集（时间戳最近邻匹配）**每轮采集都会重跑**，同一个 request_id 可能被
      改判给别的会话 —— 于是快照一旦作废，数字就和站点监控面板永久对不上。
      实测同一时刻：库/面板 664 条 ¥1.465409，而校准文件仍是 676 条 ¥1.482325
      （那 12 条在库里根本不存在，是上一轮归集的残留）。

      改成直接查库、用与 panel.py **完全相同的口径**，两边就永远是同一次查询
      的结果，不存在"快照过期"。

    返回 {"cost","calls","in","cache","out","sites","last_ts"}；查不到返回 None。
    """
    p = os.path.join(_get_proxy_monitor_dir(), "store.db")
    if not os.path.isfile(p) or not sid:
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        # ⚠️ 过滤条件必须与 proxy_monitor/panel.py 的 NON_FLOW 一致
        rows = list(con.execute("""
            SELECT u.site, COUNT(*) n, SUM(u.cost) c, SUM(u.in_tokens) i,
                   SUM(u.cache_read) cr, SUM(u.out_tokens) o, MAX(u.ts) lt
            FROM flow_sessions f JOIN usage_flows u
              ON u.site = f.site AND u.request_id = f.request_id
            WHERE f.session_id = ? AND %s
            GROUP BY u.site""" % _FLOW_NON_DUP_U, (sid,)))
        con.close()
    except Exception:
        return None
    if not rows:
        return None
    out = {"cost": 0.0, "calls": 0, "in": 0, "cache": 0, "out": 0,
           "sites": [], "last_ts": 0}
    for r in rows:
        out["cost"] += float(r["c"] or 0)
        out["calls"] += int(r["n"] or 0)
        out["in"] += int(r["i"] or 0)
        out["cache"] += int(r["cr"] or 0)
        out["out"] += int(r["o"] or 0)
        if r["site"]:
            out["sites"].append(canon_host(r["site"]))
        out["last_ts"] = max(out["last_ts"], int(r["lt"] or 0))
    return out


def site_truth_daily(day=None, host=None):
    """实时查库：某天（默认今天）各站的站方实付。供悬浮窗站点行显示。"""
    p = os.path.join(_get_proxy_monitor_dir(), "store.db")
    if not os.path.isfile(p):
        return {}
    if not day:
        day = datetime.now().strftime("%Y-%m-%d")
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        for s, n, c in con.execute(
                "SELECT site, COUNT(*), SUM(cost) FROM usage_flows "
                "WHERE day = ? AND day != '' AND %s GROUP BY site"
                % _FLOW_NON_DUP, (day,)):
            k = canon_host(str(s or ""))
            a = out.setdefault(k, {"calls": 0, "cost": 0.0})
            a["calls"] += int(n or 0)
            a["cost"] += float(c or 0)
        con.close()
    except Exception:
        return {}
    if host:
        return out.get(canon_host(host))
    return out


def _model_candidates(m):
    """一个模型名的候选取法：剥开头的 [频道tag]；剥 provider/ 前缀。"""
    out = []
    s = re.sub(r"^\[[^\]]*\]\s*", "", m)
    if s and s != m:
        out.append(s)
    if "/" in m:
        tail = m.split("/", 1)[1]
        if tail:
            out.append(tail)
        t2 = re.sub(r"^\[[^\]]*\]\s*", "", tail)
        if t2 and t2 != tail:
            out.append(t2)
    return out


def _auto_model_merge():
    """自动模型名归并（保守）：只当「剥前缀后的名字」确实是**已知标准名**时才合并。

    已知标准名 = 站点监控库用过的模型名 ∪ 账本里已在用的名字。
    → `deepseek/deepseek-v4.1-flash`、`[a]gemini-3.8-flash` 会自动归并；
      而 `stealth/ox-alpha`（剥出来的 ox-alpha 谁都没用过）原地不动。
    """
    ledger_models = _known_ledger_models()
    known = _site_monitor_models() | ledger_models
    out = {}
    for m in ledger_models:
        for c in _model_candidates(m):
            if c in known and c != m:
                out[m] = c
                break
    return out


def load_name_merge(force=False):
    """读归并配置（站点 + 模型），带签名缓存。

    返回 {"sites": {别名:主站}, "models": {别名:标准模型名},
          "auto_align": bool, "auto_model": bool}
    优先级：手动配置 > 自动规则 > 代码里的默认表。
    """
    global _SITE_MERGE_CACHE, _SITE_MERGE_MTIME
    sig = _merge_sig()
    if not force and _SITE_MERGE_CACHE is not None and sig == _SITE_MERGE_MTIME:
        return _SITE_MERGE_CACHE

    path = SITE_MERGE_PATH if "SITE_MERGE_PATH" in globals() else ""
    cands = [path,
             os.path.join(os.environ.get("APPDATA") or "", "HermesCacheMonitor", "site_merge.json"),
             os.path.join(_get_proxy_monitor_dir(), "host_alias.json")]
    cand = next((p for p in cands if p and os.path.isfile(p)), None)

    man_sites, man_models = {}, {}
    auto_align, auto_model = True, True
    if cand:
        try:
            with open(cand, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
            if isinstance(d, dict):
                if "auto_align" in d:
                    auto_align = bool(d.get("auto_align"))
                if "auto_model" in d:
                    auto_model = bool(d.get("auto_model"))
                for k, v in (d.get("map") or {}).items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        man_sites[k.strip().lower()] = v.strip().lower()
                for k, v in (d.get("models") or {}).items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        man_models[k.strip().lower()] = v.strip().lower()
                if "map" not in d and "models" not in d:
                    # 兼容最老的格式：整个文件就是站点映射表
                    for k, v in d.items():
                        if (isinstance(k, str) and isinstance(v, str)
                                and not k.startswith("_")
                                and k not in ("auto_align", "auto_model")):
                            man_sites[k.strip().lower()] = v.strip().lower()
        except Exception:
            pass

    sites = dict(_site_monitor_aliases())    # ★ 归一规则统一由站点监控侧提供
    if auto_align:
        sites.update(_auto_site_merge())
    sites.update(man_sites)
    models = dict(DEFAULT_MODEL_MERGE)
    if auto_model:
        models.update(_auto_model_merge())
    models.update(man_models)

    _SITE_MERGE_CACHE = {"sites": sites, "models": models,
                         "auto_align": auto_align, "auto_model": auto_model}
    _SITE_MERGE_MTIME = sig
    return _SITE_MERGE_CACHE


def load_site_merge():
    return load_name_merge()["sites"]


def load_model_merge():
    return load_name_merge()["models"]


def load_name_merge_raw():
    """只读配置文件里的**手动**规则（不含自动派生），给设置界面显示用。"""
    path = SITE_MERGE_PATH if "SITE_MERGE_PATH" in globals() else ""
    cands = [path,
             os.path.join(os.environ.get("APPDATA") or "", "HermesCacheMonitor", "site_merge.json")]
    cand = next((p for p in cands if p and os.path.isfile(p)), None)
    out = {"sites": {}, "models": {}, "auto_align": True, "auto_model": True}
    if not cand:
        return out
    try:
        with open(cand, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict):
            out["auto_align"] = bool(d.get("auto_align", True))
            out["auto_model"] = bool(d.get("auto_model", True))
            for k, v in (d.get("map") or {}).items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    out["sites"][k.strip().lower()] = v.strip().lower()
            for k, v in (d.get("models") or {}).items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    out["models"][k.strip().lower()] = v.strip().lower()
    except Exception:
        pass
    return out


def save_name_merge(sites=None, models=None, auto_align=True, auto_model=True):
    """把归并配置（站点表 + 模型表）写回 site_merge.json（原子写）。返回路径。"""
    global _SITE_MERGE_CACHE, _SITE_MERGE_MTIME
    path = SITE_MERGE_PATH if "SITE_MERGE_PATH" in globals() else ""
    if not path:
        path = os.path.join(os.environ.get("APPDATA") or "",
                            "HermesCacheMonitor", "site_merge.json")
    obj = {
        "_note": "把同一家的多个名字合并成一个；账本、价格、显示都用合并后的名字。",
        "_site_note": "map = 实际请求域名 → 主站名。auto_align=true 时自动读 proxy_monitor/sites.json 的域名池(bases)并入该站 host（= 对齐站点监控）；手动项优先级更高。",
        "_model_note": "models = 模型别名 → 标准模型名。auto_model=true 时自动剥开头的 [频道tag] 与 provider/ 前缀（仅当剥出的名字是站点监控用过的已知名）。",
        "auto_align": bool(auto_align),
        "auto_model": bool(auto_model),
        "map": {str(k).strip().lower(): str(v).strip().lower()
                for k, v in (sites or {}).items() if str(k).strip() and str(v).strip()},
        "models": {str(k).strip().lower(): str(v).strip().lower()
                   for k, v in (models or {}).items() if str(k).strip() and str(v).strip()},
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    _SITE_MERGE_CACHE = None
    _SITE_MERGE_MTIME = 0.0
    return path


def _chase(mapping, name, limit=10):
    """顺着归并表一路查到终点（防 a→b→c 这种链、防环）。"""
    cur = name
    seen = {cur.lower()}
    for _ in range(limit):
        nx = mapping.get(cur.lower())
        if not nx or nx.lower() in seen:
            break
        seen.add(nx.lower())
        cur = nx
    return cur


def canon_host(h):
    """站点域名归一（例如 api4.dshapi.icu -> api.dshapi.icu）。"""
    if not h or not isinstance(h, str):
        return h or "?"
    return _chase(load_site_merge(), h.strip())


def canon_model(m):
    """模型名归一（例如 deepseek/deepseek-v4.1-flash -> deepseek-v4.1-flash）。

    为什么要它：同一家中转站（或不同站）常把同一个模型写成不同名字
    —— 带 provider 前缀（`deepseek/xxx`）、带频道标签（`[a]xxx`）。
    不归一的话，账本会把同一个模型拆成好几行，价格匹配也可能落空。
    规则来源：手动配置 + 自动剥前缀（仅当剥出的名字是已知标准名）。
    """
    if not m or not isinstance(m, str):
        return m or "?"
    m = m.strip()
    if not m:
        return "?"
    return _chase(load_model_merge(), m)


def _norm_watermark_keys(keys):
    """把历史水位键按 canon_host 归一，并把折到同一键的别名**合并求和**。

    ⚠️ 为什么必须有这个（2026-10-03 实测踩坑，一次幽灵账）：
      水位键形如 "host|model"，而 `_watermark()` 里做过域名归一
      （api4.dshapi.icu → api.dshapi.icu）。若「新增归一名」这条规则是在运行
      中途才生效的，旧水位里就会残留未归一的键，于是结算时：
        · cur（新水位）只有归一后的键，且它的值已经**含**了别名那部分
        · prev.get(新键) 拿不到别名部分 → 差值 = 整批累计
        → 同一批用量被当成「一天的新增量」又记一遍。
      实测：api4 的 218 次调用 / 2.78M 输入 / 13.9M 缓存，在归一后于
      10-01、10-02 两天各入账一次（¥0.297 + ¥0.342 的幽灵账）。

      读 prev 时一并归一（别名合并求和）→ 差值归零，重复消失；
      对将来**任何**换过域名的站都免疫。
    """
    out = {}
    for k, v in (keys or {}).items():
        if not isinstance(v, dict):
            continue
        h, sep, m = str(k).partition("|")
        nk = ("%s|%s" % (canon_host(h), canon_model(m))) if sep else canon_host(h)
        t = out.setdefault(nk, {"calls": 0, "in": 0, "hit": 0, "out": 0})
        for f in ("calls", "in", "hit", "out"):
            try:
                t[f] += int(v.get(f) or 0)
            except (TypeError, ValueError):
                pass
    return out


# 「缓存明细缺失」修正的缓存（key=(会话, 调用数, 缺明细序号, 站方库签名)）——避免每秒重算
_CACHE_FIX_CACHE = {}
# 逐条流水配对的时间容差（秒）。两边时间戳有 20~30 秒系统偏移，留足余量即可。
_CACHE_FIX_TOL_S = 300


def _cache_detail_fix(sid, calls):
    """把「没拿到缓存明细」的调用，用中转站的逐条流水补回真实缓存量。

    为什么需要（2026-10-03 定位并实测）：
      · Hermes 的 agent.log **只在缓存非零时**才打印 `cache=` 字段
        （源码 agent/turn_usage.py:191 `if canonical_usage.cache_read_tokens and ...`）
      · 所以「没有 cache=」= **没拿到缓存明细**，不等于「真的没命中缓存」
      · 旧逻辑把两者当一回事 → 这批调用的整段 prompt 被按最贵的「未命中」计价
        （未命中 0.092 是缓存 0.00184 的 50 倍）
      · 实测频率：10-01 只有 1 次（两边差 −2.5%，对得上）；10-03 涨到 22 次，
        当天账被推高 4%。有过明细的调用**逐条与站方完全一致**，问题只在这批。

    做法：拿这些调用的「总 prompt + 输出」去 store.db 里找**完全相等**的那条流水
      （两边时间戳有 20~30 秒系统偏移，所以不能只靠时间，必须数值相等才算命中），
      用站方的缓存值补齐。
    站方没有（还没采到 / 采集断档）→ **不改数字**，只如实计数（不凭空估，
      免得把一个"多算"换成另一个"少算"）。站方恢复后会自动回溯补上。

    返回 {"fix": 补回的缓存 token, "matched": 修了几次, "missing": 几次没数据}
    """
    noinfo = [c for c in (calls or []) if c.get("noinfo")]
    if not noinfo:
        return {"fix": 0, "matched": 0, "missing": 0}

    pm_db = os.path.join(_get_proxy_monitor_dir(), "store.db")
    # ⚠️ 2026-10-03 自查：缓存 key 必须含站点监控库的 mtime，否则站方采集恢复、
    #    补回那条流水之后，缓存仍返回旧的 fix/matched/missing（金额长时间不更新
    #    且毫无提示）。key 里并入库文件签名即可。
    key = (sid, len(calls or []), tuple(c.get("n") for c in noinfo),
           _file_mtime(pm_db))
    hit = _CACHE_FIX_CACHE.get(key)
    if hit is not None:
        return hit

    flows = []
    if os.path.isfile(pm_db):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % pm_db.replace("\\", "/"), uri=True)
            for idx, (ts, i, cr, o) in enumerate(con.execute(
                    "SELECT ts, in_tokens, cache_read, out_tokens FROM usage_flows "
                    "WHERE request_id NOT LIKE 'sub2api-%'")):
                flows.append((idx, float(ts or 0), int(i or 0), int(cr or 0), int(o or 0)))
            con.close()
        except Exception:
            flows = []

    fix = matched = 0
    used = set()          # ⚠️ 已消费的流水下标：同一份 cache_read 不能补两次
    for c in noinfo:
        try:
            t = datetime.strptime(c["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            continue
        want_total, want_out = c["in"], c["out"]
        best = None
        for idx, ts, i, cr, o in flows:
            # ⚠️ 2026-10-03 自查：原来是「只挑时间最近的一条」，同一 300 秒内出现
            #    两条 token 完全相同的流水（连续两次相同 prompt / 重试）时，两条缺
            #    明细调用会把**同一条**流水各匹配一次 → 缓存量被加两遍（少算钱）。
            #    这里改成一条流水只能被消费一次。
            if idx in used:
                continue
            if i + cr != want_total or o != want_out:
                continue
            d = abs(ts - t)
            if d <= _CACHE_FIX_TOL_S and (best is None or d < best[0]):
                best = (d, cr, idx)
        if best is not None:
            used.add(best[2])
            fix += best[1]
            matched += 1
    res = {"fix": fix, "matched": matched, "missing": len(noinfo) - matched}
    if len(_CACHE_FIX_CACHE) > 64:
        _CACHE_FIX_CACHE.clear()
    _CACHE_FIX_CACHE[key] = res
    return res


# ─────────────────────────── 站方校准数据管理器 ───────────────────────────
class CalibManager:
    """管理站方导出的 proxy_calibration.json，支持会话级和日度级的差值计算。"""

    def __init__(self, path=None):
        self.path = path or (globals().get("CALIB_PATH") or os.path.join(_appdata_dir(), "proxy_calibration.json"))
        self._data = None
        self._mtime = 0.0
        self.enabled = True
        self.session_offsets = {}

    def _resolve_path(self):
        cands = [self.path]
        if "WORK_DIR" in globals() and WORK_DIR:
            cands.append(os.path.join(WORK_DIR, "proxy_calibration.json"))
        cands.append(os.path.join(os.environ.get("APPDATA") or "", "HermesCacheMonitor", "proxy_calibration.json"))
        cands.append(os.path.join(_get_proxy_monitor_dir(), "proxy_calibration.json"))
        existing = [p for p in set(cands) if p and os.path.isfile(p)]
        if not existing:
            return None
        return max(existing, key=lambda p: os.path.getmtime(p))

    def load(self, force=False):
        p = self._resolve_path()
        if not p:
            self._data = None
            return None
        try:
            m = os.path.getmtime(p)
            if not force and self._data is not None and m == self._mtime:
                return self._data
            with open(p, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
            if isinstance(d, dict) and "sessions" in d:
                self._data = d
                self._mtime = m
                return self._data
        except Exception as e:
            _startup_log("读取校准文件失败：%s" % e)
        return self._data

    def get_session_calib(self, sid):
        """该会话的站方实付（★ 实时查库优先，文件快照降级为兜底）。

        ⚠️ 2026-10-03 改：原来只读 `proxy_calibration.json` 快照。但归集每轮
        重跑，快照会作废（实测库 664 条 ¥1.4654 vs 快照 676 条 ¥1.4823，
        那 12 条库里根本不存在）→ 悬浮窗数字永久高于站点监控。
        现在直接查库，和面板同源同时刻；库读不到才退回文件。
        """
        if not self.enabled or not sid:
            return None
        live = site_truth_session(sid)
        if live:
            return {"cost": live["cost"], "calls": live["calls"],
                    "in_tokens": live["in"], "cache_read": live["cache"],
                    "out_tokens": live["out"], "sites": live["sites"],
                    "last_ts": live["last_ts"], "src": "live"}
        data = self.load()
        if not data:
            return None
        return data.get("sessions", {}).get(sid)

    def get_calibration_offset(self, sid, hermes_cost, hermes_cr, hermes_inp):
        """计算或获取该会话锁定的补差额。

        规则（用户 2026-10-03 明确）：
          - 站方同步时，锁定 补差 = 站方实扣 − hermes检测；
          - 两次同步之间，hermes 自己涨的那部分照加（显示 = hermes + 锁定的补差），
            这样站点没更新时价格也能跟着涨，不会"一直不涨价"；
          - 下次站方数据推进时，用新的站方值重算补差；
          - 若站方无数据，返回 None。

        ⚠️ 2026-10-03 同时改了**站方值的来源**：原来只读 proxy_calibration.json
        快照，但归集每轮重跑会让快照作废（实测库 664 条 ¥1.4654 vs 快照
        676 条 ¥1.4823，那 12 条库里根本不存在）→ 悬浮窗永久高于站点监控。
        现在优先实时查库（与 panel.py 同口径），查不到才退回快照。
        """
        if not self.enabled or not sid:
            return None
        site_sess = self.get_session_calib(sid)
        if not site_sess:
            return None

        site_cost = site_sess.get("cost", 0.0)
        site_cr = site_sess.get("cache_read", 0)
        site_inp = site_sess.get("in_tokens", 0)

        site_fp = (site_sess.get("last_ts"), site_sess.get("calls"), site_cost)
        cached = self.session_offsets.get(sid)
        # 刷新判据：只有首次计算或站方数据指纹真实推进时才重算，绝不因全局文件更新而冲抵补差
        if cached is None or cached.get("site_fp") != site_fp:
            diff_cost = max(0.0, site_cost - hermes_cost)
            diff_cr = max(0, site_cr - hermes_cr)
            diff_inp = max(0, site_inp - hermes_inp)
            cached = {
                "diff_cost": round(diff_cost, 6),
                "diff_cr": diff_cr,
                "diff_inp": diff_inp,
                "site_cost": site_cost,
                "site_fp": site_fp,
                "has_calib": True,
            }
            self.session_offsets[sid] = cached

        return cached

    def get_daily_calib(self, day, host=None):
        """某天各站的站方实付（★ 实时查库优先，与 panel.py 同口径）。"""
        if not self.enabled:
            return None
        live = site_truth_daily(day)
        if live:
            if host:
                return live.get(canon_host(host))
            return live
        data = self.load()
        if not data:
            return None
        day_data = data.get("daily", {}).get(day, {})
        if host:
            return day_data.get(canon_host(host))
        return day_data


def trigger_proxy_refresh(sites=None, timeout=15.0):
    """通知中转站监控立即刷新（支持传入 sites 进行定向极速秒刷新），并确保校准文件必定最新。"""
    ok = False
    msg = ""
    try:
        import urllib.request
        from urllib.parse import quote
        url = "http://127.0.0.1:8788/api/refresh"
        if sites:
            valid_sites = [s.strip() for s in sites if s and s != "?"]
            if valid_sites:
                url += "?sites=" + quote(",".join(valid_sites))
        req = urllib.request.Request(url, headers={"User-Agent": "cache_follow"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                ok = True
                msg = "已定向极速刷新" if sites else "已通过站方服务立即刷新"
    except Exception:
        pass

    try:
        import sys
        pm_dir = _get_proxy_monitor_dir()
        if pm_dir not in sys.path:
            sys.path.insert(0, pm_dir)
        import session_join, calib_export
        session_join.main_quiet()
        calib_export.export_calibration(verbose=False)
        return True, msg or "已本地完成归集与导出"
    except Exception as e:
        return ok, msg or ("导出失败: %s" % e)


_DB_CONN = None
_DB_LOCK = threading.Lock()
_DB_PATH = None


def _db():
    """复用同一个 sqlite 连接。

    每次 sqlite3.connect() 要 20~160ms（实测，磁盘/杀软抖动），
    而 tick 每秒都要查库 → 这就是「顿卡」的主因。复用后降到 <1ms。
    """
    global _DB_CONN, _DB_PATH
    if _DB_CONN is None or _DB_PATH != DB:
        if _DB_CONN is not None:
            try:
                _DB_CONN.close()
            except Exception:
                pass
        _DB_CONN = sqlite3.connect(DB, check_same_thread=False, timeout=3.0)
        _DB_PATH = DB
    return _DB_CONN


def db_query(sql, args=(), one=False):
    global _DB_CONN
    with _DB_LOCK:
        # 最多两轮：① 正常/瞬时异常  ② 连接真坏了 → 重建后重试一次
        for attempt in (0, 1):
            try:
                cur = _db().cursor()
                cur.execute(sql, args)
                rows = cur.fetchall()
                cur.close()
                return (rows[0] if rows else None) if one else rows
            except Exception:
                # ⚠ 只有「连接真的不可用」才拆掉长连接 —— 重连要付 1~160ms（实测磁盘抖动期
                # 可达 160ms）。瞬时 locked / SQL 写错 / 表不存在都不该连累长连接
                # （否则一次偶发异常就让后续若干次查询都变慢，正是历史上「规律性顿卡」的成因）。
                # 判定法：连接还在就用 SELECT 1 探一次；探得通 = 保留，探不通 = 拆。
                alive = False
                try:
                    if _DB_CONN is not None:
                        _DB_CONN.execute("SELECT 1")
                        alive = True
                except Exception:
                    alive = False
                if alive:
                    return None if one else []      # 连接可用 → 保留，下次照样复用
                try:
                    if _DB_CONN is not None:
                        _DB_CONN.close()
                except Exception:
                    pass
                _DB_CONN = None
                # 落到这里说明连接已拆：第 1 轮重试（重建连接），第 2 轮才真放弃
        return None if one else []


def cover_all_day(segments):
    """两段是否覆盖全天（用于保存时警告）。"""
    segs = []
    for seg in segments or []:
        try:
            s, e = seg[0], seg[1]
        except Exception:
            continue
        if not s or not e or s == e:
            continue
        if s <= e:
            segs.append((s, e))
        else:
            segs.append((s, "23:59"))
            segs.append(("00:00", e))
    if len(segs) < 2:
        return False
    segs.sort()
    cur = segs[0][1]
    for s, e in segs[1:]:
        if s > cur:
            return False
        cur = max(cur, e)
    return cur >= "23:59" and segs[0][0] <= "00:01"


# ─────────────────────────── 价格表 ───────────────────────────

class PriceBook:
    def __init__(self, path=None):
        self.path = path or PRICES_PATH
        self._mtime = None
        self._entries = []
        # 非空 = 「文件在、但读不了」→ 禁止写盘，避免用空表覆盖（见 load 注释）
        self._load_error = ""

    def load(self, force=False):
        """读价格表。

        ⚠ 三种情况必须区分（原实现把它们混成一种，导致读失败后保存会清空整个价格表）：
          ① 文件不存在且从未加载过  → 首次运行，空表，允许写盘
          ② 文件不存在但加载过      → 被同步工具/杀软移走，**保留内存**，允许写回重建
                                      （盘上没数据可丢，重建反而保住数据）
          ③ 文件在但读不了（损坏/被锁/权限）→ **保留内存 + 禁止写盘**
                                      （盘上还有救得回来的数据，绝不能覆盖）
        返回当前内存条目列表。
        """
        self._load_error = ""
        try:
            m = os.path.getmtime(self.path)
        except FileNotFoundError:
            self._mtime = None          # 文件没了 → 下次若出现要重读
            return self._entries        # 保留内存（① 时本就是空表）
        except OSError as ex:
            self._load_error = "读不到价格表文件：%s" % ex
            return self._entries
        if not force and m == self._mtime:
            return self._entries
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            self._entries = [e for e in data if isinstance(e, dict) and e.get("host")]
            self._mtime = m
        except OSError as ex:
            self._load_error = "价格表被占用：%s" % ex
        except ValueError as ex:
            self._load_error = "价格表 JSON 损坏：%s" % ex
        return self._entries

    def all(self):
        return list(self.load())

    # ---------- 价格段（时间轴：同一站/模型可随时间改价，历史段不动） ----------
    @staticmethod
    def segs(entry):
        """取条目的价格段列表（按 from 升序）。旧格式（无 periods）→ 空列表。"""
        ps = (entry or {}).get("periods")
        if isinstance(ps, list) and ps:
            return sorted([p for p in ps if isinstance(p, dict)],
                          key=lambda p: p.get("from") or "")
        return []

    @staticmethod
    def pick_seg(entry, day=None):
        """取 *day* 生效的那一段，与条目公共字段合并成「虚拟条目」。

        day 缺省 = 今天。day 早于所有段 → 用最早段。无 periods → 原样返回（旧格式单段）。
        """
        if not entry:
            return None
        ps = PriceBook.segs(entry)
        if not ps:
            return entry
        if day is None:                       # 注意：显式传 "" 表示「最早那段」，不能用 or
            day = datetime.now().strftime("%Y-%m-%d")
        picked = None
        for p in ps:
            f = p.get("from") or ""
            if f <= day and (picked is None or f > (picked.get("from") or "")):
                picked = p
        if picked is None:
            picked = ps[0]
        merged = {k: v for k, v in entry.items() if k != "periods"}
        merged.update(picked)
        merged["_seg_from"] = picked.get("from") or ""
        merged["_seg_n"] = len(ps)
        return merged

    def match(self, host, model, day=None):
        """三级匹配 + 按 *day* 选价格段（day=None → 当前生效段）。"""
        self.load()
        host = host or ""
        model = model or ""
        for e in self._entries:                                    # 1. 站+模型精确
            if e.get("host") == host and e.get("model") == model:
                return self.pick_seg(e, day)
        for e in self._entries:                                    # 2. 站默认
            if e.get("host") == host and e.get("model") in (None, "", "*"):
                return self.pick_seg(e, day)
        for e in self._entries:                                    # 3. 全局+模型
            if e.get("host") == "*" and e.get("model") == model:
                return self.pick_seg(e, day)
        return None

    def entry_of(self, host, model):
        """取原始条目（不选段），供 UI 编辑用。"""
        self.load()
        for e in self._entries:
            if e.get("host") == (host or "") and e.get("model") == (model or ""):
                return e
        return None

    def set_seg_ratio(self, host, model, seg_from, ratio):
        """把倍率写回某一段（保留旧值 ratio_prev）。seg_from="" 表示条目级（旧格式）。"""
        self.load(force=True)
        target = None
        for e in self._entries:
            if e.get("host") == (host or "") and e.get("model") == (model or ""):
                target = e
                break
        if not target:
            return False
        ps = self.segs(target)
        rec = dict(target)
        if ps:
            new_ps = []
            for p in ps:
                p = dict(p)
                if (p.get("from") or "") == (seg_from or ""):
                    if p.get("ratio") is not None:
                        p["ratio_prev"] = p.get("ratio")
                    p["ratio"] = float(ratio)
                    p.pop("ratio_kept", None)
                new_ps.append(p)
            rec["periods"] = new_ps
        else:
            if rec.get("ratio") is not None:
                rec["ratio_prev"] = rec.get("ratio")
            rec["ratio"] = float(ratio)
        rec["updated"] = datetime.now().strftime("%Y-%m-%d")
        self.upsert(rec, keep_ratio=False)
        return True

    def upsert(self, entry, keep_ratio=True):
        """写入条目（同 host+model 覆盖）。

        keep_ratio：倍率沿用保护（防「倍率框留空 / 新建同键条目」把校准过的倍率静默重置成 1.0）
          * 新格式（periods）：按 from 一对一沿用段内倍率
          * 旧格式（条目级）：新条目没带 ratio 而旧条目有 → 沿用
        想显式改回 1.0：填 1（不是留空）。
        """
        self.load(force=True)
        host, model = entry.get("host"), entry.get("model")
        entry = dict(entry)
        if keep_ratio:
            old = None
            for e in self._entries:
                if e.get("host") == host and e.get("model") == model:
                    old = e
                    break
            if old:
                new_ps = entry.get("periods")
                old_ps = self.segs(old)
                if isinstance(new_ps, list) and new_ps and old_ps:
                    keep = {}
                    for p in old_ps:
                        try:
                            r = float(p.get("ratio") or 0)
                        except (TypeError, ValueError):
                            r = 0.0
                        if r and abs(r - 1.0) > 1e-9:
                            keep[p.get("from") or ""] = r
                    for p in new_ps:
                        if not p.get("ratio") and keep.get(p.get("from") or ""):
                            p["ratio"] = keep[p.get("from") or ""]
                            p["ratio_kept"] = True
                elif not isinstance(new_ps, list) and not entry.get("ratio"):
                    try:
                        r = float(old.get("ratio") or 0)
                    except (TypeError, ValueError):
                        r = 0.0
                    if r and abs(r - 1.0) > 1e-9:
                        entry["ratio"] = r          # 沿用旧倍率
                        entry["ratio_kept"] = True  # 供 UI 提示（不参与匹配语义）
        out = []
        replaced = False
        for e in self._entries:
            if e.get("host") == host and e.get("model") == model:
                out.append(entry)
                replaced = True
            else:
                out.append(e)
        if not replaced:
            out.append(entry)
        self._write(out)
        return entry

    def delete(self, host, model):
        self.load(force=True)
        out = [e for e in self._entries
               if not (e.get("host") == host and e.get("model") == model)]
        self._write(out)

    def _write(self, entries):
        """原子写价格表。读失败时拒绝写入（否则会用空表覆盖掉能救回的数据）。"""
        if self._load_error:
            raise RuntimeError(
                "价格表读不到（%s）\n已拒绝写入，避免清空原文件。\n"
                "请确认文件未被其他程序占用后重试。" % self._load_error)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._mtime = None
        self.load(force=True)


def sync_prices_from_proxy(price_book):
    """从中转站数据库（store.db）扫描各站点真实扣费与 token，反推真实价格/倍率并写入价格表。"""
    pm_db = os.path.join(_get_proxy_monitor_dir(), "store.db")
    if not os.path.isfile(pm_db):
        return False, "未找到中转站数据库"
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % pm_db.replace("\\", "/"), uri=True)
        cur = con.cursor()
        rows = cur.execute("""
            SELECT site, model, COUNT(*), SUM(in_tokens), SUM(cache_read), SUM(out_tokens), SUM(cost)
            FROM usage_flows
            WHERE request_id NOT LIKE 'sub2api-daily:%' AND request_id NOT LIKE 'sub2api-model:%'
            GROUP BY site, model
        """).fetchall()
        con.close()
        if not rows:
            return False, "无有效中转流水数据"

        count = 0
        skipped = []          # ★ 2026-10-08：按次收费的站跳过反推
        today_str = datetime.now().strftime("%Y-%m-%d")
        for site, model, n, inp, cr, out, cost in rows:
            if not model or model == "*" or n < 3:
                continue
            entry = price_book.entry_of(site, model)
            if not entry:
                entry = price_book.entry_of("*", model) or {}
                p_in = float(entry.get("in") or 1.0)
                p_cr = float(entry.get("cache") or 0.2)
                p_out = float(entry.get("out") or 2.0)
            else:
                p_in = float(entry.get("in") or 1.0)
                p_cr = float(entry.get("cache") or 0.2)
                p_out = float(entry.get("out") or 2.0)

            # 理论标价
            nom = (inp / 1e6) * p_in + (cr / 1e6) * p_cr + (out / 1e6) * p_out
            if nom > 0:
                eff_ratio = round(cost / nom, 4)
            else:
                eff_ratio = 1.0

            new_entry = {
                "host": site,
                "model": model,
                "in": p_in,
                "cache": p_cr,
                "out": p_out,
                "ratio": eff_ratio,
                "cur": "CNY",
                "note": "中转站实扣自动同步 (基于%d条流水)" % n,
                "updated": today_str
            }
            # ★ 2026-10-08：按次收费的站**不参与**这种「按 token 反推单价」的同步。
            #   理由：它实扣 = 次数 × 单价，跟 token 量没关系，反推出来的单价
            #   会严重失真（把固定费摊进 token），反而把配置搞坏。
            _old = price_book.match(site, model) or {}
            try:
                _opc = float(_old.get("per_call") or 0)
            except (TypeError, ValueError):
                _opc = 0.0
            if _opc > 0:
                skipped.append("%s/%s" % (site, model))
                continue
            price_book.upsert(new_entry, keep_ratio=False)
            count += 1

        price_book._write(price_book._entries)
        msg = "已成功从真实流水同步 %d 个模型实扣价格与倍率！" % count
        if skipped:
            msg += "（%d 个按次收费的站已跳过：%s）" % (len(skipped), "、".join(skipped[:3]))
        return True, msg
    except Exception as e:
        return False, "同步失败：%s" % e


def compute_cost(entry, miss, hit, out, now_hm=None, day=None, calls=None):
    """按价格条目算成本。返回 dict 或 None（未配价）。

    *ratio* = 实收倍率（对齐站点真实账单）：成本与"缓存省"都要乘它。
    *day* = "YYYY-MM-DD"，用于峰谷的周末判定（weekend_off）。

    ★ 2026-10-08 新增按次收费（用户拍板）：
        条目里写了 per_call（每次 X 元）→ 成本 = calls × per_call。
        token 明细（缓存/未命中）仍照常显示，但**不参与计价**；
        「缓存省」直接置 0（按次收费时缓存命中并不省钱）。
        calls 没传时返回 None（无法计算）—— 调用方应尽量传：
          优先用**站点监控库**的流水条数（站方口径最准），
          没有才退回 Hermes 本地的模型调用次数。

    ⚠ 不要把 reasoning（思考）token 加进 out 计费（2026-09-22 试过并回退）：
      证据 = 有站方 token 明细的锚点站（tokenrhythm）显示「站方 out = 我们 out」
      （10,310,178 vs 10,326,347，差 0.16%），说明站方输出列就是**可见输出**，
      不含 thinking。加上 reasoning 会让该站虚增 59%（实测 ¥490.99 → ¥546.47）。
    """
    if not entry:
        return None
    cur = entry.get("cur") or "¥"
    try:
        ratio = float(entry.get("ratio") or 1.0)
    except (TypeError, ValueError):
        ratio = 1.0
    if entry.get("tag") == "免费":
        return {"cost": 0.0, "saved": 0.0, "list_cost": 0.0, "cur": cur,
                "free": True, "period": None, "ratio": ratio,
                "seg_from": entry.get("_seg_from") or "", "per_call": None,
                "calls": None}

    # ── ★ 按次收费：不看 token，成本 = 次数 × 每次费用 ──
    try:
        _pc = float(entry.get("per_call") or 0)
    except (TypeError, ValueError):
        _pc = 0.0
    if _pc > 0:
        if calls is None:
            return None                      # 没次数算不了（调用方需传）
        # ⚠ calls 校验：负数会产出负成本（比不显示更误导）、非数字会抛异常
        #   打断整轮刷新。脏值一律当「算不了」处理。
        try:
            _n = int(calls)
        except (TypeError, ValueError):
            return None
        if _n < 0:
            return None
        return {"cost": _n * _pc, "list_cost": _n * _pc,
                "saved": 0.0,                    # 按次：缓存不省钱
                "cur": cur, "free": False, "period": None, "ratio": ratio,
                "seg_from": entry.get("_seg_from") or "",
                "per_call": _pc, "calls": int(calls)}

    grp = entry
    period = None
    if entry.get("peak") or entry.get("off"):
        hm = now_hm or datetime.now().strftime("%H:%M")
        peak = is_peak(hm, entry.get("peak_hours") or [], day=day,
                       weekend_off=bool(entry.get("weekend_off")))
        grp = (entry.get("peak") if peak else entry.get("off")) or entry
        period = "峰" if peak else "谷"
    pin = float(grp.get("in") or 0)
    pout = float(grp.get("out") or 0)
    pcache = float(grp.get("cache") or 0)
    raw = (miss * pin + hit * pcache + out * pout) / 1e6
    return {
        "cost": raw * ratio,
        "list_cost": raw,
        "saved": hit * (pin - pcache) / 1e6 * ratio,
        "cur": cur,
        "free": False,
        "period": period,
        "ratio": ratio,
        "seg_from": entry.get("_seg_from") or "",
        "per_call": None,
        "calls": None,
    }


def site_call_counts(since_ts=None, until_ts=None):
    """★ 2026-10-08：按「站×模型」取调用次数（用于按次计费）。

    取数原则（用户拍板：**以站点监控为主**）：
      ① 站点监控库 store.db 的 usage_flows 流水条数（站方口径，最准）
      ② 读不到站点监控 → 返回 {}，由调用方退回本地调用次数

    返回 {"host|model": n}；host 已按 canon_host 归一。
    """
    p = os.path.join(_get_proxy_monitor_dir(), "store.db")
    if not os.path.isfile(p):
        return {}
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        sql = ("SELECT site, model, COUNT(*) FROM usage_flows "
               "WHERE day != '' AND %s" % _FLOW_NON_DUP)
        args = []
        if since_ts:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        if until_ts:
            sql += " AND ts <= ?"
            args.append(int(until_ts))
        sql += " GROUP BY site, model"
        for s, m, n in con.execute(sql, args):
            h = canon_host(str(s or "").strip())
            mm = canon_model(str(m or "").strip() or "*")
            k = "%s|%s" % (h, mm)
            out[k] = out.get(k, 0) + (n or 0)
        con.close()
    except Exception:
        return {}
    return out


_SITE_CALLS_CACHE = {}


def calls_for(host, model, since_ts=None, until_ts=None, fallback=None,
              prefer_site=False):
    """★ 2026-10-08：取某「站×模型」的调用次数，供按次计费使用。

    口径说明（★ 2026-10-08 修正）：
      默认 **以本地 *fallback* 为准**（= 当前会话/当前对象的真实调用数）。
      理由：浮窗整个界面都是「当前这个对话」的口径（会话 ID、命中率、成本
      都是本会话），按次计费必须同口径。而站点监控的流水是**站×模型的全量
      累计**，没有会话维度 —— 直接拿来用会让浮窗显示一串跟当前对话无关的
      累计账单（实测：本会话 373 次，站点监控全量 3790 次，差 10 倍）。

      prefer_site=True 时才反过来（站点监控优先、本地兜底）—— 用于确实要
      「该站该模型一共花了多少」的场景，如账本聚合/站点维度汇总。

    站点监控查询带 (since,until) 维度的缓存，避免每行都开一次库。
    """
    # 默认路径：本地次数说了算（没有本地数才去问站点监控）
    if not prefer_site:
        # ⚠ 用 `is not None` 而非真值判断：本地次数**确实是 0**（新会话还没调用）
        #   时必须返回 0，否则会回落到站点监控的全量次数，把别人的量算到自己头上。
        if fallback is not None:
            try:
                return int(fallback)
            except (TypeError, ValueError):
                pass
        h = canon_host(str(host or "").strip())
        mm = canon_model(str(model or "").strip() or "*")
        mp = _site_calls_cached(since_ts, until_ts)
        v = (mp or {}).get("%s|%s" % (h, mm))
        return int(v) if v else None

    h = canon_host(str(host or "").strip())
    mm = canon_model(str(model or "").strip() or "*")
    mp = _site_calls_cached(since_ts, until_ts)
    if mp:
        v = mp.get("%s|%s" % (h, mm))
        if v:
            return int(v)
    # 站点没数 → 用本地兜底（同样是 is not None，0 也是有效值）
    if fallback is not None:
        try:
            return int(fallback)
        except (TypeError, ValueError):
            pass
    return None


def _site_calls_cached(since_ts=None, until_ts=None):
    """带窗口缓存的 site_call_counts（最多留 4 组，避免长跑占内存）。

    ⚠ 缓存 key 必须带 store.db 的 mtime：站点监控是后台持续采集的，库一直在长。
    只用 (since,until) 当 key 的话，进程内会永远返回首次读到的旧值 → 按次金额
    长期不涨（实测：插 5 条流水后新值 3832，缓存仍返 3827）。
    """
    try:
        mt = os.path.getmtime(os.path.join(_get_proxy_monitor_dir(), "store.db"))
    except OSError:
        mt = None
    key = (round(since_ts, 3) if since_ts else None,
           round(until_ts, 3) if until_ts else None,
           round(mt, 3) if mt else None)
    mp = _SITE_CALLS_CACHE.get(key)
    if mp is None:
        mp = site_call_counts(since_ts, until_ts)
        if len(_SITE_CALLS_CACHE) >= 4:
            _SITE_CALLS_CACHE.clear()
        _SITE_CALLS_CACHE[key] = mp
    return mp


# ─────────────────────────── 日志账本（缺账检测） ───────────────────────────

RE_LINE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \w+ (?P<rest>.*)$")
RE_CREATED = re.compile(r"^run_agent: OpenAI client created \(chat_completion")
RE_CALL = re.compile(
    r"^(?:\[(?P<sid>[0-9A-Za-z_\-]+)\] )?agent\.conversation_loop: API call #(?P<n>\d+): "
    r"model=(?P<model>\S+) provider=(?P<prov>\S+) in=(?P<in>\d+) out=(?P<out>\d+) "
    r"total=(?P<tot>\d+) latency=(?P<lat>[\d.]+)s(?P<rest>.*)")
RE_CACHE = re.compile(r"cache=(\d+)/(\d+)")
RE_TURN = re.compile(
    r"^(?:\[(?P<sid>[0-9A-Za-z_\-]+)\] )?agent\.turn_context: conversation turn: session=(?P<sid2>[0-9A-Za-z_\-]+)")
RE_ENDED = re.compile(
    r"^(?:\[(?P<sid>[0-9A-Za-z_\-]+)\] )?agent\.conversation_loop: Turn ended: reason=(?P<reason>\S+)")


class Ledger:
    """增量 tail agent.log（含轮转），聚合 per-session 的请求/记账事件。

    关键：created 行没有 session id，按「当前回合」归属（turn_context 行划定）。
    重复读取用整行去重，保证轮转重扫不会重复计数。
    """

    FILES = ("agent.log.3", "agent.log.2", "agent.log.1", "agent.log")
    FIRST_TAIL_BYTES = 1_000_000

    def __init__(self, log_dir=None):
        self.dir = log_dir or LOGS_DIR
        self.pos = {}
        self.seen_set = set()
        self.seen_order = collections.deque()
        self.events = collections.deque(maxlen=40000)      # (ts, kind, sid, payload)
        self.sessions = {}                                  # sid -> stat
        self.turns = collections.deque(maxlen=3000)         # (ts, sid)
        self.cur_turn_sid = None
        self.earliest_ts = None
        self._first = True
        self.last_poll = 0.0

    def _new_sess(self):
        return {"calls": [], "created": 0, "interrupts": 0, "unassigned": 0}

    def _stat(self, sid):
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = self._new_sess()
        return s

    def _check_rotation(self):
        """agent.log 被轮转时（新文件比记录的读取位置短）→ offset 顺移到 .1/.2/.3。

        不做这步的话，旧 agent.log 里「已读位置 → 文件末尾」那一段会永久丢失。
        """
        try:
            size = os.path.getsize(os.path.join(self.dir, "agent.log"))
        except OSError:
            return
        off = self.pos.get("agent.log")
        if off is None or size >= off:
            return
        self.pos["agent.log.3"] = self.pos.get("agent.log.2", 0)
        self.pos["agent.log.2"] = self.pos.get("agent.log.1", 0)
        self.pos["agent.log.1"] = off
        self.pos["agent.log"] = 0

    def poll(self):
        self._check_rotation()
        for name in self.FILES:
            path = os.path.join(self.dir, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            off = self.pos.get(name, 0)
            partial = False
            if self._first and name != "agent.log":
                off = max(0, size - self.FIRST_TAIL_BYTES)
                partial = off > 0
            if size < off:          # 未预料的截断
                off = 0
                partial = False
            if size <= off:
                self.pos[name] = size
                continue
            try:
                # 二进制读取：tell() 是精确字节偏移，与 getsize 同口径（文本模式下的
                # tell() 是解码器 cookie，遇中文可能偏移，导致漏读/重复）
                with open(path, "rb") as f:
                    if off:
                        f.seek(off)
                    if partial:
                        f.readline()        # 丢弃半行
                    for raw in f:
                        self._feed(raw.decode("utf-8", "replace"))
                    self.pos[name] = f.tell()
            except Exception:
                continue
        self._first = False
        self.last_poll = time.time()

    def _feed(self, line):
        line = line.rstrip("\r\n")
        if not line:
            return
        if line in self.seen_set:
            return
        self.seen_set.add(line)
        self.seen_order.append(line)
        if len(self.seen_order) > 200000:
            old = self.seen_order.popleft()
            self.seen_set.discard(old)

        m = RE_LINE.match(line)
        if not m:
            # 行首不是「时间戳,毫秒 级别」→ 工具输出回显/多行续行，一律跳过
            return
        ts = m.group("ts")
        rest = m.group("rest")
        if not self.earliest_ts or ts < self.earliest_ts:
            self.earliest_ts = ts

        if RE_CREATED.match(rest):
            sid = self.cur_turn_sid
            if sid:
                self._stat(sid)["created"] += 1
            else:
                self.sessions.setdefault("__unassigned__", self._new_sess())["unassigned"] += 1
            self.events.append((ts, "created", sid, None))
            return

        m = RE_TURN.match(rest)
        if m:
            sid = m.group("sid2")
            self.cur_turn_sid = sid
            self._stat(sid)
            self.turns.append((ts, sid))
            self.events.append((ts, "turn", sid, None))
            return

        m = RE_CALL.match(rest)
        if m:
            sid = m.group("sid") or self.cur_turn_sid
            cm = RE_CACHE.search(m.group("rest"))
            rec = {
                "ts": ts, "n": int(m.group("n")),
                "in": int(m.group("in")), "out": int(m.group("out")),
                "hit": int(cm.group(1)) if cm else 0,
                # ⚠️ 2026-10-03：agent.log 只在 cache_read_tokens 非零时才打印 cache= 字段
                #    （源码 turn_usage.py:191）。所以「没有 cache=」= 这次调用**没拿到缓存
                #    明细**，不是「真的没命中缓存」。以前把两者当一回事，导致这些调用被
                #    按最贵的「未命中」计价（未命中单价是缓存的 50 倍）。
                #    实测：10-01 只有 1 次这种调用，10-03 涨到 22 次，把当天账推高 4%。
                "noinfo": cm is None,
            }
            self._stat(sid)["calls"].append(rec)
            self.events.append((ts, "call", sid, rec))
            return

        m = RE_ENDED.match(rest)
        if m:
            sid = m.group("sid") or self.cur_turn_sid
            reason = m.group("reason")
            if "interrupt" in reason:
                self._stat(sid)["interrupts"] += 1
                self.events.append((ts, "interrupt", sid, reason))
            else:
                self.events.append((ts, "ended", sid, reason))
            if self.cur_turn_sid == sid:
                self.cur_turn_sid = None
            return

    # -------- 查询 --------

    def stat(self, sid):
        return self.sessions.get(sid)

    def unmatched(self, sid, limit=60, grace_sec=90):
        """未配对的 created 时间点（= 缺账请求）。顺序配对：created 入队，call 出队。

        *grace_sec* 内刚发出的 created 视为「进行中的请求」，不算缺账。
        """
        pend = []
        for ts, kind, esid, _ in self.events:
            if esid != sid:
                continue
            if kind == "created":
                pend.append(ts)
            elif kind == "call" and pend:
                pend.pop(0)
        if pend and grace_sec:
            t = _ts_to_epoch(pend[-1])
            if t and (time.time() - t) < grace_sec:
                pend.pop()
        return pend[-limit:]

    def concurrent_at(self, sid, ts, window=120):
        """该时刻附近是否有其它会话的回合（归属可能不准 → 降级）。"""
        if not ts:
            return False
        try:
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return False
        for ots, osid in self.turns:
            if osid == sid or not ots:
                continue
            try:
                ot = datetime.strptime(ots, "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                continue
            if abs(ot - t) <= window:
                return True
        return False

    def report(self, sid):
        """返回该会话的账目对账结果。"""
        s = self.sessions.get(sid)
        if not s:
            return None
        A = len(s["calls"])
        R = max(s["created"], A)      # created 行覆盖全部请求（成功记账的也先发 created）
        pend_all = self.unmatched(sid, grace_sec=0)
        inflight_ts = None
        if pend_all:
            t_last = _ts_to_epoch(pend_all[-1])
            if t_last and (time.time() - t_last) < 90:
                inflight_ts = pend_all[-1]        # 刚发出、还没记账 → 进行中
        gaps = list(pend_all[:-1]) if inflight_ts else list(pend_all)
        G = max(0, R - A - (1 if inflight_ts else 0))
        if G <= 0:
            gaps = []                 # 语义一致：没缺账就不列明细
        elif len(gaps) > G:
            gaps = gaps[-G:]
        return {
            "recorded": A, "real": R, "gap": G, "gaps": gaps,
            "inflight": inflight_ts,
            "interrupts": s["interrupts"],
            "partial": self._partial(s),
            "concurrent": any(self.concurrent_at(sid, t) for t in gaps),
        }

    def _partial(self, s):
        """该会话最早的记账记录是否正好压在日志起点 → 日志可能未覆盖全程。"""
        calls = s.get("calls") or []
        if not calls or not self.earliest_ts:
            return False
        return abs(_ts_delta(calls[0]["ts"], self.earliest_ts)) <= 60


# ─────────────────────────── 监控核心 ───────────────────────────

class Monitor:
    def __init__(self):
        self.hist_mtime = 0
        self.cur_sid = None
        self.last_sid = None
        self.last_info = None
        self.last_info_at = 0.0
        self._uia_btn = None
        self._uia_last_try = 0.0
        self._hist_last_try = 0.0
        self._chg_times = []          # 最近几次跟随目标的变更时刻（用于识别 UIA 抖动）
        self._lock_until = 0.0
        self._followed_sid = None
        self._followed_at = 0.0
        self.last_refresh = None      # 上次手动刷新的结果 {sid, via, at}
        self._sid_lock = threading.Lock()
        self.prices = PriceBook()
        self.ledger = Ledger()
        self.cost_ledger = CostLedger(path=LEDGER_PATH, prices=self.prices)
        self.calib_mgr = CalibManager()
        # 内置中转站采集轮询守护（300秒一次，免外部独立进程常驻）
        self._start_proxy_daemon()
        # UIA 跨进程读取要 30~70ms（实测），放主线程里每秒卡一下 → 挪到后台线程
        self._worker = threading.Thread(target=self._follow_loop, daemon=True)
        self._worker.start()
        self._chars_cache = {}
        self._sample_last_sid = None
        self._sample_last_try = 0.0

    # ---------- signal: webview2 history ----------
    def _read_latest_task_sid(self):
        now_t = time.time()
        if now_t - self._hist_last_try < 3.0:    # 限流：最多 3 秒探一次（该信号本身延迟大）
            return self.cur_sid
        self._hist_last_try = now_t
        try:
            mtime = os.path.getmtime(HISTORY)
        except Exception:
            return self.cur_sid
        if mtime == self.hist_mtime and self.cur_sid is not None:
            return self.cur_sid
        # History 库是 WebView2 常驻写的库，实测 journal_mode=delete（非 WAL）。
        # 用 mode=ro 直连会 100% 撞 database is locked 且白等 ~950ms；immutable=1 实测 3ms。
        # ⚠ immutable 只对非 WAL 库安全 —— 主库/UI_DB 是 WAL，禁用该标志（会漏 -wal 里的最新数据）。
        # 临时文件用 mkstemp 唯一名：主线程（手动刷新/current）与后台跟随线程都会走到这里，
        # 原来写死的固定路径没有互斥，两方会互相踩（2026-09-22 修）。
        fd, tmp = tempfile.mkstemp(prefix="hermes_hist_", suffix=".db")
        os.close(fd)
        try:
            for mode in ("immutable", "copy"):
                try:
                    if mode == "immutable":
                        uri = "file:%s?immutable=1" % quote(
                            HISTORY.replace("\\", "/"), safe="/:")
                        con = sqlite3.connect(uri, uri=True, timeout=0.4)
                    else:
                        shutil.copy2(HISTORY, tmp)
                        con = sqlite3.connect(tmp)
                    cur = con.cursor()
                    cur.execute(
                        "SELECT url FROM urls WHERE url LIKE '%#/tasks/%' "
                        "ORDER BY last_visit_time DESC LIMIT 1")
                    row = cur.fetchone()
                    con.close()
                    sid = self._extract_sid(row[0]) if row else None
                    self.cur_sid = sid
                    self.hist_mtime = mtime
                    return sid
                except Exception:
                    continue
            return self.cur_sid
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

    def _extract_sid(self, url):
        try:
            frag = url.split("#/tasks/", 1)[1]
            sid = frag.split("?")[0].split("/")[0].strip()
            if not sid:
                return None
            if len(sid) <= 12 and "_" not in sid and not sid.startswith("cron"):
                resolved = self._resolve_short(sid)
                return resolved or sid
            return sid
        except Exception:
            return None

    def _resolve_short(self, short):
        try:
            con = sqlite3.connect(UI_DB)
            cur = con.cursor()
            cur.execute("SELECT value_json FROM ui_kv WHERE key='hermes:gateway-session-map'")
            row = cur.fetchone()
            con.close()
            if row:
                data = json.loads(row[0])
                v = data.get(short)
                if v and v.get("persistentId"):
                    return v["persistentId"]
        except Exception:
            pass
        return None

    # ---------- signal: UI automation (realtime) ----------
    def _read_uia_sid(self):
        if _uia is None or _NO_UIA:
            return None
        if self._uia_btn is not None:
            nm = None
            for _try in range(2):            # 元素偶发读失败 → 重试一次再放弃（避免频繁重扫）
                try:
                    nm = self._uia_btn.Name
                    break
                except Exception:
                    time.sleep(0.03)
            if nm and "复制会话 ID" in nm:
                m = re.match(r"复制会话 ID\s+(\S+)", nm.strip())
                if m:
                    sid = m.group(1)
                    if len(sid) <= 12 and "_" not in sid:
                        sid = self._resolve_short(sid) or sid
                    return sid
            # 元素失效 → 置空重扫。这里**不受 _uia_last_try 限流**：元素已明确失效时
            # 再等 0.5 秒只会白等（手动刷新点下去就卡住，2026-09-22 实测 213ms → 0.9ms 的关键）
            self._uia_btn = None
            self._uia_last_try = 0.0
        now = time.time()
        if now - self._uia_last_try < 0.5:   # 换对话后最多 0.5 秒重新扫描 UI（原 2 秒太钝）
            return None
        self._uia_last_try = now
        try:
            win = _uia.WindowControl(searchDepth=1, RegexName="Hermes Agent")
            if not win.Exists(1, 0.2):
                return None
            btn = _uia.ButtonControl(searchFromControl=win, RegexName="复制会话 ID", searchDepth=30)
            if btn.Exists(2, 0.3):
                nm = btn.Name
                m = re.match(r"复制会话 ID\s+(\S+)", (nm or "").strip())
                if m:
                    self._uia_btn = btn
                    sid = m.group(1)
                    if len(sid) <= 12 and "_" not in sid:
                        sid = self._resolve_short(sid) or sid
                    return sid
        except Exception:
            pass
        return None

    # ---------- fallback: latest api activity ----------
    def _smu_latest(self):
        row = db_query(
            "SELECT session_id FROM session_model_usage "
            "WHERE session_id NOT LIKE 'cron%' ORDER BY last_seen DESC LIMIT 1", one=True)
        return row[0] if row else None

    # ---------- session info ----------
    def session_sites_models(self, sid):
        """该会话真正用过的全部 (站, 模型) —— 来自 session_model_usage，可多行。

        ⚠️ 为什么需要它（2026-10-03）：`sessions` 表每个会话只有**一行**主记录，
        只记得最后一次的 billing_base_url / model。实际一个会话可以先后用多个站、
        多个模型（实测 20261003_021807_4dc91e：dshapi 349 次 + 示例站 118 次），
        只读主记录会把其它站整个漏掉。这里按站×模型聚合，供悬浮窗全部列出来。
        """
        out = []
        try:
            for burl, prov, model, n, i, cr, o in db_query(
                    "SELECT billing_base_url, billing_provider, model, "
                    "COALESCE(SUM(api_call_count),0), COALESCE(SUM(input_tokens),0), "
                    "COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(output_tokens),0) "
                    "FROM session_model_usage WHERE session_id=? "
                    "GROUP BY billing_base_url, billing_provider, model "
                    "ORDER BY 4 DESC", (sid,)):
                try:
                    site = canon_host(urlparse(burl).hostname or prov or "?")
                except Exception:
                    site = canon_host(prov or "?")
                out.append({"site": site,
                            "model": canon_model(model or "?"),
                            "calls": int(n or 0),
                            "in": int(i or 0), "hit": int(cr or 0), "out": int(o or 0)})
        except Exception:
            pass
        return out

    def _session_info(self, sid):
        row = db_query(
            "SELECT title, model, billing_provider, billing_base_url, "
            "COALESCE(input_tokens,0), COALESCE(cache_read_tokens,0), "
            "COALESCE(output_tokens,0), COALESCE(api_call_count,0), "
            "COALESCE(source,'desktop') FROM sessions WHERE id=?", (sid,), one=True)
        if not row:
            return {"sid": sid, "title": None, "rate": None, "inp": 0, "cr": 0, "out": 0,
                    "calls": 0, "site": "?", "model": "?", "source": "?", "parent": None}
        title, model, prov, burl, inp, cr, out, calls, src = row
        rate = (cr * 100.0 / (inp + cr)) if (inp + cr) > 0 else None
        try:
            site = canon_host(urlparse(burl).hostname or prov or "?")
        except Exception:
            site = canon_host(prov or "?")
        return {"sid": sid, "title": title, "rate": rate, "inp": inp, "cr": cr, "out": out,
                "calls": calls, "site": site, "model": model or "?", "source": src}

    # ---------- 子代理 ----------
    def _children(self, sid):
        """取子会话（delegate_task 产生的）。

        ⚠ 2026-09-22 修：原来条件是 `source='subagent'`，但 Hermes 内核改了行为 ——
        实测 `subagent` 这个 source 最后出现在 2026-09-18，之后的子会话（9/21、9/22）
        全标成 `source='desktop'` + 带 `parent_session_id`，于是旧条件一条都匹配不到，
        **子代理的账全漏**（实测某对话 3 个子代理占 85% 的 token 量）。
        改用「有 parent_session_id 即为子会话」→ 新老两种形态都能覆盖
        （全库：有 parent 的 = 52 条，其中标 subagent 的 47 条）。
        """
        return db_query(
            "SELECT id, model, billing_base_url, billing_provider, "
            "COALESCE(input_tokens,0), COALESCE(cache_read_tokens,0), "
            "COALESCE(output_tokens,0) "
            "FROM sessions WHERE parent_session_id=?", (sid,))

    def _children_cost(self, sid, now_hm, day=None):
        """子代理成本：按「当天生效的价格段」匹配（与父对话 CostLedger 同口径）。"""
        total = 0.0
        n = 0
        unpriced = 0
        cur = "¥"
        if day is None:
            day = datetime.now().strftime("%Y-%m-%d")
        for cid, cmodel, cburl, cprov, cin, ccr, cout in self._children(sid):
            n += 1
            try:
                chost = canon_host(urlparse(cburl).hostname or cprov or "?")
            except Exception:
                chost = canon_host(cprov or "?")
            entry = self.prices.match(chost, cmodel, day)
            # ★ 2026-10-08：按次收费需要次数 —— 优先站点监控，退回本地。
            #   子会话的本地次数从 sessions 表取（api_call_count）。
            _cc = None
            try:
                _r = db_query("SELECT COALESCE(api_call_count,0) FROM sessions "
                              "WHERE id=?", (cid,), one=True)
                _cc = _r[0] if _r else None
            except Exception:
                _cc = None
            r = compute_cost(entry, cin, ccr, cout, now_hm, day=day,
                             calls=calls_for(chost, cmodel, fallback=_cc))
            if r is None:
                unpriced += 1
            else:
                total += r["cost"]
                cur = r["cur"]
        return {"count": n, "cost": total, "unpriced": unpriced, "cur": cur}

    # ---------- 输出/输入估算 ----------
    def _msg_chars(self, sid):
        """统计该会话 assistant 消息的 CJK / 非 CJK 字符数（**增量累加**）。

        原实现每次全量扫 + 逐字符遍历（实测 20~219ms，会话越长越慢），
        而缺账>0 时每 5 秒就跑一轮 → 常驻卡顿。
        改成：按 rowid 增量只统计新增行，旧结果复用缓存。
        缓存键含 (条数, 字符总数, max_rowid)，三者任一变化才重算增量部分。
        """
        agg = db_query(
            "SELECT COUNT(*), COALESCE(SUM(length(COALESCE(reasoning,''))+"
            "length(COALESCE(content,''))+length(COALESCE(tool_calls,''))),0), "
            "COALESCE(MAX(rowid),0) FROM messages WHERE session_id=? AND role='assistant'",
            (sid,), one=True)
        n, total, maxrid = (agg or (0, 0, 0))
        c = self._chars_cache.get(sid)
        if c and c.get("n") == n and c.get("total") == total:
            return c["val"]                       # 完全没变 → 直接返回

        if c and n > c.get("n", 0) and maxrid > c.get("maxrid", 0):
            base = c["val"]                       # 增量：只读新增行
            since = c["maxrid"]
        else:
            base, since = (0, 0), 0               # 冷启 / 会话被改写 → 全量重算

        add_cjk = add_other = 0
        for r in db_query(
                "SELECT reasoning, content, tool_calls FROM messages "
                "WHERE session_id=? AND role='assistant' AND rowid>?",
                (sid, since)):
            for t in r:
                if not t:
                    continue
                n_cjk = 0
                for ch in t:
                    if "\u4e00" <= ch <= "\u9fff":
                        n_cjk += 1
                add_cjk += n_cjk
                add_other += len(t) - n_cjk
        val = (base[0] + add_cjk, base[1] + add_other)
        self._chars_cache[sid] = {"n": n, "total": total, "maxrid": maxrid, "val": val}
        if len(self._chars_cache) > 64:           # 切了很多对话时别无限增长
            try:
                self._chars_cache.pop(next(iter(self._chars_cache)))
            except Exception:
                pass
        return val

    def _length_marks(self, sid):
        """该会话 finish_reason=length 的消息时间戳（用于缺账分类）。"""
        rows = db_query(
            "SELECT timestamp FROM messages WHERE session_id=? AND finish_reason='length'",
            (sid,))
        return [r[0] for r in rows if r and r[0]]

    def _estimate(self, sid, info, rep):
        """缺账估算：输入用记账锚点（逐次定位），输出用落库字符换算（固定系数）。"""
        if not rep or rep["gap"] <= 0:
            return None
        st = self.ledger.stat(sid)
        calls = (st or {}).get("calls") or []
        if not calls:
            return None

        # 命中率取累计加权（单次可能恰好是缓存失效那一次，波动大）
        tot_in = sum(c["in"] for c in calls) or 1
        hit_ratio = sum(c["hit"] for c in calls) / tot_in

        # 每次缺账按「此前最近一次记账调用」的 prompt 规模锚定（上下文单调递增）
        est_in = 0
        for ts in rep["gaps"]:
            anchor = calls[0]["in"]
            for c in calls:
                if c["ts"] <= ts:
                    anchor = c["in"]
                else:
                    break
            est_in += anchor
        if est_in <= 0:      # gaps 列表已被事件窗口挤掉 → 退化用最后一次锚点 × 次数
            est_in = int(calls[-1]["in"] * rep["gap"])
        est_hit = int(est_in * hit_ratio)
        est_miss = est_in - est_hit

        cjk, other = self._msg_chars(sid)
        est_total_out = int(cjk * CHAR_PER_TOK_CJK + other * CHAR_PER_TOK_OTHER)
        on_record = sum(c["out"] for c in calls)
        est_gap_out = max(0, est_total_out - on_record)

        return {
            "in": est_in, "miss": est_miss, "hit": est_hit, "out": est_gap_out,
            "anchor_in": calls[-1]["in"], "hit_ratio": hit_ratio,
            "anchor_ts": calls[-1]["ts"],
            "note": "输入=逐次锚定（真实上界）· 输出=落库字符换算（固定系数，±20%）",
        }

    def _gap_types(self, sid, rep):
        """给缺账时间点打类型标签。

        中断：从后往前配对 —— 中断发生时被取消的就是「最后一个」发起的请求，
        一个中断事件只认一次；其余缺账再看是否落在输出截断（length）轮次附近。
        """
        gaps = rep.get("gaps") or []
        ints = [(ts, esid) for ts, kind, esid, _ in self.ledger.events
                if kind == "interrupt" and esid == sid and ts]
        assigned = {}
        used = set()
        for i in range(len(gaps) - 1, -1, -1):
            t0 = _ts_to_epoch(gaps[i])
            for j, (ets, _s) in enumerate(ints):
                if j in used:
                    continue
                d = _ts_to_epoch(ets) - t0
                if -30 <= d <= 300:
                    assigned[i] = "中断"
                    used.add(j)
                    break
        marks = None
        out = []
        for i, ts in enumerate(gaps):
            kind = assigned.get(i)
            if kind is None:
                if marks is None:
                    marks = self._length_marks(sid)
                t0 = _ts_to_epoch(ts)
                kind = "截断" if any(-60 <= (m - t0) <= 900 for m in marks) else "未归类"
            out.append((ts, kind))
        return out

    # ---------- 样本采集（用于离线拟合字符→token 系数） ----------
    def collect_sample(self):
        """把「干净会话」（无缺账、无截断、字符量足）记进 calib_samples.json。

        条件：gap==0、非 partial、无 finish_reason=length 消息、字符量 ≥2 万、desktop 源。
        采集不自动改代码常量 —— 只攒样本，拟合由人跑脚本后确认。
        """
        sid = self.last_sid
        if not sid or sid == self._sample_last_sid:
            return
        now = time.time()
        if now - self._sample_last_try < 120:
            return
        self._sample_last_try = now
        try:
            rep = self.ledger.report(sid) or {}
            if rep.get("gap") or rep.get("partial"):
                return
            st = self.ledger.stat(sid)
            if not st or not st["calls"]:
                return
            row = db_query("SELECT COUNT(*) FROM messages WHERE session_id=? "
                           "AND finish_reason='length'", (sid,), one=True)
            if not row or row[0]:
                return
            cjk, other = self._msg_chars(sid)
            if cjk + other < 20000:
                return
            src = db_query("SELECT source, model FROM sessions WHERE id=?", (sid,), one=True)
            if not src or src[0] != "desktop":
                return
            samples = []
            try:
                with open(SAMPLES_PATH, "r", encoding="utf-8-sig") as f:
                    samples = json.load(f)
                if not isinstance(samples, list):
                    samples = []
            except Exception:
                samples = []
            if any(s.get("sid") == sid for s in samples):
                self._sample_last_sid = sid
                return
            samples.append({
                "sid": sid, "model": src[1], "cjk": cjk, "non_cjk": other,
                "out_recorded": sum(c["out"] for c in st["calls"]),
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            tmp = SAMPLES_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(samples, f, ensure_ascii=False, indent=1)
            os.replace(tmp, SAMPLES_PATH)
            self._sample_last_sid = sid
        except Exception:
            pass

    # ---------- 组装 ----------
    def build(self, sid, source="指定"):
        """组装某会话的完整展示数据（成本 + 子代理 + 缺账）。"""
        d = self._session_info(sid)
        d["source"] = source
        # ★「缓存明细缺失」修正（2026-10-03，见 _cache_detail_fix 说明）：
        #   把"没拿到缓存明细"的调用按站方逐条值补回缓存，从未命中里扣掉。
        #   只在站方有对应流水时才动数字；没有就如实计数（不改数、不硬估）。
        d["cache_fix"] = {"fix": 0, "matched": 0, "missing": 0}
        try:
            _st = (self.ledger.stat(sid) or {}).get("calls") or []
            _fx = _cache_detail_fix(sid, _st)
            if _fx["fix"] > 0:
                _take = min(int(_fx["fix"]), int(d["inp"]))
                d["inp"] -= _take
                d["cr"] += _take                  # 总量不变，只把归属从"未命中"挪回"缓存"
                if (d["inp"] + d["cr"]) > 0:
                    d["rate"] = d["cr"] * 100.0 / (d["inp"] + d["cr"])
            d["cache_fix"] = _fx
        except Exception:
            pass
        now_hm = datetime.now().strftime("%H:%M")

        entry = self.prices.match(d["site"], d["model"])
        # ★ 2026-10-08：按次收费需要次数（优先站点监控，退回本会话 api_call_count）
        d["priced"] = compute_cost(
            entry, d["inp"], d["cr"], d["out"], now_hm,
            calls=calls_for(d.get("site"), d.get("model"), fallback=d.get("calls")))
        d["price_entry"] = entry

        # ★ 2026-10-09 新增：会话用过多个「站×模型」时，逐条计价再汇总。
        #   ⚠️ 必须放在上面那行**之后** —— 上面是「单组合」的老口径（价格窗
        #   回填、entry_of 等仍要用它），这里是「多组合」的权威口径，覆盖显示值。
        #   触发条件：session_model_usage 里不止一个「站|模型」分组。
        #   单组合时不启用，保证绝大多数会话的行为一字不变（零回归风险）。
        try:
            d["multi"] = _multi_priced(sid, d, now_hm, prices=self.prices)
        except Exception as _e:
            d["multi"] = None
            _startup_log("多站多模型汇总失败（已退回单组合口径）：%s" % _e)
        _mu = d.get("multi")
        if _mu and len(_mu.get("pairs") or []) > 1:
            # 用真实用量覆盖「站/模型」显示值（原来错用 sessions 主记录那一行）
            if _mu["sites"]:
                d["sites_real"] = _mu["sites"]
            if _mu["models"]:
                d["models_real"] = _mu["models"]
            # 站/模型显示改为「调用最多的那个」= pairs 已按次数降序
            _top = _mu["pairs"][0]
            d["site"] = _top["site"]
            d["model"] = _top["model"]
            # 计价：只要有任意一条匹配到价，就用逐条汇总的结果替换
            if _mu["matched"]:
                _agg = dict(_mu)
                _agg["multi_mode"] = True
                d["priced"] = _agg
                # 价格窗回填用：主组合的价格条目
                d["price_entry"] = _top.get("entry")
        elif _mu and len(_mu.get("pairs") or []) == 1:
            # 单组合但主记录与实际不一致时，也顺手纠正（如 localhost 那类）
            _only = _mu["pairs"][0]
            if _only["site"] != d.get("site") or _only["model"] != d.get("model"):
                d["site"] = _only["site"]
                d["model"] = _only["model"]
                if _only.get("entry"):
                    d["price_entry"] = _only["entry"]
                _agg = dict(_mu)
                _agg["multi_mode"] = True
                d["priced"] = _agg if _only.get("cost") is not None else d["priced"]

        d["children"] = self._children_cost(sid, now_hm)
        calib = self.calib_mgr.get_session_calib(sid) if hasattr(self, "calib_mgr") else None
        d["calib"] = calib
        hermes_cost = (d.get("priced") or {}).get("cost") or 0.0
        calib_offset = None
        if hasattr(self, "calib_mgr"):
            calib_offset = self.calib_mgr.get_calibration_offset(sid, hermes_cost, d["cr"], d["inp"])
        d["calib_offset"] = calib_offset

        rep = self.ledger.report(sid)
        d["report"] = rep
        d["estimate"] = self._estimate(sid, d, rep) if rep else None
        est = d["estimate"]
        if est:
            if entry:
                ec = compute_cost(entry, est["miss"], est["hit"], est["out"], now_hm,
                                  calls=calls_for(d.get("site"), d.get("model"),
                                                  fallback=d.get("calls")))
                est["cost"] = ec["cost"] if ec else None
            else:
                est["cost"] = None
        return d

    # ---------- 后台中转站采集守护 ----------
    def _start_proxy_daemon(self):
        """内置中转站采集轮询守护（消灭外部独立 poll_loop 进程）。"""
        def _loop():
            time.sleep(10)  # 启动延时，避免抢占启动资源
            while True:
                try:
                    import sys
                    pm_dir = _get_proxy_monitor_dir()
                    if pm_dir not in sys.path:
                        sys.path.insert(0, pm_dir)
                    import poll_loop
                    poll_loop.one_round()
                except Exception:
                    pass
                time.sleep(300)
        t = threading.Thread(target=_loop, daemon=True, name="ProxyPollDaemon")
        t.start()

    @staticmethod
    def _is_hermes_focused():
        """检查前台焦点是否在 Hermes（包含主程序与 WebView2 渲染进程）。"""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return True
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            import psutil
            p = psutil.Process(pid.value)
            name = (p.name() or "").lower()
            return ("hermes" in name or "webview" in name or "msedge" in name or "electron" in name or "python" in name)
        except Exception:
            return True

    # ---------- 后台跟随线程 ----------
    def _follow_loop(self):
        """在后台线程里做「三信号跟随」（UIA 跨进程读要 30~70ms，不能占主线程）。

        抖动抑制：UIA 偶尔会读到别的会话（鼠标悬停侧边栏时那里也有同名按钮）。
        判据 = 短时间内频繁变更（正常切对话不会 6 秒内变 3 次），
        命中则临时锁 4 秒，把抖动吃掉；正常切换仍然即时生效（零额外延迟）。
        """
        try:                                  # 后台线程需要自己的 COM 环境
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            try:
                sid = self._read_uia_sid()
                if not sid:
                    sid = self._read_latest_task_sid()
                if not sid:
                    sid = self._smu_latest()
                now = time.time()
                with self._sid_lock:
                    cur = self._followed_sid
                    if sid and sid != cur:
                        self._chg_times = [t for t in self._chg_times if now - t < 6.0]
                        self._chg_times.append(now)
                        if len(self._chg_times) >= 3 and now >= self._lock_until:
                            self._lock_until = now + 4.0     # 判定为抖动 → 锁定
                        if now < self._lock_until and cur and len(self._chg_times) >= 3:
                            sid = cur                        # 锁定期内忽略新值
                        else:
                            self._followed_sid = sid
                            self._followed_at = now
                    else:
                        self._chg_times = [t for t in self._chg_times if now - t < 6.0]
            except Exception:
                pass
            # 智能降频：若用户当前在全屏游戏/看电影/切在其他软件，休眠延长至 3 秒
            sleep_sec = 0.7 if self._is_hermes_focused() else 3.0
            time.sleep(sleep_sec)

    # ---------- 手动刷新（强制重新对齐跟随目标） ----------
    def refresh_follow(self):
        """清掉所有锁/限流/缓存，立刻同步重跑一遍三级信号，返回对齐到的 sid。

        「卡住 / 检测不到当前对话」的成因（都在这里被清掉）：
          ① 抖动锁：切换太快触发 _lock_until → 4 秒内忽略新会话
          ② UIA 重扫限流：_uia_last_try 未到期时不肯重扫（0.5 秒窗）
          ③ history 限流：_read_latest_task_sid 3 秒最多探一次，且缓存 cur_sid
          ④ last_info 5 秒缓存：current() 会直接返回旧数据
          ⑤ SMU 兜底会指向「最近有 API 调用的会话」，未必是当前打开的那个

        ⚠ 2026-09-22 性能修正（实测数据）：
          * **不再清 _uia_btn** —— 保留元素缓存时读 Name 只要 **0.1ms**，
            而清掉后强制全树搜索要 **273~346ms**。这正是「点刷新卡一下」的主因。
            元素真失效时 _read_uia_sid 自己会检测（读失败/Name 不含关键词）并置 None 重扫，
            所以保留缓存是安全的。
          * history 直连改 immutable 后只需 **4~21ms**（原 950ms），
            所以保留「同步重探三级」的语义，不必改成异步 —— 刷新时能拿到确定的最新值。

        ⚠ 后台跟随线程（每 0.7 秒一趟）会在本方法返回后重新填充这些限流字段，
        那是正常行为；本方法保证的是「此刻同步重探一次并把目标对准」。
        """
        with self._sid_lock:
            self._chg_times = []
            self._lock_until = 0.0
        # 手动刷新时清空 _uia_btn，强制重新全树探测当前活动会话
        self._uia_btn = None
        self._uia_last_try = 0.0
        self._hist_last_try = 0.0
        self.hist_mtime = 0
        self.cur_sid = None
        self.last_sid = None
        self.last_info = None
        self.last_info_at = 0.0

        hit = None
        for fn, name in ((self._read_uia_sid, "UIA"),
                         (self._read_latest_task_sid, "history"),
                         (self._smu_latest, "SMU")):
            try:
                sid = fn()
            except Exception:
                sid = None
            if sid:
                with self._sid_lock:
                    self._followed_sid = sid
                    self._followed_at = time.time()
                hit = {"sid": sid, "via": name, "at": time.time()}
                break
        # 无论成功与否都返回同一结构（避免调用方写 `(hit or {})` 兜底）
        self.last_refresh = hit or {"sid": None, "via": None, "at": time.time()}
        if hit:
            _startup_log("刷新对齐：命中 %s → %s" % (hit["via"], hit["sid"]))
        else:
            _startup_log("刷新对齐：三级信号（UIA/history/SMU）都没读到")
        return self.last_refresh

    def current(self, include_gap=False):
        self.ledger.poll()
        with self._sid_lock:
            sid = self._followed_sid
        source = "实时跟随"
        if not sid:
            sid = self._read_latest_task_sid()
            source = "切换跟随"
        if not sid:
            sid = self._smu_latest()
            source = "活动跟随"
        if not sid:
            return None
        now = time.time()
        if (sid == self.last_sid and self.last_info is not None
                and (now - self.last_info_at) < SITE_INFO_TTL):
            self.last_info["source"] = source
            return self.last_info

        d = self.build(sid, source)
        d["include_gap"] = include_gap
        self.last_sid = sid
        self.last_info = d
        self.last_info_at = now
        self.collect_sample()
        return d


def _ts_to_epoch(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def _ts_delta(a, b):
    """两个时间戳字符串的秒差（a-b）。"""
    return _ts_to_epoch(a) - _ts_to_epoch(b)


# ─────────────────────────── 价格填写弹窗 ───────────────────────────

class PriceDialog:
    def __init__(self, root, book, host, model, on_saved=None, entry=None):
        self.book = book
        self.host = host or ""
        self.model = model or ""
        self.on_saved = on_saved
        self.entry = entry or {}
        e = self.entry

        self.win = tk.Toplevel(root)
        self.win.title("价格配置")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        apply_icon(self.win)
        apply_dark_title_bar(self.win, bg_hex=BG)
        style_child_window(self.win, tree=False)   # ★ 2026-10-09 统一子窗口外观
        self.win.resizable(False, False)
        self.win.transient(root)

        pad = {"padx": 10, "pady": 3}
        tk.Label(self.win, text="站: %s" % self.host, bg=BG, fg=FG, font=FONT,
                 anchor="w").grid(row=0, column=0, columnspan=6, sticky="w", **pad)
        tk.Label(self.win, text="模型: %s" % self.model, bg=BG, fg=DIM, font=FONT_S,
                 anchor="w").grid(row=1, column=0, columnspan=6, sticky="w", **pad)

        def frm(r):
            f = tk.Frame(self.win, bg=BG)
            f.grid(row=r, column=0, columnspan=6, sticky="we", padx=10, pady=2)
            return f

        # 三价
        f = frm(2)
        tk.Label(f, text="输入价", bg=BG, fg=FG, font=FONT).pack(side="left")
        self.e_in = self._entry(f, e.get("in", ""))
        tk.Label(f, text="输出价", bg=BG, fg=FG, font=FONT).pack(side="left", padx=(8, 0))
        self.e_out = self._entry(f, e.get("out", ""))
        tk.Label(f, text="缓存价", bg=BG, fg=FG, font=FONT).pack(side="left", padx=(8, 0))
        self.e_cache = self._entry(f, e.get("cache", ""))
        tk.Label(f, text="元/百万", bg=BG, fg=DIM, font=FONT_S).pack(side="left", padx=(6, 0))

        f = frm(3)
        self.v_model_wild = tk.BooleanVar(value=(self.model in ("", "*")))
        tk.Checkbutton(f, text="本站默认（所有模型，模型填 *）", variable=self.v_model_wild,
                       bg=BG, fg=FG, selectcolor=BG, activebackground=BG,
                       activeforeground=FG, font=FONT_S,
                       command=self._toggle_wild).pack(side="left")
        self.v_free = tk.BooleanVar(value=(e.get("tag") == "免费"))
        tk.Checkbutton(f, text="免费站（全按 0 算，显示「免费」）", variable=self.v_free,
                       bg=BG, fg=FG, selectcolor=BG, activebackground=BG,
                       activeforeground=FG, font=FONT_S).pack(side="left", padx=(10, 0))
        tk.Label(f, text="  倍率", bg=BG, fg=FG, font=FONT).pack(side="left")
        self.e_ratio = self._entry(f, e.get("ratio", ""), w=6)
        tk.Label(f, text="(标价×倍率=实收，留空=沿用原倍率)", bg=BG, fg=DIM, font=FONT_S).pack(
            side="left", padx=(3, 0))
        for _w in (self.e_ratio, self.e_in, self.e_out, self.e_cache):
            _w.bind("<KeyRelease>", lambda _e: self._preview())

        # ★ 2026-10-08 按次收费：填了就改走「成本 = 调用次数 × 每次费用」，
        #   不看 token（缓存/未命中仍显示，但「缓存省」不再显示）。
        f = frm(3)
        self.v_percall = tk.BooleanVar(value=bool(e.get("per_call")))
        tk.Checkbutton(f, text="按次收费（不看 token）", variable=self.v_percall,
                       bg=BG, fg=FG, selectcolor=BG, activebackground=BG,
                       activeforeground=FG, font=FONT_S,
                       command=self._toggle_percall).pack(side="left")
        tk.Label(f, text="每次", bg=BG, fg=FG, font=FONT).pack(side="left", padx=(8, 0))
        self.e_percall = self._entry(f, e.get("per_call", ""), w=8)
        tk.Label(f, text="元/次", bg=BG, fg=DIM, font=FONT_S).pack(side="left", padx=(4, 0))
        tk.Label(f, text="（浮窗按本会话次数算；站点页按该站累计）", bg=BG, fg=DIM,
                 font=FONT_S).pack(side="left", padx=(6, 0))
        self.e_percall.bind("<KeyRelease>", lambda _e: self._preview())

        # 峰谷
        f = frm(4)
        self.v_peak = tk.BooleanVar(value=bool(e.get("peak") or e.get("off")))
        tk.Checkbutton(f, text="⚡ 峰谷价", variable=self.v_peak, bg=BG, fg=FG,
                       selectcolor=BG, activebackground=BG, activeforeground=FG,
                       font=FONT_S, command=self._toggle_peak).pack(side="left")

        f = frm(5)
        tk.Label(f, text="峰价时段1", bg=BG, fg=FG, font=FONT_S).pack(side="left")
        self.e_p1s = self._entry(f, "", w=7)
        tk.Label(f, text="—", bg=BG, fg=DIM, font=FONT_S).pack(side="left", padx=2)
        self.e_p1e = self._entry(f, "", w=7)
        tk.Label(f, text="  时段2", bg=BG, fg=FG, font=FONT_S).pack(side="left")
        self.e_p2s = self._entry(f, "", w=7)
        tk.Label(f, text="—", bg=BG, fg=DIM, font=FONT_S).pack(side="left", padx=2)
        self.e_p2e = self._entry(f, "", w=7)

        f = frm(6)
        tk.Label(f, text="谷价时段", bg=BG, fg=DIM, font=FONT_S).pack(side="left")
        self.lbl_off_range = tk.Label(f, text="—", bg=BG, fg=DIM, font=FONT_S)
        self.lbl_off_range.pack(side="left", padx=(4, 0))

        f = frm(7)
        tk.Label(f, text="峰价", bg=BG, fg=FG, font=FONT_S).pack(side="left")
        self.e_pk_in = self._entry(f, "", w=6)
        self.e_pk_out = self._entry(f, "", w=6)
        self.e_pk_cache = self._entry(f, "", w=6)
        tk.Button(f, text="谷 = 峰 × 50%", command=self._fill_half, bg="#2a2a30",
                  fg=FG, activebackground="#333", activeforeground=FG, bd=0,
                  font=FONT_S).pack(side="left", padx=(8, 0))

        f = frm(8)
        tk.Label(f, text="谷价", bg=BG, fg=FG, font=FONT_S).pack(side="left")
        self.e_of_in = self._entry(f, "", w=6)
        self.e_of_out = self._entry(f, "", w=6)
        self.e_of_cache = self._entry(f, "", w=6)

        self.lbl_msg = tk.Label(self.win, text="", bg=BG, fg=YELLOW, font=FONT_S,
                                anchor="w", justify="left", wraplength=380)
        self.lbl_msg.grid(row=9, column=0, columnspan=6, sticky="w", padx=10, pady=(4, 0))

        f = tk.Frame(self.win, bg=BG)
        f.grid(row=10, column=0, columnspan=6, sticky="e", padx=10, pady=(4, 10))
        tk.Button(f, text="保存", command=self.save, bg="#2f5d3a", fg=FG, bd=0,
                  activebackground="#3a7248", activeforeground=FG, font=FONT,
                  width=8).pack(side="left", padx=4)
        tk.Button(f, text="取消", command=self.win.destroy, bg="#2a2a30", fg=FG, bd=0,
                  activebackground="#333", activeforeground=FG, font=FONT,
                  width=8).pack(side="left")

        # 回填峰谷值
        pk = e.get("peak") or {}
        of = e.get("off") or {}
        ph = e.get("peak_hours") or []
        if len(ph) > 0 and ph[0]:
            self.e_p1s.insert(0, ph[0][0] or "")
            self.e_p1e.insert(0, ph[0][1] or "")
        if len(ph) > 1 and ph[1]:
            self.e_p2s.insert(0, ph[1][0] or "")
            self.e_p2e.insert(0, ph[1][1] or "")
        for w, v in ((self.e_pk_in, pk.get("in")), (self.e_pk_out, pk.get("out")),
                     (self.e_pk_cache, pk.get("cache")), (self.e_of_in, of.get("in")),
                     (self.e_of_out, of.get("out")), (self.e_of_cache, of.get("cache"))):
            if v is not None:
                w.insert(0, str(v))
        # ── 价格段（时间轴）：同一站/模型可按日期改价，历史段不动 ──
        f = tk.Frame(self.win, bg=BG)
        f.grid(row=11, column=0, columnspan=6, sticky="we", padx=10, pady=(0, 10))
        tk.Label(f, text="价格段", bg=BG, fg=FG, font=FONT_S).pack(side="left")
        tk.Button(f, text="◀", command=self.seg_prev, bg="#2a2a30", fg=FG, bd=0,
                  activebackground="#333", activeforeground=FG, font=FONT_S,
                  width=2).pack(side="left", padx=(4, 0))
        self.lbl_seg = tk.Label(f, text="", bg=BG, fg=YELLOW, font=FONT_S, width=24,
                                anchor="w")
        self.lbl_seg.pack(side="left", padx=4)
        tk.Button(f, text="▶", command=self.seg_next, bg="#2a2a30", fg=FG, bd=0,
                  activebackground="#333", activeforeground=FG, font=FONT_S,
                  width=2).pack(side="left")
        tk.Label(f, text=" 新生效日", bg=BG, fg=DIM, font=FONT_S).pack(side="left")
        self.e_seg_from = self._entry(f, "", w=11)
        tk.Button(f, text="＋新增段", command=self.seg_add, bg="#2a2a30", fg=FG, bd=0,
                  activebackground="#333", activeforeground=FG,
                  font=FONT_S).pack(side="left", padx=3)
        tk.Button(f, text="删除本段", command=self.seg_del, bg="#2a2a30", fg=FG, bd=0,
                  activebackground="#333", activeforeground=FG,
                  font=FONT_S).pack(side="left")

        self._toggle_peak()
        self._toggle_wild()
        self._init_segs()
        self.win.grab_set()

    # ---------- 价格段（时间轴）编辑 ----------
    def _init_segs(self):
        """把条目规整成工作副本 self._segs（列表），并载入第一段。"""
        ps = self.book.segs(self.entry)
        if ps:
            self._segs = [dict(p) for p in ps]
        else:
            e = self.entry or {}
            seg = {}
            for k in ("in", "out", "cache", "ratio", "peak", "off", "peak_hours",
                      "weekend_off", "tag"):
                if k in e:
                    seg[k] = e[k]
            self._segs = [seg]
        self._load_seg(0)

    def _put(self, w, v):
        w.delete(0, "end")
        if v not in (None, ""):
            w.insert(0, str(v))

    def _load_seg(self, idx):
        self._seg_idx = max(0, min(idx, len(self._segs) - 1))
        s = self._segs[self._seg_idx] or {}
        self._put(self.e_in, s.get("in"))
        self._put(self.e_out, s.get("out"))
        self._put(self.e_cache, s.get("cache"))
        self._put(self.e_ratio, s.get("ratio"))
        self.v_free.set(s.get("tag") == "免费")
        is_peak = bool(s.get("peak") or s.get("off"))
        self.v_peak.set(is_peak)
        ph = s.get("peak_hours") or []
        self._put(self.e_p1s, ph[0][0] if len(ph) > 0 and ph[0] else "")
        self._put(self.e_p1e, ph[0][1] if len(ph) > 0 and ph[0] else "")
        self._put(self.e_p2s, ph[1][0] if len(ph) > 1 and ph[1] else "")
        self._put(self.e_p2e, ph[1][1] if len(ph) > 1 and ph[1] else "")
        pk = s.get("peak") or {}
        of = s.get("off") or {}
        self._put(self.e_pk_in, pk.get("in"))
        self._put(self.e_pk_out, pk.get("out"))
        self._put(self.e_pk_cache, pk.get("cache"))
        self._put(self.e_of_in, of.get("in"))
        self._put(self.e_of_out, of.get("out"))
        self._put(self.e_of_cache, of.get("cache"))
        self._toggle_peak()
        self._refresh_seg_label()
        self._toggle_percall()          # ★ 2026-10-08：按次开关的初始状态

    def _toggle_percall(self):
        """★ 2026-10-08：按次收费开关 —— 开了就把 token 单价输入框灰掉（不参与计价）。"""
        on = bool(self.v_percall.get())
        for w in (self.e_in, self.e_out, self.e_cache):
            try:
                w.config(state=("disabled" if on else "normal"))
            except Exception:
                pass
        try:
            self.e_percall.config(state=("normal" if on else "disabled"))
        except Exception:
            pass
        self._preview()

    def _stash_seg(self):
        """把输入框当前值存回当前段（宽松存，严格校验留到 save）。"""
        if not getattr(self, "_segs", None):
            return
        s = dict(self._segs[self._seg_idx] or {})
        s["in"] = self.e_in.get().strip()
        s["out"] = self.e_out.get().strip()
        s["cache"] = self.e_cache.get().strip()
        r = self.e_ratio.get().strip()
        if r == "":
            s.pop("ratio", None)
        else:
            s["ratio"] = r
        if self.v_free.get():
            s["tag"] = "免费"
        else:
            s.pop("tag", None)
        if self.v_peak.get():
            try:
                segs = self._segments()
            except Exception:
                segs = s.get("peak_hours") or []
            if segs:
                s["peak_hours"] = segs
            s["peak"] = {"in": self.e_pk_in.get().strip(), "out": self.e_pk_out.get().strip(),
                         "cache": self.e_pk_cache.get().strip()}
            s["off"] = {"in": self.e_of_in.get().strip(), "out": self.e_of_out.get().strip(),
                        "cache": self.e_of_cache.get().strip()}
        else:
            for k in ("peak", "off", "peak_hours"):
                s.pop(k, None)
        self._segs[self._seg_idx] = s

    def _refresh_seg_label(self):
        s = self._segs[self._seg_idx] if self._segs else {}
        self.lbl_seg.config(text="第 %d/%d 段 · %s 起" % (
            self._seg_idx + 1, len(self._segs), s.get("from") or "(最早)"))

    def seg_prev(self):
        if self._seg_idx > 0:
            self._stash_seg()
            self._load_seg(self._seg_idx - 1)

    def seg_next(self):
        if self._seg_idx < len(self._segs) - 1:
            self._stash_seg()
            self._load_seg(self._seg_idx + 1)

    def seg_add(self):
        d = self.e_seg_from.get().strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            self.lbl_msg.config(text="⚠ 新生效日填 YYYY-MM-DD（如 2026-08-17）", fg=RED)
            return
        if any((s.get("from") or "") == d for s in self._segs):
            self.lbl_msg.config(text="⚠ 该生效日已存在", fg=RED)
            return
        self._stash_seg()
        cur = dict(self._segs[self._seg_idx])
        cur["from"] = d
        cur.pop("ratio_kept", None)
        self._segs.append(cur)
        self._segs.sort(key=lambda x: x.get("from") or "")
        idx = [i for i, s in enumerate(self._segs) if (s.get("from") or "") == d]
        self._load_seg(idx[0] if idx else 0)
        self.e_seg_from.delete(0, "end")
        self.lbl_msg.config(text="已新增价格段 %s（改好价后点保存）" % d, fg=GREEN)

    def seg_del(self):
        if len(self._segs) <= 1:
            self.lbl_msg.config(text="⚠ 至少要留一段", fg=RED)
            return
        name = self._segs[self._seg_idx].get("from") or "(最早)"
        if not messagebox.askyesno("删除价格段", "删除「%s 起」这一段？" % name, parent=self.win):
            return
        i = self._seg_idx
        self._segs.pop(i)
        self._load_seg(min(i, len(self._segs) - 1))
        self.lbl_msg.config(text="已删除一段（点保存才生效）", fg=YELLOW)

    def _entry(self, parent, val="", w=8):
        e = tk.Entry(parent, width=w, font=FONT, bg="#26262c", fg=FG,
                     insertbackground=FG, bd=0, highlightthickness=1,
                     highlightbackground="#3a3a42", highlightcolor=BLUE)
        if val != "" and val is not None:
            e.insert(0, str(val))
        e.pack(side="left", padx=2)
        return e

    def _toggle_wild(self):
        if self.v_model_wild.get():
            pass

    def _preview(self):
        """倍率预览：标价 × 倍率 = 实收。

        ★ 2026-10-08：按次模式显示「每次 X 元 → 20 次约 ¥Y」的提示，
          而不是 token 单价预览（那个此时不参与计价）。
        """
        def v(w):
            try:
                return float(w.get().strip() or 0)
            except ValueError:
                return None

        # 按次收费：预览按次单价即可（次数取实时值意义不大且要有会话上下文）
        if self.v_percall.get():
            pc = v(self.e_percall)
            if not pc or pc <= 0:
                self.lbl_msg.config(text="按次收费：请填「每次 X 元」", fg=RED)
            else:
                self.lbl_msg.config(
                    text="按次收费 ¥%.4g/次（不看 token；成本 = 调用次数 × 单价）" % pc,
                    fg=BLUE)
            return

        try:
            r = float(self.e_ratio.get().strip() or 1.0)
        except ValueError:
            r = None
        if r is None or r <= 0:
            self.lbl_msg.config(text="倍率要填正数（默认 1，不填=不变）", fg=RED)
            return
        if abs(r - 1.0) < 1e-9:
            self.lbl_msg.config(text="", fg=YELLOW)
            return
        parts = []
        for name, w in (("输入", self.e_in), ("输出", self.e_out), ("缓存", self.e_cache)):
            x = v(w)
            if x:
                parts.append("%s %.4g→%.4g" % (name, x, x * r))
        self.lbl_msg.config(
            text="倍率 ×%.4g ｜ %s" % (r, " ｜ ".join(parts)) if parts
            else "倍率 ×%.4g" % r, fg=BLUE)

    def _toggle_peak(self):
        state = "normal" if self.v_peak.get() else "disabled"
        for w in (self.e_p1s, self.e_p1e, self.e_p2s, self.e_p2e, self.e_pk_in,
                  self.e_pk_out, self.e_pk_cache, self.e_of_in, self.e_of_out,
                  self.e_of_cache):
            w.config(state=state)
        self._refresh_off_range()

    def _fill_half(self):
        try:
            for src, dst in ((self.e_pk_in, self.e_of_in), (self.e_pk_out, self.e_of_out),
                             (self.e_pk_cache, self.e_of_cache)):
                v = float(src.get() or 0) / 2.0
                dst.delete(0, "end")
                dst.insert(0, ("%g" % v))
        except Exception:
            pass

    def _segments(self):
        segs = []
        for s, e in ((self.e_p1s.get().strip(), self.e_p1e.get().strip()),
                     (self.e_p2s.get().strip(), self.e_p2e.get().strip())):
            if s and e and s != e:
                segs.append([s, e])
        return segs

    def _refresh_off_range(self):
        if not self.v_peak.get():
            self.lbl_off_range.config(text="—")
            return
        segs = self._segments()
        if not segs:
            self.lbl_off_range.config(text="全天谷价")
            return
        def hm2m(x):
            h, m = x.split(":")
            return int(h) * 60 + int(m)
        taken = []
        for s, e in segs:
            a, b = hm2m(s), hm2m(e)
            if a <= b:
                taken += [(a, b)]
            else:
                taken += [(a, 1440), (0, b)]
        taken.sort()
        free = []
        cur = 0
        for a, b in taken:
            if a > cur:
                free += [(cur, a)]
            cur = max(cur, b)
        if cur < 1440:
            free += [(cur, 1440)]
        txt = " · ".join("%02d:%02d-%02d:%02d" % (a // 60, a % 60, b // 60, b % 60)
                         for a, b in free) or "全天谷价"
        self.lbl_off_range.config(text=txt)

    def save(self):
        """保存：把工作副本里的**所有价格段**写回条目。

        单段且无生效日 → 保持旧格式（兼容老条目）；多段或带生效日 → 写 periods 时间轴。
        """
        def num(v):
            s = str(v if v is not None else "").strip()
            return 0.0 if s == "" else float(s)

        try:
            self._stash_seg()             # 当前编辑的段先落回工作副本
            old_r = 0.0
            try:
                old_r = float(self.entry.get("ratio") or 0)
            except (TypeError, ValueError):
                old_r = 0.0
            raw_r = self.e_ratio.get().strip()
            periods = []
            for i, s in enumerate(self._segs):
                free = (s.get("tag") == "免费")
                pin, pout, pcache = num(s.get("in")), num(s.get("out")), num(s.get("cache"))
                if free:
                    pin = pout = pcache = 0.0
                rr = s.get("ratio")
                if rr in (None, ""):
                    # 留空 = 沿用原倍率（仅原条目所在段有旧值可沿用），否则 1.0
                    if i == self._seg_idx and raw_r == "" and old_r and abs(old_r - 1.0) > 1e-9:
                        ratio = old_r
                    else:
                        ratio = 1.0
                else:
                    try:
                        ratio = float(rr)
                    except (TypeError, ValueError):
                        raise ValueError("倍率填数字（默认 1，留空=沿用原倍率）")
                if ratio <= 0:
                    raise ValueError("倍率要大于 0")
                seg = {"from": s.get("from") or "",
                       "in": pin, "out": pout, "cache": pcache}
                if abs(ratio - 1.0) > 1e-9:
                    seg["ratio"] = ratio
                elif raw_r != "" and i == self._seg_idx and old_r and abs(old_r - 1.0) > 1e-9:
                    seg["ratio"] = 1.0    # 明确填了 1 → 显式清零
                if free:
                    seg["tag"] = "免费"
                pk, of = s.get("peak") or {}, s.get("off") or {}
                if pk or of:
                    segs_hm = s.get("peak_hours") or []
                    if not segs_hm:
                        raise ValueError("峰谷价需要至少一段有效时段")
                    for a, b in segs_hm:
                        self._check_hm(a)
                        self._check_hm(b)
                    if len(segs_hm) == 2 and self._overlap(segs_hm[0], segs_hm[1]):
                        raise ValueError("两段峰价时段重叠了")
                    if cover_all_day(segs_hm):
                        if not messagebox.askyesno(
                                "确认", "「%s」段的两段峰价已覆盖全天，没有谷价时段了，确认保存？"
                                % (s.get("from") or "最早")):
                            return
                    seg["peak_hours"] = segs_hm
                    seg["peak"] = {k: num(pk.get(k)) for k in ("in", "out", "cache")}
                    seg["off"] = {k: num(of.get(k)) for k in ("in", "out", "cache")}
                if s.get("weekend_off"):
                    seg["weekend_off"] = True
                periods.append(seg)
            periods.sort(key=lambda x: x.get("from") or "")
            rec = {
                "host": self.host,
                "model": "*" if (self.v_model_wild.get() or not self.model) else self.model,
                "cur": self.entry.get("cur") or "¥",
                "updated": datetime.now().strftime("%Y-%m-%d"),
            }
            # ★ 2026-10-08 按次收费：写入 per_call（勾了才写，>0 才有效）
            pc = num(self.e_percall.get()) if self.v_percall.get() else None
            # ⚠ 勾了「按次收费」但金额空/0 → 明确报错，不静默存成 token 条目
            #   （那是反效果：用户以为按次，实际按 token 计价且 token 价是 0）。
            if self.v_percall.get() and (not pc or pc <= 0):
                messagebox.showerror("按次收费", "勾了「按次收费」就得填每次金额（>0）。\n"
                                     "不想按次请取消勾选。", parent=self)
                return
            if pc and pc > 0:
                rec["per_call"] = pc
                # 按次模式下 token 单价强制清零，避免两个口径混算
                for _s in periods:
                    _s["in"] = _s["out"] = _s["cache"] = 0.0
            if len(periods) > 1 or (periods and periods[0].get("from")):
                rec["periods"] = periods              # 时间轴格式
            else:
                one = dict(periods[0]) if periods else {}
                one.pop("from", None)
                rec.update(one)                       # 单段无日期 → 旧格式
            self.book.upsert(rec)
        except Exception as ex:
            self.lbl_msg.config(text="⚠ %s" % ex)
            return
        self.win.destroy()
        if self.on_saved:
            self.on_saved()

    @staticmethod
    def _check_hm(x):
        if not re.match(r"^\d{1,2}:\d{2}$", x):
            raise ValueError("时间格式应为 HH:MM（如 08:30）")

    @staticmethod
    def _overlap(a, b):
        def hm2m(x):
            h, m = x.split(":")
            return int(h) * 60 + int(m)
        def seg(x):
            s, e = hm2m(x[0]), hm2m(x[1])
            if s <= e:
                return [(s, e)]
            return [(s, 1440), (0, e)]
        for s1, e1 in seg(a):
            for s2, e2 in seg(b):
                if max(s1, s2) < min(e1, e2):
                    return True
        return False


# ─────────────────────────── 价格表管理窗口 ───────────────────────────

class PriceManager:
    def __init__(self, root, book, on_changed=None, cur_host=None, cur_model=None):
        self.book = book
        self.on_changed = on_changed
        self.cur_host = cur_host or ""
        self.cur_model = cur_model or ""
        self.root = root

        self.win = tk.Toplevel(root)
        self.win.title("价格表管理")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        apply_icon(self.win)
        apply_dark_title_bar(self.win, bg_hex=BG)
        style_child_window(self.win, tree=True)   # ★ 2026-10-09 统一子窗口外观
        self.win.geometry("640x360")

        cols = ("host", "model", "in", "out", "cache", "peak", "updated")
        heads = ("站", "模型", "输入", "输出", "缓存", "峰谷", "更新")
        self.tree = ttk.Treeview(self.win, columns=cols, show="headings", height=12,
                                 style="Dark.Treeview")
        for c, h in zip(cols, heads):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=90 if c != "host" else 170, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.tree.bind("<Double-1>", lambda e: self.edit())
        self.tree.bind("<Button-3>", self._popup)

        f = tk.Frame(self.win, bg=BG)
        f.pack(fill="x", padx=10, pady=(0, 10))
        for text, cmd in (("新建当前站", self.new_current), ("编辑", self.edit),
                          ("删除", self.delete), ("刷新", self.refresh)):
            tk.Button(f, text=text, command=cmd, bg="#2a2a30", fg=FG, bd=0,
                      activebackground="#333", activeforeground=FG,
                      font=FONT).pack(side="left", padx=3)
        tk.Button(f, text="⚡ 从中转站实扣同步价格", command=self.sync_from_proxy,
                  bg="#2f5d3a", fg=FG, bd=0, activebackground="#3a7248",
                  activeforeground=FG, font=FONT).pack(side="right", padx=3)

        self.menu = tk.Menu(self.win, tearoff=0, **MENU_STYLE)
        self.menu.add_command(label="编辑", command=self.edit)
        self.menu.add_command(label="删除", command=self.delete)
        self.refresh()

    def sync_from_proxy(self):
        ok, msg = sync_prices_from_proxy(self.book)
        self.refresh()
        if self.on_changed:
            self.on_changed()
        try:
            import tkinter.messagebox as mb
            if ok:
                mb.showinfo("同步结果", msg)
            else:
                mb.showwarning("同步提示", msg)
        except Exception:
            pass

    def refresh(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for e in self.book.all():
            pk = ""
            if e.get("peak") or e.get("off"):
                segs = e.get("peak_hours") or []
                pk = "峰谷 " + " ".join("%s-%s" % (s, t) for s, t in segs)
            elif e.get("tag"):
                pk = e["tag"]
            self.tree.insert("", "end", values=(
                e.get("host", ""), e.get("model", ""), e.get("in", ""), e.get("out", ""),
                e.get("cache", ""), pk, e.get("updated", "")))

    def _sel_entry(self):
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0], "values")
        for e in self.book.all():
            if e.get("host") == vals[0] and str(e.get("model", "")) == str(vals[1]):
                return e
        return None

    def _popup(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def new_current(self):
        if not self.cur_host:
            return
        PriceDialog(self.win, self.book, self.cur_host, self.cur_model,
                    on_saved=self._changed)
        self.win.after(400, self.refresh)

    def edit(self):
        e = self._sel_entry()
        if not e:
            return
        PriceDialog(self.win, self.book, e.get("host", ""),
                    "*" if e.get("model") in ("*", "", None) else e.get("model", ""),
                    on_saved=self._changed, entry=e)
        self.win.after(400, self.refresh)

    def delete(self):
        e = self._sel_entry()
        if not e:
            return
        if not messagebox.askyesno("删除", "删除条目 %s / %s ？" % (e.get("host"), e.get("model"))):
            return
        try:
            self.book.delete(e.get("host"), e.get("model"))
        except Exception as ex:
            messagebox.showerror("删除失败", str(ex), parent=self.win)
            return
        self._changed()

    def _changed(self):
        self.refresh()
        if self.on_changed:
            self.on_changed()


# ─────────────────────────── 日账本（跨站成本统计） ───────────────────────────


class CostLedger:
    """日账本：每天结算一次各站×模型的消费（水位差值法）+ 价格冻结。

    水位差值 = 今日 DB 累计 − 上次结算时的 DB 累计。
    因为差值天然规避"跨天会话"的归属问题，所以比按 last_seen 切分更准。
    结算时把「当时算出的成本（含当时倍率）」一起写死 → 日后改价不污染历史。
    """

    def __init__(self, path=None, prices=None):
        self.path = path or LEDGER_PATH
        self.prices = prices
        self.data = {"days": {}, "approx": [], "bills": [],
                     "watermark": {"ts": None, "day": None, "keys": {}}}
        # 非空 = 「文件在、但读不了」→ 禁止写盘（否则 60 秒内 maybe_settle 就会覆盖掉全部历史）
        self._load_error = ""
        self.load()

    # ---------- 存取 ----------
    def load(self):
        """读账本。

        ⚠ 与 PriceBook 同理：文件不存在 ≠ 读失败。
        读失败时必须保留内存数据并禁止写盘 —— 否则 tick 每 60 秒调一次 maybe_settle，
        会立刻用空账本覆盖掉全部历史账目（实测复现）。
        """
        self._load_error = ""
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
        except FileNotFoundError:
            pass                        # 首次运行 → 空账本，正常
        except OSError as ex:
            self._load_error = "账本被占用：%s" % ex
            return
        except ValueError as ex:
            self._load_error = "账本 JSON 损坏：%s" % ex
            return
        else:
            if isinstance(d, dict) and "days" in d:
                self.data = d
        self.data.setdefault("days", {})
        self.data.setdefault("approx", [])
        self.data.setdefault("bills", [])
        self.data.setdefault("watermark", {"ts": None, "day": None, "keys": {}})

    def save(self):
        """原子写账本。读失败时拒绝写盘（返回 False），避免覆盖能救回的数据。"""
        if self._load_error:
            _startup_log("账本保存被拒（%s）" % self._load_error)
            return False
        if not self.path:
            # 路径解析失败（如环境变量缺失导致的异常启动）→ 别去 replace("" ) 报 WinError 3
            _startup_log("账本保存被拒：数据目录未解析出来")
            return False
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return True
        except Exception as ex:
            _startup_log("账本保存失败：%s" % ex)
            return False

    # ---------- 水位 ----------
    @staticmethod
    def _watermark():
        """DB 全量累计水位：{"host|model": {calls,in,hit,out}}"""
        out = {}
        for host, model, calls, inp, hit, o in db_query(
                "SELECT billing_base_url, model, COALESCE(SUM(api_call_count),0), "
                "COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_tokens),0), "
                "COALESCE(SUM(output_tokens),0) FROM session_model_usage "
                "GROUP BY billing_base_url, model"):
            h = host or "?"
            try:
                h = canon_host(urlparse(host).hostname or h)
            except Exception:
                pass
            key = "%s|%s" % (h, canon_model(model))
            k = out.setdefault(key, {"calls": 0, "in": 0, "hit": 0, "out": 0})
            k["calls"] += calls or 0
            k["in"] += inp or 0
            k["hit"] += hit or 0
            k["out"] += o or 0
        return out

def _multi_priced(sid, d, now_hm, day=None, prices=None):
    """★ 2026-10-09 新增（模块级）：一个会话用过**多个「站×模型」**时，逐条匹配价格再汇总。

    ⚠️ 为什么必须加（用户 2026-10-09 报的 bug）：
      `sessions` 表每个会话只有**一行**主记录，只记得**最后一次**的
      billing_base_url / model。原实现拿这一对去匹配价格，于是：
        会话 20261008_200019 实际用了
          localhost:8787 × gemini-3.8-flash-high（76 次）
          api9.dshapi.icu × deepseek-v4.1-flash（6 次）
        但主记录停在最后一对 = api9.dshapi.icu × gemini-3.8-flash-high
        → 匹配到的价格条目完全不对，站和模型张冠李戴
          （实测 `--sid` 输出 site=api.dshapi.icu / model=gemini-3.8-flash-high
            / price=未配置，而价格表里 `localhost|gemini-3.8-flash-high` 明明有）。

    正确做法：以 `session_model_usage` 的真实分组为准（一行 = 一个「站×模型」），
      每行各自匹配自己的价格、各自算成本，最后把金额相加。
      站名与模型名也一并按真实用量汇总返回，供界面显示。

    ⚠️ 调用方在 Monitor 里，所以本函数做成**模块级**（早先版本误挂到 CostLedger
      下，日志实测报 `'Monitor' object has no attribute '_multi_priced'`）。

    *prices* = PriceBook 实例（由调用方传，避免这里再 new 一个）。

    返回 dict（无数据时返回 None）：
      {pairs/sites/models/matched/unmatched/total_cost/saved/cur/free/per_call/
       cost/calls/list_cost/period/ratio/seg_from}
      —— 后几个字段是为了让渲染层能直接当 priced 字典用。
    """
    pairs = []
    try:
        rows = db_query(
            "SELECT billing_base_url, model, "
            "COALESCE(SUM(api_call_count),0), COALESCE(SUM(input_tokens),0), "
            "COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(output_tokens),0) "
            "FROM session_model_usage WHERE session_id=? "
            "GROUP BY billing_base_url, model "
            "ORDER BY 3 DESC", (sid,))
    except Exception:
        rows = []

    if not rows:
        return None

    total_cost = 0.0
    total_saved = 0.0
    cur = "¥"
    n_matched = 0
    n_unmatched = 0
    all_free = True
    all_per_call = True
    any_per_call = False
    sites, models = [], []

    for burl, model, calls, inp, hit, out in rows:
        try:
            site = canon_host(urlparse(burl).hostname or "?")
        except Exception:
            site = canon_host(burl or "?")
        model_n = canon_model(model or "?")
        if site and site != "?" and site not in sites:
            sites.append(site)
        if model_n and model_n != "?" and model_n not in models:
            models.append(model_n)

        calls = int(calls or 0)
        inp, hit, out = int(inp or 0), int(hit or 0), int(out or 0)

        entry = prices.match(site, model_n, day) if prices else None
        c = compute_cost(entry, inp, hit, out, now_hm, day=day,
                         calls=calls_for(site, model_n, fallback=calls))
        rec = {"site": site, "model": model_n, "calls": calls,
               "in": inp, "hit": hit, "out": out,
               "entry": entry, "cost": None, "free": False,
               "per_call": None, "saved": 0.0, "cur": cur}
        if c is None:
            n_unmatched += 1
            all_free = False
            all_per_call = False
        else:
            n_matched += 1
            cur = c.get("cur") or cur
            rec["cost"] = c.get("cost")
            rec["saved"] = c.get("saved") or 0.0
            rec["free"] = bool(c.get("free"))
            rec["per_call"] = c.get("per_call")
            rec["cur"] = cur
            if not c.get("free"):
                all_free = False
            if c.get("per_call"):
                any_per_call = True
            else:
                all_per_call = False
            total_cost += (c.get("cost") or 0.0)
            total_saved += (c.get("saved") or 0.0)
        pairs.append(rec)

    if not n_matched:
        all_per_call = False

    return {"pairs": pairs, "sites": sites, "models": models,
            "matched": n_matched, "unmatched": n_unmatched,
            "total_cost": total_cost, "saved": total_saved, "cur": cur,
            "free": bool(all_free and n_matched),
            "per_call": bool(all_per_call and n_matched),
            "any_per_call": any_per_call,
            # 兼容原 priced 字典的字段名，渲染层可直接读
            "cost": total_cost,
            "calls": sum(p["calls"] for p in pairs),
            "list_cost": total_cost,
            "period": None, "ratio": 1.0, "seg_from": ""}


class CostLedger:
    """日账本：每天结算一次各站×模型的消费（水位差值法）+ 价格冻结。

    水位差值 = 今日 DB 累计 − 上次结算时的 DB 累计。
    因为差值天然规避"跨天会话"的归属问题，所以比按 last_seen 切分更准。
    结算时把「当时算出的成本（含当时倍率）」一起写死 → 日后改价不污染历史。
    """

    def __init__(self, path=None, prices=None):
        self.path = path or LEDGER_PATH
        self.prices = prices
        self.data = {"days": {}, "approx": [], "bills": [],
                     "watermark": {"ts": None, "day": None, "keys": {}}}
        # 非空 = 「文件在、但读不了」→ 禁止写盘（否则 60 秒内 maybe_settle 就会覆盖掉全部历史）
        self._load_error = ""
        self.load()

    # ---------- 存取 ----------
    def load(self):
        """读账本。

        ⚠ 与 PriceBook 同理：文件不存在 ≠ 读失败。
        读失败时必须保留内存数据并禁止写盘 —— 否则 tick 每 60 秒调一次 maybe_settle，
        会立刻用空账本覆盖掉全部历史账目（实测复现）。
        """
        self._load_error = ""
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
        except FileNotFoundError:
            pass                        # 首次运行 → 空账本，正常
        except OSError as ex:
            self._load_error = "账本被占用：%s" % ex
            return
        except ValueError as ex:
            self._load_error = "账本 JSON 损坏：%s" % ex
            return
        else:
            if isinstance(d, dict) and "days" in d:
                self.data = d
        self.data.setdefault("days", {})
        self.data.setdefault("approx", [])
        self.data.setdefault("bills", [])
        self.data.setdefault("watermark", {"ts": None, "day": None, "keys": {}})

    def save(self):
        """原子写账本。读失败时拒绝写盘（返回 False），避免覆盖能救回的数据。"""
        if self._load_error:
            _startup_log("账本保存被拒（%s）" % self._load_error)
            return False
        if not self.path:
            # 路径解析失败（如环境变量缺失导致的异常启动）→ 别去 replace("") 报 WinError 3
            _startup_log("账本保存被拒：数据目录未解析出来")
            return False
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return True
        except Exception as ex:
            _startup_log("账本保存失败：%s" % ex)
            return False

    def _price_cost(self, host, model, d, day=None, now_hm=None):
        """按 *day* 生效的价格段算成本。返回 (cost, ratio, seg_from)。"""
        entry = self.prices.match(host, model, day) if self.prices else None
        # ★ 2026-10-08：按次收费需次数 —— d 里带 calls 就用它（0 也算有效值），
        #   没带才去问站点监控（见 calls_for 的 is not None 判断）。
        _calls = d.get("calls")
        c = compute_cost(entry, d.get("in", 0), d.get("hit", 0), d.get("out", 0),
                         now_hm=now_hm, day=day,
                         calls=calls_for(host, model, fallback=_calls))
        if not c:
            return None, None, ""
        return round(c["cost"], 6), c["ratio"], c.get("seg_from") or ""

    # ---------- 重算历史（账本价格冻结的出口） ----------
    def _settle_hm(self):
        """结算时刻的 HH:MM（重建当时峰谷判断用的时段）。无水位 → None（用当前钟点）。"""
        ts = (self.data.get("watermark") or {}).get("ts")
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except (TypeError, ValueError):
            return None

    def recompute(self, mode="day"):
        """按价格表的时间轴重算账本历史成本（tokens 原样保留）。

        价格段带生效日期后，「重算」不再等于「用今天的价覆盖历史」：
          mode="day"     ★默认：每一天按「当天生效的价格段」算 → 历史稳定，不受未来调价影响
          mode="missing"  只补 cost=None 的条目（其余一律不动），同样按当天段
          mode="all"      一律用「当前生效段」覆盖（价格整段填错时才用，会改写历史）
        返回 dict(scanned, priced, unpriced, skipped, changed, mode, hm)
        """
        days = self.data.get("days") or {}
        hm = self._settle_hm()
        # ⚠️ 2026-10-03 加的保险：没有价格表时重算 = 把**全部金额清成「缺价」**，
        #    这是一次数据事故（实测把 196 条全写成了 None）。宁可不算，也不动。
        if self.prices is None:
            try:
                self.prices = PriceBook()
                self.prices.load(force=True)
            except Exception:
                self.prices = None
        if self.prices is None:
            return {"scanned": 0, "priced": 0, "unpriced": 0, "skipped": 0, "changed": 0,
                    "mode": mode, "hm": hm,
                    "error": "没有加载价格表，已放弃重算（否则会把全部金额清空）"}
        scanned = priced = unpriced = skipped = changed = 0
        for day, rec in days.items():
            if not isinstance(rec, dict):
                continue
            for key, v in rec.items():
                if not isinstance(v, dict):
                    continue
                try:
                    host, model = key.split("|", 1)
                    host = canon_host(host)
                    model = canon_model(model)
                except ValueError:
                    continue
                if mode == "missing" and v.get("cost") is not None:
                    skipped += 1
                    continue
                scanned += 1
                day_arg = None if mode == "all" else day
                # ★ 2026-10-08 修：必须把该天该模型**自己存的 calls** 传下去。
                #   原来这里只传 in/hit/out，_price_cost 里 d.get("calls")=None
                #   → 按次模式回落站点监控全量 → 把「有史以来全部次数」重复
                #   记进每一天（实测 3 天 ×10 次本该 ¥2.4，算成 ¥918.48）。
                cost, ratio, seg = self._price_cost(
                    host, model,
                    {"in": v.get("in") or 0, "hit": v.get("hit") or 0,
                     "out": v.get("out") or 0,
                     "calls": v.get("calls") or 0},
                    day=day_arg, now_hm=hm)
                old = v.get("cost")
                if cost is None:
                    v["cost"] = None
                    unpriced += 1
                else:
                    v["cost"] = cost
                    if ratio is not None:
                        v["ratio"] = ratio
                    if seg:
                        v["seg_from"] = seg
                    priced += 1
                if (old is None) != (cost is None) or (
                        old is not None and cost is not None and abs(old - cost) > 1e-9):
                    changed += 1
        self.data["recomputed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.data["recompute_stat"] = {"scanned": scanned, "priced": priced,
                                       "unpriced": unpriced, "skipped": skipped,
                                       "changed": changed, "mode": mode, "hm": hm}
        self.save()
        return dict(self.data["recompute_stat"])

    def regroup(self):
        """按「站点归并表」把账本里所有天的键重新分组（别名合并求和）。

        为什么必须单独一步：`recompute` 只按 key 取价、**不改 key**。归并规则
        新增后，历史各天里同一家中转站的多个域名仍是各自独立的键
        （如 10-01 的 api4.dshapi.icu 与 api.dshapi.icu 各记一笔），不重新分组
        就永远显示成两个站、各算一份钱。
        返回 dict(days, merged, changed)
        """
        days = self.data.get("days") or {}
        stat = {"days": 0, "merged": 0, "changed": 0}
        for day, rec in days.items():
            if not isinstance(rec, dict) or not rec:
                continue
            stat["days"] += 1
            new = {}
            for key, v in rec.items():
                if not isinstance(v, dict):
                    continue
                h, sep, m = str(key).partition("|")
                nk = ("%s|%s" % (canon_host(h), canon_model(m))) if sep else canon_host(h)
                t = new.get(nk)
                if t is None:
                    t = new[nk] = {"calls": 0, "in": 0, "hit": 0, "out": 0, "cost": None}
                for f in ("calls", "in", "hit", "out"):
                    try:
                        t[f] += int(v.get(f) or 0)
                    except (TypeError, ValueError):
                        pass
                c = v.get("cost")
                if isinstance(c, (int, float)):
                    t["cost"] = float(t["cost"] or 0.0) + float(c)
            if len(new) != len(rec):
                stat["changed"] += 1
                stat["merged"] += len(rec) - len(new)
            days[day] = new
        # 水位键一并归一（防止残留别名键在将来被当成增量重记一次）
        wm = self.data.get("watermark") or {}
        if isinstance(wm.get("keys"), dict):
            wm["keys"] = _norm_watermark_keys(wm["keys"])
        self.save()
        return stat

    # ---------- 实付锚点（站方账单 → 校准历史 / 反推倍率） ----------
    def add_bill(self, bill):
        """录入一条实付锚点（同 host+model+时间段 → 覆盖）。"""
        b = {
            "host": bill.get("host") or "",
            "model": bill.get("model") or "*",
            "seg_from": bill.get("seg_from") or "",
            "from": bill.get("from") or "",
            "to": bill.get("to") or "",
            "amount": float(bill.get("amount") or 0),
            "cur": bill.get("cur") or "¥",
            "tokens": bill.get("tokens") or {},
            "tokens_by_seg": bill.get("tokens_by_seg") or {},
            "amount_items": bill.get("amount_items") or {},
            "note": bill.get("note") or "",
            "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        rest = [x for x in (self.data.get("bills") or [])
                if not (x.get("host") == b["host"] and x.get("model") == b["model"]
                        and x.get("from") == b["from"] and x.get("to") == b["to"])]
        rest.append(b)
        rest.sort(key=lambda x: (x.get("host") or "", x.get("from") or ""))
        self.data["bills"] = rest
        self.save()
        return b

    def del_bill(self, host, model, frm, to):
        rest = [x for x in (self.data.get("bills") or [])
                if not (x.get("host") == host and x.get("model") == model
                        and x.get("from") == frm and x.get("to") == to)]
        self.data["bills"] = rest
        self.save()
        return len(rest)

    def _bill_tokens(self, bill):
        """该账单覆盖的 token：站方给了就用站方，否则按时间段从账本聚合。"""
        by = bill.get("tokens_by_seg") or {}
        if by:
            m = c = o = 0.0
            for t in by.values():
                m += float((t or {}).get("miss") or 0)
                c += float((t or {}).get("cache") or 0)
                o += float((t or {}).get("out") or 0)
            return m, c, o, "站方(分段)"
        t = bill.get("tokens") or {}
        if t:
            return (float(t.get("miss") or 0), float(t.get("cache") or 0),
                    float(t.get("out") or 0), "站方")
        miss = cache = out = 0.0
        host = bill.get("host") or ""
        frm, to = bill.get("from") or "", bill.get("to") or ""
        for day, rec in (self.data.get("days") or {}).items():
            if frm and day < frm:
                continue
            if to and day > to:
                continue
            for key, v in rec.items():
                if not isinstance(v, dict) or key.split("|", 1)[0] != host:
                    continue
                miss += v.get("in") or 0
                cache += v.get("hit") or 0
                out += v.get("out") or 0
        return miss, cache, out, "账本"

    def _resolve_single(self, host, model, seg_from, tokens, amount, amount_items=None, cur="¥"):
        """单段反解：按该段标价算估算额，与实付/分项金额对比。

        seg_from="" 表示「最早那段」（不能写成 None —— None = 当前生效段）。
        """
        entry = (self.prices.match(host, model or "", seg_from)
                 if self.prices else None)
        miss = float((tokens or {}).get("miss") or 0)
        cache = float((tokens or {}).get("cache") or 0)
        out = float((tokens or {}).get("out") or 0)
        pin = float((entry or {}).get("in") or 0)
        pc = float((entry or {}).get("cache") or 0)
        po = float((entry or {}).get("out") or 0)
        toks = {"miss": miss, "cache": cache, "out": out}
        prices = {"miss": pin, "cache": pc, "out": po}
        labels = {"miss": "未命中输入", "cache": "缓存命中", "out": "输出"}
        ai = amount_items or {}
        est_items = {k: toks[k] * prices[k] / 1e6 for k in toks}
        est = sum(est_items.values())
        items = []
        for k in ("miss", "cache", "out"):
            amt = float(ai.get(k) or 0) if ai else None
            items.append({
                "key": k, "label": labels[k], "tokens": toks[k], "price": prices[k],
                "est": est_items[k], "amount": amt,
                "unit": (amt / (toks[k] / 1e6)) if (amt is not None and toks[k]) else None,
                "k": (amt / est_items[k]) if (amt is not None and est_items[k]) else None,
            })
        return {"entry": entry, "seg_from": (entry or {}).get("_seg_from", seg_from or ""),
                "tokens": toks, "prices": prices, "est_items": est_items, "est": est,
                "amount": amount, "cur": cur, "items": items,
                "k": (amount / est) if est else None}

    def resolve_bill(self, bill):
        """反解账单：整体倍率 k、逐项倍率、真单价。支持 tokens_by_seg（跨调价段精确估算）。"""
        host = bill.get("host")
        model = bill.get("model") or ""
        cur = bill.get("cur") or "¥"
        amount = float(bill.get("amount") or 0)
        by = bill.get("tokens_by_seg") or {}
        if by:
            parts = []
            for seg_from, t in sorted(by.items(), key=lambda kv: kv[0] or ""):
                parts.append(self._resolve_single(host, model, seg_from, t, 0.0, None, cur))
            total_est = sum(p["est"] for p in parts)
            for p in parts:
                p["amount_share"] = (amount * p["est"] / total_est) if total_est else 0.0
                p["k_share"] = (p["amount_share"] / p["est"]) if p["est"] else None
            toks = {k: sum(p["tokens"][k] for p in parts) for k in ("miss", "cache", "out")}
            est_items = {k: sum(p["est_items"][k] for p in parts) for k in ("miss", "cache", "out")}
            items = []
            for k in ("miss", "cache", "out"):
                items.append({"key": k, "label": {"miss": "未命中输入", "cache": "缓存命中",
                                                  "out": "输出"}[k],
                              "tokens": toks[k], "price": None, "est": est_items[k],
                              "amount": None, "unit": None, "k": None})
            return {"seg_rows": parts, "tokens": toks, "est_items": est_items,
                    "est": total_est, "amount": amount, "cur": cur, "items": items,
                    "tokens_src": "站方(分段)",
                    "k": (amount / total_est) if total_est else None,
                    "seg_from": ""}
        miss, cache, out, src = self._bill_tokens(bill)
        r = self._resolve_single(host, model,
                                 bill.get("seg_from") or bill.get("to") or bill.get("from"),
                                 {"miss": miss, "cache": cache, "out": out},
                                 amount, bill.get("amount_items"), cur)
        r["tokens_src"] = src
        return r

    def apply_bill_ratio(self, bill, ratio=None):
        """把反推出的倍率写回该账单对应的价格段（保留旧值 ratio_prev）。

        多段条目：每段都填同一个整体倍率（k 是「标价 → 实收」的统一系数）。
        """
        if ratio is None:
            ratio = (self.resolve_bill(bill) or {}).get("k")
        if not ratio or not self.prices:
            return False
        host = bill.get("host")
        model = bill.get("model") or "*"
        entry = self.prices.entry_of(host, model)
        segs = self.prices.segs(entry or {})
        if segs:
            ok = False
            for p in segs:
                ok = self.prices.set_seg_ratio(host, model, p.get("from") or "",
                                               float(ratio)) or ok
            return ok
        return self.prices.set_seg_ratio(host, model, "", float(ratio))

    def settle(self, day):
        """把「上次水位 → 现在」的差值累加记入 *day*，并推进水位。"""
        wm = self._watermark()
        # ⚠️ 旧水位键可能残留「未归一的域名」（见 _norm_watermark_keys 的踩坑注释）。
        #    不归一就会出现「同一批用量被记两天」的幽灵账，所以读的时候一并归一。
        prev = _norm_watermark_keys(self.data["watermark"].get("keys") or {})
        dayrec = self.data["days"].setdefault(day, {})
        moved = 0
        for key, cur in wm.items():
            p = prev.get(key) or {"calls": 0, "in": 0, "hit": 0, "out": 0}
            d = {f: (cur.get(f, 0) - p.get(f, 0)) for f in ("calls", "in", "hit", "out")}
            if not any(v > 0 for v in d.values()):
                continue
            moved += 1
            host, model = key.split("|", 1)
            host, model = canon_host(host), canon_model(model)
            cost, ratio, seg = self._price_cost(host, model, d, day=day)
            tgt = dayrec.setdefault(key, {"calls": 0, "in": 0, "hit": 0, "out": 0, "cost": 0.0})
            for f in ("calls", "in", "hit", "out"):
                tgt[f] = tgt.get(f, 0) + d[f]
            if cost is None:
                tgt["cost"] = None if tgt.get("cost") is None else tgt["cost"]
            else:
                tgt["cost"] = (tgt.get("cost") or 0) + cost
                if ratio is not None:
                    tgt["ratio"] = ratio
                if seg:
                    tgt["seg_from"] = seg       # 这一段是按哪个价格段算的
        self.data["watermark"]["keys"] = wm
        self.data["watermark"]["ts"] = time.time()
        self.save()
        return moved

    def maybe_settle(self):
        """跨日 / 首次运行时推进账本。由 App 定时调用（内部限流）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        wm = self.data["watermark"]
        if wm.get("day") == today:
            return False
        if not wm.get("day"):
            # 首次：只记水位，不做增量（避免把全部历史算成今天）
            wm["keys"] = self._watermark()
            wm["ts"] = time.time()
            wm["day"] = today
            self.save()
            return False
        # 已跨日 → 把差值记到「上次结算日」
        self.settle(wm["day"])
        wm["day"] = today
        self.save()
        return True

    # ---------- 实时聚合 ----------
    def live(self, since_ts=None, until_ts=None):
        """按 last_seen 落在区间内实时聚合（用于 24h / 今天）。"""
        agg = {}
        today = datetime.now().strftime("%Y-%m-%d")
        for host, model, calls, inp, hit, o, ls in db_query(
                "SELECT billing_base_url, model, api_call_count, input_tokens, "
                "cache_read_tokens, output_tokens, last_seen FROM session_model_usage"):
            try:
                t = float(ls or 0)
            except (TypeError, ValueError):
                t = 0.0
            if t > 1e12:
                t /= 1000.0
            if since_ts and t < since_ts:
                continue
            if until_ts and t > until_ts:
                continue
            h = host or "?"
            try:
                h = canon_host(urlparse(host).hostname or h)
            except Exception:
                pass
            model = canon_model(model)
            key = "%s|%s" % (h, model)
            a = agg.setdefault(key, {"calls": 0, "in": 0, "hit": 0, "out": 0,
                                     "cost": 0.0, "unpriced": False,
                                     "entry": None})
            a["calls"] += calls or 0
            a["in"] += inp or 0
            a["hit"] += hit or 0
            a["out"] += o or 0
            a["entry"] = self.prices.match(h, model) if self.prices else None
        # ★ 2026-10-08 修：成本必须**循环后统一算一次**。
        #   原来在循环体内逐行算 + 累加 —— 按 token 计价时因为 (in+cr+out) 线性
        #   相加，结果恰好正确；但**按次计价**是「总次数 × 单价」，逐行算会把
        #   次数重复计入（第 2 行时 calls 已含第 1 行）→ 金额翻倍。实测推理发现。
        for key, a in agg.items():
            h, _, model = key.partition("|")
            # ★ 口径：聚合是「站×模型」的累计视角 → 要站点口径。
            #   ⚠ 必须把 live() 的窗口原样传下去！否则 site_call_counts 会查
            #   全表，按次金额 =「该站上线以来全部调用 × 单价」，跟窗口无关
            #   （实测：查「今天」显示 3827 次 ×0.08 = ¥306，而当天实际 482 次）。
            c = compute_cost(a["entry"], a["in"], a["hit"], a["out"], day=today,
                             calls=calls_for(h, model, since_ts=since_ts,
                                             until_ts=until_ts,
                                             fallback=a.get("calls"),
                                             prefer_site=True))
            if c is None:
                a["unpriced"] = True
            else:
                a["cost"] += c["cost"]
            a.pop("entry", None)
        return agg

    # ---------- 站方真值优先 ----------
    @staticmethod
    def _window_days(since_ts):
        """窗口覆盖到的自然日列表（'YYYY-MM-DD'）；since_ts 为空 = 不限。"""
        if not since_ts:
            return None
        d0 = datetime.fromtimestamp(since_ts).date()
        d1 = datetime.now().date()
        out, d = [], d0
        while d <= d1:
            out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    @staticmethod
    def _truth_hosts(days_list, td):
        """{day: {有站方流水的站,...}} —— 全 0 行不算「有流水」。"""
        out = {}
        for d in (days_list or []):
            out[d] = {k.split("|", 1)[0] for k, v in (td.get(d) or {}).items()
                      if (v.get("calls") or v.get("in") or v.get("hit")
                          or v.get("out") or v.get("cost"))}
        return out

    @staticmethod
    def _put_truth(acc, keys_vals, hosts):
        """把站方真值（只限 hosts 里的站）放进聚合，打 src=site。

        ⚠️ 2026-10-03 自查：跳过「全 0」的站方行。站方库里可能出现某些 key
        （比如未使用 key 的占位、或按模型汇总的空行）带 0 用量 —— 如果照收，
        就会把该站当天的 Hermes 估算整体替换成 0（有键即覆盖），等于凭空抹账。
        """
        for k, v in (keys_vals or {}).items():
            if k.split("|", 1)[0] not in hosts:
                continue
            if not (v.get("calls") or v.get("in") or v.get("hit")
                    or v.get("out") or v.get("cost")):
                continue                      # 全 0 行不进账
            a = acc.setdefault(k, {"calls": 0, "in": 0, "hit": 0, "out": 0,
                                   "cost": 0.0, "unpriced": False,
                                   "unpriced_hist": False})
            for f in ("calls", "in", "hit", "out"):
                a[f] = (a.get(f) or 0) + v[f]
            a["cost"] = (a.get("cost") or 0.0) + v["cost"]
            a["src"] = "site"
            # ⚠️ 站方流水里 cost 为 NULL = 「这笔还没结算出金额」，不能当成 0 元实付。
            #    标成缺价，界面才会提示而不是显示一个假的 0。
            if v.get("cost_missing"):
                a["unpriced_site"] = True

    # ---------- 查询入口 ----------
    def report(self, win):
        """win: '24h'|'today'|'7d'|'30d'|'all' → (agg, source_label)

        ★站方真值优先（2026-10-03）：站方监控有流水的「站·天」，直接用站方实际
          收的钱（= 实付）；站方没覆盖到的（采集失效 / 超出保留期）才退回 Hermes 估算。
        ⚠️ 必须**按天**替换，不能按整个窗口替换：站方库只留 7 天、账本有几十天，
           整体替换会把站方没覆盖的天一起抹掉（会少算）。
        """
        now = datetime.now()
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = today0.strftime("%Y-%m-%d")
        td = site_truth_days()
        seen_site, seen_hermes = set(), set()

        if win in ("24h", "today"):
            since = (time.time() - 86400) if win == "24h" else today0.timestamp()
            acc = self.live(since_ts=since)
            # ★ 用带 ts 下界的站方聚合，而不是「按天抽表」。
            #   ⚠️ 2026-10-03 自查：24h 是**滚动**窗口，而按天抽表拿的是整天流水；
            #   昨天更早时段、前天尾段的用量会被算进 24h，导致显示金额高于真实实付。
            #   today 是自然日窗口，才可以用按天口径。
            if win == "today":
                days_list = self._window_days(since) or []
                per_day = self._truth_hosts(days_list, td)
                full = set.intersection(*per_day.values()) if per_day else set()
                if full:
                    for k in list(acc):
                        if k.split("|", 1)[0] in full:
                            del acc[k]
                    for d in days_list:
                        if _day_in_scope(d, set(days_list)):
                            self._put_truth(acc, td.get(d) or {}, full)
            else:
                truth = site_truth_agg(since)
                if truth:
                    hosts = {k.split("|", 1)[0] for k, v in truth.items()
                             if (v.get("calls") or v.get("in") or v.get("hit")
                                 or v.get("out") or v.get("cost"))}
                    for k in list(acc):
                        if k.split("|", 1)[0] in hosts:
                            del acc[k]
                    self._put_truth(acc, truth, hosts)
            seen_site |= {k.split("|", 1)[0] for k, v in acc.items()
                          if v.get("src") == "site"}
            seen_hermes |= {k.split("|", 1)[0] for k in acc} - seen_site
            self.last_srcs = {"site": seen_site, "hermes": seen_hermes}
            return acc, ("实时" + ("·站方优先" if seen_site else ""))

        n = {"7d": 7, "30d": 30, "all": None}.get(win, 7)
        acc = {}
        approx_hit = False
        for day, rec in (self.data.get("days") or {}).items():
            # ⚠️ 2026-10-03 修：原来只判 `(now - dd).days >= n`。那个减法带着时刻，
            #    未来日期会算出**负数** → 条件恒假 → 未来日期不但不被跳过，还会被
            #    30d/all 累加进账本聚合。改成先挡掉「不晚于今天」之外的日期。
            if not _day_in_scope(day, None):
                continue
            if n is not None:
                try:
                    dd = datetime.strptime(day, "%Y-%m-%d")
                except ValueError:
                    continue
                if (now.date() - dd.date()).days >= n:
                    continue
                if day in (self.data.get("approx") or []):
                    approx_hit = True
            # ⚠️ 只把「这天确实有非 0 站方用量」的站算作已覆盖；全 0 行不能把
            #    Hermes 估算顶掉（否则等于凭空抹账，见 _put_truth 的说明）
            day_hosts = {k.split("|", 1)[0] for k, v in (td.get(day) or {}).items()
                         if (v.get("calls") or v.get("in") or v.get("hit")
                             or v.get("out") or v.get("cost"))}
            for key, v in rec.items():
                parts = key.split("|", 1)
                if len(parts) == 2:
                    key = "%s|%s" % (canon_host(parts[0]), canon_model(parts[1]))
                if key.split("|", 1)[0] in day_hosts:
                    continue              # 这天这个站有站方真值 → 跳过 Hermes 估算
                a = acc.setdefault(key, {"calls": 0, "in": 0, "hit": 0, "out": 0,
                                         "cost": 0.0, "unpriced": False,
                                         "unpriced_hist": False})
                for f in ("calls", "in", "hit", "out"):
                    a[f] += v.get(f) or 0
                if v.get("cost") is None:
                    # 账本里 cost=None = 结算/回填时该站还没配价。再看当前价格表：
                    #  - 现在能匹配到 → 「历史缺价」：补价了没重算，点「重算历史」即可消除
                    #  - 现在也匹配不到 → 「未配价」：价格表里真没这条，得先去补配
                    if self.prices is not None and self.prices.match(*key.split("|", 1)):
                        a["unpriced_hist"] = True
                    else:
                        a["unpriced"] = True
                else:
                    a["cost"] += v["cost"]
        # 叠加「今天的实时」（账本只记到昨天，不会重复）
        today_hosts = {k.split("|", 1)[0] for k, v in (td.get(today) or {}).items()
                       if (v.get("calls") or v.get("in") or v.get("hit")
                           or v.get("out") or v.get("cost"))}
        for key, v in self.live(since_ts=today0.timestamp()).items():
            if key.split("|", 1)[0] in today_hosts:
                continue
            a = acc.setdefault(key, {"calls": 0, "in": 0, "hit": 0, "out": 0,
                                     "cost": 0.0, "unpriced": False,
                                     "unpriced_hist": False})
            for f in ("calls", "in", "hit", "out"):
                a[f] += v.get(f) or 0
            a["cost"] += v.get("cost") or 0
            a["unpriced"] = a["unpriced"] or v.get("unpriced", False)
        # 放站方真值（只放窗口内、且站方确实有流水的那些天）
        win_days = self._window_days(
            (today0.timestamp() - (n - 1) * 86400) if n else None)
        day_set = set(win_days) if win_days else None
        for d, blob in td.items():
            if not _day_in_scope(d, day_set):
                continue
            hosts = {k.split("|", 1)[0] for k, v in blob.items()
                     if (v.get("calls") or v.get("in") or v.get("hit")
                         or v.get("out") or v.get("cost"))}
            self._put_truth(acc, blob, hosts)
        seen_site |= {k.split("|", 1)[0] for k, v in acc.items()
                      if v.get("src") == "site"}
        seen_hermes |= {k.split("|", 1)[0] for k in acc} - seen_site
        self.last_srcs = {"site": seen_site, "hermes": seen_hermes}
        return acc, ("账本" + ("+≈近似" if approx_hit else "")
                     + ("·站方优先" if seen_site else ""))


class SiteMergeDialog:
    """站点 / 模型 名归并设置（点选式）。

    为什么要它：账本按 `host|model` 记账，而同一个中转站常有多个入口域名
    （dshapi 的 api / api2 / api4）、同一个模型也常被写成不同名字
    （`deepseek/deepseek-v4.1-flash`、`[a]gemini-3.8-flash`）。不归并就会被
    拆成好几行各算一份，价格匹配也可能落空。

    操作：左边选「要归并的名字」，右边选「并到哪个」（下拉里列出账本里已有的
    全部名字），点「加入」；最后点「保存并重算历史」。
    """

    def __init__(self, root, ledger):
        self.ledger = ledger
        raw = load_name_merge_raw()
        self.rules_site = {k: v for k, v in raw["sites"].items() if k != v}
        self.rules_model = {k: v for k, v in raw["models"].items() if k != v}
        self.auto_sites = _auto_site_merge()
        self.auto_models = _auto_model_merge()
        self._keys_site, self._keys_model = [], []

        self.win = tk.Toplevel(root)
        self.win.title("站点与模型归并")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        apply_icon(self.win)
        apply_dark_title_bar(self.win, bg_hex=BG)
        style_child_window(self.win, tree=False)   # ★ 2026-10-09 统一子窗口外观
        self.win.transient(root)
        self.win.geometry("780x600")

        tk.Label(self.win,
                 text="把同一家的多个名字合并成一个 —— 账本、价格、显示都用合并后的名字。",
                 bg=BG, fg=FG, font=FONT).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(self.win,
                 text="左边选「要归并的名字」，右边选「并到哪个」（下拉里是账本里已有的全部名字），点「加入」。",
                 bg=BG, fg=DIM, font=FONT_S).pack(anchor="w", padx=14)

        def _row(lab, vals, cmd):
            f = tk.Frame(self.win, bg=BG)
            f.pack(fill="x", padx=14, pady=(8, 0))
            tk.Label(f, text=lab, bg=BG, fg=FG, font=FONT_S, width=5,
                     anchor="w").pack(side="left")
            a = ttk.Combobox(f, values=vals, width=30, font=FONT_S)
            a.pack(side="left")
            tk.Label(f, text="  →  ", bg=BG, fg=DIM, font=FONT_S).pack(side="left")
            b = ttk.Combobox(f, values=vals, width=30, font=FONT_S)
            b.pack(side="left")
            tk.Button(f, text="＋ 加入", bd=0, font=FONT_S, bg="#2f5d3a", fg=FG,
                      activebackground="#3a7248", activeforeground=FG,
                      command=cmd).pack(side="left", padx=8)
            return a, b

        sites = self._collect("site")
        models = self._collect("model")
        self.cb_s_from, self.cb_s_to = _row("站点", sites, self._add_site)
        self.cb_m_from, self.cb_m_to = _row("模型", models, self._add_model)

        mid = tk.Frame(self.win, bg=BG)
        mid.pack(fill="both", expand=True, padx=14, pady=(10, 0))
        self.lb_site, self.lb_model = self._make_lists(mid)

        auto = tk.Frame(self.win, bg=BG)
        auto.pack(fill="x", padx=14, pady=(8, 0))
        self.var_align = tk.BooleanVar(value=bool(raw["auto_align"]))
        self.var_model = tk.BooleanVar(value=bool(raw["auto_model"]))
        tk.Checkbutton(auto,
                       text="自动对齐站点监控（读 sites.json 的域名池，把别名并入该站 host；当前 %d 条）"
                            % len(self.auto_sites),
                       variable=self.var_align, bg=BG, fg=FG, font=FONT_S,
                       activebackground=BG, activeforeground=FG, selectcolor="#2a2a30",
                       highlightthickness=0, bd=0, anchor="w").pack(anchor="w")
        tk.Checkbutton(auto,
                       text="自动归并模型名（剥开头的 [频道] 与 provider/ 前缀，只认已知标准名；当前 %d 条）"
                            % len(self.auto_models),
                       variable=self.var_model, bg=BG, fg=FG, font=FONT_S,
                       activebackground=BG, activeforeground=FG, selectcolor="#2a2a30",
                       highlightthickness=0, bd=0, anchor="w").pack(anchor="w")
        _al = "、".join("%s→%s" % kv for kv in sorted(self.auto_sites.items())[:4])
        _am = "、".join("%s→%s" % kv for kv in sorted(self.auto_models.items())[:4])
        tk.Label(auto, text="站点自动：%s%s\n模型自动：%s%s" % (
            _al or "无", " …" if len(self.auto_sites) > 4 else "",
            _am or "无", " …" if len(self.auto_models) > 4 else ""),
            bg=BG, fg=DIM, font=FONT_S, justify="left").pack(anchor="w", pady=(2, 0))

        self.lbl = tk.Label(self.win, text="", bg=BG, fg=YELLOW, font=FONT_S,
                            anchor="w", justify="left", wraplength=750)
        self.lbl.pack(fill="x", padx=14, pady=(6, 0))

        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill="x", padx=14, pady=(6, 12))
        tk.Button(bar, text="保存并重算历史", bg="#2f5d3a", fg=FG, bd=0, font=FONT_S,
                  activebackground="#3a7248", activeforeground=FG,
                  command=lambda: self._save(True)).pack(side="left")
        tk.Button(bar, text="仅保存", bg="#2a2a30", fg=FG, bd=0, font=FONT_S,
                  activebackground="#333", activeforeground=FG,
                  command=lambda: self._save(False)).pack(side="left", padx=6)
        tk.Button(bar, text="取消", bg="#2a2a30", fg=FG, bd=0, font=FONT_S,
                  activebackground="#333", activeforeground=FG,
                  command=self.win.destroy).pack(side="left")
        tk.Label(bar, text="  重算 = 按新规则重新分组 + 按天重算金额（不改 token）",
                 bg=BG, fg=DIM, font=FONT_S).pack(side="left")

        self._refresh_lists()

    # -------- 候选名字（账本里出现过的）--------
    def _collect(self, kind):
        cnt = {}
        for _d, rec in (self.ledger.data.get("days") or {}).items():
            for k in (rec or {}):
                parts = str(k).split("|", 1)
                n = parts[0] if kind == "site" else (parts[1] if len(parts) > 1 else "")
                if n and n != "?":
                    cnt[n] = cnt.get(n, 0) + 1
        # 水位里有、但当天还没入账的（刚用上的新名字）
        wm = ((self.ledger.data.get("watermark") or {}).get("keys")) or {}
        for k in wm:
            parts = str(k).split("|", 1)
            n = parts[0] if kind == "site" else (parts[1] if len(parts) > 1 else "")
            if n and n not in cnt:
                cnt[n] = 0
        # ★Hermes 库里现用的名字：刚加的新站账本还没结算，这里也能立刻选到
        try:
            for h, m in _db_hosts_models():
                n = h if kind == "site" else m
                if n and n not in cnt:
                    cnt[n] = 0
        except Exception:
            pass
        names = sorted(cnt, key=lambda x: (-cnt[x], x))
        seen = set(cnt)
        rules = self.rules_site if kind == "site" else self.rules_model
        autos = self.auto_sites if kind == "site" else self.auto_models
        for d in (rules, autos):
            for k, v in d.items():
                for n in (k, v):
                    if n and n not in seen:
                        seen.add(n)
                        names.append(n)
        return names

    def _make_lists(self, parent):
        out = []
        for title, deleter in (("站点归并规则", self._del_site),
                               ("模型归并规则", self._del_model)):
            col = tk.Frame(parent, bg=BG)
            col.pack(side="left", fill="both", expand=True, padx=(0, 8))
            tk.Label(col, text=title, bg=BG, fg=DIM, font=FONT_S).pack(anchor="w")
            box = tk.Frame(col, bg=BG)
            box.pack(fill="both", expand=True)
            sb = tk.Scrollbar(box)
            sb.pack(side="right", fill="y")
            lb = tk.Listbox(box, bg="#1e1e24", fg=FG, font=FONT_S, bd=0, height=9,
                            highlightthickness=1, highlightbackground="#3a3a42",
                            selectbackground="#2f5d3a", selectforeground=FG,
                            yscrollcommand=sb.set)
            lb.pack(side="left", fill="both", expand=True)
            sb.config(command=lb.yview)
            tk.Button(col, text="删除选中", bd=0, font=FONT_S, bg="#2a2a30", fg=FG,
                      activebackground="#333", activeforeground=FG,
                      command=deleter).pack(anchor="e", pady=(2, 0))
            out.append(lb)
        return out

    def _refresh_lists(self):
        keys = []
        for lb, rules in ((self.lb_site, self.rules_site), (self.lb_model, self.rules_model)):
            lb.delete(0, "end")
            ks = sorted(rules)
            for k in ks:
                lb.insert("end", "%-26s → %s" % (k, rules[k]))
            keys.append(ks)
        self._keys_site, self._keys_model = keys

    def _msg(self, t, warn=True):
        self.lbl.config(text=t, fg=(YELLOW if warn else FG))

    # -------- 加 / 删 --------
    def _add_site(self):
        self._add_rule((self.cb_s_from.get() or "").strip(),
                       (self.cb_s_to.get() or "").strip(), self.rules_site, "站点")

    def _add_model(self):
        self._add_rule((self.cb_m_from.get() or "").strip(),
                       (self.cb_m_to.get() or "").strip(), self.rules_model, "模型")

    def _add_rule(self, a, b, rules, kind):
        if not a or not b:
            self._msg("「%s」两边都要选：左边=要归并的名字，右边=并到哪个。" % kind)
            return
        if a.lower() == b.lower():
            self._msg("两边一样，不需要归并。")
            return
        tgt, seen = b, {a.lower()}
        for _ in range(20):                 # 顺着链走到终点，避免 A→B→C
            nx = rules.get(tgt.lower())
            if not nx or nx.lower() in seen:
                break
            seen.add(nx.lower())
            tgt = nx
        rules[a.lower()] = tgt
        for k, v in list(rules.items()):    # 已经指向 a 的旧规则一起改道（别留悬空链）
            if k != a.lower() and v.lower() == a.lower():
                rules[k] = tgt
        self._refresh_lists()
        self._msg("已加入：%s → %s   （点「保存并重算历史」生效）" % (a, tgt), warn=False)

    def _del_site(self):
        for i in reversed(self.lb_site.curselection()):
            if i < len(self._keys_site):
                self.rules_site.pop(self._keys_site[i], None)
        self._refresh_lists()

    def _del_model(self):
        for i in reversed(self.lb_model.curselection()):
            if i < len(self._keys_model):
                self.rules_model.pop(self._keys_model[i], None)
        self._refresh_lists()

    # -------- 存 --------
    def _save(self, then_recompute):
        try:
            path = save_name_merge(sites=self.rules_site, models=self.rules_model,
                                   auto_align=bool(self.var_align.get()),
                                   auto_model=bool(self.var_model.get()))
        except Exception as ex:
            messagebox.showerror("保存失败", str(ex), parent=self.win)
            return
        msg = ("已写入：\n%s\n\n手动规则：站点 %d 条 ｜ 模型 %d 条\n"
               "自动对齐站点：%s ｜ 自动归并模型：%s" % (
                   path, len(self.rules_site), len(self.rules_model),
                   "开" if self.var_align.get() else "关",
                   "开" if self.var_model.get() else "关"))
        if then_recompute:
            try:
                if getattr(self.ledger, "prices", None) is None:
                    self.ledger.prices = PriceBook()
                    self.ledger.prices.load(force=True)
                st = self.ledger.regroup()
                rs = self.ledger.recompute("day")
                msg += ("\n\n重新分组：%d 天，合并 %d 个重复键"
                        "\n重算：扫描 %d 条，%d 条金额有变化") % (
                    st.get("days", 0), st.get("merged", 0),
                    rs.get("scanned", 0), rs.get("changed", 0))
            except Exception as ex:
                msg += "\n\n重算失败（配置已保存，可稍后手动重算）：%s" % ex
        messagebox.showinfo("站点与模型归并", msg, parent=self.win)
        self.win.destroy()


class GapDialog:
    def __init__(self, root, mon, d):
        self.mon = mon
        self.d = d
        sid = d["sid"]
        rep = d.get("report") or {}
        est = d.get("estimate") or {}

        self.win = tk.Toplevel(root)
        self.win.title("缺账明细")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        apply_icon(self.win)
        apply_dark_title_bar(self.win, bg_hex=BG)
        style_child_window(self.win, tree=True)   # ★ 2026-10-09 统一子窗口外观
        self.win.geometry("520x430")

        self.sid = sid
        self.lbl_head = tk.Label(self.win, text="", bg=BG, fg=FG, font=FONT, anchor="w")
        self.lbl_head.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(self.win, text="会话 %s · 每 2 秒自动刷新" % sid, bg=BG, fg=DIM,
                 font=FONT_S, anchor="w").pack(fill="x", padx=12)

        types = mon._gap_types(sid, rep) if rep.get("gaps") else []
        anchor = (est or {}).get("anchor_in") or 0

        # 底部信息区（先 pack，避免被表格挤掉）
        bottom = tk.Frame(self.win, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=12, pady=(0, 10))
        self.v_inc = tk.BooleanVar(value=bool(d.get("include_gap")))
        tk.Checkbutton(bottom, text="把缺账估算计入成本行", variable=self.v_inc, bg=BG,
                       fg=FG, selectcolor=BG, activebackground=BG, activeforeground=FG,
                       font=FONT_S, command=self._toggle).pack(anchor="w")
        if est:
            txt = ("合计估算：+%s in（未命中 %s / 命中 %s） · +%s out%s"
                   % (fmt_vol(est["in"]), fmt_vol(est["miss"]), fmt_vol(est["hit"]),
                      fmt_vol(est["out"]),
                      "　≈ %s" % fmt_money(est["cost"]) if est.get("cost") is not None else ""))
        else:
            txt = "无缺账估算（该会话没有记账锚点）"
        self.lbl_sum = tk.Label(bottom, text=txt, bg=BG, fg=YELLOW, font=FONT_S, anchor="w",
                                justify="left", wraplength=470)
        self.lbl_sum.pack(anchor="w", pady=(5, 0))
        tk.Label(bottom, text=(est or {}).get("note", ""), bg=BG, fg=DIM,
                 font=FONT_S, anchor="w").pack(anchor="w")

        # 表格区
        mid = tk.Frame(self.win, bg=BG)
        mid.pack(side="top", fill="both", expand=True, padx=12, pady=8)
        vs = ttk.Scrollbar(mid, orient="vertical")
        self.tree = ttk.Treeview(mid, columns=("t", "k", "v"), show="headings",
                                 height=10, yscrollcommand=vs.set, style="Dark.Treeview")
        vs.config(command=self.tree.yview)
        for c, h, w in (("t", "时间", 90), ("k", "类型", 70), ("v", "估算", 280)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        self._refresh()

    def _refresh(self):
        """每 2 秒重算：把「进行中」的请求也列出来，避免被误当成缺账。"""
        try:
            d = self.mon.build(self.sid)
            rep = d.get("report") or {}
            est = d.get("estimate") or {}
            mark = ""
            if rep.get("partial"):
                mark = "（日志未覆盖全程）"
            elif rep.get("concurrent"):
                mark = "（并发时段，归属近似）"
            self.lbl_head.config(text="真实请求 %d ｜ 已记账 %d ｜ 缺账 %d%s" % (
                rep.get("real", 0), rep.get("recorded", 0), rep.get("gap", 0), mark))
            for i in self.tree.get_children():
                self.tree.delete(i)
            if rep.get("inflight"):
                self.tree.insert("", "end", values=(
                    rep["inflight"][11:19], "进行中", "等待记账 →"))
            anchor = est.get("anchor_in") or 0
            for ts, kind in (self.mon._gap_types(self.sid, rep) if rep.get("gaps") else []):
                self.tree.insert("", "end", values=(
                    ts[11:19] if ts else "?", kind, "≈ %s prompt" % fmt_vol(anchor)))
            if est:
                self.lbl_sum.config(text="合计估算：+%s in（未命中 %s / 命中 %s） · +%s out%s" % (
                    fmt_vol(est["in"]), fmt_vol(est["miss"]), fmt_vol(est["hit"]),
                    fmt_vol(est["out"]),
                    "　≈ %s" % fmt_money(est["cost"]) if est.get("cost") is not None else ""))
        except Exception:
            pass
        self.win.after(2000, self._refresh)

    def _toggle(self):
        cb = getattr(self.mon, "on_gap_toggle", None)
        if cb:
            cb(self.v_inc.get())


# ─────────────────────────── 悬浮窗 ───────────────────────────

class StatsDialog:
    """成本统计窗口：按站（或站+模型）汇总，5 个时间梯度。"""

    WINS = (("24h", "24h"), ("today", "今天"), ("7d", "7天"),
            ("30d", "30天"), ("all", "总共"))

    def __init__(self, root, mon, on_close=None):
        self.mon = mon
        self.ledger = mon.cost_ledger
        self.view = "site"
        self.on_close = on_close
        self._alive = True
        self._after_id = None
        self._probe_cache = None
        self.win = tk.Toplevel(root)
        self.win.title("成本统计")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        apply_icon(self.win)
        apply_dark_title_bar(self.win, bg_hex=BG)
        style_child_window(self.win, tree=True)   # ★ 2026-10-09 统一子窗口外观
        self.win.geometry("700x470")

        top = tk.Frame(self.win, bg=BG)
        top.pack(fill="x", padx=10, pady=(10, 2))
        self.btns = {}
        for key, label in self.WINS:
            b = tk.Button(top, text=label, width=6, bd=0, font=FONT, bg="#2a2a30", fg=FG,
                          activebackground="#3a3a44", activeforeground=FG,
                          command=lambda k=key: self.switch(k))
            b.pack(side="left", padx=2)
            self.btns[key] = b

        # 视图切换：按中转站 / 按模型 / 总量
        viewbar = tk.Frame(self.win, bg=BG)
        viewbar.pack(fill="x", padx=10, pady=(2, 4))
        tk.Label(viewbar, text="视图:", bg=BG, fg=DIM, font=FONT_S).pack(side="left")
        self.vbtns = {}
        for key, label in (("site", "按中转站"), ("model", "按模型"), ("total", "总量")):
            b = tk.Button(viewbar, text=label, width=9, bd=0, font=FONT_S, bg="#2a2a30",
                          fg=FG, activebackground="#3a3a44", activeforeground=FG,
                          command=lambda k=key: self.set_view(k))
            b.pack(side="left", padx=2)
            self.vbtns[key] = b
        self.lbl_hint = tk.Label(viewbar, text="", bg=BG, fg=DIM, font=FONT_S)
        self.lbl_hint.pack(side="right")

        cols = ("site", "model", "calls", "rate", "inp", "out", "cost", "share")
        heads = ("站", "模型", "调用", "命中率", "命中 / 未命中", "输出", "成本", "占比")
        widths = (150, 130, 55, 60, 150, 75, 80, 55)
        self.tree = ttk.Treeview(self.win, columns=cols, show="headings", height=12,
                                 style="Dark.Treeview")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=4)

        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill="x", padx=10, pady=(0, 2))
        tk.Button(bar, text="重算历史", bd=0, font=FONT_S, bg="#2a2a30", fg=FG,
                  activebackground="#3a3a44", activeforeground=FG,
                  command=self.recompute_history).pack(side="left")
        tk.Button(bar, text="录入实付", bd=0, font=FONT_S, bg="#2a2a30", fg=FG,
                  activebackground="#3a3a44", activeforeground=FG,
                  command=self.enter_bill).pack(side="left", padx=4)
        tk.Button(bar, text="站点归并…", bd=0, font=FONT_S, bg="#2a2a30", fg=FG,
                  activebackground="#3a3a44", activeforeground=FG,
                  command=self.open_site_merge).pack(side="left", padx=4)
        self.lbl_rc = tk.Label(bar, text="", bg=BG, fg=DIM, font=FONT_S, anchor="w")
        self.lbl_rc.pack(side="left", padx=8)

        self.lbl_sum = tk.Label(self.win, text="", bg=BG, fg=YELLOW, font=FONT_S,
                                anchor="w", justify="left", wraplength=670)
        self.lbl_sum.pack(fill="x", padx=10, pady=(0, 10))
        self.switch("24h")
        self.win.bind("<Destroy>", self._on_destroy)
        self._schedule()

    # -------- 定时自动刷新（价格/账本变了窗口自己跟上） --------
    REFRESH_MS = 3000

    def _schedule(self):
        if not self._alive:
            return
        try:
            self._after_id = self.win.after(self.REFRESH_MS, self._auto_refresh)
        except tk.TclError:
            self._after_id = None
            self._alive = False

    def _auto_refresh(self):
        self._after_id = None
        if not self._alive:
            return
        try:
            # 窗口不可见（最小化/被遮挡/已切走）→ 不做重活，只排下一次
            if not self.win.winfo_viewable():
                self._schedule()
                return
            self.refresh()
        except tk.TclError:
            self._alive = False
            return
        self._schedule()

    def _on_destroy(self, ev=None):
        """关窗：停掉定时器（否则 after 回调打到已销毁窗口 → TclError）。"""
        if ev is not None and getattr(ev, "widget", None) is not self.win:
            return
        self._alive = False
        if self._after_id:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self.on_close:
            try:
                self.on_close(self)
            except Exception:
                pass

    def notify_prices_changed(self):
        """价格保存后由 App 调用：立刻热重载价格表并重绘。"""
        if not self._alive:
            return
        try:
            if self.ledger.prices:
                self.ledger.prices.load(force=True)
        except Exception:
            pass
        try:
            self.refresh(force=True)          # 价格变了，DB 探针看不出，强制重建
        except tk.TclError:
            self._alive = False

    def open_site_merge(self):
        """站点归并设置：把同一家中转站的多个域名并成一个站名，并重算历史。"""
        dlg = SiteMergeDialog(self.win, self.ledger)
        self.win.wait_window(dlg.win)
        self.refresh(force=True)

    def recompute_history(self):
        """重算历史：按天（推荐）/ 只补缺 / 全部按当前价 —— 三档，破坏性操作单独标红。"""
        dlg = tk.Toplevel(self.win)
        dlg.title("重算历史账本")
        dlg.configure(bg=BG)
        dlg.attributes("-topmost", True)
        dlg.transient(self.win)
        dlg.resizable(False, False)
        tk.Label(dlg, text="按价格表的时间轴重算历史成本（tokens 不动）",
                 bg=BG, fg=FG, font=FONT).pack(padx=16, pady=(12, 4), anchor="w")
        tk.Label(dlg, text="· 按天重算：每天用「当天生效的价格段」→ 后来调价也不污染历史\n"
                           "· 只补缺：只算「未配价」的条目，已算好的一律不碰\n"
                           "· 全按当前价：用最新价格段覆盖全部历史（价格整段填错时才用）",
                 bg=BG, fg=DIM, font=FONT_S, justify="left").pack(padx=16, anchor="w")
        f = tk.Frame(dlg, bg=BG)
        f.pack(padx=16, pady=12)
        for text, mode, col in (("按天重算（推荐）", "day", "#2f5d3a"),
                                ("只补缺", "missing", "#2a2a30"),
                                ("全按当前价", "all", "#5d2f2f")):
            tk.Button(f, text=text, bg=col, fg=FG, bd=0, font=FONT_S,
                      activebackground="#3a3a44", activeforeground=FG,
                      command=lambda m=mode, d=dlg: (d.destroy(), self._do_recompute(m))
                      ).pack(side="left", padx=4)
        tk.Button(f, text="取消", bg="#2a2a30", fg=FG, bd=0, font=FONT_S,
                  activebackground="#3a3a44", activeforeground=FG,
                  command=dlg.destroy).pack(side="left", padx=4)

    def _do_recompute(self, mode):
        try:
            st = self.ledger.recompute(mode)
        except Exception as ex:
            messagebox.showerror("重算失败", str(ex), parent=self.win)
            return
        self.refresh()
        if st.get("error"):
            messagebox.showwarning("重算未执行", st["error"], parent=self.win)
            return
        messagebox.showinfo(
            "重算完成",
            "模式：%s\n扫描 %d 条 ｜ 算出金额 %d 条 ｜ 仍缺价 %d 条\n跳过 %d 条 ｜ 数值有变化 %d 条%s" % (
                {"day": "按天重算", "missing": "只补缺", "all": "全按当前价"}.get(mode, mode),
                st["scanned"], st["priced"], st["unpriced"], st["skipped"],
                st.get("changed", 0),
                "\n\n仍缺价的条目 = 价格表里确实没有该「站+模型」，去补配即可。"
                if st["unpriced"] else ""),
            parent=self.win)

    def enter_bill(self):
        """录一次站方实付账单（锚点）→ 自动反推倍率 / 真单价。"""
        agg, _src = self.ledger.report(self.cur)
        host, best = "", -1
        for key, v in (agg or {}).items():
            h = key.split("|", 1)[0]
            if (v.get("calls") or 0) > best:
                best = v.get("calls") or 0
                host = h
        dlg = BillDialog(self.win, self.ledger, host=host)
        self.win.wait_window(dlg.win)
        self.refresh(force=True)

    def switch(self, key):
        for k, b in self.btns.items():
            b.config(bg="#2f5d3a" if k == key else "#2a2a30")
        self.cur = key
        self.refresh()

    def set_view(self, key):
        self.view = key
        for k, b in self.vbtns.items():
            b.config(bg="#2f5d3a" if k == key else "#2a2a30")
        self.lbl_hint.config(text={"site": "每个中转站单独合计", "model": "站 × 模型逐条",
                                   "total": "全部站 + 全部模型总计"}.get(key, ""))
        self.refresh()

    def _probe(self):
        """便宜的「数据有没有变」探针（比 live() 省一个数量级）。"""
        try:
            r = db_query("SELECT COUNT(*), MAX(last_seen), SUM(api_call_count) "
                         "FROM session_model_usage", one=True)
        except Exception:
            r = None
        lb = self.ledger.data or {}
        # ★ 站点监控库也要盯（2026-10-03 自查发现）：站方真值优先之后，站方数据
        #   变了而 Hermes 库没变时，表格里的数字其实已经过期 —— 不盯它就不刷新。
        try:
            store = os.path.join(_get_proxy_monitor_dir(), "store.db")
            st_sig = (round(os.path.getmtime(store), 3), os.path.getsize(store)) \
                if os.path.isfile(store) else None
        except OSError:
            st_sig = None
        return (self.cur, self.view, tuple(r) if r else None,
                len(lb.get("days") or {}), lb.get("recomputed_at"),
                len(lb.get("bills") or []),
                (lb.get("watermark") or {}).get("day"), st_sig)

    def refresh(self, force=False):
        # 数据没变就不重建表格 —— 否则每 2 秒拆一次 30+ 行 Treeview，会顿卡 + 丢选中/滚动
        try:
            probe = self._probe()
        except Exception:
            probe = None
        if not force and probe is not None and probe == self._probe_cache:
            return
        self._probe_cache = probe
        try:
            agg, src = self.ledger.report(self.cur)
        except Exception as ex:
            self.lbl_sum.config(text="统计失败：%s" % ex)
            return
        # 记住选中行 + 滚动位置，重建后恢复
        sel_vals, yv = None, 0.0
        try:
            s = self.tree.selection()
            if s:
                sel_vals = self.tree.item(s[0], "values")
            yv = self.tree.yview()[0]
        except Exception:
            pass
        for i in self.tree.get_children():
            self.tree.delete(i)
        rows = {}
        for key, v in (agg or {}).items():
            host, model = key.split("|", 1)
            if self.view == "site":
                k = (host, "")
            elif self.view == "model":
                k = (host, model)
            else:
                k = ("全部中转站", "")
            r = rows.setdefault(k, {"calls": 0, "in": 0, "hit": 0, "out": 0,
                                    "cost": 0.0, "unpriced": False,
                                    "unpriced_hist": False})
            for f in ("calls", "in", "hit", "out"):
                r[f] += v.get(f) or 0
            r["cost"] += v.get("cost") or 0
            if v.get("unpriced"):
                r["unpriced"] = True
            if v.get("unpriced_hist"):
                r["unpriced_hist"] = True
            if v.get("src") == "site":
                r["src"] = "site"          # 该行数字来自站方真值（实付）
            if v.get("unpriced_site"):
                r["unpriced_site"] = True  # 站方那笔还没结算出金额
        total = sum(r["cost"] for r in rows.values())
        unpriced_live = 0      # 价格表里真没条目
        unpriced_hist = 0      # 账本里 cost=None（重算可消）
        unpriced_site = 0      # 站方库里 cost 为空（那笔还没结算出金额）
        for (host, model), r in sorted(rows.items(), key=lambda kv: -kv[1]["calls"]):
            tot_in = r["in"] + r["hit"]
            rate = (r["hit"] * 100.0 / tot_in) if tot_in else 0.0
            share = (r["cost"] / total * 100.0) if total else 0.0
            cost_s = fmt_money(r["cost"])
            missing = r["unpriced"] or r["unpriced_hist"]
            if not r["cost"] and missing:
                if r["unpriced"] and r["unpriced_hist"]:
                    cost_s = "未配价/历史缺价"
                elif r["unpriced"]:
                    cost_s = "未配价"
                else:
                    cost_s = "历史未配价"
            elif missing:
                cost_s += " ✱"      # 有金额但含缺价部分（金额只算已配价的那部分）
            if not r["cost"] and missing:
                if r["unpriced"]:
                    unpriced_live += r["calls"]
                if r["unpriced_hist"]:
                    unpriced_hist += r["calls"]
                if r.get("unpriced_site"):
                    unpriced_site += r["calls"]
            mark = " ●" if r.get("src") == "site" else ""
            self.tree.insert("", "end", values=(
                host + mark, model or "（全部模型）", "{:,}".format(r["calls"]),
                "%.1f%%" % rate, "%s / %s" % (fmt_vol(r["hit"]), fmt_vol(r["in"])),
                fmt_vol(r["out"]), cost_s, ("%.1f%%" % share) if total else "-"))
        notes = []
        if unpriced_site:
            notes.append("站方 %d 次调用尚未结算出金额（站方库里 cost 为空，先按 0 计）"
                         % unpriced_site)
        if unpriced_live:
            notes.append("未配价 %d 次调用（价格表无此条目，去补配）" % unpriced_live)
        if unpriced_hist:
            notes.append("历史缺价 %d 次调用（点「重算历史」消除）" % unpriced_hist)
        extra = ""
        if notes:
            extra = "　⚠ " + "；".join(notes)
        if unpriced_live or unpriced_hist:
            extra += "　✱ 含缺价条目的行金额只算已配价部分"
        # 数字来源标注：● = 站方真值（实付），无记号 = Hermes 估算
        _srcs = getattr(self.ledger, "last_srcs", None) or {}
        if _srcs.get("site"):
            extra += "\n● 站方真值：" + "、".join(sorted(_srcs["site"]))
        if _srcs.get("hermes"):
            _hs = sorted(_srcs["hermes"])
            extra += "　｜　其余为 Hermes 估算：" + "、".join(_hs[:6]) + (
                " 等 %d 个" % len(_hs) if len(_hs) > 6 else "")
        # 锚点对照：站方实付 vs 本表估算（有锚点的站才显示）
        anchors = []
        for b in (self.ledger.data.get("bills") or []):
            if b["host"] not in {h for (h, _m) in rows}:
                continue
            rb = self.ledger.resolve_bill(b)
            ours = sum(r2["cost"] for (h2, _m2), r2 in rows.items() if h2 == b["host"])
            k = (rb["amount"] / ours) if ours else 0
            calib = total - ours + rb["amount"]
            anchors.append("[锚点] %s 本表 %s ｜ 站方 %s%.2f（k=%.4f）→ 校准后总合计 %s" % (
                b["host"], fmt_money(ours), rb["cur"], rb["amount"], k, fmt_money(calib)))
        if anchors:
            extra += "\n" + "　".join(anchors)
        if self.cur in ("30d", "all") and (self.ledger.data.get("approx") or []):
            extra += "　≈ 回填段（%d 天）的时间分布为估算" % len(self.ledger.data.get("approx") or [])
        self.lbl_sum.config(
            text="合计 %s ｜ %d 个条目 ｜ 数据来源：%s%s" % (
                fmt_money(total), len(rows), src, extra))
        # 重算状态
        st = self.ledger.data.get("recompute_stat") or {}
        at = self.ledger.data.get("recomputed_at")
        mode_txt = {"day": "按天", "missing": "只补缺", "all": "全按当前价"}.get(st.get("mode"), "")
        if at:
            self.lbl_rc.config(text="上次重算 %s ｜ %s %d 条（仍缺价 %d，变化 %d）" % (
                at, mode_txt or st.get("mode") or "?", st.get("priced", 0),
                st.get("unpriced", 0), st.get("changed", 0)))
        else:
            self.lbl_rc.config(text="按天重算：每天用当天生效的价格段；改价不会污染历史")
        # 恢复选中行与滚动位置
        try:
            if sel_vals is not None:
                for i in self.tree.get_children():
                    if tuple(self.tree.item(i, "values"))[:2] == tuple(sel_vals)[:2]:
                        self.tree.selection_set(i)
                        break
            self.tree.yview_moveto(yv)
        except Exception:
            pass


class BillDialog:
    """录入站方实付账单（锚点）→ 自动反推整体倍率 / 真单价 → 可选写回价格段。"""

    def __init__(self, root, ledger, host="", model="*"):
        self.ledger = ledger
        self.win = tk.Toplevel(root)
        self.win.title("录入站方实付")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        apply_icon(self.win)
        apply_dark_title_bar(self.win, bg_hex=BG)
        style_child_window(self.win, tree=False)   # ★ 2026-10-09 统一子窗口外观
        self.win.resizable(False, False)
        self.win.transient(root)

        def frm(r):
            f = tk.Frame(self.win, bg=BG)
            f.grid(row=r, column=0, columnspan=6, sticky="we", padx=12, pady=3)
            return f

        def lab(f, t, dim=False):
            tk.Label(f, text=t, bg=BG, fg=(DIM if dim else FG),
                     font=FONT_S).pack(side="left")

        def ent(f, val="", w=12):
            e = tk.Entry(f, width=w, font=FONT_S, bg="#26262c", fg=FG,
                         insertbackground=FG, bd=0, highlightthickness=1,
                         highlightbackground="#3a3a42", highlightcolor=BLUE)
            if val not in ("", None):
                e.insert(0, str(val))
            e.pack(side="left", padx=2)
            return e

        tk.Label(self.win, text="录一次站方账单，自动反推倍率 / 真单价（估算额按价格表算）",
                 bg=BG, fg=FG, font=FONT).grid(row=0, column=0, columnspan=6,
                                               sticky="w", padx=12, pady=(10, 2))
        f = frm(1)
        lab(f, "站")
        self.e_host = ent(f, host, 26)
        lab(f, "  模型")
        self.e_model = ent(f, model, 22)

        f = frm(2)
        lab(f, "时间段")
        self.e_from = ent(f, "", 11)
        lab(f, " ~ ")
        self.e_to = ent(f, "", 11)
        lab(f, "  实付")
        self.e_amt = ent(f, "", 10)
        lab(f, "¥", True)

        tk.Label(self.win, text="分项 token（可选：填了就用站方口径反解；跨调价段请分行填）",
                 bg=BG, fg=DIM, font=FONT_S).grid(row=3, column=0, columnspan=6,
                                                  sticky="w", padx=12, pady=(6, 0))
        self.frm_tok = tk.Frame(self.win, bg=BG)
        self.frm_tok.grid(row=4, column=0, columnspan=6, sticky="we", padx=12)
        self.tok_rows = []
        self._build_tok_rows(host, model)
        fb = tk.Frame(self.win, bg=BG)
        fb.grid(row=5, column=0, columnspan=6, sticky="w", padx=12, pady=(2, 0))
        tk.Button(fb, text="重新读取价格段", bd=0, font=FONT_S, bg="#2a2a30", fg=FG,
                  activebackground="#333", activeforeground=FG,
                  command=lambda: self._build_tok_rows(
                      self.e_host.get().strip(), self.e_model.get().strip() or "*")
                  ).pack(side="left")

        f = frm(6)
        lab(f, "备注")
        self.e_note = ent(f, "", 46)

        self.lbl = tk.Label(self.win, text="", bg=BG, fg=YELLOW, font=FONT_S,
                            anchor="w", justify="left", wraplength=580)
        self.lbl.grid(row=7, column=0, columnspan=6, sticky="w", padx=12, pady=(6, 0))

        f = tk.Frame(self.win, bg=BG)
        f.grid(row=8, column=0, columnspan=6, sticky="e", padx=12, pady=(8, 12))
        tk.Button(f, text="保存并反解", command=self.save_bill, bg="#2f5d3a", fg=FG, bd=0,
                  activebackground="#3a7248", activeforeground=FG,
                  font=FONT_S).pack(side="left", padx=4)
        tk.Button(f, text="取消", command=self.win.destroy, bg="#2a2a30", fg=FG, bd=0,
                  activebackground="#333", activeforeground=FG,
                  font=FONT_S).pack(side="left")

    def _build_tok_rows(self, host, model):
        """按该站的价格段动态生成 token 输入行（跨调价也能精确反解）。"""
        for w in self.frm_tok.winfo_children():
            w.destroy()
        self.tok_rows = []
        segs = []
        try:
            if self.ledger.prices:
                entry = self.ledger.prices.entry_of(host, model or "*")
                segs = self.ledger.prices.segs(entry or {})
        except Exception:
            segs = []
        froms = [p.get("from") or "" for p in segs] or [""]
        for f in froms:
            r = tk.Frame(self.frm_tok, bg=BG)
            r.pack(fill="x", pady=1)
            tk.Label(r, text="段 %-12s" % (f or "(全天)"), bg=BG, fg=FG,
                     font=FONT_S, width=17, anchor="w").pack(side="left")
            tk.Label(r, text="未命中", bg=BG, fg=DIM, font=FONT_S).pack(side="left")

            def mk(w):
                e = tk.Entry(r, width=w, font=FONT_S, bg="#26262c", fg=FG,
                             insertbackground=FG, bd=0, highlightthickness=1,
                             highlightbackground="#3a3a42", highlightcolor=BLUE)
                e.pack(side="left", padx=2)
                return e

            e1 = mk(13)
            tk.Label(r, text="缓存", bg=BG, fg=DIM, font=FONT_S).pack(side="left")
            e2 = mk(13)
            tk.Label(r, text="输出", bg=BG, fg=DIM, font=FONT_S).pack(side="left")
            e3 = mk(11)
            self.tok_rows.append((f, e1, e2, e3))

    def save_bill(self):
        host = self.e_host.get().strip()
        if not host:
            self.lbl.config(text="⚠ 「站」要填，例如 tokenrhythm.studio", fg=RED)
            return
        try:
            amt = float(self.e_amt.get().strip() or 0)
        except ValueError:
            self.lbl.config(text="⚠ 实付金额填数字", fg=RED)
            return
        if amt <= 0:
            self.lbl.config(text="⚠ 实付金额要大于 0", fg=RED)
            return

        def iv(w):
            try:
                return int(float(w.get().strip() or 0))
            except ValueError:
                return 0

        bill = {"host": host, "model": self.e_model.get().strip() or "*",
                "from": self.e_from.get().strip(), "to": self.e_to.get().strip(),
                "amount": amt, "cur": "¥", "note": self.e_note.get().strip()}
        by = {}
        for f, e1, e2, e3 in self.tok_rows:
            mm, cc, oo = iv(e1), iv(e2), iv(e3)
            if mm or cc or oo:
                by[f] = {"miss": mm, "cache": cc, "out": oo}
        if by:
            if len(by) == 1 and "" in by:
                bill["tokens"] = by[""]
            else:
                bill["tokens_by_seg"] = by
        self.ledger.add_bill(bill)
        r = self.ledger.resolve_bill(bill)
        k = r.get("k") or 0
        txt = "估算 ¥%.2f ｜ 实付 ¥%.2f ｜ 整体倍率 k=%.4f\n（token 来源：%s）" % (
            r["est"], r["amount"], k, r.get("tokens_src"))
        for it in (r.get("items") or []):
            txt += "\n  %s：单价 %s ｜ 估算 ¥%.2f" % (it["label"], it["price"], it["est"])
        if r.get("seg_rows"):
            for sr in r["seg_rows"]:
                txt += "\n  · 段 %s：估算 ¥%.2f ｜ 分摊实付 ¥%.2f" % (
                    sr["seg_from"] or "(最早)", sr["est"], sr.get("amount_share") or 0)

        if k and abs(k - 1.0) > 0.005:      # 0.5% 以内视为一致，不值得动价格
            if messagebox.askyesno(
                    "反解结果",
                    txt + "\n\n要把倍率 %.4f 填进价格表吗？\n"
                    "（多段条目会把各段都填上这个倍率）" % k, parent=self.win):
                ok = self.ledger.apply_bill_ratio(bill, k)
                self.lbl.config(
                    text=("✅ 已把倍率 %.4f 写入 %s 的价格段（可在价格表里核对）" % (k, host))
                    if ok else "⚠ 该站还没有价格条目，先去价格表建一条再应用", fg=GREEN)
                return
        else:
            messagebox.showinfo(
                "反解结果",
                txt + "\n\nk ≈ 1.0（差 %.2f%%）：标价与站方账单基本一致，不需要填倍率。"
                % (abs(k - 1.0) * 100 if k else 0), parent=self.win)
        self.win.destroy()


# ─────────────── 提示注入：预设库（hints.json） ───────────────
# 存在我们自己的数据目录（%APPDATA%\HermesCacheMonitor\），不碰 Hermes 任何文件。

HINT_BUILTIN_NAME = "无注入"


def hint_presets_path():
    """预设库路径。放自己数据目录，与 Hermes 完全隔离。"""
    return os.path.join(_appdata_dir(), "hints.json")


def hint_confirm_text(preset):
    """构造「注入确认框」的标题句 + 正文（菜单与管理窗共用）。"""
    kind = preset.get("kind")
    text = (preset.get("text") or "").rstrip()
    if kind == "none":
        return ("确定删除 environment_hint（回到无注入）？",
                "当前内容会被移除，下次开新会话就不再注入。")
    lines = text.split("\n")
    return ("确定注入「%s」？" % preset.get("name"),
            "首行：%s\n共 %d 字符 / %d 行" % (
                (lines[0][:48] if lines and lines[0] else "（空内容）"), len(text), len(lines)))


def apply_hint_preset_direct(preset, parent):
    """不起管理窗，直接对一个预设走「确认 → 写入」。菜单点预设用这条。"""
    store = HintStore()
    store.load()
    head, body = hint_confirm_text(preset)
    if not messagebox.askokcancel(
            "提示注入", "%s\n\n%s\n\n⚠ 只对**新开**的会话生效。" % (head, body), parent=parent):
        return False
    if preset.get("kind") == "none":
        ok, msg = write_hint("", remove=True)
    else:
        ok, msg = write_hint((preset.get("text") or "").rstrip())
    if ok:
        store.active = preset.get("name")
        store.save()
        _HINT_CACHE["t"] = 0.0
        messagebox.showinfo("提示注入", "%s\n\n开新会话后生效。" % msg, parent=parent)
    else:
        messagebox.showerror("提示注入", msg, parent=parent)
    return ok


class HintStore:
    """environment_hint 预设库。

    initial 记「首次运行本功能时」config 的原始状态（方案 A，「恢复初始」用）；
    active 只是便利缓存，**真值以 config.yaml 实时读为准**（手动改过 config 也不会显示错）。
    """

    def __init__(self, path=None):
        self.path = path or hint_presets_path()
        self.version = 1
        self.initial = {"present": False, "raw_block": ""}
        self.presets = []
        self.active = HINT_BUILTIN_NAME
        # 非空 = 「文件在、但读不了」→ 禁止写回（否则会把预设库覆盖成空库）
        self._load_error = ""

    # ---------- 读写 ----------
    def load(self):
        """读预设库。

        ⚠ 关键：文件不存在（首次运行）才允许「建库并落盘」；
        读失败（被同步工具锁住 / 文件损坏）必须直接返回、**绝不写回** ——
        原实现不分情况一律 save()，会把用户的预设（含万字大块）清空（实测复现）。
        """
        self._load_error = ""
        d = None
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
        except FileNotFoundError:
            d = None                    # 首次运行 → 建库
        except Exception as ex:
            self._load_error = "预设库读失败：%s" % ex
            return                      # ← 直接返回，不 save
        if not isinstance(d, dict):
            # 文件不存在、或内容不是预期结构 → 从空库起步
            self.presets = []
            self.active = HINT_BUILTIN_NAME
            self._ensure_builtin()
            self.first_run_capture()
            self.save()
            return
        self.version = d.get("version") or 1
        ini = d.get("initial")
        self.initial = ini if isinstance(ini, dict) else {"present": False, "raw_block": ""}
        ps = d.get("presets")
        self.presets = ([p for p in ps if isinstance(p, dict) and p.get("name")]
                        if isinstance(ps, list) else [])
        self.active = d.get("active") or HINT_BUILTIN_NAME
        self._ensure_builtin()
        self.first_run_capture()

    def save(self):
        """原子写：临时文件 + os.replace（避免写一半被 kill 变空文件）。

        读失败（_load_error 非空）时拒绝写盘 —— 否则会用空库覆盖原文件。
        """
        if self._load_error:
            _startup_log("预设库保存被拒（%s）" % self._load_error)
            return False
        d = {
            "version": self.version,
            "initial": dict(self.initial or {}),
            "presets": list(self.presets),
            "active": self.active or HINT_BUILTIN_NAME,
        }
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except Exception:
            pass
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return True
        except Exception:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    # ---------- 初始状态（方案 A） ----------
    def first_run_capture(self):
        """只在「还没抓过」时抓一次 config 当前状态。抓完立刻落盘（防没保存就关）。"""
        if self.initial.get("captured"):
            return False
        t, present, p = read_hint()
        if not p:
            return False            # 找不到 config，不写死，下次再试
        self.initial = {
            "present": bool(present),
            "raw_block": t or "",
            "captured": True,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save()
        return True

    # ---------- 预设增删改查 ----------
    def _ensure_builtin(self):
        """保证「无注入」这条存在且不可删（kind=none → 删键）。"""
        for p in self.presets:
            if p.get("name") == HINT_BUILTIN_NAME:
                p["kind"] = "none"
                p["builtin"] = True
                p.setdefault("text", "")
                return
        self.presets.insert(0, {"name": HINT_BUILTIN_NAME, "kind": "none",
                                "builtin": True, "text": ""})

    def list_presets(self):
        self._ensure_builtin()
        return self.presets

    def get(self, name):
        for p in self.presets:
            if p.get("name") == name:
                return p
        return None

    def upsert(self, name, text, old_name=None):
        """新增 / 改名 / 改内容。返回 (ok, message)。

        old_name=None → **新建语义**：同名已存在则拒绝（GUI「新建」走这条）。
        old_name=某名  → 编辑语义：把那条改成 name（改名也走这条）。
        """
        name = (name or "").strip()
        if not name:
            return False, "预设名不能为空"
        if len(name) > 40:
            return False, "预设名太长（≤40 字符）"
        existing = self.get(name)
        if old_name is None:
            if existing is not None:
                if existing.get("builtin"):
                    return False, ("「%s」是内置项，不能改名或改内容\n"
                                   "（它的作用就是「删除注入、回到初始」）" % HINT_BUILTIN_NAME)
                return False, "已存在同名预设「%s」" % name
            cur = None
        else:
            cur = self.get((old_name or "").strip())
            if cur is None:
                return False, "找不到要修改的预设「%s」" % old_name
            if cur.get("builtin"):
                return False, ("「%s」是内置项，不能改名或改内容\n"
                               "（它的作用就是「删除注入、回到初始」）" % HINT_BUILTIN_NAME)
            if existing is not None and existing is not cur:
                return False, "已存在同名预设「%s」" % name
        if cur is not None:
            cur["text"] = text or ""
            cur["kind"] = "text"
            cur["name"] = name
        else:
            self.presets.append({"name": name, "kind": "text",
                                 "builtin": False, "text": text or ""})
        self.save()
        return True, "已保存「%s」" % name

    def delete(self, name):
        p = self.get(name)
        if p is None:
            return False, "没有「%s」" % name
        if p.get("builtin"):
            return False, "「%s」是内置项，不能删" % HINT_BUILTIN_NAME
        self.presets = [x for x in self.presets if x.get("name") != name]
        if self.active == name:
            self.active = HINT_BUILTIN_NAME
        self.save()
        return True, "已删除「%s」" % name

    # ---------- 生效状态（实时读 config，不信 active 字段） ----------
    def current_active(self):
        """返回当前生效的预设名；比不中 → 「（自定义/外部修改）」。"""
        t, present, p = read_hint()
        if not p:
            return "（未找到 config）"
        if not present:
            return HINT_BUILTIN_NAME
        for pr in self.list_presets():
            if pr.get("kind") == "text" and \
                    (pr.get("text") or "").rstrip() == (t or "").rstrip():
                return pr.get("name")
        return "（自定义/外部修改）"


class HintDialog:
    """提示注入管理窗（结构照抄 PriceDialog：Toplevel + 确认框 + 底部按钮）。"""

    def __init__(self, root, on_changed=None):
        self.root = root
        self.on_changed = on_changed
        self.store = HintStore()
        self.store.load()
        self._edit_name = None

        self.win = tk.Toplevel(root)
        self.win.title("提示注入（environment_hint）")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        apply_icon(self.win)
        apply_dark_title_bar(self.win, bg_hex=BG)
        style_child_window(self.win, tree=False)   # ★ 2026-10-09 统一子窗口外观
        self.win.resizable(False, False)
        self.win.transient(root)

        self.lbl_cur = tk.Label(self.win, text="", bg=BG, fg=YELLOW, font=FONT,
                                anchor="w", justify="left")
        self.lbl_cur.grid(row=0, column=0, columnspan=2, sticky="w",
                          padx=12, pady=(10, 2))

        # 左：预设列表
        lf = tk.Frame(self.win, bg=BG)
        lf.grid(row=1, column=0, sticky="ns", padx=(12, 6), pady=4)
        tk.Label(lf, text="预设", bg=BG, fg=DIM, font=FONT_S, anchor="w").pack(fill="x")
        self.lst = tk.Listbox(lf, width=17, height=11, bg="#26262c", fg=FG, font=FONT,
                              bd=0, highlightthickness=1, highlightbackground="#3a3a42",
                              highlightcolor=BLUE, selectbackground="#3a5a7a",
                              selectforeground=FG, activestyle="none")
        self.lst.pack()
        self.lst.bind("<<ListboxSelect>>", self.on_pick)

        # 右：名字 + 内容
        rf = tk.Frame(self.win, bg=BG)
        rf.grid(row=1, column=1, sticky="nsew", padx=(0, 12), pady=4)
        nf = tk.Frame(rf, bg=BG)
        nf.pack(fill="x")
        tk.Label(nf, text="名字", bg=BG, fg=FG, font=FONT).pack(side="left")
        self.e_name = tk.Entry(nf, width=20, font=FONT, bg="#26262c", fg=FG,
                               insertbackground=FG, bd=0, highlightthickness=1,
                               highlightbackground="#3a3a42", highlightcolor=BLUE)
        self.e_name.pack(side="left", padx=4)
        self.lbl_count = tk.Label(nf, text="0 字符", bg=BG, fg=DIM, font=FONT_S)
        self.lbl_count.pack(side="left", padx=(6, 0))

        self.txt = tk.Text(rf, width=46, height=9, bg="#26262c", fg=FG, font=FONT,
                           insertbackground=FG, bd=0, highlightthickness=1,
                           highlightbackground="#3a3a42", highlightcolor=BLUE,
                           wrap="word", undo=True)
        self.txt.pack(fill="both", expand=True, pady=(4, 0))
        self.txt.bind("<KeyRelease>", lambda _e: self._refresh_count())

        tk.Label(rf, text="⚠ 需开新会话生效（当前会话的系统提示已冻结，不受影响）",
                 bg=BG, fg="#c98a4b", font=FONT_S, anchor="w",
                 justify="left", wraplength=340).pack(fill="x", pady=(4, 0))

        self.lbl_msg = tk.Label(self.win, text="", bg=BG, fg=YELLOW, font=FONT_S,
                                anchor="w", justify="left", wraplength=440)
        self.lbl_msg.grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(6, 0))

        bf = tk.Frame(self.win, bg=BG)
        bf.grid(row=3, column=0, columnspan=2, sticky="e", padx=12, pady=(6, 12))
        for text, cmd, color in (
                ("注入选中", self.inject, "#2f5d3a"),
                ("新建", self.new_preset, "#2a2a30"),
                ("保存修改", self.save_preset, "#2a2a30"),
                ("删除", self.del_preset, "#2a2a30"),
                ("恢复初始", self.restore_initial, "#5a3a2a"),
                ("关闭", self.win.destroy, "#2a2a30")):
            tk.Button(bf, text=text, command=cmd, bg=color, fg=FG, bd=0,
                      activebackground="#3a3a42", activeforeground=FG, font=FONT,
                      width=9).pack(side="left", padx=3)

        self.reload_list()
        try:
            self.win.grab_set()
        except Exception:
            pass

    # ---------- 列表与选中 ----------
    def reload_list(self, keep=None):
        try:
            cur = self.store.current_active()
        except Exception:
            cur = ""
        try:
            self.lbl_cur.config(text="当前生效：%s" % cur, fg=YELLOW)
        except Exception:
            pass
        try:
            self.lst.delete(0, "end")
        except Exception:
            pass
        names = []
        for p in self.store.list_presets():
            n = p.get("name")
            names.append(n)
            mark = "● " if n == cur else ("✓ " if p.get("builtin") else "   ")
            self.lst.insert("end", "%s%s" % (mark, n))
        if not names:
            return
        idx = names.index(keep) if (keep and keep in names) else 0
        self.lst.selection_clear(0, "end")
        self.lst.selection_set(idx)
        self.lst.activate(idx)
        self.on_pick()

    def sel_name(self):
        sel = self.lst.curselection()
        if not sel:
            return None
        s = self.lst.get(sel[0])
        for pre in ("● ", "✓ ", "   "):
            if s.startswith(pre):
                s = s[len(pre):]
                break
        return s.strip()

    def on_pick(self, _ev=None):
        name = self.sel_name()
        if not name:
            return
        p = self.store.get(name)
        if p is None:
            return
        self._edit_name = name
        self.e_name.delete(0, "end")
        self.e_name.insert(0, name)
        self.txt.delete("1.0", "end")
        if p.get("kind") == "text":
            self.txt.insert("1.0", p.get("text") or "")
        self._refresh_count()
        if p.get("builtin"):
            self.lbl_msg.config(
                text="「%s」= 删除注入、回到初始状态（内置项，内容不可改）" % HINT_BUILTIN_NAME,
                fg=DIM)
        else:
            self.lbl_msg.config(text="", fg=YELLOW)

    def _refresh_count(self):
        t = self.txt.get("1.0", "end-1c")
        try:
            self.lbl_count.config(text="%d 字符 / %d 行" % (
                len(t), (t.count("\n") + 1) if t else 0))
        except Exception:
            pass
        return t

    # ---------- 按钮动作 ----------
    def inject(self):
        name = self.sel_name()
        if not name:
            self.lbl_msg.config(text="⚠ 先在左边选一个预设", fg=RED)
            return
        p = self.store.get(name)
        if p is None:
            return
        head, body = hint_confirm_text(p)
        if not messagebox.askokcancel(
                "提示注入", "%s\n\n%s\n\n⚠ 只对**新开**的会话生效。" % (head, body),
                parent=self.win):
            return
        if p.get("kind") == "none":
            ok, msg = write_hint("", remove=True)
        else:
            ok, msg = write_hint((p.get("text") or "").rstrip())
        if ok:
            self.store.active = name
            self.store.save()
            _HINT_CACHE["t"] = 0.0
            if self.on_changed:
                self.on_changed()
            self.reload_list(keep=name)
            self.lbl_msg.config(text="✓ %s　（开新会话后生效）" % msg.replace("\n", " "),
                                fg=GREEN)
        else:
            self.lbl_msg.config(text="⚠ %s" % msg.replace("\n", " "), fg=RED)

    def new_preset(self):
        self.lst.selection_clear(0, "end")
        self._edit_name = None
        self.e_name.delete(0, "end")
        self.txt.delete("1.0", "end")
        self._refresh_count()
        try:
            self.e_name.focus_set()
        except Exception:
            pass
        self.lbl_msg.config(text="填名字和内容 → 点「保存修改」即新建（再点「注入选中」生效）",
                            fg=BLUE)

    def save_preset(self):
        name = self.e_name.get().strip()
        text = self.txt.get("1.0", "end-1c")
        p = self.store.get(self._edit_name) if self._edit_name else None
        if p is not None and p.get("builtin"):
            self.lbl_msg.config(text="⚠ 「%s」是内置项，不能改" % HINT_BUILTIN_NAME, fg=RED)
            return
        ok, msg = self.store.upsert(name, text, old_name=self._edit_name)
        if not ok:
            self.lbl_msg.config(text="⚠ %s" % msg.replace("\n", " "), fg=RED)
            return
        self._edit_name = name
        self.reload_list(keep=name)
        self.lbl_msg.config(text="✓ %s（还没注入，点「注入选中」才生效）" % msg, fg=GREEN)

    def del_preset(self):
        name = self.sel_name()
        if not name:
            return
        p = self.store.get(name)
        if p is not None and p.get("builtin"):
            self.lbl_msg.config(text="⚠ 「%s」是内置项，不能删" % HINT_BUILTIN_NAME, fg=RED)
            return
        if not messagebox.askyesno(
                "删除预设", "删除预设「%s」？\n（只删这条预设，不动 config.yaml）" % name,
                parent=self.win):
            return
        ok, msg = self.store.delete(name)
        self._edit_name = None
        self.reload_list()
        self.lbl_msg.config(text=("✓ " if ok else "⚠ ") + msg, fg=GREEN if ok else RED)

    def restore_initial(self):
        ini = self.store.initial or {}
        present = bool(ini.get("present"))
        raw = ini.get("raw_block") or ""
        if not present:
            head = "初始状态 = environment_hint 键不存在"
            body = "执行后等价于「无注入」（删除该键）。"
        else:
            head = "初始状态（首次运行本功能时抓到的）"
            body = "共 %d 字符 / %d 行：\n%s%s" % (
                len(raw), raw.count("\n") + 1, raw[:180], "…" if len(raw) > 180 else "")
        if not messagebox.askokcancel(
                "恢复初始",
                "%s\n\n%s\n\n记于：%s\n\n确定写回？" % (head, body, ini.get("at") or "（未知）"),
                parent=self.win):
            return
        if present:
            ok, msg = write_hint(raw)
        else:
            ok, msg = write_hint("", remove=True)
        if ok:
            _HINT_CACHE["t"] = 0.0
            if self.on_changed:
                self.on_changed()
            self.reload_list()
            self.lbl_msg.config(text="✓ 已恢复初始状态（开新会话后生效）", fg=GREEN)
        else:
            self.lbl_msg.config(text="⚠ %s" % msg.replace("\n", " "), fg=RED)


class App:
    def __init__(self):
        self.mon = Monitor()
        self.include_gap = False
        self.mon.on_gap_toggle = self._set_include_gap
        self.last_d = None
        self._stats = []          # 已打开的成本统计窗口（价格改完主动通知刷新）
        self._hint_dlg = None     # 提示注入管理窗（单例，重复点只抬起）
        self._flash_msg = None    # 状态行临时反馈：(文本, 过期时间, 是否警告)

        cfg = load_config()
        self.alpha = float(cfg.get("alpha", 0.90))
        self.mini_mode = bool(cfg.get("mini_mode", False))
        # ★ 2026-10-09 用户拍板：完整/精简模式**各自记住宽和高**，切换时不互相继承
        #   （旧行为：宽度全局共用 win_w，高度各自记忆 → 宽被继承）
        self.full_w = int(cfg.get("full_w", cfg.get("win_w", 420)))
        _def_h = 286 + (18 if (_uia is None or _NO_UIA or _DATA_SPLIT) else 0)
        self.full_h = int(cfg.get("full_h", _def_h))
        self.mini_w = int(cfg.get("mini_w", cfg.get("win_w", 420)))
        self.mini_h = int(cfg.get("mini_h", 104))
        # 兼容旧字段：win_w 作为「当前模式的宽」的别名（其他代码仍在读它）
        self.win_w = self.mini_w if self.mini_mode else self.full_w
        # ★ 2026-10-09 多屏：记住窗口位置（可为负 = 副屏在主屏左边/上方）
        #   None = 没存过 → 首次运行走「主屏右上角」的默认位置
        self.pos_x = cfg.get("pos_x")
        self.pos_y = cfg.get("pos_y")
        try:
            self.pos_x = None if self.pos_x is None else int(self.pos_x)
            self.pos_y = None if self.pos_y is None else int(self.pos_y)
        except (TypeError, ValueError):
            self.pos_x = self.pos_y = None
        self.cur_x = self.pos_x
        self.cur_y = self.pos_y

        self.root = tk.Tk()
        self.root.title("缓存跟随监控 v%s" % APP_VERSION)
        _startup_log("启动: icon_b64=%d, hermes_home=%s, data_dir=%s, no_uia=%s, profile=%s" % (
            len(_ICON_B64), HERMES_HOME or "(未找到)", WORK_DIR, _NO_UIA, _PROFILE))
        apply_icon(self.root)
        apply_dark_title_bar(self.root, bg_hex=BG)
        # ★ 2026-10-09 界面优化：去掉整条系统标题栏（省 32px 高度）。
        #   用户拍板：只用得到最小化/×，已全部移入右键菜单；
        #   拖动由 bind_window_drag() 接管（留出非交互文字区）。
        try:
            self.root.overrideredirect(True)
        except Exception as e:
            _startup_log("去掉系统标题栏失败（不影响功能）：%s" % e)
        _dx0, _dy0, _dx1, _dy1 = virtual_screen(self.root)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        try:
            self.root.attributes("-alpha", self.alpha)
        except Exception:
            pass
        # ★ 2026-10-09 修：上面设 -alpha 会把 WS_EX_TOOLWINDOW 冲掉 → 窗口会进任务栏。
        #   这里补回来（详见 keep_out_of_taskbar 的注释与实测取证）。
        #   ⚠️ 为什么用**轮询**而不是定时几次（实测教训）：
        #      exe 版首次启动要 **8.4 秒**才把窗口显示出来（源码版只要 0.3 秒），
        #      固定延时（30/300/1000/2500ms）全都跑在窗口出现**之前**，等于没补。
        #      改成：从 0.2 秒起每 0.4 秒补一次、连补 40 次（≈16 秒），
        #      只要窗口在就不受启动快慢影响。幂等操作，重复调用无副作用。
        self._keep_tb_tries = 0

        def _keep_tb_loop():
            try:
                keep_out_of_taskbar(self.root)
            except Exception:
                pass
            self._keep_tb_tries += 1
            # 前 8 次（≈3.2 秒）密集补 —— 覆盖 alpha 设置与窗口映射
            # 之后拉长到 1.5 秒一次，直到 40 次用完（≈45 秒），覆盖冷启动慢的情况
            if self._keep_tb_tries < 40:
                delay = 400 if self._keep_tb_tries < 8 else 1500
                try:
                    self.root.after(delay, _keep_tb_loop)
                except Exception:
                    pass

        self.root.after(200, _keep_tb_loop)

        # === 完整模式容器 ===
        self.frm_full = tk.Frame(self.root, bg=BG)

        # ── 底部固定区（先 side='bottom' 打包，焊死在窗口底端，无论上面文字多长都不被顶掉）──
        warn = []
        if _uia is None:
            warn.append("UIA 不可用（跟随变慢）→ pip install uiautomation")
        elif _NO_UIA:
            warn.append("UIA 已手动禁用（--no-uia）")
        if _DATA_SPLIT:
            warn.append("检测到两份数据，命令行请用 py 运行")
        self.lbl_warn = tk.Label(self.frm_full, text="⚠ " + "　⚠ ".join(warn) if warn else "",
                                 bg=BG, fg="#c98a4b", font=FONT_S, anchor="w",
                                 justify="left", wraplength=390)
        if warn:
            self.lbl_warn.pack(side="bottom", fill="x", padx=12, pady=(0, 2))

        self.lbl_footer = tk.Label(self.frm_full, text="", bg=BG, fg=DIM, font=FONT_S,
                                   anchor="w", cursor="hand2")
        self.lbl_footer.pack(side="bottom", fill="x", padx=12, pady=(2, 6))
        self.lbl_footer.bind("<Button-1>", self.on_footer_click)

        frm_tools = tk.Frame(self.frm_full, bg=BG)
        frm_tools.pack(side="bottom", fill="x", padx=12, pady=(2, 2))
        self.lbl_stats = tk.Label(frm_tools, text="📊 成本统计", bg=BG, fg=BLUE,
                                  font=FONT_S, cursor="hand2")
        self.lbl_stats.pack(side="left")
        self.lbl_stats.bind("<Button-1>", lambda _e: self.open_stats())
        self.use_calibration = True
        self.lbl_calib = tk.Label(frm_tools, text="⚖ 校准", bg=BG, fg=BLUE,
                                  font=FONT_S, cursor="hand2")
        self.lbl_calib.pack(side="left", padx=(8, 0))
        self.lbl_calib.bind("<Button-1>", self.on_calib_click)
        self.lbl_prices = tk.Label(frm_tools, text="⚙ 价格表", bg=BG, fg=BLUE,
                                   font=FONT_S, cursor="hand2")
        self.lbl_prices.pack(side="right")
        self.lbl_prices.bind("<Button-1>", lambda _e: self.on_cost_right())
        self.lbl_hint = tk.Label(frm_tools, text="💬 提示注入", bg=BG, fg=BLUE,
                                 font=FONT_S, cursor="hand2")
        self.lbl_hint.pack(side="right", padx=(0, 10))
        self.lbl_hint.bind("<Button-1>", lambda _e: self.open_hint())
        self.lbl_refresh = tk.Label(frm_tools, text="🔄 刷新", bg=BG, fg=BLUE,
                                    font=FONT_S, cursor="hand2")
        self.lbl_refresh.pack(side="right", padx=(0, 10))
        self.lbl_refresh.bind("<Button-1>", self.do_refresh)

        self.frm_gap = tk.Frame(self.frm_full, bg=BG)
        self.frm_gap.pack(side="bottom", fill="x", padx=12, pady=(2, 0))
        self.lbl_gap = tk.Label(self.frm_gap, text="", bg=BG, fg="#c98a4b", font=FONT_S,
                                anchor="w", justify="left", cursor="hand2")
        self.lbl_gap.pack(side="left")
        self.lbl_gap.bind("<Button-1>", self.on_gap_click)
        self.lbl_gap_detail = tk.Label(self.frm_gap, text="明细 ▸", bg=BG, fg=BLUE,
                                       font=FONT_S, cursor="hand2")
        self.lbl_gap_detail.pack(side="right", padx=(6, 0))
        self.lbl_gap_detail.bind("<Button-1>", self.on_gap_detail)

        # ── 顶部内容区（从上往下排列，文字自动换行）──
        frm_header = tk.Frame(self.frm_full, bg=BG)
        frm_header.pack(side="top", fill="x", padx=12, pady=(8, 0))
        # ★ 先 pack 右侧缩小按钮：保证绝对不会被左侧长文本挤掉！
        self.btn_mini = tk.Label(frm_header, text="🗕", bg=BG, fg=DIM,
                                 font=("Segoe UI Symbol", 11), cursor="hand2")
        self.btn_mini.pack(side="right", padx=(6, 0))
        self.btn_mini.bind("<Button-1>", lambda _e: self.toggle_mini_mode())
        # 左侧标题后 pack
        self.lbl_title = tk.Label(frm_header, text="...", bg=BG, fg=FG,
                                  font=("Microsoft YaHei UI", 10), anchor="w", justify="left",
                                  wraplength=max(140, self.win_w - 60))
        self.lbl_title.pack(side="left", fill="x", expand=True)

        # ★ 2026-10-09：命中率 + 今日站点消费同一行
        #   用户要求「今日站点消费放在缓存命中边上」——共行不额外占高度。
        frm_rate = tk.Frame(self.frm_full, bg=BG)
        frm_rate.pack(side="top", fill="x", padx=12)
        self.lbl_rate = tk.Label(frm_rate, text="--", bg=BG, fg=GREEN, font=FONT_L, anchor="w")
        self.lbl_rate.pack(side="left")
        # 数据源 = 站点监控 store.db 当天实扣合计（today_site_total）。
        self.lbl_today = tk.Label(frm_rate, text="", bg=BG, fg=DIM, font=FONT_CODE_S,
                                  anchor="e", justify="right")
        self.lbl_today.pack(side="right")

        self.lbl_detail = tk.Label(self.frm_full, text="", bg=BG, fg=DIM, font=FONT,
                                   anchor="w", justify="left", wraplength=max(180, self.win_w - 28))
        self.lbl_detail.pack(side="top", fill="x", padx=12)

        self.lbl_cost = tk.Label(self.frm_full, text="", bg=BG, fg=YELLOW, font=FONT_CODE,
                                 anchor="w", justify="left", cursor="hand2", wraplength=max(180, self.win_w - 28))
        self.lbl_cost.pack(side="top", fill="x", padx=12, pady=(2, 0))
        # ★ 2026-10-09 用户要求：成本行「双击」才弹价格配置窗（单击太容易误触）
        #   注意：Tk 的双击会先发两次 <Button-1> 再发 <Double-Button-1>，
        #   所以**必须摘掉单击绑定**，否则单击就会弹窗，双击还会弹两次。
        self.lbl_cost.bind("<Double-Button-1>", self.on_cost_click)
        # 成本行的右键不要任何动作（原来弹价格管理表，取消）；连主菜单也不弹 → 吞掉事件
        self.lbl_cost.bind("<Button-3>", lambda _e: "break")

        # === 迷你模式容器（★ 2026-10-09 极简三行改造） ===
        #   用户拍板：只留 ①命中率 ②缓存/未缓存 token ③校准后金额，外加今日站点消费；
        #   「相加相减/已校准/子代理/缓存省」这些过程文字一律不显示。
        self.frm_mini = tk.Frame(self.root, bg=BG)
        frm_mini_top = tk.Frame(self.frm_mini, bg=BG)
        frm_mini_top.pack(fill="x", padx=10, pady=(6, 2))

        # ★ 先 pack 右侧展开按钮：保证绝对不被左边文字挤掉！
        self.btn_expand = tk.Label(frm_mini_top, text="🗖", bg=BG, fg=DIM,
                                   font=("Segoe UI Symbol", 11), cursor="hand2")
        self.btn_expand.pack(side="right", padx=(6, 0))
        self.btn_expand.bind("<Button-1>", lambda _e: self.toggle_mini_mode())

        # 今日站点消费（精简模式也显示，靠右）
        self.mini_lbl_today = tk.Label(frm_mini_top, text="", bg=BG, fg=DIM,
                                       font=FONT_CODE_S)
        self.mini_lbl_today.pack(side="right", padx=(6, 8))

        self.mini_lbl_rate = tk.Label(frm_mini_top, text="--", bg=BG, fg=GREEN,
                                      font=FONT_L)
        self.mini_lbl_rate.pack(side="left")

        self.mini_lbl_tokens = tk.Label(self.frm_mini, text="", bg=BG, fg=FG,
                                        font=FONT_CODE_S, anchor="w",
                                        justify="left", wraplength=max(100, self.win_w - 24))
        self.mini_lbl_tokens.pack(fill="x", padx=10)

        self.mini_lbl_cost = tk.Label(self.frm_mini, text="", bg=BG, fg=YELLOW,
                                      font=FONT_CODE, anchor="w", justify="left",
                                      wraplength=max(180, self.win_w - 28), cursor="hand2")
        self.mini_lbl_cost.pack(fill="x", padx=10, pady=(0, 6))
        self.mini_lbl_cost.bind("<Double-Button-1>", self.on_cost_click)
        # 成本行右键不要任何动作（原来弹价格管理表，取消）
        self.mini_lbl_cost.bind("<Button-3>", lambda _e: "break")

        # 初始打包与尺寸
        #   ★ 2026-10-09 修正：原来这里写死 max(104,…)/max(280,…)，与拉伸下限
        #   （min_w=240/min_h=80）不一致；统一走 _MIN_* 常量，避免"手动能缩到 240、
        #   一切换就被抬回去"的割裂感。
        #   ★ 2026-10-09 多屏修正：位置改走 clamp_to_desktop（虚拟桌面），
        #   并支持恢复上次位置（pos_x/pos_y）—— 原来每次都强制回到主屏右上角，
        #   拖到副屏后一重启就"跑回主屏"，看着像"上不了副屏"。
        _dx0, _dy0, _dx1, _dy1 = virtual_screen(self.root)
        _pw = self.win_w
        _ph = (max(_MIN_MINI_H, self.mini_h) if self.mini_mode
               else max(_MIN_FULL_H, self.full_h))
        _px, _py = self.pos_x, self.pos_y
        if _px is None or _py is None:                 # 首次运行 → 主屏右上角
            _px, _py = _dx1 - _pw - 20, _dy0 + 60
        _px, _py = clamp_to_desktop(_px, _py, _pw, _ph, self.root)
        self.cur_x, self.cur_y = _px, _py
        if self.mini_mode:
            self.frm_mini.pack(fill="both", expand=True)
        else:
            self.frm_full.pack(fill="both", expand=True)
        self.root.geometry("%dx%d+%d+%d" % (_pw, _ph, _px, _py))

        # 监听窗口拖拽尺寸调整（自适应换行并记忆）
        self.root.bind("<Configure>", self.on_window_resize)

        # 双击任意控件快速切换极简/完整模式
        #   ★ 注意：成本行（lbl_cost / mini_lbl_cost）**不在此列** —— 它们的双击
        #   已被用户指定为「弹价格配置窗」，不能再兼职切模式。
        #   ★ 必须 return "break"：Tk 的 <Double-Button-1> 会冒泡到 root，而 root
        #   也绑了同一个回调 → 一次双击被触发两次（切过去又切回来 = 看着没反应）。
        #   实测抓出（toggle 调用 = [None, None]）。
        def _on_dbl(_e):
            self.toggle_mini_mode()
            return "break"

        for w in (self.root, self.frm_full, self.frm_mini, self.lbl_title, self.lbl_rate,
                  self.lbl_detail, self.mini_lbl_rate, self.mini_lbl_tokens,
                  self.lbl_today, self.mini_lbl_today):
            w.bind("<Double-Button-1>", _on_dbl)

        # ★ 2026-10-09 无边框拖动 + **四角**拉伸（见 bind_window_drag / enable_border_resize）
        #   拖动：绑在 root 上（Tk 的 Motion 事件发给指针所在控件，只绑 Label 会"只有文字能拖"）
        #   拉伸：★ 用户第三次调整 —— 只要四个角（14px），四条边改成纯拖动区。
        #         原先四边各留 6px 热区，整圈边框都是拉伸感应区，
        #         浮窗小、内容紧贴边缘 → 想拖动时经常按到边上变成拉伸，误触频繁。
        try:
            bind_window_drag(self.root, [self.frm_full, self.frm_mini,
                                         self.lbl_title, self.lbl_detail,
                                         self.lbl_rate, self.lbl_today,
                                         self.mini_lbl_rate, self.mini_lbl_tokens,
                                         self.mini_lbl_today],
                             is_edge=lambda e: bool(self._edge_of(e)))
        except Exception as e:
            _startup_log("拖动绑定失败（不影响功能）：%s" % e)

        # 自绘深色菜单（tk.Menu 在 Windows 上有 2px 浅色白边，实测改不掉 → 自绘）
        self.menu = DarkMenu(self.root, width=214)
        self.menu.add("🗕 最小化到托盘", self.on_close_to_tray)
        self.menu.add_sep()
        self.menu.add("🗕 切换精简/完整模式", self.toggle_mini_mode)
        self.menu.add("成本统计…", self.open_stats)
        self.menu.add("价格表管理…", self.on_cost_right)
        self.menu.add("⚭ 站点归并设置…", self.open_site_merge)
        self.menu.add("🔄 重新对齐对话", self.do_refresh)
        self.menu.add("⚖ 重新加载站方校准", self.on_calib_click)
        self.menu.add("⚡ 从中转站实扣同步价格表", self.do_sync_proxy_prices)
        self.menu.add("🌐 打开网页看板 (8788)", self.open_web_panel)
        self.menu.add_sub("思考档位…", [("（加载中）", None)])
        self.menu.add_sub("提示注入…", [("（加载中）", None)])
        # ★ 2026-10-09 用户要求：透明度改回菜单、改成滑块、最低 30%
        #   只作用于**主窗口**（完整/精简是同一窗口，两者共用）；子窗口一律不透明。
        #   init 传 lambda：每次弹菜单现取当前值（否则会弹回启动时的旧值）
        self.menu.add_slider("窗口透明度", 30, 100,
                             lambda: int(round(self.alpha * 100)), self.set_alpha_pct)
        self.menu.add_sep()
        self.menu.add("关闭软件", self.exit_app)
        self.root.bind("<Button-3>", self.on_root_right)
        self._last_settle = 0.0

        # ★ 四边/四角拉伸（去掉系统标题栏后原本拉不动）
        self.resizer = enable_border_resize(self.root, min_w=_MIN_W, min_h=_MIN_MINI_H)
        try:
            self.resizer["bind_tree"]([self.frm_full, self.frm_mini,
                                       self.lbl_title, self.lbl_detail, self.lbl_rate,
                                       self.lbl_today, self.lbl_cost, self.mini_lbl_rate,
                                       self.mini_lbl_tokens, self.mini_lbl_cost,
                                       self.mini_lbl_today])
        except Exception as e:
            _startup_log("四边拉伸绑定失败（不影响功能）：%s" % e)
        # 系统标题栏已去掉 → 无系统 ✕；窗口级关闭仍走托盘（右键菜单里有）
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_to_tray)

        # ★ 2026-10-09：进程级深色菜单（影响原生菜单：托盘菜单）
        #   tk 菜单已换成自绘 DarkMenu；这里管的是托盘那个原生菜单。
        force_dark_menus()
        # ★ 2026-10-09：Win11 原生圆角 + 自带淡阴影（实测对无边框窗口有效）
        try:
            self.root.after(60, lambda: apply_round_corners(self.root, 2))
        except Exception:
            pass

        # 初始化 Windows 系统托盘（隐藏图标区）
        self.tray_icon = None
        self._init_tray()

        self.tick()

    # -------- 系统托盘（任务栏隐藏图标）--------
    def _init_tray(self):
        """初始化 Windows 任务栏系统托盘图标。"""
        try:
            import pystray
            from PIL import Image, ImageDraw
            import io, base64

            if _ICON_B64:
                try:
                    img = Image.open(io.BytesIO(base64.b64decode(_ICON_B64)))
                except Exception:
                    img = None
            else:
                img = None

            if not img:
                img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                d.rectangle((4, 4, 28, 28), fill="#4ade80")

            def _on_show(icon, item):
                self.root.after(0, self.show_from_tray)

            def _on_toggle(icon, item):
                self.root.after(0, self.toggle_mini_mode)

            def _on_stats(icon, item):
                self.root.after(0, self.open_stats)

            def _on_exit(icon, item):
                self.root.after(0, self.exit_app)

            menu = pystray.Menu(
                pystray.MenuItem("显示悬浮窗", _on_show, default=True),
                pystray.MenuItem("🗕 切换精简/完整模式", _on_toggle),
                pystray.MenuItem("📊 成本统计…", _on_stats),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出程序", _on_exit),
            )

            self.tray_icon = pystray.Icon("HermesCacheMonitor", img, "缓存跟随监控", menu)
            self.tray_icon.run_detached()
        except Exception as e:
            _startup_log("托盘初始化失败：%s" % e)

    def on_close_to_tray(self):
        """点「🗕 最小化到托盘」→ 隐藏到隐藏图标区，不退出程序。"""
        self.root.withdraw()

    def show_from_tray(self):
        """从托盘恢复窗口显示并置顶。

        ★ 2026-10-09 修：deiconify/lift 之后重新补一次「不进任务栏」样式 ——
          用户报「最小化到托盘再打开，任务栏里就多出一个窗口」。
          根因是 Tk 设 -alpha 时冲掉了 WS_EX_TOOLWINDOW（见 keep_out_of_taskbar）。
          这里再补一次，确保任何路径唤回后都不会混进任务栏。
        """
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        keep_out_of_taskbar(self.root)

    def exit_app(self):
        """彻底退出程序（清理托盘与后台守护）。"""
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    # -------- 交互 --------
    def _edge_of(self, ev):
        """判断该事件位置是否落在窗口边缘热区（用于拖动/拉伸互斥）。"""
        try:
            return self.resizer["which"](ev)
        except Exception:
            return None

    def set_alpha(self, val, quiet=False):
        """设置主窗口透明度（★ 2026-10-09 用户要求：最低 30%、改滑块驱动）。

        只作用于主窗口 root；子窗口一律不透明（见 style_child_window）。
        quiet=True 用于滑块拖动过程：不弹状态行提示、保存做防抖。
        """
        try:
            v = float(val)
        except (TypeError, ValueError):
            return
        self.alpha = max(MIN_ALPHA, min(1.0, v))
        try:
            self.root.attributes("-alpha", self.alpha)
        except Exception:
            pass
        # ★ 2026-10-09：Tk 设 -alpha 会把 WS_EX_TOOLWINDOW 冲掉 → 窗口进任务栏。
        #   每次改完透明度都补回来（用户实测：改透明度后任务栏多出窗口）。
        keep_out_of_taskbar(self.root)
        # 拖动过程每帧都写盘太浪费 → 防抖保存（松手 0.6 秒后落盘一次）
        try:
            if getattr(self, "_alpha_save_job", None):
                self.root.after_cancel(self._alpha_save_job)
        except Exception:
            pass
        self._alpha_save_job = self.root.after(600, self._save_alpha)
        if not quiet:
            self._flash("透明度已设为 %d%%" % int(self.alpha * 100))

    def set_alpha_pct(self, pct):
        """滑块回调：入参是百分比（30~100）。"""
        self.set_alpha(float(pct) / 100.0, quiet=True)

    def _save_alpha(self):
        """防抖落盘：透明度 + 窗口位置（★ 2026-10-09 多屏后一并存位置）。

        位置含副屏的负坐标，必须原样保存，否则重启就"跑回主屏"。
        """
        self._alpha_save_job = None
        try:
            cfg = load_config()
            cfg["alpha"] = self.alpha
            if self.pos_x is not None and self.pos_y is not None:
                cfg["pos_x"], cfg["pos_y"] = int(self.pos_x), int(self.pos_y)
            save_config(cfg)
        except Exception:
            pass

    def on_window_resize(self, ev):
        """用户拖拽改变窗口大小/位置时：记忆尺寸与位置，并动态调整文字折行宽度。"""
        if ev.widget != self.root:
            return
        # ★ 2026-10-09 修正（用户实测：「宽度还是继承」）：
        #   切换模式时我们会主动 geometry() 改成目标模式的尺寸，但 Tk 会先发几个
        #   **旧尺寸**的 Configure 事件（窗口还没真正变成新尺寸），于是刚写进去的
        #   full_w/mini_w 立刻被另一个模式的宽覆盖 → 表现成"宽度还是继承"。
        #   解法：切换期间挂一个闩锁，忽略这些过渡 Configure。
        if getattr(self, "_resize_lock", False):
            return
        w = ev.width
        h = ev.height
        if w < 100 or h < 30:
            return

        # ★ 2026-10-09 多屏：位置也一并记下来（拖到副屏后要能留住，坐标可为负）
        try:
            self.pos_x, self.pos_y = self.root.winfo_x(), self.root.winfo_y()
            self.cur_x, self.cur_y = self.pos_x, self.pos_y
            # 拖动过程每帧都写盘太浪费 → 复用透明度的防抖机制（松手 0.6 秒后落盘一次）
            if getattr(self, "_alpha_save_job", None):
                self.root.after_cancel(self._alpha_save_job)
            self._alpha_save_job = self.root.after(600, self._save_alpha)
        except Exception:
            pass

        # ★ 2026-10-09：完整/精简各自记宽高（切换互不继承）
        if self.mini_mode:
            self.mini_w, self.mini_h = w, h
        else:
            self.full_w, self.full_h = w, h
        self.win_w = w          # 兼容旧字段（= 当前模式的宽）

        # 动态自适应文字折行宽度
        wrap_val = max(180, w - 28)
        try:
            self.lbl_title.config(wraplength=max(140, w - 60))
            self.lbl_detail.config(wraplength=wrap_val)
            self.lbl_cost.config(wraplength=wrap_val)
            self.lbl_warn.config(wraplength=wrap_val)
            self.mini_lbl_tokens.config(wraplength=max(100, w - 24))
            self.mini_lbl_cost.config(wraplength=wrap_val)
        except Exception:
            pass

    def toggle_mini_mode(self, force=None):
        # ★ 2026-10-09 防抖（实测抓出）：Tk 的 <Double-Button-1> 在**同一次双击**里
        #   会被触发两次（以及三击会再触发一次），不加这层会导致"双击切模式 =
        #   切过去又切回来"，表现成"双击没反应"。
        if force is None:
            now = time.time()
            if now - getattr(self, "_toggle_at", 0) < 0.4:
                return
            self._toggle_at = now
        if force is not None:
            self.mini_mode = force
        else:
            self.mini_mode = not self.mini_mode

        try:
            cur_x = self.root.winfo_x()
            cur_y = self.root.winfo_y()
        except Exception:
            cur_x, cur_y = (self.cur_x, self.cur_y)
            if cur_x is None or cur_y is None:
                cur_x, cur_y = clamp_to_desktop(0, 60, self.win_w, 100, self.root)
        # ★ 2026-10-09 多屏：记住当前位置（切换模式时位置不变，但要存下来）
        self.cur_x, self.cur_y = cur_x, cur_y

        cfg = load_config()
        cfg["mini_mode"] = self.mini_mode
        cfg["full_w"] = self.full_w
        cfg["full_h"] = self.full_h
        cfg["mini_w"] = self.mini_w
        cfg["mini_h"] = self.mini_h
        cfg["win_w"] = self.win_w        # 兼容旧字段
        # ★ 2026-10-09 多屏：位置一并记住（副屏坐标为负，要原样存）
        cfg["pos_x"], cfg["pos_y"] = cur_x, cur_y
        save_config(cfg)

        # ★ 2026-10-09：切到哪个模式就用哪个模式**自己的**宽和高（不再继承宽度）
        #   切换期间上闩：忽略 Tk 在过渡期发出的旧尺寸 Configure（见 on_window_resize）
        self._resize_lock = True
        try:
            if self.mini_mode:
                self.frm_full.pack_forget()
                self.frm_mini.pack(fill="both", expand=True)
                target_w = max(_MIN_W, self.mini_w)
                target_h = max(_MIN_MINI_H, self.mini_h)
            else:
                self.frm_mini.pack_forget()
                self.frm_full.pack(fill="both", expand=True)
                target_w = max(_MIN_W, self.full_w)
                target_h = max(_MIN_FULL_H, self.full_h)
            # ★ 2026-10-09 多屏：切模式时位置也走虚拟桌面钳制
            #   （原来直接透传 cur_x/cur_y，副屏上会被旧的主屏钳制逻辑挤回去）
            cur_x, cur_y = clamp_to_desktop(cur_x, cur_y, target_w, target_h, self.root)
            self.root.geometry("%dx%d+%d+%d" % (target_w, target_h, cur_x, cur_y))
            self.win_w = target_w
            self.pos_x, self.pos_y = cur_x, cur_y
        finally:
            # 解锁放到 geometry 生效之后（约 250ms），期间所有 Configure 都丢弃
            self.root.after(250, lambda: setattr(self, "_resize_lock", False))

        if self.last_d:
            try:
                self.render(self.last_d)
            except Exception:
                pass

    def on_root_right(self, ev):
        """★ 2026-10-09：右键弹出主菜单（自绘 DarkMenu，无白边）。

        子菜单内容每次弹出前重建：思考档位要打勾、提示注入要标当前预设。
        """
        try:
            self._refresh_sub_menus()
        except Exception:
            pass
        try:
            self.menu.popup(ev.x_root, ev.y_root)
        except Exception as e:
            _startup_log("右键菜单弹出失败：%s" % e)

    def _refresh_sub_menus(self):
        """重建「思考档位 / 提示注入」两个子菜单（含当前项标记）。"""
        for i, (kind, lbl, cmd, sub) in enumerate(self.menu.items):
            if lbl and lbl.startswith("思考档位"):
                self.menu.items[i] = ("item", "思考档位…  ▸", None, self._effort_sub())
            elif lbl and lbl.startswith("提示注入"):
                self.menu.items[i] = ("item", "提示注入…  ▸", None, self._hint_sub())

    def _effort_sub(self):
        try:
            cur, path = read_effort()
        except Exception:
            cur, path = "", ""
        if not path:
            return [("（未找到 config.yaml）", None)]
        out = []
        for val, name in EFFORT_LEVELS:
            mark = "✓ " if val == (cur or "") else "   "
            out.append((mark + name, (lambda v=val: self.switch_effort(v))))
        return out

    def _hint_sub(self):
        try:
            store = HintStore()
            store.load()
            cur = store.current_active()
            presets = store.list_presets()
        except Exception:
            return [("（读取失败）", None)]
        out = [("打开管理…", self.open_hint), None]
        if not presets:
            out.append(("（还没有预设）", None))
            return out
        for p in presets:
            name = p.get("name") or ""
            mark = "● " if name == cur else "   "
            out.append((mark + name,
                        (lambda pr=p: apply_hint_preset_direct(pr, self.root))))
        return out

    def do_sync_proxy_prices(self):
        """从中转站真实流水一键反推并写入价格表。"""
        ok, msg = sync_prices_from_proxy(self.mon.prices)
        self._flash(msg, is_err=(not ok))
        self.mon.last_info_at = 0

    def open_web_panel(self):
        """按需拉起中转看板并在默认浏览器打开（平时不常驻占用内存）。"""
        import webbrowser, urllib.request, subprocess
        running = False
        try:
            with urllib.request.urlopen("http://127.0.0.1:8788/", timeout=1) as resp:
                if resp.status == 200:
                    running = True
        except Exception:
            running = False

        if not running:
            pm = _get_proxy_monitor_dir()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            py_exe = "pyw" if os.system("where pyw >nul 2>&1") == 0 else "python"
            subprocess.Popen([py_exe, "panel.py", "--port", "8788"], cwd=pm,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags)
            time.sleep(0.5)

        webbrowser.open("http://127.0.0.1:8788")
        self._flash("已在浏览器打开中转看板")

    # -------- 思考档位 --------
    def on_footer_click(self, ev):
        """左键点底部状态行 → 弹档位菜单（自绘，无白边）。"""
        try:
            items = self._effort_sub()
        except Exception:
            return
        m = DarkMenu(self.root, width=200)
        m.add("思考档位", None).add_sep()
        for it in items:
            if it is None:
                m.add_sep()
            else:
                m.add(it[0], it[1])
        m.popup(ev.x_root, ev.y_root)
        self._effort_popup = m          # 持引用，防止被 GC 掉

    def show_effort_menu(self):
        """兼容旧调用点：返回档位项列表（自绘菜单用）。"""
        return self._effort_sub()

    def switch_effort(self, level):
        """带确认框的档位切换。"""
        cur, _ = read_effort()
        if (cur or "") == (level or ""):
            return
        msg = "确定把思考档位切到「%s」？\n\n当前：%s\n（立即生效，所有对话通用）" % (
            effort_label(level), effort_label(cur))
        if level in EFFORT_RISKY:
            msg += "\n\n⚠ 该档位在部分中转站会被拒绝（报 400）\n换模型后才可能生效。"
        if not messagebox.askokcancel("思考档位", msg, parent=self.root):
            return
        ok, info = write_effort(level)
        if ok:
            messagebox.showinfo("思考档位", "%s\n\n已立即生效。" % info, parent=self.root)
        else:
            messagebox.showerror("思考档位", info, parent=self.root)

    def open_stats(self):
        self._stats.append(StatsDialog(self.root, self.mon, on_close=self._stats_closed))

    def open_site_merge(self):
        """站点归并设置：把同一家中转站的多个域名并成一个「站名」。"""
        ledger = getattr(self.mon, "cost_ledger", None)
        if ledger is None:
            messagebox.showerror("站点归并", "账本还没就绪，稍后再试。", parent=self.root)
            return
        dlg = SiteMergeDialog(self.root, ledger)
        self.root.wait_window(dlg.win)
        self.do_refresh()

    # -------- 提示注入（新增） --------
    def show_hint_menu(self):
        """兼容旧调用点：返回提示注入子菜单项（自绘菜单用）。"""
        return self._hint_sub()

    def open_hint(self):
        """单例打开管理窗（已开着就抬起来）。"""
        d = self._hint_dlg
        if d is not None:
            try:
                if d.win.winfo_exists():
                    d.win.lift()
                    return
            except Exception:
                pass
        try:
            self._hint_dlg = HintDialog(self.root, on_changed=self._hint_changed)
        except Exception as e:
            messagebox.showerror("提示注入", "打开失败：%s" % e, parent=self.root)

    def _hint_changed(self):
        """注入/恢复后：让状态行缓存立刻失效。"""
        _HINT_CACHE["t"] = 0.0
        _HINT_CACHE["v"] = ""
        _HINT_CACHE["present"] = False

    # -------- 刷新 / 重新对齐（新增） --------
    def _flash(self, text, warn=False, secs=8.0):
        """状态行临时反馈，几秒后自动消失。"""
        self._flash_msg = (text, time.time() + secs, warn)
        self._render_footer()

    def do_refresh(self, _ev=None):
        """手动刷新：强制清锁 + 同步重跑三级信号 + 强制重载校准 + 立即重渲染。"""
        if hasattr(self.mon, "calib_mgr"):
            self.mon.calib_mgr.load(force=True)
        try:
            hit = self.mon.refresh_follow()
        except Exception:
            hit = None
        self.mon.last_info_at = 0.0
        try:
            d = self.mon.current(include_gap=self.include_gap)
        except Exception:
            d = None
        self.last_d = d
        try:
            self.render(d)
        except Exception:
            pass
        # ⚠ 先登记当前 sid：否则下一轮 tick 会认为「对话变了」而把刚设的提示清掉
        self._last_sid_shown = (d or {}).get("sid")
        sid = (hit or {}).get("sid")
        via = (hit or {}).get("via")
        if sid and d:
            title = (d.get("title") or ("会话 " + sid[-6:]))
            self._flash("✓ 已对齐：%s（%s）" % (title[:18], via))
        elif sid:
            self._flash("✓ 已对齐会话 %s（%s，暂无数据）" % (sid[-6:], via))
        else:
            self._flash("⚠ 没检测到当前对话（UIA/history/SMU 都没读到）", warn=True)
        _startup_log("手动刷新：sid=%s via=%s title=%s" % (
            sid, via, (d or {}).get("title")))

    def on_calib_click(self, _ev=None):
        """手动点击校准：定向极速刷新当前对话站点，拉取最新流水并重新对齐当前对话。"""
        d = self.last_d or {}
        st = d.get("site")
        target_sites = []
        if st and st != "?":
            target_sites.append(canon_host(st))
        calib = d.get("calib") or {}
        for s in calib.get("sites") or []:
            cs = canon_host(s)
            if cs and cs != "?" and cs not in target_sites:
                target_sites.append(cs)

        site_hint = "（%s）" % "、".join(target_sites) if target_sites else ""
        self._flash("正在连接中转站极速刷新%s…" % site_hint)

        def _worker():
            ok, msg = trigger_proxy_refresh(sites=target_sites, timeout=15.0)

            def _done():
                res = None
                if hasattr(self.mon, "calib_mgr"):
                    res = self.mon.calib_mgr.load(force=True)
                cnt = len((res or {}).get("sessions", {}))
                cur_sid = (self.last_d or {}).get("sid") or getattr(self.mon, "last_sid", None) or getattr(self.mon, "cur_sid", None)
                hit = (res or {}).get("sessions", {}).get(cur_sid) if (res and cur_sid) else None
                if hit:
                    cost = hit.get("cost", 0.0)
                    self._flash("✓ 已匹配站方最新数据：实扣 ¥%.4f" % cost)
                elif ok:
                    self._flash("✓ 站方用量已刷新（当前会话暂无新流水）")
                else:
                    self._flash("⚠ 站方刷新失败：%s" % msg, warn=True)
                self.mon.last_info_at = 0.0
                try:
                    d = self.mon.current(include_gap=self.include_gap)
                except Exception:
                    d = None
                self.last_d = d
                try:
                    self.render(d)
                except Exception:
                    pass

            try:
                self.root.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _render_footer(self):
        """状态行：正常显示实时状态；有临时反馈时优先显示反馈（几秒）。"""
        fm = self._flash_msg
        if fm and time.time() < fm[1]:
            self.lbl_footer.config(text=fm[0], fg=(RED if fm[2] else GREEN))
            return
        if fm:
            self._flash_msg = None
        src = (self.last_d or {}).get("source", "")
        try:
            _eff, _ = read_effort_cached()
        except Exception:
            _eff = ""
        try:
            _ht, _hpresent, _ = read_hint_cached()
            _hname = "无" if not _hpresent else ("%d字" % len(_ht))
        except Exception:
            _hname = "--"
        self.lbl_footer.config(
            text="更新 %s · %s · 档位:%s · 注入:%s ▾" % (
                datetime.now().strftime("%H:%M:%S"), src,
                effort_label(_eff), _hname), fg=DIM)

    def _stats_closed(self, dlg):
        try:
            self._stats.remove(dlg)
        except ValueError:
            pass

    def _set_include_gap(self, val):
        self.include_gap = bool(val)
        if self.mon.last_info is not None:
            self.mon.last_info["include_gap"] = self.include_gap
            self.mon.last_info_at = 0  # 立即重算

    def on_gap_click(self, _ev=None):
        if not (self.last_d or {}).get("report", {}).get("gap"):
            return
        self._set_include_gap(not self.include_gap)
        self.render(self.last_d)

    def on_gap_detail(self, _ev=None):
        d = self.last_d
        if not d or not (d.get("report") or {}).get("gap"):
            return
        GapDialog(self.root, self.mon, d)

    def on_cost_click(self, ev=None):
        """★ 2026-10-09 用户要求：成本行**双击**才弹价格配置窗。

        ⚠️ 必须 return "break"：Tk 的 <Double-Button-1> 会**冒泡到父窗口**，
        而 root 上绑着「双击切精简/完整模式」（实测：精简模式双击成本行会
        同时弹出价格窗 + 切模式）。返回 break 阻止冒泡。

        ⚠️ 还要防连击：Tk 把「三击」也会当成一次 Double 再触发，实测快速连点
        会弹出多个价格窗 → 用 400ms 静默期只认第一次。
        """
        now = time.time()
        if now - getattr(self, "_cost_click_at", 0) < 0.4:
            return "break"
        self._cost_click_at = now
        d = self.last_d
        if d:
            host = d.get("site") or ""
            model = d.get("model") or ""
            entry = d.get("price_entry")
            if entry and entry.get("model") not in ("*", None, "") and entry.get("model") != model:
                entry = None
            PriceDialog(self.root, self.mon.prices, host, model,
                        on_saved=self._price_saved, entry=entry)
        return "break"

    def on_cost_right(self, _ev=None):
        d = self.last_d or {}
        PriceManager(self.root, self.mon.prices, on_changed=self._price_saved,
                     cur_host=d.get("site"), cur_model=d.get("model"))

    def _price_saved(self):
        self.mon.last_info_at = 0
        if self.mon.last_info is not None:
            self.mon.last_info_at = 0
        self.mon.prices.load(force=True)
        # 已打开的统计窗口：立刻热重载价格并重绘（不等 2 秒定时器）
        for dlg in list(self._stats):
            try:
                dlg.notify_prices_changed()
            except Exception:
                pass

    # -------- 渲染 --------
    def render(self, d):
        if d is None:
            self.lbl_title.config(text="(无活动对话)")
            self.lbl_rate.config(text="--", fg=DIM)
            self.lbl_detail.config(text="")
            self.lbl_cost.config(text="")
            self.lbl_gap.config(text="")
            self.lbl_today.config(text="")
            try:
                self.mini_lbl_rate.config(text="--", fg=DIM)
                self.mini_lbl_tokens.config(text="")
                self.mini_lbl_cost.config(text="(无活动对话)")
                self.mini_lbl_today.config(text="")
            except Exception:
                pass
            self.last_d = None
            return
        title = d.get("title") or ("会话 " + d["sid"][-6:])
        self.lbl_title.config(text="▶ %s" % title[:34])
        if d["rate"] is None:
            self.lbl_rate.config(text="--", fg=DIM)
        else:
            color = GREEN if d["rate"] >= 80 else (YELLOW if d["rate"] >= 50 else RED)
            self.lbl_rate.config(text="%.1f%%" % d["rate"], fg=color)

        rep = d.get("report") or {}
        cov = ""
        if rep:
            if rep.get("partial"):
                cov = " ｜ 账目 --（日志未覆盖）"
            else:
                cov = " ｜ 账目 %d/%d 次%s" % (rep.get("recorded", 0), rep.get("real", 0),
                                            "~" if rep.get("concurrent") else "")
        offset = d.get("calib_offset") if getattr(self, "use_calibration", True) else None
        if offset and offset.get("has_calib"):
            diff_cr = offset.get("diff_cr", 0)
            diff_inp = offset.get("diff_inp", 0)
            c_cr = d["cr"] + diff_cr
            c_inp = d["inp"] + diff_inp
        else:
            c_cr = d["cr"]
            c_inp = d["inp"]

        today_str = datetime.now().strftime("%Y-%m-%d")
        # ★ 该会话**真正用过**的全部站与模型（2026-10-03 修）
        #   ⚠️ 原来只取 sessions 表那一行主记录的单个站，导致「一个会话用了两个站」
        #   时只显示一个（实测 4dc91e：dshapi 349 次 + 示例站 118 次只露出 示例站）。
        #   规矩：用过的站和模型都要列出来。主记录优先排前，其余按调用次数排。
        cur_sites, cur_models = [], []
        try:
            _used = self.mon.session_sites_models(d["sid"])
        except Exception:
            _used = []
        host_main = canon_host(d.get("site")) if d.get("site") and d["site"] != "?" else None
        model_main = canon_model(d.get("model")) if d.get("model") and d["model"] != "?" else None
        if host_main:
            cur_sites.append(host_main)
        if model_main:
            cur_models.append(model_main)
        for u in _used:
            if u["site"] and u["site"] != "?" and u["site"] not in cur_sites:
                cur_sites.append(u["site"])
            if u["model"] and u["model"] != "?" and u["model"] not in cur_models:
                cur_models.append(u["model"])
        # 主站未知时，才退回校准记录里的推测站点
        if not host_main:
            calib_raw = d.get("calib") or {}
            for s in calib_raw.get("sites") or []:
                cs = canon_host(s)
                if cs and cs != "?" and cs not in cur_sites:
                    cur_sites.append(cs)

        # 每个站当天的站方实扣（今天有流水才带金额）
        _cost_by_site = {}
        for u in _used:
            if u["site"] and u["site"] != "?":
                _cost_by_site[u["site"]] = _cost_by_site.get(u["site"], 0) + u["calls"]
        site_items = []
        for s in cur_sites:
            st_data = self.mon.calib_mgr.get_daily_calib(today_str, s) if hasattr(self.mon, "calib_mgr") else None
            # 站点短名（更干净，不占行宽）
            s_short = s.replace(".xin", "").replace("api.", "").replace(".icu", "")
            if st_data and st_data.get("cost") is not None:
                site_items.append("[● %s · ¥%.2f (%d次)]" % (s_short, st_data["cost"], st_data.get("calls", 0)))
            else:
                site_items.append("[● %s]" % s_short)
        site_str = "  ".join(site_items) if site_items else d["site"]
        # ★ 2026-10-09：站/模型列表优先用「真实用量」的顺序（session_model_usage
        #   按调用次数降序），不再让 sessions 主记录那一行排第一个 —— 主记录只记得
        #   最后一次用的组合，排它第一会误导（实测 20261008_200019 主记录是
        #   api9×gemini，但实际 76 次都用的是 localhost×gemini）。
        _sites_show = d.get("sites_real") or cur_sites
        _models_show = d.get("models_real") or cur_models
        if len(_sites_show) > 1 or len(_models_show) > 1:
            _parts = []
            if _sites_show:
                _parts.append("站：" + "、".join(_sites_show))
            if _models_show:
                _parts.append("模型：" + "、".join(_models_show))
            if _parts:
                site_str += "\n" + " ｜ ".join(_parts)

        # ★ 2026-10-09：多组合逐条计价时，给一行明细（哪个组合花了多少）
        _mu = d.get("multi") or {}
        _pairs = _mu.get("pairs") or []
        if _mu.get("multi_mode") and len(_pairs) > 1:
            _lines = []
            for _p in _pairs:
                _c = ("免费" if _p.get("free") else
                      (fmt_money(_p["cost"], _p.get("cur")) if _p.get("cost") is not None
                       else "未配价"))
                _lines.append("%s×%s %s" % (_p["site"], _p["model"], _c))
            _unp = _mu.get("unmatched") or 0
            _tail = "（%d 条未配价）" % _unp if _unp else ""
            site_str += "\n明细：" + " ｜ ".join(_lines) + _tail

        # 「缓存明细缺失」的提示（2026-10-03）：补齐了几次 / 几次没数据可补
        _fx = d.get("cache_fix") or {}
        _fx_note = ""
        if _fx.get("matched"):
            _fx_note = " ｜ 明细补齐 %d 次" % _fx["matched"]
        if _fx.get("missing"):
            _fx_note += "（%d 次暂无数据）" % _fx["missing"]

        self.lbl_detail.config(text="缓存 %s / 未命中 %s ｜ %s 次调用\n%s%s%s" % (
            fmt_vol(c_cr), fmt_vol(c_inp), d["calls"], site_str,
            (" " + cov if cov else ""), _fx_note))

        # 成本行
        priced = d.get("priced")
        ch = d.get("children") or {}
        if priced is None:
            self.lbl_cost.config(text="⚙ 价格未配置 · 点此填写", fg=DIM)
        elif priced.get("free"):
            extra = ""
            if ch.get("count"):
                extra = " ｜ 子%d %s" % (ch["count"],
                                       "免费" if not ch["cost"] else fmt_money(ch["cost"], ch["cur"]))
            self.lbl_cost.config(text="≈ 免费%s · 缓存省 %s" % (
                extra, fmt_money(priced["saved"], priced["cur"])), fg=GREEN)
        elif priced.get("per_call"):
            # ★ 2026-10-08 按次收费：成本 = 次数 × 每次费用。
            #   不显示「缓存省」（按次不看 token，缓存命中并不省钱）。
            #   子代理的按次费用已在 _children_cost 里各自按次数算好，可并入显示。
            ch_cost = ch.get("cost") or 0.0
            cur = priced["cur"]
            total = priced["cost"] + ch_cost
            s = "≈ %s · 按次 %d 次 × %s" % (
                fmt_money(total, cur), priced.get("calls") or 0,
                fmt_money(priced["per_call"], cur))
            if ch.get("count"):
                s += " ｜ 含 %d 子代理 %s" % (ch["count"], fmt_money(ch_cost, cur))
            self.lbl_cost.config(text=s, fg=YELLOW)
        else:
            base_cost = priced["cost"]
            ch_cost = ch.get("cost") or 0.0
            cur = priced["cur"]

            # 用户规则（2026-10-03 定稿）：
            #   总价 = 主会话 Hermes 估算 + 补差
            #   补差 = 站方总数 − 主会话 Hermes 估算
            #   子代理【不进总价】：站方总数本身就含子代理的流水（子代理归在独立
            #   名下但钱记在站方账上），所以补差里已经包含它。再把 ch_cost 加一遍
            #   就是重复计算（实测多出 ¥0.0099 ≈ 子代理价格）。
            #   子代理只在尾部显示一行 Hermes 侧参考值，不做任何加减。
            hermes_total = base_cost
            sub_total = base_cost + ch_cost   # 仅用于无站方数据时的兜底显示

            if offset and offset.get("has_calib"):
                diff_cost = offset.get("diff_cost", 0.0)
                final_cost = hermes_total + diff_cost
                if diff_cost >= 0.0001:
                    cost_head = "≈ %s + 补差 %s = %s (已校准)" % (
                        fmt_money(hermes_total, cur), fmt_money(diff_cost, cur), fmt_money(final_cost, cur))
                else:
                    cost_head = "≈ %s (已校准)" % fmt_money(final_cost, cur)
            else:
                # 无站方数据：只能靠 Hermes 自己，此时子代理要算进去
                hermes_total = sub_total
                cost_head = "≈ %s" % fmt_money(hermes_total, cur)

            tail = ""
            if ch.get("count"):
                tail = " ｜ %d 个子代理合计 %s（参考·不计入）" % (
                    ch["count"], fmt_money(ch["cost"], ch["cur"]))
                if ch.get("unpriced"):
                    tail += "（%d 未配价）" % ch["unpriced"]
            gp = ""
            est = d.get("estimate") or {}
            if self.include_gap and est.get("cost"):
                gp = " + 缺账 %s" % fmt_money(est["cost"], priced["cur"])
            tag = ""
            if priced.get("period"):
                tag = " [%s]" % priced["period"]
            # ★ 2026-10-08 按次收费：成本 = 次数 × 单价，**不显示「缓存省」**
            #   （按次收费时缓存命中并不省钱，显示它反而误导）。
            if priced.get("per_call"):
                self.lbl_cost.config(text="%s%s · 按次 %d 次 × %s%s%s" % (
                    cost_head, tag,
                    priced.get("calls") or 0,
                    fmt_money(priced["per_call"], priced["cur"]), tail, gp))
            else:
                self.lbl_cost.config(text="%s%s · 缓存省 %s%s%s" % (
                    cost_head, tag,
                    fmt_money(priced["saved"], priced["cur"]), tail, gp))

        # 缺账行
        if rep and rep.get("gap"):
            est = d.get("estimate") or {}
            if est:
                txt = "⚠ 缺账 %d · 估 +%s in / +%s out" % (
                    rep["gap"], fmt_vol(est["in"]), fmt_vol(est["out"]))
            else:
                txt = "⚠ 缺账 %d · 无锚点可估" % rep["gap"]
            txt += " ｜ %s" % ("已计入" if self.include_gap else "未计入")
            self.lbl_gap.config(text=txt)
            self.lbl_gap_detail.config(text="明细 ▸")
        else:
            self.lbl_gap.config(text="")
            self.lbl_gap_detail.config(text="")

        # 今日站点消费（完整 + 精简 都更新）★ 跟着本对话用过的站走
        _today_txt = self._today_cost_text(cur_sites)
        try:
            self.lbl_today.config(text=_today_txt)
        except Exception:
            pass

        # 同步更新迷你模式控件（★ 2026-10-09 极简：只留 命中率 / token / 校准后金额）
        try:
            rate_txt = self.lbl_rate.cget("text")
            rate_fg = self.lbl_rate.cget("fg")
            self.mini_lbl_rate.config(text=rate_txt, fg=rate_fg)
            self.mini_lbl_tokens.config(text="缓存 %s / 未命中 %s" % (fmt_vol(c_cr), fmt_vol(c_inp)))
            self.mini_lbl_cost.config(text=self._mini_cost_text(priced, offset))
            self.mini_lbl_today.config(text=_today_txt)
        except Exception:
            pass

    def _today_cost_text(self, sites=None):
        """「今日站点 ¥X.XX」——**当前对话所用站点**的今日实扣合计。

        ★ 2026-10-09 用户反馈修正：原来是把**所有站点**当天消费加总，
          换到别的中转站后显示的仍是 dsh（跟对话无关）→ 改成跟着对话的站走。
          sites 为当前会话用过的站列表（主站 + session_sites_models 里的）；
          没有归属站时留空，不显示一个跟当前对话无关的合计。
        """
        if not sites:
            return ""
        try:
            allc = today_site_costs()
        except Exception:
            return ""
        if not allc:
            return ""
        keys = {canon_host(s) for s in sites if s}
        matched = [v for k, v in allc.items() if k in keys]
        if not matched:
            return ""
        return "今日站点 ¥%.2f" % sum(v["cost"] for v in matched)

    def _mini_cost_text(self, priced, offset):
        """精简模式的金额行：★ 只要「校准后的最终金额」，不带任何过程文字。

        用户拍板（2026-10-09）：不显示「相加相减」「已校准」「子代理」「缓存省」，
        就一个 ≈ ¥X.XX。
        """
        if priced is None:
            return "⚙ 价格未配置"
        cur = priced.get("cur") or "¥"
        if priced.get("free"):
            return "免费"
        base = priced.get("cost") or 0.0
        # 有站方校准时，取「校准后的最终金额」（= Hermes 估算 + 补差），但不显示补差过程
        if offset and offset.get("has_calib"):
            base = base + (offset.get("diff_cost") or 0.0)
        elif priced.get("per_call") and (priced.get("cost") is not None):
            base = priced.get("cost")
        return "≈ %s" % fmt_money(base, cur)

    def tick(self):
        # 智能静止休眠：若窗口最小化或不可见，直接 3 秒后重试，跳过所有重绘与计算
        try:
            if not self.root.winfo_viewable() or self.root.state() == "iconic":
                self.root.after(3000, self.tick)
                return
        except Exception:
            pass

        _t0 = time.perf_counter()
        # 自动感知校准文件更新（后台 5 分钟轮询导出后秒级热同步）
        if hasattr(self.mon, "calib_mgr"):
            mgr = self.mon.calib_mgr
            p = mgr._resolve_path()
            if p:
                try:
                    m = os.path.getmtime(p)
                    if m != mgr._mtime:
                        mgr.load(force=True)
                        self.mon.last_info_at = 0.0
                except Exception:
                    pass
        try:
            d = self.mon.current(include_gap=self.include_gap)
        except Exception:
            d = None
        self.last_d = d
        _t1 = time.perf_counter()
        try:
            self.render(d)
        except Exception:
            pass
        _t2 = time.perf_counter()
        # 对话切换 → 先清掉上一次的临时反馈（避免旧提示挂在状态行上误导），再渲染状态行
        _now_sid = (d or {}).get("sid")
        if _now_sid != getattr(self, "_last_sid_shown", None):
            self._last_sid_shown = _now_sid
            self._flash_msg = None
        self._render_footer()
        # 日账本：跨日结算（限流 60 秒）
        now_t = time.time()
        if now_t - self._last_settle > 60:
            self._last_settle = now_t
            try:
                self.mon.cost_ledger.maybe_settle()
            except Exception:
                pass
        _t3 = time.perf_counter()
        if _PROFILE:
            ms = ((_t1 - _t0) * 1000, (_t2 - _t1) * 1000, (_t3 - _t2) * 1000)
            if max(ms) > 30:      # 只记慢的，避免日志刷屏
                # ⚠ 原来这里用的是未定义的名字 `src`（tick 里从未赋值）→ NameError 被外层
                # except 吞掉，于是 --profile 慢日志自 2026-09-14 起一行都写不出来。
                # 诊断工具自己失效是最坑的，改成从 d 取 source。
                _src = (d or {}).get("source", "-")
                try:
                    with open(os.path.join(WORK_DIR, "tick_profile.log"),
                              "a", encoding="utf-8") as f:
                        f.write("%s  跟随 %.1fms ｜ 渲染 %.1fms ｜ 结算 %.1fms ｜ "
                                "src=%s sid=%s\n" % (
                                    datetime.now().strftime("%H:%M:%S"), ms[0], ms[1], ms[2],
                                    _src, (d or {}).get("sid", "-")))
                except Exception:
                    pass
        self.root.after(REFRESH_MS, self.tick)

    def run(self):
        self.root.mainloop()


# ─────────────────────────── 入口 ───────────────────────────

def _dump(m, d):
    """调试输出：把展示数据打成文本。"""
    if not d:
        print("no data")
        return
    print("sid:    ", d["sid"])
    print("title:  ", d["title"])
    print("site:   ", d["site"])
    print("model:  ", d["model"])
    print("calls:  ", d["calls"])
    print("cache:  %s / miss: %s / out: %s" % (
        fmt_vol(d["cr"]), fmt_vol(d["inp"]), fmt_vol(d["out"])))
    print("rate:   %.1f%%" % d["rate"] if d["rate"] is not None else "rate:   n/a")
    p = d.get("priced")
    if p is None:
        print("price:   未配置")
    elif p.get("free"):
        print("price:   免费 ｜ 缓存省 %s" % fmt_money(p["saved"], p["cur"]))
    elif p.get("per_call"):
        print("price:   ≈ %s · 按次 %d 次 × %s（不看 token）" % (
            fmt_money(p["cost"], p["cur"]), p.get("calls") or 0,
            fmt_money(p["per_call"], p["cur"])))
    else:
        print("price:   ≈ %s%s ｜ 缓存省 %s" % (
            fmt_money(p["cost"] + (d.get("children") or {}).get("cost", 0), p["cur"]),
            (" [%s]" % p["period"]) if p.get("period") else "",
            fmt_money(p["saved"], p["cur"])))
    ch = d.get("children") or {}
    print("children: %d 个（%s，其中未配价 %d）" % (
        ch.get("count", 0), fmt_money(ch.get("cost", 0), ch.get("cur", "¥")),
        ch.get("unpriced", 0)))
    rep = d.get("report")
    if rep:
        mark = "（日志未覆盖全程）" if rep.get("partial") else (
            "（并发时段，归属近似）" if rep.get("concurrent") else "")
        print("ledger: 真实 %d / 记账 %d / 缺账 %d%s" % (
            rep["real"], rep["recorded"], rep["gap"], mark))
        if rep.get("inflight"):
            print("        进行中 %s（等待记账 → 不算缺账）" % rep["inflight"])
        for ts, kind in m._gap_types(d["sid"], rep)[:12]:
            print("        缺账 %s  %s" % (ts, kind))
    est = d.get("estimate")
    if est:
        print("estimate: +%s in（未命中 %s / 命中 %s） / +%s out%s" % (
            fmt_vol(est["in"]), fmt_vol(est["miss"]), fmt_vol(est["hit"]),
            fmt_vol(est["out"]),
            ("  ≈ %s" % fmt_money(est["cost"])) if est.get("cost") is not None else ""))
    print("source: ", d["source"])


def _dump_stats(cl, win):
    """文本形式打印成本统计（对应 GUI「成本统计」窗口，调试/无人值守用）。"""
    agg, src = cl.report(win)
    rows = {}
    for key, v in (agg or {}).items():
        host, model = key.split("|", 1)
        r = rows.setdefault((host, model), {
            "calls": 0, "in": 0, "hit": 0, "out": 0, "cost": 0.0,
            "unpriced": False, "unpriced_hist": False})
        for f in ("calls", "in", "hit", "out"):
            r[f] += v.get(f) or 0
        r["cost"] += v.get("cost") or 0
        r["unpriced"] = r["unpriced"] or bool(v.get("unpriced"))
        r["unpriced_hist"] = r["unpriced_hist"] or bool(v.get("unpriced_hist"))
    total = sum(r["cost"] for r in rows.values())
    print("[统计] 窗口=%s 来源=%s 条目=%d 合计=%s" % (win, src, len(rows), fmt_money(total)))
    for (host, model), r in sorted(rows.items(), key=lambda kv: -kv[1]["calls"]):
        tot_in = r["in"] + r["hit"]
        rate = (r["hit"] * 100.0 / tot_in) if tot_in else 0.0
        flag = []
        if r["unpriced"]:
            flag.append("未配价")
        if r["unpriced_hist"]:
            flag.append("历史缺价")
        # 没配价的条目金额列显示 "--"，别显示 ¥0.0000（容易被误读成「免费」）
        cost_s = fmt_money(r["cost"]) if not (flag and not r["cost"]) else "--"
        print("  %-26s %-24s %7d %6.1f%% %12s  %s" % (
            host[:26], (model or "*")[:24], r["calls"], rate,
            cost_s, "+".join(flag) or "-"))
    # 锚点对照：站方实付 vs 本表估算（并反推整体倍率）
    for b in (cl.data.get("bills") or []):
        rb = cl.resolve_bill(b)
        ours = sum(v["cost"] for (h, _m), v in rows.items() if h == b["host"])
        k = (rb["amount"] / ours) if ours else 0
        print("  [锚点] %-20s 本表估算 %8.2f ｜ 站方实付 %8.2f ｜ k=%.4f（差 %+.1f%%）" % (
            b["host"], ours, rb["amount"], k, (k - 1) * 100 if ours else 0))
        for sr in (rb.get("seg_rows") or []):
            print("           段 %-12s 估算 %8.2f ｜ 分摊实付 %8.2f ｜ k=%.4f" % (
                sr["seg_from"] or "(最早)", sr["est"], sr.get("amount_share") or 0,
                sr.get("k_share") or 0))
    return rows


def _dump_segments(pb):
    """打印价格表的分段情况（时间轴）。"""
    print("[价格表] %d 条" % len(pb.all()))
    for e in pb.all():
        head = "%s / %s" % (e.get("host"), e.get("model"))
        ps = pb.segs(e)
        if not ps:
            r = e.get("ratio")
            print("  %-46s 单段(全天)  in=%s out=%s cache=%s%s" % (
                head, e.get("in"), e.get("out"), e.get("cache"),
                ("  ratio=%s" % r) if r else ""))
            continue
        print("  %-46s %d 段（时间轴）:" % (head, len(ps)))
        for p in ps:
            r = p.get("ratio")
            flag = " ⚡峰谷" if (p.get("peak") or p.get("off")) else ""
            print("      from %-12s in=%-10s out=%-10s cache=%-10s%s%s%s" % (
                p.get("from") or "(最早)", p.get("in"), p.get("out"), p.get("cache"),
                ("  ratio=%s" % r) if r else "",
                flag, "  周末全天闲时" if p.get("weekend_off") else ""))


def _do_maint(m, args):
    """无 GUI 的维护动作。

    --segments              打印价格表分段
    --recompute [day|missing|all]
    --stats [24h|today|7d|30d|all]
    --bill-file <json>      导入实付锚点（单对象或数组）
    --bills                 列出锚点 + 反解
    --resolve               额外打印逐项明细
    --bill-del host|model|from|to
    """
    cl = m.cost_ledger
    done = []
    if "--segments" in args:
        _dump_segments(m.prices)
        done.append("segments")
    if "--bill-file" in args:
        i = args.index("--bill-file")
        path = args[i + 1] if i + 1 < len(args) else ""
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else [data]
        for b in items:
            rec = cl.add_bill(b)
            print("[锚点] 录入 %s / %s  %s~%s  %s%s%s" % (
                rec["host"], rec["model"], rec["from"] or "-", rec["to"] or "-",
                rec["cur"], rec["amount"],
                ("  备注: %s" % rec["note"]) if rec["note"] else ""))
        done.append("bills")
    if "--bill-del" in args:
        i = args.index("--bill-del")
        spec = args[i + 1] if i + 1 < len(args) else ""
        parts = (spec.split("|") + ["", "", "", ""])[:4]
        n = cl.del_bill(parts[0], parts[1] or "*", parts[2], parts[3])
        print("[锚点] 已删除，剩余 %d 条" % n)
        done.append("bills")
    if "--bills" in args or "--resolve" in args:
        bills = cl.data.get("bills") or []
        if not bills:
            print("[锚点] 还没有录入实付账单（用 --bill-file 导入，或在统计窗口里点「录入实付」）")
        for b in bills:
            r = cl.resolve_bill(b)
            print("\n[锚点] %s / %s   %s ~ %s   %s" % (
                b["host"], b["model"], b["from"] or "-", b["to"] or "-",
                b.get("added") or ""))
            print("   token 来源=%s  miss=%d 缓存=%d 输出=%d" % (
                r["tokens_src"], r["tokens"]["miss"], r["tokens"]["cache"], r["tokens"]["out"]))
            print("   估算 %s%.2f ｜ 实付 %s%.2f ｜ 整体倍率 k=%.4f（差 %+.1f%%）" % (
                r["cur"], r["est"], r["cur"], r["amount"],
                r["k"] or 0, ((r["amount"] / r["est"] - 1) * 100) if r["est"] else 0))
            for sr in (r.get("seg_rows") or []):
                print("      · 段 %-12s 估算 %8.2f ｜ 按估算分摊实付 %8.2f ｜ k=%.4f" % (
                    sr["seg_from"] or "(最早)", sr["est"], sr.get("amount_share") or 0,
                    sr.get("k_share") or 0))
            if "--resolve" in args and not r.get("seg_rows"):
                for it in r["items"]:
                    line = "      %-8s 单价=%-9s 估算 %8.2f" % (
                        it["label"], it["price"], it["est"])
                    if it["amount"] is not None:
                        line += " ｜ 实付 %8.2f ｜ 真单价 %-9.5f ｜ k=%.4f" % (
                            it["amount"], it["unit"] or 0, it["k"] or 0)
                    print(line)
        done.append("bills")
    if "--recompute" in args:
        i = args.index("--recompute")
        mode = args[i + 1] if (i + 1 < len(args) and not args[i + 1].startswith("-")) else "day"
        if mode not in ("day", "missing", "all"):
            print("[重算] mode 只能是 day / missing / all")
            raise SystemExit(4)
        st = cl.recompute(mode)
        print("[重算] 模式=%s 扫描=%d 算出金额=%d 仍缺价=%d 跳过=%d 数值变化=%d 峰谷按时段=%s" % (
            st["mode"], st["scanned"], st["priced"], st["unpriced"], st["skipped"],
            st.get("changed", 0), st.get("hm") or "当前钟点"))
        done.append("recompute")
    if "--stats" in args:
        i = args.index("--stats")
        win = args[i + 1] if (i + 1 < len(args) and not args[i + 1].startswith("-")) else "7d"
        if win not in ("24h", "today", "7d", "30d", "all"):
            print("[统计] 窗口只能是 24h / today / 7d / 30d / all")
            raise SystemExit(4)
        _dump_stats(cl, win)
        done.append("stats")
    return done


def _force_utf8_output():
    """把 stdout/stderr 切成 UTF-8。

    ⚠ 为什么必须做：windowed exe 从中文控制台（cmd / PowerShell）启动时，
    sys.stdout.encoding 是 GBK，而输出里有「¥」「·」等字符 → print 直接抛
    UnicodeEncodeError 崩溃。实测 `HermesCacheMonitor.exe --once` 与 `--stats all`
    双双崩掉（退出码 1），而 README 里正是这么教用户用的。
    对已被 _ensure_output() 换成文件对象的 stdout 同样适用（那也是 TextIOWrapper）。
    """
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue
        try:
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_SINGLE_INSTANCE_MUTEX = None


def _acquire_single_instance():
    """单实例保护（仅 GUI 模式调用）。

    为什么需要（2026-10-06 事故）：
        程序没有单实例锁 → 每被启动一次就多一个进程，且点 ✕ 只是隐藏到托盘
        （不退出），于是进程会无限累积。实测被反复启动后攒到 43 个进程、
        每个约 115MB，把系统内存吃光。
    行为：
        已有实例在跑 → 把那个窗口唤到前台，然后本进程直接退出。
    返回：
        True = 本进程是唯一实例，继续跑；False = 已有实例，本进程应退出。
    """
    global _SINGLE_INSTANCE_MUTEX

    if os.name != "nt":
        return True

    try:
        import ctypes
        from ctypes import wintypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # Local\ 前缀 = 当前登录会话内唯一（同一用户多开会被拦住；
        # 用 Global\ 会跨会话，远程/多用户场景下反而不合适）
        name = "Local\\HermesCacheMonitor_SingleInstance_v1"
        handle = kernel32.CreateMutexW(None, False, name)
        last_err = ctypes.get_last_error()

        if not handle:
            # 拿不到互斥体（极少见）→ 不拦，按老行为继续
            _startup_log("单实例：CreateMutex 失败（err=%d），跳过检查" % last_err)
            return True

        _SINGLE_INSTANCE_MUTEX = handle       # 保持引用，进程活着期间不能释放

        if last_err == ERROR_ALREADY_EXISTS:
            _startup_log("单实例：已有实例在运行，唤醒它后退出")
            _wake_existing_window()
            return False
        return True
    except Exception as ex:
        _startup_log("单实例：检查异常 %s（按无障碍处理）" % ex)
        return True


def _wake_existing_window():
    """把已在运行的悬浮窗唤到前台（找不到就算了，不报错）。"""
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        # 主窗口标题形如「缓存跟随监控 v1.2.0」，按前缀模糊找
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _lparam):
            try:
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                t = buf.value or ""
                if t.startswith("缓存跟随监控"):
                    found.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(_cb, 0)
        if not found:
            return

        hwnd = found[0]
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)     # 从托盘/最小化恢复
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def main():
    _force_utf8_output()
    args = sys.argv
    headless = ("--once" in args or "--sid" in args
                or "--recompute" in args or "--stats" in args
                or "--segments" in args or "--bills" in args
                or "--bill-file" in args or "--resolve" in args
                or "--bill-del" in args)

    # ① 路径解析（自动探测 / 配置文件 / 手动选择）
    ok, msg = init_paths()
    if not ok:
        if headless:
            print("[配置] %s" % msg)
            found = scan_hermes_homes()
            print("[配置] 扫描到 %d 个候选：" % len(found))
            for c in found:
                print("        ", c)
            raise SystemExit(2)
        picked = first_run_dialog(msg)
        if not picked:
            return
        cfg = load_config()
        cfg["hermes_home"] = picked
        save_config(cfg)
        ok, msg = init_paths(cfg)
        if not ok:
            messagebox.showerror("配置失败", msg)
            return

    # ② 兼容性检查
    ok2, msg2 = check_schema()
    if not ok2:
        if headless:
            logp = _ensure_output()
            print("[兼容性] %s" % msg2)
            if logp:
                print("（输出已写入 %s）" % logp)
            raise SystemExit(3)
        messagebox.showerror("数据不兼容", "%s\n\n程序将退出。" % msg2)
        return

    if headless:
        m = Monitor()
        m.ledger.poll()
        time.sleep(0.3)
        m.ledger.poll()
        # headless 也要推进跨日结算（原先只有 GUI 的 tick 才调，导致 CLI 重算拿旧钟点）
        try:
            m.cost_ledger.maybe_settle()
        except Exception:
            pass
        logp = _ensure_output()        # windowed exe：把输出接到数据目录的 cli_output.log
        print("[配置] hermes-home = %s" % HERMES_HOME)
        print("[配置] 数据目录    = %s" % WORK_DIR)
        if _DATA_SPLIT:
            print("[警告] 发现第二份数据（MSIX 虚拟化目录）：%s" % _DATA_SPLIT)
            print("       混用 python / py 会读写不同副本 → 命令行请统一用 py")
        print("[配置] WebView2    = %s" % (HISTORY or "(未找到，已停用该信号)"))
        if logp:
            print("[配置] 完整输出同时写入 = %s" % logp)
        print()
        # 维护动作（重算历史 / 打印统计）优先，跑完即退
        if _do_maint(m, args):
            raise SystemExit(0)
        if "--sid" in args:
            i = args.index("--sid")
            sid = args[i + 1] if i + 1 < len(args) else ""
            d = m.build(sid) if sid else None
        else:
            d = m.current(include_gap=False)
        _dump(m, d)
        raise SystemExit(0)
    # ③ GUI 模式：单实例保护（headless 命令行不拦，否则 --stats 等会被挡住）
    if not _acquire_single_instance():
        return

    App().run()


if __name__ == "__main__":
    main()
