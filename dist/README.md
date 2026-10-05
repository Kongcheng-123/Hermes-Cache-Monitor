# dist 目录说明

这个目录**不进版本库**（见仓库根目录 `.gitignore` 的 `dist/`）。
把编译好的 exe 放在这里只是为了打包/发版方便。

## 怎么拿到 exe

**普通用户** —— 去 [Releases](https://github.com/Kongcheng-123/Hermes-Cache-Monitor/releases)
下载最新版的 `HermesCacheMonitor.exe`，双击即用，不需要装 Python。

**开发者** —— 自己编译：

```bash
# 1. 装依赖（必须包含 pystray 和 Pillow，否则托盘功能会静默失效）
pip install uiautomation comtypes pystray Pillow pyinstaller

# 2. 在项目根目录执行
py -m PyInstaller "build/HermesCacheMonitor.spec" ^
   --distpath "dist" --workpath "build/work" --noconfirm
```

产物就是本目录下的 `HermesCacheMonitor.exe`（单文件，约 37 MB）。

## 打包时会发生什么

`build/HermesCacheMonitor.spec` 做了三件事：

1. **依赖强校验** —— 缺 `pystray` / `Pillow` / `uiautomation` / `comtypes`
   会直接报错退出，不会打出一个功能残缺的包。
2. **把 `proxy_monitor/` 一起打进 exe** —— 站方采集模块（`session_join`、
   `calib_export`、`panel`、`poll_loop` 等）是运行时从磁盘 import 的，
   不打进去的话，「校准」和「打开网页看板」会失效。
3. **自动剔除运行时数据** —— 数据库、日志、个人配置（`store.db`、`poll.log`、
   `sites.json`、`dsh_auth.json` 等）不会被打包，只带代码和 `.example` 模板。

## exe 启动时会做什么

首次运行，exe 会检查自己同级目录下有没有可用的 `proxy_monitor/`：

- **有** → 直接用（单机复用，不复制）
- **没有** → 从 exe 内部释放一份到同级目录

所以单文件 exe 拷到任何地方都能跑，站方功能开箱可用。
如果 `proxy_monitor/` 里已经有你的 `store.db`（采集数据），
exe 会优先复用它，不会覆盖。

> 想指定用哪个目录，可以设环境变量 `HERMES_CACHE_PM_DIR`。
