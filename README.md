# Hermes Cache Monitor (缓存跟随监控)

专为 **Hermes Agent (CN 桌面版)** 打造的极简、轻量、高颜值桌面 HUD 缓存跟随与第三方中转站实际扣费对账系统。
描述是ai写的（
---

## ✨ 核心特性

- 🎯 **实时会话跟随**：通过 Windows 极低开销的 UIA 与事件信号，秒级感知 Hermes 桌面版当前活动的会话，实时统计当前对话的**缓存命中率、命中 Token、未命中 Token**。
- 💎 **现代极简 HUD 视觉**：
  - 深度调用 Windows 11 DWM 沉浸式暗黑标题栏，底色与窗口浑然一体，彻底告别刺眼白边；
  - 模块化站点胶囊（Badges），一目了然掌握各站今日消耗与调用次数。
- 🗕 **一键迷你胶囊模式**：
  - 右上角一键（或双击窗口任意位置）秒切为 86px 的极简桌面微型小条，只保留命中率、Token 读数与价格；
  - 手动调整窗口宽度后自动锁定记忆，展开折叠绝不粗暴重置。
- 🔕 **系统托盘与静止休眠**：
  - 点击右上角 `✕` 自动隐藏至 Windows 任务栏系统托盘（隐藏图标区），双击随时唤回；
  - 智能焦点感知：当用户全屏游戏或离开 Hermes 时自动降频至 3 秒；无人使用时跳过网络请求，日常维持 **0.0% CPU 占用与 ~16MB 内存**。
- ⚖ **中转站实扣采集与价格表自动同步**：
  - 兼容主流 **NewAPI** 与 **sub2api** 架构的中转站；
  - 自动抓取站方真实的每笔扣费、账号余额与套餐消费；
  - 支持 **一键将站方实际扣款折算单价/倍率回填到本地价格表**，彻底告别手工填价！
  - 建议交给agent给中转站进行站点监控配置，需要站点监控是因为hermes的数据来源不全，经常会丢token

---

## 🚀 快速上手

### 方式 A：直接运行打包版（开箱即用，推荐）
1. 前往 GitHub **Releases** 页面下载最新发布的 `HermesCacheMonitor_v5_Release.zip`；
2. 解压到任意目录，直接双击运行 `HermesCacheMonitor.exe` 即可（无需安装 Python 环境）。

### 方式 B：源码运行（适合开发者）
环境需求：Windows 10 / 11，Python 3.10+
```bash
# 1. 克隆本仓库
git clone https://github.com/your-username/Hermes-Cache-Monitor.git
cd Hermes-Cache-Monitor

# 2. 安装必要依赖
pip install uiautomation comtypes pystray Pillow

# 3. 运行悬浮窗
pythonw cache_follow.py
```

---

## ⚙️ 中转站对账配置（可选）

若需要自动同步第三方中转站后台的真实扣款与套餐余额：
1. 将 `proxy_monitor/sites.example.json` 重命名为 `proxy_monitor/sites.json`；
2. 填入你的中转站地址和 Hermes 中对应的 Provider 标识（程序会自动从你的 Hermes `config.yaml` 读 key）：
```json
[
  {
    "name": "我的中转站",
    "base_url": "https://api.your-proxy-site.com/v1",
    "hermes_providers": [
      "custom:your-provider-name"
    ],
    "enabled": true
  }
]
```
3. 打开悬浮窗后，后台每 5 分钟会自动进行增量采集；
4. 右键悬浮窗点击 **`🌐 打开网页看板 (8788)`** 即可在浏览器查看大盘报表；
5. 右键悬浮窗点击 **`⚡ 从中转站实扣同步价格表`** 即可一键同步真实单价！

---

## 📚 深度技术文档与排障手册

本项目沉淀了详尽的中转站实测对账与避坑档案，供深度定制与排障参考：

- 📖 [01. 加站与自动化体检指南](docs/01-加站与自动化体检指南.md)（NewAPI 与 sub2api 自动嗅探及余量体检）
- 🔌 [02. 中转站接口参考手册](docs/02-中转站接口参考手册(NewAPI与sub2api).md)（一手抓包实测字段与分页机制）
- 💰 [03. 计费换算与 Quota 对账规则](docs/03-计费换算与Quota对账规则.md)（真实人民币折算与公式中位校准）
- ⚠ [04. 常见踩坑与避坑全集](docs/04-常见踩坑与避坑全集.md)（时区 8h 漂移、聚合重复相加、TLS 握手应对）
- 🛠 [05. 运维自启与编码规范](docs/05-运维自启与编码规范.md)（PowerShell 5.1 BOM 避坑与单实例锁）

---

## 🛠️ 自行打包 EXE

本仓库已内置 PyInstaller 打包规范：
```bash
pyinstaller build/HermesCacheMonitor.spec --distpath dist --workpath build/work --noconfirm
```
生成的单文件可执行程序将位于 `dist/HermesCacheMonitor.exe`。

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 协议开源。
