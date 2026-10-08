# -*- coding: utf-8 -*-
"""按对话归集站方请求 v4 —— 站点约束 + 数量对账（2026-10-08 重写）

## 演进史

v1  会话级 MIN/MAX 宽窗口      → 一个 9 小时对话吸走期间所有请求
v2  纯时间最近邻               → 无站点约束，跨站错配（4.9%）
v3  站点约束 + 时间窗           → 跨站错配归零，但站内仍靠时间猜；
                                  `site_only` 档把窗口外 30 分钟的流水硬塞进来
v4  站点约束 + **数量对账**      → 本次

## v3 实测暴露的问题（2026-10-08）

会话 `20261007_231651_97570e`（Hermes 记录 `api_call_count=3`）：
  · v3 归给它 245 条流水（site_near 111 + site_only 134）
  · 时间是 23:09 ~ 00:09，比会话实际调用窗（23:17~23:55）还宽
  · 根因①：WINDOW_PAD=300 把窗口撑大，相邻时段连成一片
  · 根因②：`site_only` 档用 MAX_DIST=1800 兜底 → 窗口外半小时也归，纯猜
  · 根因③：**完全没有用 `api_call_count` 这个硬事实**

## v4 策略：时间是软证据，数量是硬约束

```
第 1 步  站点约束   sites.json 的 hermes_providers → 候选会话
第 2 步  时间定位   时间窗内（含小 padding）的流水，按距离排序
第 3 步  数量对账   每个会话最多吃下 api_call_count 条
                    超出的 → 标 unassigned，不硬塞
```

**为什么数量是硬约束**：Hermes 自己记账，`session_model_usage.api_call_count`
就是「这个会话真实发起了几次 API 调用」。站方流水条数**不应该超过**它
（可以少于 —— 采集遗漏/站方口径差异）。这是唯一能从数学上约束归属的依据。

**三档 match_kind**：
  · `site_count`  站点匹配 + 窗内 + 数量配额内  → 高置信（0.9）
  · `site_multi`  同上但该时段有多个候选会话，按距离竞争配额 → 中置信（0.6）
  · `none`        配额用尽 / 窗内无候选 → **诚实标未归属**

**回退**：sites.json 没登记该 provider → 退回纯时间最近邻（低置信，保底不丢）。
"""
import bisect
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UTC = timezone.utc
HERMES_DB = r"D:\Hermes Agent CN Desktop\data\hermes-home\state.db"
HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "store.db")
SITES = os.path.join(HERE, "sites.json")

# 回退档（纯时间）的距离阈值
MAX_DIST = 1800.0
GOOD_DIST = 120.0
# 时间窗两侧的宽容（秒）：只补「调用结束→流水落库」的尾延迟，不能太大
# ⚠️ v3 用 300 秒，导致相邻时段连片 → 降到 90 秒
WINDOW_PAD = 90.0

# host 别名：同一站多个域名（流水表与 sites.json 记的可能不同）
HOST_ALIASES = {
    "api.dshapi.icu": "api2.dshapi.icu",
    "api4.dshapi.icu": "api2.dshapi.icu",
}


def _norm_host(u):
    s = (u or "").strip().lower()
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    return s.split("/")[0].split(":")[0]


def main_domain(u):
    """取主域名（用于「按域名自动认站」）。

    api9.dshapi.icu     → dshapi.icu
    newmirror.example.com → example.com
    localhost / IP      → 原样返回（不折叠）

    ★ 2026-10-09 新增：用户换中转站入口（如 api9.example.com）后，Hermes 记录的
      billing_provider 会退化成裸 `custom`（没有 `:后缀`），而 sites.json 里
      登记的是 `custom:your-provider` → provider 映射不上、bases 里又没这个新入口，
      → 该会话之后的流水全部归集不上（实测 1627 条 match_kind=none）。
      对策：归集时**以 API 地址的主域名为主**（域名是硬事实，不随名字变），
      provider 名只作辅助。同一主域名下挂多个站时不猜（返回全部候选）。
    """
    h = _norm_host(u)
    if not h:
        return ""
    if h in ("localhost", "127.0.0.1") or re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
        return h
    parts = h.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return h


def canon_host(h):
    h = _norm_host(h)
    return HOST_ALIASES.get(h, h)


def load_domain_map(dbg=None):
    """{主域名: [站点 host...]} —— 按 API 地址自动认站用的索引。

    ★ 2026-10-09 新增。站点配置里所有地址（base_url + bases）的主域名都进索引，
      归集时拿会话的 billing_base_url 一查就知道属于哪个站。
      同一主域名下挂多个站 → 列表里有多个候选，调用方**不猜**（返回全部，
      交给上层标「需要用户确认」）。
    """
    out = defaultdict(list)
    try:
        cfg = json.load(open(SITES, encoding="utf-8"))
    except Exception as e:
        if dbg:
            dbg("⚠️ 读 sites.json 失败（域名索引为空）：%s" % e)
        return out
    sites = cfg.get("sites") if isinstance(cfg, dict) else cfg
    for s in (sites or []):
        if not isinstance(s, dict):
            continue
        host = _norm_host(s.get("host") or "")
        if not host:
            host = _norm_host(s.get("base_url") or "")
        if not host:
            continue
        for u in [s.get("base_url")] + list(s.get("bases") or []):
            dm = main_domain(u)
            if dm and host not in out[dm]:
                out[dm].append(host)
    return out


def find_site_by_domain(base_url, dmap):
    """按 API 地址的主域名找站点。唯一命中才返回 host，多个候选返回 None（不猜）。

    ★ 2026-10-09：这是「换入口域名后自动认站」的核心。实测：
        api9.dshapi.icu   → dshapi.icu  → api.dshapi.icu ✓
        newmirror.example.com → example.com → example.com ✓
      认不出来时返回 None，调用方继续走 provider 映射 / 时间兜底。
    """
    dm = main_domain(base_url)
    if not dm:
        return None
    cands = dmap.get(dm) or []
    if len(cands) == 1:
        return cands[0]
    return None


def load_provider_map(dbg=None):
    """{provider名: site host}, {base_url host: site host}"""
    pmap, bmap = {}, {}
    try:
        cfg = json.load(open(SITES, encoding="utf-8"))
    except Exception as e:
        if dbg:
            dbg("⚠️ 读 sites.json 失败：%s（退回纯时间归集）" % e)
        return pmap, bmap
    sites = cfg.get("sites") if isinstance(cfg, dict) else cfg
    for s in (sites or []):
        if not isinstance(s, dict):
            continue
        host = _norm_host(s.get("base_url") or s.get("host") or "")
        if not host:
            continue
        bmap[host] = host
        for p in (s.get("hermes_providers") or []):
            if isinstance(p, str) and p.strip():
                pmap[p.strip()] = host
    return pmap, bmap


def req_ts(rid, fallback):
    """request_id 前 14 位是 UTC 时刻（NewAPI）；解析失败用 fallback。"""
    if rid and len(rid) >= 14:
        try:
            t = datetime.strptime(rid[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            return int(t.timestamp())
        except Exception:
            pass
    return fallback


def run(verbose=True):
    def log(*a):
        if verbose:
            print(*a)

    pmap, bmap = load_provider_map(dbg=log)
    log("站点→provider 映射：%d 名 / %d host" % (len(pmap), len(bmap)))
    # ★ 2026-10-09：主域名 → 站点。归集时域名优先（provider 名会因换地址退化）
    dmap = load_domain_map(dbg=log)

    hcon = sqlite3.connect("file:%s?mode=ro" % HERMES_DB.replace("\\", "/"), uri=True)
    hcon.row_factory = sqlite3.Row

    # ── 候选会话：session_model_usage（带 provider + 调用次数 + 时间窗）──
    usages = []
    try:
        usages = hcon.execute("""
            SELECT session_id, billing_provider, billing_base_url,
                   api_call_count, first_seen, last_seen
            FROM session_model_usage WHERE first_seen > 0""").fetchall()
    except Exception as e:
        log("⚠️ session_model_usage 读失败：%s" % e)
    log("session_model_usage：%d 行" % len(usages))

    # site → [(sid, lo, hi, quota, prov)]   quota = api_call_count
    site_sessions = defaultdict(list)
    by_domain_only = 0
    for u in usages:
        prov = (u["billing_provider"] or "").strip()
        burl = (u["billing_base_url"] or "").strip()
        # ★ 顺序很重要：先域名（硬事实）→ 再 provider（可能已退化成裸 custom）
        host = None
        if burl:
            host = find_site_by_domain(burl, dmap)
            if host:
                by_domain_only += 1
        if not host:
            host = pmap.get(prov) or (bmap.get(_norm_host(burl)) if burl else None)
        if not host:
            continue
        lo = (u["first_seen"] or 0) - WINDOW_PAD
        hi = (u["last_seen"] or u["first_seen"] or 0) + WINDOW_PAD
        site_sessions[host].append(
            (u["session_id"], lo, hi, int(u["api_call_count"] or 0), prov))
    log("候选会话：%d 个站点在用（其中 %d 条靠域名认出来）"
        % (len(site_sessions), by_domain_only))
    for h, v in sorted(site_sessions.items()):
        log("  %-22s %d 条会话记录（配额合计 %d）" % (
            h, len(v), sum(x[3] for x in v)))

    # ── 回退用：messages 时间轴 ──
    times, sids = [], []
    for r in hcon.execute("""SELECT session_id, timestamp FROM messages
                             WHERE timestamp > 0 ORDER BY timestamp"""):
        times.append(r["timestamp"])
        sids.append(r["session_id"])
    log("Hermes 消息：%d 条 / %d 会话" % (len(times), len(set(sids))))

    # ── 流水 ──
    scon = sqlite3.connect(STORE)
    scon.row_factory = sqlite3.Row
    flows = scon.execute("""SELECT request_id, ts, site FROM usage_flows
                            WHERE request_id NOT LIKE 'sub2api-%'
                            ORDER BY ts""").fetchall()
    log("站方流水：%d 条" % len(flows))

    # ── 预分配：按站点把流水分组，准备做配额竞争 ──
    #   配额账本：{site: {sid: 剩余配额}}
    quota_left = {}
    for host, cands in site_sessions.items():
        quota_left[host] = defaultdict(int)
        for (sid, lo, hi, q, prov) in cands:
            quota_left[host][sid] += q

    # 先把流水按站点归堆，每堆内按 ts 排序（已整体排好，这里分桶）
    by_site = defaultdict(list)
    for f in flows:
        by_site[canon_host(f["site"])].append(f)

    stats = defaultdict(int)
    conf_sum = 0.0
    batch = []

    for host, fl in by_site.items():
        cands = site_sessions.get(host)
        if not cands:
            # 回退：没登记的站 → 纯时间最近邻
            for f in fl:
                ts = req_ts(f["request_id"], f["ts"])
                if times and ts:
                    i = bisect.bisect_left(times, ts)
                    bd, bs = None, None
                    for j in range(max(0, i - 3), min(len(times), i + 4)):
                        dd = abs(times[j] - ts)
                        if bd is None or dd < bd:
                            bd, bs = dd, sids[j]
                    if bd is not None and bd <= GOOD_DIST:
                        stats["near"] += 1
                        conf = max(0.1, 1.0 - bd / (GOOD_DIST * 4))
                        batch.append((f["site"], f["request_id"], bs, "near", conf, bd))
                        conf_sum += conf
                        continue
                    if bd is not None and bd <= MAX_DIST:
                        stats["far_ok"] += 1
                        batch.append((f["site"], f["request_id"], bs, "far_ok", 0.5, bd))
                        conf_sum += 0.5
                        continue
                stats["far"] += 1
                batch.append((f["site"], f["request_id"], None, "none", 0.0, -1))
            continue

        # 有候选 → 配额竞争
        #  ① 每条流水先算出「窗内候选 + 距离」，按距离升序（近的优先拿配额）
        scored = []
        for f in fl:
            ts = req_ts(f["request_id"], f["ts"])
            hits = []
            for (sid, lo, hi, q, prov) in cands:
                if lo <= ts <= hi:
                    # 窗内：距离取「到窗中心」的距离，用于同分排序
                    mid = (lo + hi) / 2.0
                    hits.append((abs(ts - mid), sid))
            scored.append((ts, f, sorted(hits)))

        #  ② 按时间先后处理，窗内候选 ≥1 时给它最近的、且还有配额的会话
        for ts, f, hits in scored:
            if not hits:
                stats["outside"] += 1
                batch.append((f["site"], f["request_id"], None, "none", 0.0, -1))
                continue
            picked = None
            for (d, sid) in hits:
                if quota_left[host].get(sid, 0) > 0:
                    picked = (d, sid)
                    break
            if picked is None:
                # 窗内有候选但配额都用光了 → 诚实标未归属
                stats["quota_out"] += 1
                batch.append((f["site"], f["request_id"], None, "none", 0.0, -1))
                continue
            d, sid = picked
            quota_left[host][sid] -= 1
            multi = len(hits) > 1
            kind = "site_multi" if multi else "site_count"
            conf = 0.6 if multi else 0.9
            stats[kind] += 1
            conf_sum += conf
            batch.append((f["site"], f["request_id"], sid, kind, conf, d))

    scon.executescript("""
    DROP TABLE IF EXISTS flow_sessions_tmp;
    CREATE TABLE flow_sessions_tmp (
        site TEXT, request_id TEXT, session_id TEXT,
        match_kind TEXT, confidence REAL, dist_sec REAL,
        PRIMARY KEY (site, request_id)
    );
    """)
    scon.executemany("""INSERT INTO flow_sessions_tmp
                     (site,request_id,session_id,match_kind,confidence,dist_sec)
                     VALUES (?,?,?,?,?,?)""", batch)
    scon.executescript("""
    BEGIN EXCLUSIVE;
    DROP TABLE IF EXISTS flow_sessions;
    ALTER TABLE flow_sessions_tmp RENAME TO flow_sessions;
    CREATE INDEX IF NOT EXISTS idx_fs_session ON flow_sessions(session_id);
    COMMIT;
    """)

    tot = sum(stats.values()) or 1
    log()
    log("=== 归集结果（v4 站点约束 + 数量对账）===")
    for k, label in [
        ("site_count", "站点+独占(高信)"),
        ("site_multi", "站点+竞争(中信)"),
        ("outside",    "窗口外"),
        ("quota_out",  "配额用尽"),
        ("near",       "回退·近距"),
        ("far_ok",     "回退·中距"),
        ("far",        "无法归属"),
    ]:
        v = stats.get(k, 0)
        if v:
            log("  %-18s %-7d (%.1f%%)" % (label, v, v / tot * 100))
    ok = tot - stats.get("far", 0) - stats.get("outside", 0) - stats.get("quota_out", 0)
    log("  可归属 %d / %d = %.1f%%" % (ok, tot, ok / tot * 100))
    log("  平均置信度 %.2f" % (conf_sum / tot))

    scon.close()
    hcon.close()
    return dict(stats)


def main():
    run(verbose=True)


def main_quiet():
    return run(verbose=False)


if __name__ == "__main__":
    main()
