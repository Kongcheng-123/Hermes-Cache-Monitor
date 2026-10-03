# 中转站侧用量监控 · 说明文档

> 建立：2026-10-01　|　状态：**双站打通，与站方网页对账误差 1.7%**
> 详细交接：`<项目目录>\handoffs\中转站用量监控：站方数据采集与对账.md`
> 网页用法：`网页使用说明.md`

---

## 一、这系统干什么的

从**中转站站方 API** 抓真实用量数据（不是从 Hermes 本地估算），用于：

- 看每个站**真实花了多少钱**（站方扣费口径，不是估算）
- 看**每个对话**的缓存命中率和花费
- 与站方网页对账，定位差异

**与既有的 `cache_follow.py`（Hermes 本地监控）完全独立**，这是两套系统：

| | 数据源 | 用途 |
|---|---|---|
| `cache_follow.py` | Hermes `state.db` | 本地视角（会话快照） |
| **本项目** | 站方 API | **站方权威账单** |

---

## 二、快速上手

```powershell
cd "<项目目录>\proxy_monitor"

# 采一轮
python site_collect.py

# 起 Web 面板（推荐日常用这个）
python panel.py --open          # 自动开浏览器 → http://127.0.0.1:8788

# 起采集守护（放后台，5 分钟一轮）
python poll_loop.py

# 文本报告 + 对账
python site_report.py

# 按对话归集（采集后跑，面板会自动调）
python session_join.py

# 查站点有哪些 key
python find_keys.py example.com
```

**无第三方依赖**，标准库即可。

---

## 三、已打通的接口（一手实测）

### NewAPI（example.com）—— 逐条流水

```
GET {base}/api/log/token
Header: Authorization: Bearer sk-xxxxx
```

⚠️ **关键实测结论**（穷举 20+ 组参数 + 官方 Go 源码交叉验证）：

- `p` / `page` / `page_size` / `start_timestamp` / `model_name` **全被忽略**
- 恒返回「最近约 1000 条」（源码 `MaxRecentItems=1000`，无分页）
- **必须靠高频轮询 + `request_id` 去重累积**，不能翻页取历史
- 官方默认限流 `CriticalRateLimit` = 20 次/20 分钟/按 IP
  （example.com 实测**未开启**，连打 50+ 次 0 拦截；但按开启来设计）

返回字段关键部分：
```json
{
  "request_id": "202609301905548935499718268d9d6YBrnc3Cs",
  "created_at": 1790795157,
  "model_name": "deepseek-v4.1-flash",
  "quota": 97,                    // 扣费额度
  "prompt_tokens": 61536,         // ⚠ 含缓存
  "completion_tokens": 71,
  "token_name": "key1", "group": "svip",
  "other": {                      // JSON 字符串，需二次解析
    "cache_tokens": 101248,       // ⭐ 缓存命中
    "cache_ratio": 0.02,
    "completion_ratio": 4,
    "model_ratio": 0.03,
    "subscription_consumed": 128, // 与 quota 完全一致
    "subscription_plan_title": "中胃袋套餐"
  }
}
```

### sub2api（api.dshapi.icu）—— 两套接口，粒度不同 ★

**① sk- key 接口（聚合，不能归集）**
```
GET {base}/v1/usage?days=90
Header: Authorization: Bearer sk-xxx
→ 账号级聚合：balance + daily_usage[] + model_stats[]
```
⚠️ **是 `/v1/usage`，不是 `/api/v1/usage`**（后者要登录态 JWT）
★ 彩蛋 `GET /v1/sub2api/billing` → `effective_rate_multiplier`（结算倍率 0.08）

**② 网页端接口（逐条流水，能归集）★ 2026-10-01 打通**
```
GET {base}/api/v1/usage?page=N&page_size=M     ← 实测 page_size=1000 可一次拉全
Header: Authorization: Bearer <登录态 JWT，不是 sk- key>
→ {code:0, data:{total, items:[{request_id, created_at(毫秒),
     model, input_tokens, output_tokens, cache_read_tokens,
     total_cost, actual_cost, rate_multiplier, duration_ms, stream,
     api_key_id, user_agent, ip_address, ...}]}}
```
- `request_id` 形如 `client:<uuid>`，**不带时间戳**（与 NewAPI 不同）
- `created_at` 带 `+08:00` 偏移，**毫秒级** → 归集用
- 有真分页（`total` + `page`/`page_size` 有效）；`start_date` 等参数**被忽略**

**登录态维护（`dsh_auth.py`）**
```
POST /api/v1/auth/refresh  {"refresh_token": "rt_xxx"}
→ {code:0, data:{access_token, refresh_token, expires_in:86400, token_type}}
```
- ⚠️ **refresh_token 一次性**（强轮换）：用一次即作废，必须立刻存新值。
  实测同值二次调用 → 401 `invalid refresh token`
- ⚠️ 与浏览器共用账号时：我们刷新会作废浏览器的 refresh_token，
  因此**刷新后必须反向写回浏览器 localStorage**（`dsh_auth.sync_to_browser()`）
- 首次喂凭据：`python dsh_auth.py --import`（走 Kimi WebBridge 读浏览器）

**逐条采集（`dsh_flows.py`）**
```
python dsh_flows.py          # 采一轮（自动续期）
python dsh_flows.py --stats  # 看库内
```

**⚠️ 时区坑（踩过，必读）**：站方 `created_at` 的 `fromisoformat().timestamp()`
是最新 UTC 绝对时刻，而 `session_join.req_ts()` 对「非 NewAPI 格式 request_id」
**回退时假定 ts 已是北京时间戳**。若不在采集时 `+8h` 补偿 → 归集距离全差 28800 秒
→ **100% 归集失败**。`dsh_flows.norm()` 已补偿。
同样，`day` 必须直接取 `created_at[:10]`，不能对已偏移的 ts 再 `fromtimestamp(ts, CST)`（会多 +8h，把一天的账劈成两天）。

---

## 四、⚠️ 七个必知坑

### 0. ★★ 站方网页是「账号级」，多 key 必须全采（本次最大的坑）

站方网页统计的是**账号下所有 key 的汇总**，而 `/api/log/token` **按 key 隔离**。

只采一个 key → 与网页**永远差 10%+**，而且**怎么调系数都对不上**。

**解法**：`sites.json` 里把账号下**全部 key** 都配上（`hermes_providers` + `extra_keys`）。
用 `find_keys.py` 或站方网页「API 密钥」页核对。

> example.com 示例站通常配多个 key（本机按需添加）
> 只配了 `key1` 时差 11.6%；三个全配后差 1.7%（纯时间差）。

### 1. 金额换算：`quota ÷ 500000 = 元`

站方使用记录页显示官方单价 **`标准 · ¥0.06 / ¥0.24/M`**（输入/输出，元每百万 token）。

单条验算：
```
记录 prompt=399,324（缓存 396,800 / 未命中 2,524）output=142，quota=331
按官方单价算 = ¥0.00066168
quota÷500000 = ¥0.00066200   ← 差 1.0 倍，吻合
```

系数在 `sites.json` 的 `quota_per_cny`，**换站/调口径只改这个，不动代码**。

### 2. 智能审批会污染命中率

特征极明显：**输入 330~850、输出恒为 3、缓存恒为 0、非流式、耗时 1~2 秒**。

它天然吃不到缓存 → 混进统计会把命中率拉低。
实测占**条数 14%，花费仅 0.5%**。

**处理**：命中率只算业务请求，审批单独列。
```sql
-- 识别条件
out_tokens <= 10 AND cache_read = 0 AND total_prompt < 1500
```

### 3. TLS 握手偶发超时

example.com 会随机卡死（同一请求可能 3 秒成功、也可能 90 秒超时）。
`http_get()` 已做**阶梯超时 + 3 次重试**，别去掉。

### 4. Hermes 侧 `billing_base_url` 有别名

同一 host 拆成 4 个 provider 别名（`custom:your-provider` / `-2` / `custom` / `auto`）
+ 2 种 URL 变体（尾斜杠）。**统计必须 `LIKE '%host%'`**，否则漏 31%。

### 5. Hermes 侧不是流水，是快照

`session_model_usage` 是「会话×模型×任务」累计，`first_seen`/`last_seen` 是
**会话活动窗口**（实测最长 71 小时），**无法精确归日**。

### 6. 窗口不对等会让总量对比毫无意义

站方只留近 1000 条，本地是全量历史。直接比总量得到 ±几百% 的**假差异**。
**必须只在「共同日」上严格对比。**

### 7. 改了代码必须重启进程

Python 服务不热加载。旧进程会继续往库里写旧口径的数据（曾因此出现 ¥3.18 的错误显示）。

---

## 五、口径与换算

```
金额（元）        = quota ÷ quota_per_cny   （默认 500000）
站方 prompt       ≈ Hermes input + cache_read
站方 quota        == subscription_consumed   （完全一致，就是套餐真实扣减）
业务命中率        = cache_read ÷ (cache_read + in_tokens)   ← 剔除审批
```

**`request_id` 前 14 位 = 请求的 UTC 时刻**（实测与 `created_at` 恒差 -28804 秒 = 8 小时时区）
→ 这是「按对话归集」的基础。

---

## 六、按对话归集（`session_join.py`）

**原理**：用 `request_id` 解析出精确时刻，在 Hermes `messages` 表的时间戳里做**最近邻匹配**，
把站方流水归到对应会话。

**为什么用最近邻**（踩过的坑）：
- ✗ 会话级 MIN/MAX 宽窗口 → 一个 9 小时长对话把所有请求吸走
- ✗ 5 分钟间隙切片段 → 切出 1472 个碎片，76.6% 失配
- ✓ **最近邻**（消息间隔中位数仅 3~6 秒，最近邻几乎总命中同一会话）

实测：**87~91% 可归属，平均置信度 0.88**。

---

## 七、配置文件 `sites.json`　★ 加站只需填两行

**2026-10-01 起支持极简配置** —— `kind` 不用填，系统自动探测：

```json
{
  "base_url": "https://api.xxx.com/v1",
  "hermes_providers": ["custom:xxx"]      // key 从 hermes config.yaml 现读
}
```

就这样，两行。加进去就自动能被采集。

### 完整字段（都可省，除 base_url）

```json
{
  "base_url": "https://api.xxx.com/v1",  // ★唯一必填
  "host": "api.xxx.com",                 // 省略 → 从 base_url 推
  "kind": "newapi",                      // 省略 → 自动探测（推荐省略）
  "hermes_providers": ["custom:xxx"],    // key 来源（从 config.yaml 现读）
  "api_key": "sk-xxx",                   // 或直接写 key
  "extra_keys": [{"label":"vip键","key":"sk-xxx"}],  // 站方网页上的其他 key
  "quota_per_cny": 500000,               // 换算系数，默认 500000
  "label": "显示名",                      // 默认用 host
  "enabled": true
}
```

### `kind` 自动探测（`site_probe.py`）

| 网站类型 | 判定特征 | 说明 |
|---|---|---|
| **NewAPI** | `/api/status` 返回含 `quota_per_unit` | 接口：`/api/log/token` |
| **sub2api** | `/v1/sub2api/billing` 返回含 `rate_multiplier`，或该路径返回 **401** | 接口：`/v1/usage` |
| official | 域名命中官方站列表 | 自动跳过（不需要监控） |
| unknown | 都不命中 | 打印提示，不采集 |

**两层探测**：先无鉴权探（不消耗额度），失败再带 key 探（更准）。
实测 18 个站 → 12 个自动判出；带 key 后 `littlecold`(NewAPI) 与 `tryaigc`(sub2api) 均判对。

**人工优先**：显式写了 `kind` 的站**不探测** —— 你说了算。

探测结果缓存在 `_probe_cache`，不会每轮都探。

### 加了站怎么生效

```bash
cd "<项目目录>/proxy_monitor"
python site_collect.py --list      # 看类型判得对不对
python site_collect.py             # 采一轮
```

守护进程（5 分钟一轮）会自动带上新站，**不用重启**。

### ★ 新 key 来了怎么办（2026-10-02 定规）

**判据是「账号」，不是「域名」也不是「key」。**

| 情况 | 做法 |
|---|---|
| **同账号的新 key**（最常见） | **什么都不用做** —— 站方 `/v1/usage` 是账号级聚合，新 key 的用量自动含在账号总额里；多 key 采集彼此幂等去重 |
| **另一个中转站** | 走上面的「填两行」 |
| **新账号**（注册了第二个账号） | 作为独立站点加 `sites.json` |

**怎么判断同不同账号**：拿新 key 查 `/v1/usage`，与现有 key 比余额。

```
余额一致  → 同账号 → 不用配
余额不同  → 不同账号 → 作为独立站加进来
```

⚠️ **失效 key 要清掉**：实测某个失效的 provider 别名长期返回
`401 INVALID_API_KEY`，但因为旧版采集器静默吞错误，一直没被发现。
新版会报「不完整：N/M key 失败」，看到就把失效 key 从 `hermes_providers` 删掉。

### 采集完整性标记（2026-10-02 新增）

`collect_state` 表新增三列：

| 列 | 含义 |
|---|---|
| `complete` | 1 = 全部 key 成功（**可信锚点**）；0 = 部分/全部失败 |
| `ok_keys` / `total_keys` | 成功的 key 数 / 应有的 key 数 |

**用途**：融合显示层要用站方值重置基准前，**必须确认 `complete=1`** ——
否则「某个 key 挂了但整站仍标记成功」会让站方累计值偏低，误当完整锚点后
把本地待确认增量错误清零，等 key 恢复就会突然补涨。

部分失败时**仍有数据就入库**（不浪费），但标记 `complete=0` 并写明原因。

### 体检怎么看这些

```bash
python site_health.py
```

会显示每站的 `complete` 状态与失败 key 明细。

### kind=unknown 怎么办

启动时会打印：

```
⚠️ 类型未识别（自动探测没命中）。
   若确认是 NewAPI/sub2api，在 sites.json 里显式写 "kind": "newapi" 即可。
```

按提示补一行 `kind` 就好 —— 这是**唯一**需要手动介入的情形。

---

## 八、文件清单

### 运行时（8 个）

| 文件 | 作用 |
|---|---|
| `sites.json` | 站点配置（**加站只改这里**） |
| `site_collect.py` | 采集器（多 key + 幂等 + 重试 + 自动探测接入） |
| `site_probe.py` | ★ 站点类型自动探测 |
| `poll_loop.py` | 采集守护（5 分钟一轮） |
| `panel.py` | Web 面板（127.0.0.1:8788，含刷新按钮） |
| `session_join.py` | 按对话归集 |
| `site_report.py` | 文本报告 + 对账 + HTML |
| `dsh_auth.py` / `dsh_flows.py` | dsh 站登录态 + 逐条流水 |

### 辅助

| 文件 | 作用 |
|---|---|
| `find_keys.py` | 查站点账号下有哪些 key（加站时用） |
| `store.db` | 采集库（`usage_flows` / `flow_sessions` / `site_accounts`） |
| `网页使用说明.md` | 面板用法 |
| `archive/` | 一次性勘探/排查脚本（见 `archive/说明.md`） |
| `对账参考/` | recon 对账分析器（独立可跑） |
| `DESIGN.md` | 设计文档 |

**排查脚本**已移入 `archive/探索与排查/`（含 `verify_*` 验算类、`diag_*` 排查类、
`probe_*` 参数穷举类），详见 `archive/说明.md`。需要时搬回主目录即可运行。

---

## 九、当前状态与待办

**运行中（✅ 已开机自启，2026-10-01）**：

| 进程 | 作用 |
|---|---|
| `poll_loop.py` | 采集守护（5 分钟一轮） |
| `panel.py` | Web 面板（127.0.0.1:8788） |

自启挂在计划任务 `HermesDesktopAutoStart` → `<项目目录>\hermes_autostart.ps1`。
**幂等**：已在跑则跳过，不会重复堆进程。想手动验证：跑一遍那个 ps1，应输出
`proxy-monitor: already running (2 proc)`。

⚠️ **该 ps1 必须存成 UTF-8 with BOM** —— PowerShell 5.1 默认按 GBK 读 .ps1，
无 BOM 时中文路径（`<项目目录>\...`）会被读坏，`Test-Path` 误判 not found。
（2026-10-01 踩过这个坑）

**监控站点（2 个）**：`example.com`（NewAPI）、`api.dshapi.icu`（sub2api，请求走 api2 副域名）

**体检**：

```bash
python site_health.py       # 站点/库体积/采集余量/异常，一屏看完
```

**dshapi 逐条流水（2026-10-01 打通）**：

| 文件 | 作用 |
|---|---|
| `dsh_auth.py` | 登录态维护（JWT 自动续期 + 反向同步浏览器） |
| `dsh_flows.py` | 抓网页端逐条流水（`client:<uuid>` 行），可归集到对话 |
| `dsh_auth.json` | 凭据存储（含 refresh_token） |

- 已接入 `panel.py` 的「立即刷新」与 `poll_loop.py` 流程
- 归集率 100%（平均距离 2 秒）

**待办**：
- [x] ~~开机自启~~（2026-10-01 完成）
- [x] ~~加站自动化~~（2026-10-01 完成：`site_probe.py` 自动判类型，配置只填两行）
- [x] ~~瘦身~~（2026-10-01 完成：33 个 py → 9 个，勘探脚本移入 `archive/`）
- [ ] 采集改并发 —— 站变多时才需要，`site_health.py` 会在占用超 60% 时提醒
- [ ] tryaigc/littlecold 逐条流水（需登录态；当前已从监控移除）
- [ ] 考虑并入 `cache_follow.py`（低，需授权，建议「开一扇只读窗」而非合并代码）

**已知遗留**：
- `dsh_auth.json` 里的 refresh_token **一次性**，谁刷新谁持有。
  若长期不开 `poll_loop`，浏览器与脚本的 refresh 链可能各自漂移 →
  任一方掉线时跑 `python dsh_auth.py --import` 重新对齐即可。

---

## 十、唤起词

「接着做中转站监控」→ 读本文件 + `handoffs\中转站用量监控：站方数据采集与对账.md`
