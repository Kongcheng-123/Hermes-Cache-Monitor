# 接口参考（一手实测）

## NewAPI（d1api.xin 类）

```
GET {base}/api/log/token     Header: Authorization: Bearer sk-xxx
→ 200，返回逐条请求流水（含 cache_tokens / quota）
```

原始字段：`id / user_id / created_at / type / content / username /
token_name / model_name / quota / prompt_tokens / completion_tokens / use_time`

### ⚠️ 分页是幻觉（头号坑）

`p` / `page` / `page_size` / 时间范围 / 模型筛选**全部被忽略**，任何请求恒返回同一批「最近 N 条」（源码 `model/log.go`：`WHERE token_id=? ORDER BY id DESC LIMIT 1000`，无分页）。

- 别写翻页循环 —— 会导致数据重复累加 + 耗时爆炸
- **必须用 `request_id` 幂等去重，靠轮询累积**
- ⚠️ 官方 OpenAPI 文档声明的 `key` 参数也是错的，源码不读，**以源码为准**

### 限流

官方默认 `CriticalRateLimit` = 20 次 / 20 分钟 / 按 IP。
d1api.xin 实测未开启，但别的站可能开着 → 采集间隔保守（默认 5 分钟）。

### TLS 抖动

`d1api.xin` 的 TLS 握手偶发超时（同一请求可能 3s 成功、也可能 90s 超时）。
`http_get()` 已做阶梯超时 + 3 次重试，**别去掉**。

### 排查/验算脚本（已归档到 `archive/探索与排查/`）

2026-10-01 主目录瘦身，以下脚本移入归档（功能已固化进代码注释与 README），
需要时可搬回主目录：

| 脚本 | 用途 |
|---|---|
| `probe_paging.py` | 穷举 NewAPI 分页参数（印证「分页是幻觉」） |
| `probe_limit.py` | 测上限 / 限流 / 替代接口 |
| `verify_time.py` | 验本地时间戳语义（时区那套） |
| `verify_0920.py` | 验漏记 / 归日 |
| `verify_quota_formula.py` | 验算计费公式 |
| `recalibrate_coef.py` | 重校准 quota 系数 |
| `diag_747.py` | 排查「接口 vs 网页」次数差异 |

## sub2api（dshapi.icu 类）—— 两套接口，粒度不同

### ① sk- key 接口（聚合，不能归集）

```
GET {base}/v1/usage?days=90     Header: Authorization: Bearer sk-xxx
→ 200，返回 balance + daily_usage[] + model_stats[]
含 cache_read_tokens / cache_write_tokens
可选参数 days（1-90，91 报 400）/ start_date / end_date

GET {base}/v1/sub2api/billing   ★彩蛋：结算倍率
→ {"effective_rate_multiplier":0.08, "peak_rate_enabled":false, ...}
```

⚠️ 是 `/v1/usage`，**不是** `/api/v1/usage`（后者要登录态 JWT）。

### ② 网页端接口（逐条流水，能归集）★ 2026-10-01 打通

```
GET {base}/api/v1/usage?page=N&page_size=M    Header: Bearer <JWT，非 sk- key>
→ {code:0, data:{total, items:[{request_id("client:<uuid>"), created_at(毫秒,+08:00),
     input_tokens, output_tokens, cache_read_tokens, total_cost, actual_cost,
     rate_multiplier, duration_ms, stream, api_key_id, ...}]}}
```

- `total` 是全量条数；`page_size` 可给 1000（实测 OK），一次能拉完
- ⚠️ 以 `created_at` 倒序返回；`start_date` / `end_date` 参数**被忽略**（实测无效）
  → 靠 `page_size` 拉全 + `request_id` 幂等去重累积，跟 NewAPI 同一套路

### ★ 域名池（主站故障自动回退，2026-10-01 实测）

主站 `api.dshapi.icu` 与副站 `api2.dshapi.icu` 是**同一账号的不同入口**
（实测：余额 / 用量 / 倍率完全一致，倍率同为 0.08）。

代码里做成域名池 `BASES = [api2, api]`，按顺序探测谁通用谁。

⚠️ **站点身份与请求域名必须解耦**：
`host` 字段保持 `api.dshapi.icu` 不变（库内历史流水都挂在它名下），
只改 `base_url` / `bases`。否则同一站的账会被劈成两个站。

## 登录态（JWT）维护 —— 一次性 refresh_token 是核心坑

```
POST {base}/api/v1/auth/refresh  {"refresh_token": "rt_xxx"}
→ {code:0, data:{access_token, refresh_token, expires_in:86400, token_type}}
```

- **refresh_token 一次性（强轮换）**：用一次即废，同值二次调用 → 401
  `invalid refresh token`。必须立刻把新值落盘。
- **与浏览器共用账号**：脚本刷新会作废浏览器的 rt → 刷新后必须把新 token
  **反向写回浏览器 localStorage**，否则你的网页会被登出（`sync_to_browser()`）。
- access_token 有效期 86400 秒（24h）；脚本提前 1h 自动续。
- 脚本：`dsh_auth.py`（`--import` 首次喂凭据 / `--status` / `--refresh` / `--test`）
  与 `dsh_flows.py`（拉逐条流水）。已接入 `panel.py` 刷新与 `poll_loop.py`。

### ⚠️ 导入凭据的正确姿势（2026-10-01 修，踩过）

旧逻辑：从浏览器读到 refresh_token 就**立刻**拿去换长效 token。
**这是错的** —— 实测踩到两种情形：

1. refresh_token 已被前端轮换过（它是一次性的）→ 401
2. 换域名期间（主站 TLS 挂 / 切副站）同一个 rt 也会 401

而此时浏览器里的 **access_token 往往还有 20+ 小时有效期、且能正常调接口**。

→ 正确逻辑：**access_token 还有效就直接用，不强行刷新** ——
刷新是消耗品，不该在未知有效性时盲目烧掉一次。
只有 access_token 也失效了，才退回 refresh 尝试。

### 浏览器凭据在 localStorage 的键名

```
auth_token          access_token
refresh_token       refresh_token
token_expires_at    毫秒时间戳
```

走 Kimi WebBridge 读：

```bash
# session 名固定 dsh-auth；中文必须走文件 body，否则乱码
# body.json:
# {"action":"evaluate","args":{"code":"JSON.stringify({a:localStorage.getItem('auth_token')||'',r:localStorage.getItem('refresh_token')||''})"},"session":"dsh-auth"}
curl.exe -s -X POST http://127.0.0.1:10086/command -H "Content-Type: application/json" --data-binary @body.json
```

⚠️ session 没标签页时 evaluate 报 `session "dsh-auth" has no tab` → 先 `navigate`。

## ★ 时区语义（踩过，导致 100% 归集失败）

`session_join.req_ts()` 的约定：

- **能从前 14 位解析出时间戳的 request_id**（NewAPI 格式）走 +8h 分支
- **否则回退直接用 ts 字段，且假定 ts 已是北京时间戳**

dsh 的 request_id 是 UUID → 走回退分支 → 采集时必须自己 `+8h` 补偿，
否则归集距离全差 28800 秒，**一条都归不上**。

同理 `day` 必须直接取 `created_at[:10]`，不能对已偏移的 ts 再 `fromtimestamp(ts, CST)`
（会多 +8h，把一天的账劈成两天）。

### 后果：两个站的 ts 语义不同

- NewAPI 站：ts 是**标准 epoch**
- dsh 站：ts 是**北京时间戳**（epoch + 8h）

→ 任何读 ts 的工具都要能区分。`site_health.py` 的做法：
`ts > now + 1h` 就按 UTC 格式化还原，否则按 CST。
（2026-10-01 踩过：dsh 时间显示成「未来 8 小时」）
