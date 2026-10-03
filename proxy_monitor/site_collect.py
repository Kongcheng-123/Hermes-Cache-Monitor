# -*- coding: utf-8 -*-
"""site_collect.py — 中转站侧用量采集器（NewAPI / sub2api）

独立于 Hermes 本地监控（cache_follow.py），专责从站方 API 拉逐条请求流水。

数据源实测结论（2026-10-01）：
  NewAPI 站: GET {base}/api/log/token?p=N&page_size=100  ← sk- key 直接可用
  sub2api 站: GET {base}/api/v1/usage                    ← 需登录态 session token

用法:
  python site_collect.py                 # 采集全部启用站点
  python site_collect.py --site example.com
  python site_collect.py --full          # 忽略水位，全量重拉
  python site_collect.py --stats         # 只看库内统计，不联网
  python site_collect.py --list          # 列出站点配置
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SITES_PATH = os.path.join(HERE, "sites.json")
STORE_PATH = os.path.join(HERE, "store.db")
HERMES_CONFIG = r"D:\Hermes Agent CN Desktop\data\hermes-home\config.yaml"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ⚠️ quota → 金额 的换算系数
#    · 用户口径（2026-10-01 明确）：两站均 1 美元 = 1 人民币，所以最终金额直接由
#      quota ÷ quota_per_cny 得到「元」，不再二次乘汇率。
#    · NewAPI 的 quota_per_unit 由站方配置（example.com 的 /api/status 公开为 500000），
#      但每个站可不同 → 在 sites.json 里按站配置 quota_per_cny。
#    · 换站/调口径只需改 sites.json，不改代码。
QUOTA_PER_CNY_DEFAULT = 500000.0


# ───────────────────── 采集异常（2026-10-02 修）─────────────────────
# ⚠️ 背景：以前 HTTP 非 200 时适配器直接 `return [], {}`（空结果、不抛异常），
#    上层只有捕获到异常才算失败 → 结果「401/500/TLS 失败」被当成「窗口内无记录」，
#    最终写入 last_ok=1 / last_msg=ok，**静默骗过上层**。
#    这会直接破坏基于站方数据的「差值补贴」逻辑（误以为是完整锚点）。
#    现在：所有非 200 一律抛 CollectError，由 run_site 归类为失败。


class CollectError(Exception):
    """采集失败（HTTP 错误 / 解析失败 / 业务错误）—— 必须让上层知道"""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def _show_api_err(body, limit=180):
    """把接口返回的错误体压成一行，便于日志阅读"""
    try:
        return (body or "").strip().replace("\n", " ")[:limit]
    except Exception:
        return ""


def quota_per_cny(site):
    """取站点的 quota→元 换算系数（sites.json 的 quota_per_cny）"""
    if isinstance(site, dict):
        v = site.get("quota_per_cny")
        if v:
            return float(v)
    return QUOTA_PER_CNY_DEFAULT
CST = timezone(timedelta(hours=8))

PAGE_SIZE = 100
MAX_PAGES = 400                 # 安全上限（400 页 = 4 万条）


# ───────────────────────── 配置读取 ─────────────────────────

def parse_hermes_providers(path=HERMES_CONFIG):
    """从 hermes config.yaml 解析 providers（py 无 pyyaml，纯正则）。

    返回 {prov_name: {base_url, api_key, ...}}
    """
    out = {}
    try:
        raw = open(path, encoding="utf-8").read()
    except Exception:
        return out
    lines = raw.splitlines()
    inprov = False
    cur = None
    buf = {}
    for ln in lines:
        if re.match(r"^providers:\s*$", ln):
            inprov = True
            continue
        if inprov and re.match(r"^\S", ln):
            break
        if not inprov:
            continue
        m = re.match(r"^  ([A-Za-z0-9_:.\-]+):\s*$", ln)
        if m:
            if cur:
                out[cur] = buf
            cur = m.group(1)
            buf = {}
            continue
        if cur:
            m = re.match(r"^    ([a-z_]+):\s*(.+?)\s*$", ln)
            if m:
                buf[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    if cur:
        out[cur] = buf
    return out


def load_sites(autoprobe=True):
    """读站点配置。

    ⚠️ 2026-10-01 起支持【极简配置】：一条站点只写 base_url (+ api_key /
    hermes_providers) 即可，kind 不用填 —— 这里会自动探测补全。

        { "base_url": "https://api.xxx.com/v1", "hermes_providers": ["custom:xxx"] }

     · 显式写了 kind 的站点【不探测】（人工判断优先，也省一次网络请求）
     · 探测结果会缓存进 sites.json 的 _probe_cache，避免每轮都探
     · 探测失败（unknown）不让整个流程崩，只是后续用默认适配器试
    """
    if not os.path.isfile(SITES_PATH):
        return []
    with open(SITES_PATH, encoding="utf-8") as f:
        d = json.load(f)
    sites = d.get("sites", d if isinstance(d, list) else [])

    if not autoprobe:
        return sites

    try:
        import site_probe
    except Exception:
        return sites          # 探测模块缺失时退化为原行为

    cache = d.get("_probe_cache") or {}
    cache_dirty = False
    provs = None

    for s in sites:
        base = s.get("base_url") or ""
        if s.get("kind") or not base:
            continue                       # 显式指定/无地址 → 不探
        hit = cache.get(base)
        if hit and hit.get("kind"):
            s["kind"] = hit["kind"]
            continue
        if provs is None:
            provs = parse_hermes_providers()
        try:
            normalized = site_probe.normalize_site(s, provs)
            s["kind"] = normalized.get("kind") or "unknown"
            cache[base] = {"kind": s["kind"],
                           "reason": normalized.get("_probe", ""),
                           "probed_at": int(time.time())}
            cache_dirty = True
        except Exception as e:
            s["kind"] = "unknown"
            cache[base] = {"kind": "unknown",
                           "reason": "探测异常 %s" % str(e)[:80],
                           "probed_at": int(time.time())}
            cache_dirty = True

    if cache_dirty:
        try:
            d["_probe_cache"] = cache
            tmp = SITES_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SITES_PATH)      # 原子替换
        except Exception:
            pass                             # 写缓存失败不影响采集
    return sites


def resolve_key(site, provs):
    """解析站点 API key：显式 > 环境变量 > hermes config provider。

    返回 (key, 来源说明)
    """
    if site.get("api_key"):
        return site["api_key"], "sites.json"
    env = site.get("api_key_env")
    if env and os.environ.get(env):
        return os.environ[env], "env:" + env
    for pname in site.get("hermes_providers", []):
        p = provs.get(pname)
        if p and p.get("api_key"):
            return p["api_key"], "hermes:%s" % pname
    return "", ""


def resolve_all_keys(site, provs):
    """解析站点下【全部】key（多 key 站点要全部采集，否则与站方账号级统计对不上）

    返回 [(label, key), ...]，去重后返回
    """
    out = []
    seen = set()

    def add(label, k):
        if k and k not in seen:
            seen.add(k)
            out.append((label, k))

    for pname in site.get("hermes_providers", []):
        p = provs.get(pname)
        if p and p.get("api_key"):
            add(pname, p["api_key"])
    # sites.json 里直接写的额外 key（站方网页复制来的）
    for item in site.get("extra_keys", []):
        if isinstance(item, dict):
            add(item.get("label") or "extra", item.get("key") or "")
        elif isinstance(item, str):
            add("extra", item)
    if site.get("api_key"):
        add("sites.json", site["api_key"])
    env = site.get("api_key_env")
    if env and os.environ.get(env):
        add("env:" + env, os.environ[env])
    return out


# ───────────────────────── HTTP ─────────────────────────

def http_get(url, headers, timeout=45, retries=3):
    """带重试的 GET。

    ⚠️ 实测：example.com 的 TLS 握手偶发超时（同一个请求可能 3s 成功、也可能 90s 超时），
    属于网络层抖动而非接口问题 —— 必须重试 + 阶梯超时，否则采集器会随机失败。
    """
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=timeout + attempt * 30)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # HTTP 错误是明确的业务响应，不重试
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    return None, "%s: %s" % (type(last).__name__, str(last)[:160])


# ───────────────────────── 存储 ─────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_flows (
    site         TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    ts           INTEGER,
    day          TEXT,
    model        TEXT,
    group_name   TEXT,
    token_name   TEXT,
    token_id     INTEGER,
    channel      INTEGER,
    in_tokens    INTEGER,      -- 未命中输入（prompt - cache）
    cache_read   INTEGER,      -- 缓存命中
    cache_write  INTEGER,      -- 缓存写入（sub2api 有）
    out_tokens   INTEGER,
    total_prompt INTEGER,      -- 站方原始 prompt_tokens（含缓存）
    quota        INTEGER,      -- 扣费额度
    cost         REAL,         -- 金额（元；用户口径 1 USD = 1 CNY，由 quota ÷ quota_per_cny 得）
    use_time     REAL,
    is_stream    INTEGER,
    billing_src  TEXT,
    raw_json     TEXT,
    collected_at INTEGER,
    PRIMARY KEY (site, request_id)
);
CREATE INDEX IF NOT EXISTS idx_flows_day   ON usage_flows(site, day);
CREATE INDEX IF NOT EXISTS idx_flows_model ON usage_flows(site, model);

CREATE TABLE IF NOT EXISTS collect_state (
    site          TEXT PRIMARY KEY,
    last_run      INTEGER,
    last_ok       INTEGER,
    watermark_ts  INTEGER,     -- 已采到的最新 created_at
    total_rows    INTEGER,
    last_msg      TEXT
);

CREATE TABLE IF NOT EXISTS site_accounts (
    site        TEXT PRIMARY KEY,
    user_id     TEXT,
    username    TEXT,
    plan_title  TEXT,
    plan_total  INTEGER,
    plan_used   INTEGER,
    plan_remain INTEGER,
    rate_multiplier REAL DEFAULT 0,
    mode        TEXT,
    updated_at  INTEGER
);
"""


def db():
    con = sqlite3.connect(STORE_PATH, timeout=30)
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    """轻量迁移：给已存在的旧表补新列 + 重命名 cost→cost"""
    # 1) 列重命名（sqlite 3.25+ 支持 RENAME COLUMN）
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(usage_flows)")}
        if "cost_usd" in have and "cost" not in have:
            con.execute("ALTER TABLE usage_flows RENAME COLUMN cost TO cost")
            con.commit()
    except Exception:
        pass
    # 2) 补新列
    want = {
        "site_accounts": [("rate_multiplier", "REAL DEFAULT 0"), ("mode", "TEXT")],
        "usage_flows": [("cost", "REAL")],
        # ★ 2026-10-02：采集完整性标记（供「差值补贴」判断能否重置基准）
        "collect_state": [("complete", "INTEGER DEFAULT 0"),
                          ("ok_keys", "INTEGER DEFAULT 0"),
                          ("total_keys", "INTEGER DEFAULT 0")],
    }
    for table, cols in want.items():
        if not cols:
            continue
        try:
            have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        except Exception:
            continue
        for col, decl in cols:
            if col not in have:
                try:
                    con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
                except Exception:
                    pass
    con.commit()


def watermark(con, site):
    r = con.execute("SELECT watermark_ts FROM collect_state WHERE site=?", (site,)).fetchone()
    return (r[0] or 0) if r else 0


# ───────────────────── 适配器：NewAPI ─────────────────────

def norm_newapi(row, site_host, per_cny=None):
    """NewAPI /api/log/token 单条记录 → 统一 schema"""
    per_cny = per_cny or QUOTA_PER_CNY_DEFAULT
    other = {}
    try:
        other = json.loads(row.get("other") or "{}")
    except Exception:
        pass
    prompt_total = row.get("prompt_tokens") or 0
    cache_read = other.get("cache_tokens") or 0
    cache_write = other.get("cache_write_tokens") or other.get("cache_creation_tokens") or 0
    in_tokens = max(0, prompt_total - cache_read)
    quota = row.get("quota") or 0
    ts = row.get("created_at") or 0
    return {
        "site": site_host,
        "request_id": row.get("request_id") or ("%s-%s" % (site_host, row.get("id"))),
        "ts": ts,
        "day": datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d") if ts else "",
        "model": row.get("model_name") or "",
        "group_name": row.get("group") or "",
        "token_name": row.get("token_name") or "",
        "token_id": row.get("token_id") or 0,
        "channel": row.get("channel") or 0,
        "in_tokens": in_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "out_tokens": row.get("completion_tokens") or 0,
        "total_prompt": prompt_total,
        "quota": quota,
        "cost": quota / per_cny,          # 元（用户口径 1 USD = 1 CNY）
        "use_time": row.get("use_time") or 0,
        "is_stream": 1 if row.get("is_stream") else 0,
        "billing_src": other.get("billing_source") or "",
        "raw_json": json.dumps(row, ensure_ascii=False),
    }


def collect_newapi(site, key, since_ts=0, log=print):
    """拉取 NewAPI 日志。

    ⚠️ 实测重要结论（2026-10-01，穷举 20+ 组参数验证）：
      /api/log/token 是「最近 N 条」的**固定窗口**接口 —— p / page / page_size /
      start_timestamp / model_name 等参数**全部被服务端忽略**，任何请求都返回
      最新的一批记录（当前实测 510 条，id 从 1 连续到 510，按时间倒序）。
      所以**不能靠翻页取历史**，只能靠「高频轮询 + request_id 去重」累积。

    策略：每次拉窗口内全部记录 → 归一化 → 按 (site, request_id) 幂等入库。
    轮询间隔需小于窗口被写满的时间（510 条 / 按当前用量约数小时→ 3 分钟轮询很安全）。
    返回 (归一化记录列表, 账号信息 dict)
    """
    base = site["base_url"].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    hdr = {"Authorization": "Bearer " + key}
    st, body = http_get("%s/api/log/token?p=0&page_size=100" % base, hdr, timeout=60)
    if st != 200:
        # ⚠️ 不能静默返回空（2026-10-02 修）—— 见 CollectError 注释
        raise CollectError("NewAPI /api/log/token HTTP %s：%s"
                           % (st, _show_api_err(body)), status=st)
    try:
        d = json.loads(body)
    except Exception:
        raise CollectError("NewAPI 响应解析失败：%s" % _show_api_err(body, 150))
    rows = d.get("data") or []
    if not rows:
        log("    窗口内无记录")
        return [], {}
    log("    窗口返回 %d 条" % len(rows))

    # 按 request_id 去重（同一 request_id 可能有多条，只保留 token 数最大的一条）
    best = {}
    for r in rows:
        rid = r.get("request_id") or ("id-%s" % r.get("id"))
        score = (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
        if rid not in best or score > best[rid][0]:
            best[rid] = (score, r)

    norms = [norm_newapi(r, site["host"], per_cny=site.get("quota_per_cny"))
             for _, r in best.values()]
    norms.sort(key=lambda x: x["ts"])

    # 账号/套餐信息：从最新一条的 other 里提取
    acct = {}
    for r in sorted(rows, key=lambda x: -(x.get("created_at") or 0))[:20]:
        try:
            o = json.loads(r.get("other") or "{}")
        except Exception:
            continue
        if o.get("subscription_id"):
            acct = {
                "site": site["host"],
                "user_id": str(r.get("user_id") or ""),
                "username": r.get("username") or "",
                "plan_title": o.get("subscription_plan_title") or "",
                "plan_total": o.get("subscription_total") or 0,
                "plan_used": o.get("subscription_used") or 0,
                "plan_remain": o.get("subscription_remain") or 0,
                "updated_at": int(time.time()),
            }
            break
    return norms, acct


# ───────────────────── 适配器：sub2api ─────────────────────

def norm_sub2api(row, site_host):
    """sub2api usage 逐条记录 → 统一 schema（字段名容错取值）"""
    def g(*names, default=0):
        for n in names:
            v = row.get(n)
            if v is not None:
                return v
        return default

    ts_raw = g("created_at", "timestamp", "time", "created_time", "date")
    ts = 0
    if isinstance(ts_raw, (int, float)) and ts_raw:
        ts = int(ts_raw)
        if ts > 1e12:            # 毫秒
            ts //= 1000
    elif isinstance(ts_raw, str) and ts_raw:
        try:
            ts = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
        except Exception:
            ts = 0

    cache_read = g("cache_read_tokens", "cache_tokens")
    cache_write = g("cache_creation_tokens", "cache_write_tokens")
    in_tokens = g("input_tokens")
    out_tokens = g("output_tokens", "completion_tokens")
    cost = float(g("actual_cost", "cost", "total_cost") or 0)
    quota = int(g("quota") or 0)

    return {
        "site": site_host,
        "request_id": str(g("request_id", "id", "uuid", default="")),
        "ts": ts,
        "day": datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d") if ts else "",
        "model": g("model", "model_name", default=""),
        "group_name": str(g("group_name", "group", default="")),
        "token_name": str(g("api_key_name", "key_name", "token_name", default="")),
        "token_id": g("api_key_id", "token_id"),
        "channel": g("account_id", "channel_id"),
        "in_tokens": in_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "out_tokens": out_tokens,
        "total_prompt": in_tokens + cache_read,
        "quota": quota,
        "cost": cost,
        "use_time": g("duration_ms", "use_time") or 0,
        "is_stream": 1 if g("stream", "is_stream") else 0,
        "billing_src": str(g("billing_mode", "billing_type", default="")),
        "raw_json": json.dumps(row, ensure_ascii=False),
    }


def collect_sub2api(site, key, since_ts=0, log=print):
    """拉取 sub2api 用量。

    ⚠️ 实测重要结论（2026-10-01）：
      正确路径是 **`/v1/usage`**（不是 /api/v1/usage —— 那个需要登录态 JWT）。
      用 **sk- API key** 直连即可 200，返回聚合数据（非逐条流水）：
        { balance, remaining, unit, planName, isValid, mode,
          daily_usage: [{date, requests, input_tokens, output_tokens,
                         cache_read_tokens, cache_write_tokens,
                         total_tokens, cost, actual_cost}],
          model_stats: [{model, requests, input_tokens, output_tokens,
                         cache_creation_tokens, cache_read_tokens, total_tokens,
                         cost, actual_cost, account_cost}],
          usage: {today:{...}, total:{...}, average_duration_ms, rpm} }

      所以 sub2api 侧是「按日 + 按模型」的**聚合口径**，没有逐条 request_id。
      入库时以 `sub2api:<日期>:<模型>` 合成稳定 request_id，保证幂等。
    """
    base = site["base_url"].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    hdr = {"Authorization": "Bearer " + key}

    st, body = http_get(base + "/v1/usage", hdr, timeout=45)
    # ⚠️ 域名故障回退（2026-10-01）：主站曾整站 TLS 挂掉。
    #    sites.json 的 bases 是请求域名池，按顺序试，谁通用谁。
    #    base_url 已是首选域名，这里只补试池里其余的。
    if st != 200:
        for alt in (site.get("bases") or []):
            alt = (alt or "").rstrip("/")
            if not alt or alt == base:
                continue
            st2, body2 = http_get(alt + "/v1/usage", hdr, timeout=45)
            if st2 == 200:
                log("    首选域名不通（%s），回退 %s 成功" % (st, alt))
                base, st, body = alt, st2, body2
                break
    if st != 200:
        # ⚠️ 不能静默返回空（2026-10-02 修）—— 见 CollectError 注释
        raise CollectError("sub2api /v1/usage HTTP %s：%s"
                           % (st, _show_api_err(body)), status=st)
    try:
        d = json.loads(body)
    except Exception:
        raise CollectError("sub2api 响应解析失败：%s" % _show_api_err(body, 150))

    log("    余额 %s %s，计划 %s" % (
        d.get("balance"), d.get("unit"), d.get("planName")))

    # ★ 结算倍率（彩蛋接口）：标价 × rate = 实付。用于成本校准，免去手工价格表
    rate = 0.0
    st2, b2 = http_get(base + "/v1/sub2api/billing", hdr, timeout=25, retries=2)
    if st2 == 200:
        try:
            rate = float(json.loads(b2).get("effective_rate_multiplier") or 0)
            log("    结算倍率 effective_rate_multiplier = %s" % rate)
        except Exception:
            pass

    norms = []
    # ── 逐日 × 模型：daily_usage 只给总量，model_stats 只给区间总量
    #    两者维度不同，这里分别按「日」和「模型」两个维度入库，用不同 rid 前缀区分
    for it in (d.get("daily_usage") or []):
        day = it.get("date") or ""
        if not day:
            continue
        try:
            ts = int(datetime.strptime(day, "%Y-%m-%d")
                     .replace(tzinfo=CST).timestamp())
        except Exception:
            ts = 0
        in_t = it.get("input_tokens") or 0
        cr = it.get("cache_read_tokens") or 0
        norms.append({
            "site": site["host"],
            "request_id": "sub2api-daily:%s" % day,
            "ts": ts, "day": day,
            "model": "*",
            "group_name": str(d.get("planName") or ""),
            "token_name": "", "token_id": 0, "channel": 0,
            "in_tokens": in_t,
            "cache_read": cr,
            "cache_write": it.get("cache_write_tokens") or 0,
            "out_tokens": it.get("output_tokens") or 0,
            "total_prompt": in_t + cr,
            "quota": 0,
            "cost": float(it.get("actual_cost") or it.get("cost") or 0),
            "use_time": 0,
            "is_stream": 0,
            "billing_src": str(d.get("mode") or ""),
            "raw_json": json.dumps(it, ensure_ascii=False),
        })

    for it in (d.get("model_stats") or []):
        model = it.get("model") or ""
        if not model:
            continue
        in_t = it.get("input_tokens") or 0
        cr = it.get("cache_read_tokens") or 0
        norms.append({
            "site": site["host"],
            "request_id": "sub2api-model:%s" % model,
            "ts": int(time.time()), "day": "",
            "model": model,
            "group_name": str(d.get("planName") or ""),
            "token_name": "", "token_id": 0, "channel": 0,
            "in_tokens": in_t,
            "cache_read": cr,
            "cache_write": it.get("cache_creation_tokens") or 0,
            "out_tokens": it.get("output_tokens") or 0,
            "total_prompt": in_t + cr,
            "quota": 0,
            "cost": float(it.get("actual_cost") or it.get("cost") or 0),
            "use_time": 0,
            "is_stream": 0,
            "billing_src": "model-stat",
            "raw_json": json.dumps(it, ensure_ascii=False),
        })

    acct = {
        "site": site["host"],
        "user_id": "",
        "username": "",
        "plan_title": str(d.get("planName") or ""),
        "plan_total": 0,
        "plan_used": 0,
        "plan_remain": int(float(d.get("remaining") or 0) * 500000),
        "rate_multiplier": rate,
        "mode": str(d.get("mode") or ""),
        "updated_at": int(time.time()),
    }
    return norms, acct


ADAPTERS = {"newapi": collect_newapi, "sub2api": collect_sub2api}


# ───────────────────────── 主流程 ─────────────────────────

# ── 采集端保留过滤（★ 2026-10-02）──
# 背景：站方接口恒返回最近约 1000 条（含很久以前的）。如果先删库、下轮采集又
# 把老数据拉回来入库 → 「删了又拉、拉了又删」死循环，保留策略形同虚设。
#
# 方案 B（用户 2026-10-02 选定）：**从采集源头过滤** —— 早于保留期的记录
# 直接不入库。这样站方再拉回来也无所谓，库自然稳定在保留窗口内。
#
# ⚠️ 与 poll_loop.auto_retention() 的分工：
#    · 这里（upsert）：拦住**新进来**的老数据
#    · poll_loop：清理**历史上已经存下**的过期数据（首次启用时用得上）
#    两者配合才完整：只做前者，库里旧的存量清不掉；只做后者，会删了又拉。
#
# 设 0 或负数 = 关闭过滤（保留全部）
KEEP_DAYS = 7

# 供上层读取「本轮因超期被丢弃多少条」（日志用）
_UPSERT_STAT = {}


def keep_days():
    """当前保留天数（可用环境变量覆盖，便于测试）"""
    try:
        v = os.environ.get("PROXY_KEEP_DAYS")
        if v is not None:
            return int(v)
    except Exception:
        pass
    return KEEP_DAYS


def upsert(con, norms):
    """幂等写入：request_id 已存在则更新（站方记录可能后续才补全 quota 等字段）

    ★ 2026-10-02：增加保留期过滤 —— 早于 KEEP_DAYS 的记录直接跳过不入库。
    """
    # 保留期过滤
    kd = keep_days()
    dropped = 0
    if kd and kd > 0:
        cutoff = int(time.time()) - kd * 86400
        kept = []
        for r in norms:
            ts = r.get("ts") or 0
            # ts 为 0 的（解析失败）不拦，避免误伤
            if ts and ts < cutoff:
                dropped += 1
                continue
            kept.append(r)
        norms = kept

    cols = ["site", "request_id", "ts", "day", "model", "group_name", "token_name",
            "token_id", "channel", "in_tokens", "cache_read", "cache_write",
            "out_tokens", "total_prompt", "quota", "cost", "use_time",
            "is_stream", "billing_src", "raw_json"]
    now = int(time.time())
    placeholders = ",".join("?" * len(cols))
    updates = ",".join("%s=excluded.%s" % (c, c) for c in cols if c not in ("site", "request_id"))
    sql = ("INSERT INTO usage_flows (%s, collected_at) VALUES (%s, ?) "
           "ON CONFLICT(site, request_id) DO UPDATE SET %s, collected_at=excluded.collected_at"
           % (",".join(cols), placeholders, updates))
    n_before = con.execute("SELECT COUNT(*) FROM usage_flows").fetchone()[0]
    for r in norms:
        con.execute(sql, [r.get(c) for c in cols] + [now])
    con.commit()
    n_after = con.execute("SELECT COUNT(*) FROM usage_flows").fetchone()[0]
    if dropped:
        _UPSERT_STAT["dropped"] = _UPSERT_STAT.get("dropped", 0) + dropped
    return n_after - n_before


def run_site(site, provs, full=False, log=print):
    host = site["host"]
    kind = site.get("kind", "newapi")
    log("─" * 70)
    log("站点 %s  [%s]  %s" % (host, kind, site.get("base_url", "")))
    if kind == "unknown":
        log("  ⚠️ 类型未识别（自动探测没命中）。")
        log("     该站可能不是标准中转站。若确认是 NewAPI/sub2api，")
        log("     在 sites.json 里给它显式写 \"kind\": \"newapi\" 或 \"sub2api\" 即可。")
        return None
    if kind == "official":
        log("  ⏭ 官方站（非中转站），跳过")
        return None

    keys = resolve_all_keys(site, provs)
    if not keys and not site.get("session_token") and not site.get("cookie"):
        log("  ✗ 无可用凭据（跳过）")
        return None
    log("  凭据: %d 个 key（%s）" % (len(keys), "、".join(k for k, _ in keys)))

    con = db()
    wm = 0 if full else watermark(con, host)
    if wm:
        log("  水位线: %s" % datetime.fromtimestamp(wm, CST).strftime("%Y-%m-%d %H:%M"))

    fn = ADAPTERS.get(kind, collect_newapi)
    t0 = time.time()
    all_norms = []
    acct = {}
    errs = []
    ok_keys = 0          # ★ 2026-10-02：统计成功的 key 数，用于完整性判定
    total_keys = len(keys)
    for label, key in keys:
        try:
            norms, a = fn(site, key, since_ts=wm, log=(lambda m: None))
            all_norms.extend(norms)
            ok_keys += 1
            if a and not acct:
                acct = a
            log("    %-34s → %d 条" % (label[-34:], len(norms)))
        except Exception as e:
            errs.append("%s: %s" % (label, str(e)[:120]))
            log("    %-34s → 失败 %s" % (label[-34:], str(e)[:120]))

    # ── 完整性判定（★ 2026-10-02 新增，供「差值补贴」等上层消费者判断可信度）──
    #   complete=1：全部 key 成功 → 本轮数据可作权威锚点
    #   complete=0：部分/全部失败 → 数据不完整，上层不应据此重置基准
    complete = 1 if (total_keys and ok_keys == total_keys) else 0
    if total_keys == 0:
        complete = 0                      # 无凭据，谈不上完整

    if not all_norms and errs:
        msg = "; ".join(errs)[:200]
        con.execute("""INSERT INTO collect_state (site,last_run,last_ok,last_msg,complete,ok_keys,total_keys)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(site) DO UPDATE SET last_run=?,last_msg=?,
                         last_ok=0, complete=0, ok_keys=?, total_keys=?""",
                    (host, int(time.time()), 0, msg, complete, ok_keys, total_keys,
                     int(time.time()), msg, ok_keys, total_keys))
        con.commit()
        con.close()
        return None

    # 部分 key 失败：有数据但不算完整 → 仍入库，但标记 complete=0 并记下失败原因
    if errs and all_norms:
        log("  ⚠️ 部分 key 失败（%d/%d 成功）—— 本轮标记为不完整"
            % (ok_keys, total_keys))

    # 同一站多 key 可能返回重叠记录 → upsert 前先按 request_id 去重
    dedup = {}
    for n in all_norms:
        rid = n["request_id"]
        prev = dedup.get(rid)
        if prev is None:
            dedup[rid] = n
        else:
            a = (prev.get("in_tokens") or 0) + (prev.get("cache_read") or 0) + (prev.get("out_tokens") or 0)
            b = (n.get("in_tokens") or 0) + (n.get("cache_read") or 0) + (n.get("out_tokens") or 0)
            if b > a:
                dedup[rid] = n
    norms = list(dedup.values())

    added = upsert(con, norms)
    # 本轮被保留策略拦掉的条数（upsert 里记的，读后清零）
    dropped = _UPSERT_STAT.get("dropped", 0)
    _UPSERT_STAT["dropped"] = 0
    new_wm = max([n["ts"] for n in norms if n.get("ts")] + [wm])
    # ★ 2026-10-02：写入完整性标记。complete=1 时才是「可信锚点」。
    # 部分 key 失败仍入库（数据不浪费），但 complete=0，上层可据此拒绝重置基准。
    ok_msg = "ok" if complete else ("不完整：%d/%d key 失败 — %s"
                                    % (total_keys - ok_keys, total_keys,
                                       "; ".join(errs)[:120]))
    con.execute("""INSERT INTO collect_state
                     (site,last_run,last_ok,watermark_ts,total_rows,last_msg,
                      complete,ok_keys,total_keys)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(site) DO UPDATE SET
                     last_run=?, last_ok=?, watermark_ts=?,
                     total_rows=(SELECT COUNT(*) FROM usage_flows WHERE site=?),
                     last_msg=?, complete=?, ok_keys=?, total_keys=?""",
                (host, int(time.time()), 1, new_wm, len(norms), ok_msg,
                 complete, ok_keys, total_keys,
                 int(time.time()), 1, new_wm, host, ok_msg,
                 complete, ok_keys, total_keys))
    if acct:
        con.execute("""INSERT INTO site_accounts (site,user_id,username,plan_title,
                          plan_total,plan_used,plan_remain,rate_multiplier,mode,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(site) DO UPDATE SET
                          user_id=?,username=?,plan_title=?,plan_total=?,
                          plan_used=?,plan_remain=?,rate_multiplier=?,mode=?,updated_at=?""",
                    (acct["site"], acct["user_id"], acct["username"], acct["plan_title"],
                     acct["plan_total"], acct["plan_used"], acct["plan_remain"],
                     acct.get("rate_multiplier") or 0, acct.get("mode") or "", acct["updated_at"],
                     acct["user_id"], acct["username"], acct["plan_title"], acct["plan_total"],
                     acct["plan_used"], acct["plan_remain"],
                     acct.get("rate_multiplier") or 0, acct.get("mode") or "", acct["updated_at"]))
    con.commit()
    con.close()
    if dropped:
        log("  ✓ 拉到 %d 条，新增 %d 条（另有 %d 条超出 %d 天保留期，已跳过），耗时 %.1fs"
            % (len(norms), added, dropped, keep_days(), time.time() - t0))
    else:
        log("  ✓ 拉到 %d 条，新增 %d 条，耗时 %.1fs" % (len(norms), added, time.time() - t0))
    return {"site": host, "fetched": len(norms), "added": added, "dropped": dropped}


def show_stats(con=None):
    own = con is None
    con = con or db()
    print("\n" + "=" * 78)
    print("站方采集库统计  %s" % STORE_PATH)
    print("=" * 78)
    tot = con.execute("SELECT COUNT(*) FROM usage_flows").fetchone()[0]
    print("总流水条数: %d" % tot)
    if not tot:
        print("（库为空，先跑一次采集）")
        if own:
            con.close()
        return

    print("\n── 按站点 ──")
    for r in con.execute("""SELECT site, COUNT(*) n, MIN(day), MAX(day),
                                   SUM(in_tokens), SUM(cache_read), SUM(out_tokens),
                                   SUM(cost)
                            FROM usage_flows GROUP BY site ORDER BY n DESC"""):
        print("  %-20s n=%-7d %s ~ %s" % (r[0], r[1], r[2], r[3]))
        print("      in=%-12d cache=%-12d out=%-10d cost=$%.4f" % (
            r[4] or 0, r[5] or 0, r[6] or 0, r[7] or 0))

    print("\n── 按日（近 15 天）──")
    rows = con.execute("""SELECT day, COUNT(*) n, SUM(in_tokens) i, SUM(cache_read) c,
                                 SUM(out_tokens) o, SUM(cost) cost
                          FROM usage_flows WHERE day != ''
                          GROUP BY day ORDER BY day DESC LIMIT 15""").fetchall()
    print("  %-12s %-7s %-13s %-13s %-11s %s" % ("日期", "请求", "输入", "缓存命中", "输出", "花费$"))
    for d, n, i, c, o, cost in rows:
        hit = (c or 0) / ((i or 0) + (c or 0)) * 100 if (i or 0) + (c or 0) else 0
        print("  %-12s %-7d %-13d %-13d %-11d %.4f   (命中率 %.1f%%)" % (
            d, n, i or 0, c or 0, o or 0, cost or 0, hit))

    print("\n── 按模型 ──")
    mrows = con.execute("""SELECT model, COUNT(*) n, SUM(in_tokens) i, SUM(cache_read) c,
                                  SUM(out_tokens) o, SUM(cost) cost
                           FROM usage_flows GROUP BY model ORDER BY n DESC LIMIT 12""").fetchall()
    print("  %-30s %-7s %-13s %-13s %-11s %s" % ("模型", "请求", "输入", "缓存命中", "输出", "花费$"))
    for m, n, i, c, o, cost in mrows:
        hit = (c or 0) / ((i or 0) + (c or 0)) * 100 if (i or 0) + (c or 0) else 0
        print("  %-30s %-7d %-13d %-13d %-11d %.4f  (%.1f%%)" % (
            m, n, i or 0, c or 0, o or 0, cost or 0, hit))

    print("\n── 账号/套餐 ──")
    for r in con.execute("""SELECT site, username, plan_title, plan_total, plan_used, plan_remain
                            FROM site_accounts"""):
        used_pct = (r[4] or 0) / r[3] * 100 if r[3] else 0
        print("  %-20s %s 套餐=%s  %d/%d (%.2f%%)  余 %d" % (
            r[0], r[1], r[2], r[4] or 0, r[3] or 0, used_pct, r[5] or 0))

    print("\n── 采集状态 ──")
    for r in con.execute("""SELECT site, last_run, last_ok, watermark_ts, total_rows, last_msg
                            FROM collect_state"""):
        ts = datetime.fromtimestamp(r[1], CST).strftime("%m-%d %H:%M") if r[1] else "-"
        wm = datetime.fromtimestamp(r[3], CST).strftime("%m-%d %H:%M") if r[3] else "-"
        print("  %-20s 上次=%s ok=%s 水位=%s 本次=%s %s" % (
            r[0], ts, r[2], wm, r[4], r[5] or ""))
    if own:
        con.close()


def main():
    ap = argparse.ArgumentParser(description="中转站侧用量采集器")
    ap.add_argument("--site", help="只采某个站点 host")
    ap.add_argument("--full", action="store_true", help="忽略水位全量重拉")
    ap.add_argument("--stats", action="store_true", help="只显示库内统计")
    ap.add_argument("--list", action="store_true", help="列出站点配置")
    args = ap.parse_args()

    if args.stats:
        show_stats()
        return

    sites = load_sites()
    provs = parse_hermes_providers()

    if args.list:
        print("已配置站点 %d 个：\n" % len(sites))
        for s in sites:
            key, src = resolve_key(s, provs)
            print("  %-20s kind=%-8s enabled=%-5s 凭据=%s" % (
                s.get("host"), s.get("kind"), s.get("enabled", True),
                src or ("session" if s.get("session_token") else "无")))
        return

    if not sites:
        print("没有站点配置（%s 不存在）" % SITES_PATH)
        return

    results = []
    for s in sites:
        if not s.get("enabled", True):
            continue
        if args.site and s.get("host") != args.site:
            continue
        results.append(run_site(s, provs, full=args.full))

    print()
    show_stats()


if __name__ == "__main__":
    main()
