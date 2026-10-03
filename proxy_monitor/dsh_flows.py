# -*- coding: utf-8 -*-
"""dsh_flows.py — 抓 dshapi（sub2api 站）的【逐条流水】

为什么需要它（2026-10-01 实测）：
  · site_collect.py 的 sub2api 适配器走 sk- key + /v1/usage → 只有聚合数据，
    一行 = 一整天，没有时刻 → session_join 无法归集到对话（dsh 面板「按对话」是空的）。
  · 网页端 /api/v1/usage 才是【逐条流水】：带 request_id + 毫秒 created_at，
    可归集。已实测「按对话」能跑通。

接口（实测）：
  GET /api/v1/usage?page=N&page_size=M     Header: Authorization: Bearer <JWT>
  → {code:0, data:{total, items:[{request_id, created_at, model, input_tokens,
       output_tokens, cache_read_tokens, cache_creation_tokens,
       total_cost, actual_cost, rate_multiplier, duration_ms, stream,
       api_key_id, inbound_endpoint, user_agent, ip_address, ...}]}}
  · total 是全量条数；page_size 可给大（1000 实测 OK），一次能拉完。
  · ⚠️ 以 created_at 倒序返回。start_date/end_date 参数被忽略（实测无效），
    所以靠 page_size 拉全 + request_id 幂等去重累积，跟 NewAPI 是同一套路。

用法：
  python dsh_flows.py              # 采一轮（自动续期）
  python dsh_flows.py --pages 5    # 最多翻 5 页
  python dsh_flows.py --stats      # 看库内 dsh 逐条情况
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dsh_auth                                    # noqa: E402
import site_collect as sc                          # noqa: E402

CST = timezone(timedelta(hours=8))
STORE = os.path.join(HERE, "store.db")
# ⚠️ 站点身份 vs 请求域名（2026-10-01 切换，重要）：
#   · SITE 是【库里的站点身份】，历史数据全挂在 'api.dshapi.icu' 名下，
#     换域名后必须保持不变，否则同一站的账会被劈成两个站。
#   · BASES 是【实际请求域名池】。主站 api.dshapi.icu 曾整站 TLS 挂掉，
#     副站 api2.dshapi.icu 是同一账号的另一个入口（余额/用量/倍率一致）。
#   即：换域名 ≠ 换站点，两者解耦。
# 2026-10-04 改造：不再硬编码域名，全部从 sites.json 读（配置驱动）。
#   配置里给 sub2api 站写 host / bases 即可；读不到时用占位符，不会崩。
try:
    from . import site_resolver as _sr
except ImportError:
    import site_resolver as _sr

_S2 = _sr.default_sub2api_site()
SITE = _sr.host_of(_S2) or "sub2api.example.com"
BASES = _sr.bases_of(_S2) or ["https://api.example.com"]
API = "/api/v1/usage"
PAGE_SIZE = 1000          # 实测可给 1000，一次拉完最省事
MAX_PAGES = 30            # 安全上限


def pick_base(log=None):
    """探测哪个域名当前可用（都不通则返回首选，让上层拿到明确错误）"""
    for b in BASES:
        try:
            r = sc.http_get(b + "/", {"User-Agent": sc.UA}, timeout=8, retries=1)
            if r[0] and r[0] < 500:
                if log:
                    log("  dsh 域名可用: %s" % b)
                return b
        except Exception:
            continue
    return BASES[0]


def fetch_items(tok, pages=MAX_PAGES, log=print, base=None):
    """按页拉逐条流水，按 request_id 去重"""
    base = base or pick_base(log)
    out = {}
    total = None
    for p in range(1, pages + 1):
        url = "%s%s?page=%d&page_size=%d" % (base, API, p, PAGE_SIZE)
        st, body = sc.http_get(url, {"Authorization": "Bearer " + tok,
                                     "User-Agent": sc.UA}, timeout=45, retries=3)
        if st != 200:
            log("    第%d页 HTTP %s：%s" % (p, st, (body or "")[:120]))
            break
        try:
            j = json.loads(body)
        except Exception:
            log("    第%d页 解析失败" % p)
            break
        if j.get("code") != 0:
            log("    第%d页 业务失败：%s" % (p, j.get("message")))
            break
        d = j.get("data") or {}
        items = d.get("items") or []
        if total is None:
            total = d.get("total")
        if not items:
            break
        new = 0
        for it in items:
            rid = it.get("request_id") or ""
            if rid and rid not in out:
                out[rid] = it
                new += 1
        log("    第%d页 %d 条（新增 %d，累计 %d）" % (p, len(items), new, len(out)))
        if len(items) < PAGE_SIZE:
            break                      # 最后一页
    return list(out.values()), total


def norm(it, host="api.dshapi.icu"):
    """逐条 → 统一 schema（与 site_collect 的 upsert 兼容）

    ⚠️ 时区关键（2026-10-01 踩坑）：
      站方 created_at 带 +08:00 偏移，fromisoformat().timestamp() 得到的是
      【正确的 UTC 绝对时刻】。而 session_join.req_ts() 的约定是：
        优先从 request_id 前 14 位解析（NewAPI 格式），否则回退用 ts 字段，
        且【回退时假定 ts 已经是「北京时间戳」】（它对 request_id 分支会 +8h）。
      dsh 的 request_id 是 UUID，走回退分支 → 若不补偿，ts 会少 8 小时，
      归集距离全部差 28800 秒 → 100% 归集失败（实测踩过）。
      → 这里统一存【北京时间戳】，与 NewAPI 分支语义一致。
    """
    ts_raw = it.get("created_at") or ""
    ts = 0
    if ts_raw:
        try:
            # 拿到 UTC 绝对时刻（标准 Unix epoch）
            ts = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
        except Exception:
            ts = 0
    cr = it.get("cache_read_tokens") or 0
    it_in = it.get("input_tokens") or 0
    cost = float(it.get("actual_cost") or it.get("total_cost") or 0)
    # ⚠️ day 直接取站方 created_at 的日期部分（站方已按 +08:00 返回 → 就是北京日期）。
    #   不能用 fromtimestamp(ts, CST)：ts 已是「北京时间戳」，再按 CST 转会多 +8h，
    #   导致 17:00 后的请求被算到第二天（实测踩过：一天的账被劈成两天）。
    day = (ts_raw[:10] if ts_raw else "")
    return {
        "site": host,
        "request_id": it.get("request_id") or "",
        "ts": ts,
        "day": day,
        "model": it.get("model") or "",
        "group_name": str(it.get("group_id") or ""),
        "token_name": str(it.get("api_key_id") or ""),
        "token_id": it.get("api_key_id") or 0,
        "channel": it.get("account_id") or 0,
        "in_tokens": it_in,
        "cache_read": cr,
        "cache_write": it.get("cache_creation_tokens") or 0,
        "out_tokens": it.get("output_tokens") or 0,
        "total_prompt": it_in + cr,
        "quota": 0,                    # sub2api 无 quota，金额直接用 actual_cost
        "cost": cost,
        "use_time": (it.get("duration_ms") or 0) / 1000.0,
        "is_stream": 1 if it.get("stream") else 0,
        "billing_src": "webapi-flow",
        "raw_json": json.dumps(it, ensure_ascii=False),
    }


def main():
    ap = argparse.ArgumentParser(description="抓 dshapi 逐条流水")
    ap.add_argument("--pages", type=int, default=MAX_PAGES)
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    if a.stats:
        con = sqlite3.connect(STORE)
        n = con.execute("SELECT COUNT(*) FROM usage_flows WHERE site='api.dshapi.icu' "
                        "AND request_id LIKE 'client:%'").fetchone()[0]
        print("dsh 逐条流水条数:", n)
        for r in con.execute("""SELECT day, COUNT(*) n, SUM(in_tokens) i,
                                       SUM(cache_read) cr, SUM(out_tokens) o, SUM(cost) c
                                FROM usage_flows WHERE site='api.dshapi.icu'
                                  AND request_id LIKE 'client:%'
                                GROUP BY day ORDER BY day DESC LIMIT 10"""):
            print("  %s  请求=%d 输入=%d 缓存=%d 输出=%d 花费=¥%.6f" % r)
        return

    print("=" * 70)
    print("dshapi 逐条流水采集  %s" % datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)

    tok = dsh_auth.ensure_token(log=lambda m: print(m))
    if not tok:
        print("✗ 拿不到登录态。请先跑： python dsh_auth.py --import")
        return
    if not dsh_auth.verify(tok):
        print("✗ token 已失效，尝试续期…")
        s = dsh_auth.refresh()
        if not s or not dsh_auth.verify(s["access_token"]):
            print("✗ 续期后仍不可用。需重新 --import")
            return
        tok = s["access_token"]

    t0 = time.time()
    items, total = fetch_items(tok, pages=a.pages)
    print("  合计拉到 %d 条（站方 total=%s）" % (len(items), total))
    if not items:
        print("  （无新数据）")
        return

    norms = [norm(it) for it in items if it.get("request_id")]
    con = sc.db()
    added = sc.upsert(con, norms)
    con.commit()
    con.close()
    print("  ✓ 新增 %d 条，耗时 %.1fs" % (added, time.time() - t0))
    print("\n  提示：跑 python session_join.py 让这些流水归集到对话")


if __name__ == "__main__":
    main()
