# -*- coding: utf-8 -*-
"""site_report.py — 站方用量监控报告 + 与 Hermes 本地对账

用法:
  python site_report.py                 # 控制台报告
  python site_report.py --html out.html # 生成 HTML 面板
  python site_report.py --recon         # 只看对账
  python site_report.py --days 7        # 限定天数
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "store.db")
HERMES_DB = r"D:\Hermes Agent CN Desktop\data\hermes-home\state.db"
CST = timezone(timedelta(hours=8))


def q(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args).fetchall()]


# ────────────────── 站方侧 ──────────────────

def site_summary(con, days=None):
    out = {}
    where = ""
    args = []
    if days:
        since = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
        where = "WHERE day >= ?"
        args = [since]

    out["sites"] = q(con, """
        SELECT site, COUNT(*) n, MIN(day) d0, MAX(day) d1,
               SUM(in_tokens) in_t, SUM(cache_read) cache_r, SUM(cache_write) cache_w,
               SUM(out_tokens) out_t, SUM(cost) cost
        FROM usage_flows WHERE %s %s GROUP BY site ORDER BY n DESC""" % (
        NON_FLOW, ("AND " + where[6:] if where else "")), args)

    out["daily"] = q(con, """
        SELECT site, day, COUNT(*) n, SUM(in_tokens) in_t, SUM(cache_read) cache_r,
               SUM(cache_write) cache_w, SUM(out_tokens) out_t, SUM(cost) cost
        FROM usage_flows WHERE %s AND day != '' %s
        GROUP BY site, day ORDER BY day DESC, site""" % (
        BUSINESS_FILTER, " AND day >= ?" if days else ""),
        ([since] if days else []))

    out["models"] = q(con, """
        SELECT site, model, COUNT(*) n, SUM(in_tokens) in_t, SUM(cache_read) cache_r,
               SUM(cache_write) cache_w, SUM(out_tokens) out_t, SUM(cost) cost
        FROM usage_flows WHERE %s AND model != '*' %s
        GROUP BY site, model ORDER BY n DESC LIMIT 20""" % (
        BUSINESS_FILTER, ("AND day >= ?" if days else "")), args)

    # 业务请求 vs 内部任务（智能审批等）分开统计
    out["split"] = q(con, """
        SELECT site,
          SUM(CASE WHEN %s THEN 1 ELSE 0 END) biz_n,
          SUM(CASE WHEN %s THEN in_tokens ELSE 0 END) biz_in,
          SUM(CASE WHEN %s THEN cache_read ELSE 0 END) biz_cache,
          SUM(CASE WHEN %s THEN out_tokens ELSE 0 END) biz_out,
          SUM(CASE WHEN %s THEN cost ELSE 0 END) biz_cost,
          SUM(CASE WHEN %s THEN 1 ELSE 0 END) int_n,
          SUM(CASE WHEN %s THEN in_tokens ELSE 0 END) int_in,
          SUM(CASE WHEN %s THEN out_tokens ELSE 0 END) int_out,
          SUM(CASE WHEN %s THEN cost ELSE 0 END) int_cost
        FROM usage_flows WHERE %s GROUP BY site""" % (
        BUSINESS_FILTER, BUSINESS_FILTER, BUSINESS_FILTER, BUSINESS_FILTER, BUSINESS_FILTER,
        INTERNAL_FILTER, INTERNAL_FILTER, INTERNAL_FILTER, INTERNAL_FILTER, NON_FLOW))

    out["accounts"] = q(con, """SELECT site, username, plan_title, plan_total,
                                       plan_used, plan_remain, rate_multiplier, mode,
                                       updated_at FROM site_accounts""")
    out["state"] = q(con, """SELECT site, last_run, last_ok, watermark_ts, total_rows,
                                    last_msg FROM collect_state""")
    return out


def hit_rate(cache_r, in_t):
    tot = (cache_r or 0) + (in_t or 0)
    return (cache_r or 0) / tot * 100 if tot else 0.0


# 内部任务（智能审批/标题生成等）识别：短输入 + 极短输出 + 无缓存 + 非流式
# 实测特征（2026-10-01）：输入 330~850、输出恒为 3、cache=0、frt=-1000、耗时 1~2s
# 占条数 14% 但只占花费 0.5% —— 单列，不计入命中率
INTERNAL_FILTER = ("(out_tokens <= 10 AND cache_read = 0 AND total_prompt < 1500)")
BUSINESS_FILTER = ("(NOT (out_tokens <= 10 AND cache_read = 0 AND total_prompt < 1500))")
# ⚠️ 非流水行过滤（2026-10-01 修，10-01 晚更新为「优先逐条」）
#   dshapi(sub2api) 同一笔用量在库里留多种行，是同一笔账的不同切面：
#     · client:<uuid>            逐条流水（网页端 /api/v1/usage）★最细，优先
#     · sub2api-daily:<日期>     按日汇总（sk- key /v1/usage）
#     · sub2api-model:<模型>     按模型汇总（同上，day 为空）
#   三者 token/cost 重叠，全表 SUM 会把同一笔钱算多次 → 金额虚高数倍。
#   策略：有逐条数据那天【只认逐条】；无逐条（如断采的旧日）退回按日聚合；
#        按模型聚合行永不进总额（避免双计）。
NON_FLOW = ("(request_id NOT LIKE 'sub2api-model:%' "
            " AND (request_id NOT LIKE 'sub2api-daily:%' "
            "      OR NOT EXISTS (SELECT 1 FROM usage_flows x "
            "                     WHERE x.site = usage_flows.site "
            "                       AND x.request_id LIKE 'client:%' "
            "                       AND x.day = usage_flows.day)))")


# ────────────────── Hermes 侧 ──────────────────

def hermes_rows(con_h, host):
    """按 host 聚合本地 usage（覆盖所有 provider 别名 + base_url 尾斜杠变体）"""
    con_h.row_factory = sqlite3.Row
    rows = con_h.execute("""
        SELECT model,
               SUM(api_call_count) calls,
               SUM(input_tokens) in_t,
               SUM(cache_read_tokens) cache_r,
               SUM(cache_write_tokens) cache_w,
               SUM(output_tokens) out_t,
               MIN(first_seen) t0, MAX(last_seen) t1
        FROM session_model_usage
        WHERE billing_base_url LIKE ?
        GROUP BY model""", ("%" + host + "%",)).fetchall()
    return [dict(r) for r in rows]


def hermes_by_day(con_h, host):
    con_h.row_factory = sqlite3.Row
    rows = con_h.execute("""
        SELECT last_seen, api_call_count, input_tokens, cache_read_tokens,
               cache_write_tokens, output_tokens
        FROM session_model_usage WHERE billing_base_url LIKE ?""",
        ("%" + host + "%",)).fetchall()
    byday = {}
    for r in rows:
        if not r["last_seen"]:
            continue
        d = datetime.fromtimestamp(r["last_seen"], CST).strftime("%Y-%m-%d")
        b = byday.setdefault(d, {"calls": 0, "in_t": 0, "cache_r": 0, "cache_w": 0, "out_t": 0})
        b["calls"] += r["api_call_count"] or 0
        b["in_t"] += r["input_tokens"] or 0
        b["cache_r"] += r["cache_read_tokens"] or 0
        b["cache_w"] += r["cache_write_tokens"] or 0
        b["out_t"] += r["output_tokens"] or 0
    return byday


# ────────────────── 报告 ──────────────────

def session_rows(con, limit=25):
    """按对话汇总（依赖 session_join.py 建好的 flow_sessions 表）"""
    try:
        rows = q(con, """
            SELECT f.session_id, COUNT(*) n,
                   SUM(u.in_tokens) i, SUM(u.cache_read) cr, SUM(u.out_tokens) o,
                   SUM(u.cost) c, AVG(f.confidence) conf
            FROM flow_sessions f JOIN usage_flows u
              ON u.site=f.site AND u.request_id=f.request_id
            WHERE f.session_id IS NOT NULL
            GROUP BY f.session_id ORDER BY c DESC LIMIT ?""", (limit,))
        # 会话的最后活动时间 / 是否当前活跃
        return rows
    except Exception:
        return []


def session_titles(hcon):
    """会话显示名（取该会话第一条 user 消息的开头）"""
    out = {}
    try:
        for r in hcon.execute("""
                SELECT session_id, content FROM messages
                WHERE role='user' AND content IS NOT NULL AND content != ''
                GROUP BY session_id HAVING MIN(timestamp)"""):
            pass
    except Exception:
        pass
    return out


def fmt(n):
    return "{:,}".format(int(n or 0))


def console_report(days=None, recon=False):
    if not os.path.isfile(STORE):
        print("采集库不存在：%s（先跑 site_collect.py）" % STORE)
        return
    con = sqlite3.connect(STORE)
    s = site_summary(con, days)

    print("=" * 88)
    print("中转站侧用量监控报告      %s" % datetime.now(CST).strftime("%Y-%m-%d %H:%M"))
    print("=" * 88)

    print("\n【站点总览】")
    split = {r["site"]: r for r in s.get("split", [])}
    for r in s["sites"]:
        sp = split.get(r["site"], {})
        print("  %-18s %-7s 次请求  %s ~ %s" % (r["site"], fmt(r["n"]), r["d0"], r["d1"]))
        # 业务请求（真实对话）
        bi = sp.get("biz_in") or 0
        bc = sp.get("biz_cache") or 0
        print("    ├ 业务请求 %-6s 命中率 %-6.1f%%  输入 %-12s 缓存 %s" % (
            fmt(sp.get("biz_n")), hit_rate(bc, bi), fmt(bi), fmt(bc)))
        # 内部任务（智能审批等）
        if sp.get("int_n"):
            print("    └ 内部任务 %-6s (智能审批等，输出≤10 无缓存，不计命中率)  花费 $%.5f" % (
                fmt(sp.get("int_n")), sp.get("int_cost") or 0))
        print("       总花费 $%.4f  (业务 $%.4f + 内部 $%.5f)" % (
            r["cost"] or 0, sp.get("biz_cost") or 0, sp.get("int_cost") or 0))

    print("\n【按日明细】（命中率仅算业务请求）")
    print("  %-11s %-16s %-6s %-6s %-13s %-13s %-8s %-10s %s" % (
        "日期", "站点", "业务", "内部", "输入", "缓存命中", "命中率", "花费¥", "花费$"))
    for r in s["daily"]:
        sp = split.get(r["site"], {})
        # 该日业务/内部拆分
        row_split = con.execute("""
            SELECT SUM(CASE WHEN %s THEN 1 ELSE 0 END) biz_n,
                   SUM(CASE WHEN %s THEN 1 ELSE 0 END) int_n
            FROM usage_flows WHERE site=? AND day=? AND %s""" % (
            BUSINESS_FILTER, INTERNAL_FILTER, NON_FLOW),
            (r["site"], r["day"])).fetchone()
        biz_n = row_split["biz_n"] or 0
        int_n = row_split["int_n"] or 0
        usd = r["cost"] or 0
        print("  %-11s %-16s %-6d %-6d %-13s %-13s %-7.1f%% %-10.6f %.6f" % (
            r["day"], r["site"], biz_n, int_n, fmt(r["in_t"]), fmt(r["cache_r"]),
            hit_rate(r["cache_r"], r["in_t"]), usd * 7.1, usd))

    print("\n【按模型】")
    print("  %-18s %-30s %-7s %-13s %-13s %-9s %s" % (
        "站点", "模型", "请求", "输入", "缓存命中", "命中率", "花费$"))
    for r in s["models"]:
        print("  %-18s %-30s %-7s %-13s %-13s %-8.1f%% %.4f" % (
            r["site"], (r["model"] or "-")[:30], fmt(r["n"]), fmt(r["in_t"]),
            fmt(r["cache_r"]), hit_rate(r["cache_r"], r["in_t"]), r["cost"] or 0))

    # ── 按对话 ──
    srows = session_rows(con)
    if srows:
        print("\n【按对话】★ 站方数据归集到具体对话（最近邻算法，平均置信度见末列）")
        print("  %-34s %-5s %-12s %-13s %-8s %-10s %s" % (
            "会话", "请求", "输入", "缓存命中", "命中率", "花费¥", "置信"))
        for r in srows:
            tot = (r["i"] or 0) + (r["cr"] or 0)
            hit = (r["cr"] or 0) / tot * 100 if tot else 0
            print("  %-34s %-5d %-12s %-13s %-7.1f%% %-10.6f %.2f" % (
                (r["session_id"] or "")[:34], r["n"], fmt(r["i"]), fmt(r["cr"]),
                hit, (r["c"] or 0) * 7.1, r["conf"] or 0))
    else:
        print("\n【按对话】未归集（先跑 python session_join.py）")

    print("\n【账号 / 套餐】")
    for r in s["accounts"]:
        rate = r.get("rate_multiplier") or 0
        rate_s = ("  结算倍率 ×%.4g（实付 = 标价×%.4g）" % (rate, rate)) if rate else ""
        if r["plan_total"]:
            pct = (r["plan_used"] or 0) / r["plan_total"] * 100
            print("  %-18s %-24s %s  %s/%s (%.2f%%)%s" % (
                r["site"], (r["username"] or "-")[:24], r["plan_title"],
                fmt(r["plan_used"]), fmt(r["plan_total"]), pct, rate_s))
        else:
            print("  %-18s %-24s %s  余额 $%.4f%s" % (
                r["site"], (r["username"] or "-")[:24], r["plan_title"],
                (r["plan_remain"] or 0) / 500000.0, rate_s))

    print("\n【采集状态】")
    for r in s["state"]:
        ts = datetime.fromtimestamp(r["last_run"], CST).strftime("%m-%d %H:%M") if r["last_run"] else "-"
        wm = datetime.fromtimestamp(r["watermark_ts"], CST).strftime("%m-%d %H:%M") if r["watermark_ts"] else "-"
        print("  %-18s 上次采集 %s  状态 %s  水位 %s  本次 %s" % (
            r["site"], ts, "OK" if r["last_ok"] else "失败", wm, r["n"] if "n" in r.keys() else r.get("total_rows")))

    # ── 对账 ──
    print("\n" + "=" * 88)
    print("与 Hermes 本地对账")
    print("=" * 88)
    if not os.path.isfile(HERMES_DB):
        print("  找不到 Hermes state.db")
        con.close()
        return
    ch = sqlite3.connect("file:%s?mode=ro" % HERMES_DB.replace("\\", "/"), uri=True)

    for r in s["sites"]:
        site = r["site"]
        if site.startswith("api.dshapi"):
            host = "dshapi.icu"
        else:
            host = site
        hrows = hermes_rows(ch, host)
        if not hrows:
            print("\n  【%s】本地无对应记录" % site)
            continue
        h_calls = sum(x["calls"] or 0 for x in hrows)
        h_in = sum(x["in_t"] or 0 for x in hrows)
        h_cr = sum(x["cache_r"] or 0 for x in hrows)
        h_cw = sum(x["cache_w"] or 0 for x in hrows)
        h_out = sum(x["out_t"] or 0 for x in hrows)

        print("\n  【%s】" % site)
        print("  %-22s %-16s %-16s %s" % ("口径", "站方", "Hermes", "差异"))
        def line(lbl, a, b):
            if not a and not b:
                return
            d = ("%+.1f%%" % ((b - a) / a * 100)) if a else "n/a"
            flag = ""
            if a and abs((b - a) / a) > 0.05:
                flag = "  ⚠"
            print("  %-22s %-16s %-16s %s%s" % (lbl, fmt(a), fmt(b), d, flag))

        # ⚠️ 口径警告：站方只有近几天窗口数据，本地是全量历史 → 直接比总量必然巨差
        site_days = sorted({x["day"] for x in s["daily"] if x["site"] == site and x["day"]})
        hbd = hermes_by_day(ch, host)
        overlap = sorted(set(site_days) & set(hbd.keys()))
        print("  ⚠ 窗口不对等：站方仅 %d 天（%s），本地共 %d 天；"
              "共同日仅 %d 天" % (
                  len(site_days), "、".join(site_days), len(hbd), len(overlap)))
        print("  （下方总差异仅作参考；真正有效的是「共同日」对比）")

        line("请求数", r["n"], h_calls)
        line("输入(含缓存)", (r["in_t"] or 0) + (r["cache_r"] or 0), h_in + h_cr)
        line("  原始输入", r["in_t"], h_in)
        line("  缓存命中", r["cache_r"], h_cr)
        line("  缓存写入", r["cache_w"], h_cw)
        line("输出", r["out_t"], h_out)
        print("  花费: 站方 $%.4f" % (r["cost"] or 0))

        # 只在共同日上做严格对比
        if overlap:
            print("\n  ── 共同日严格对比（口径验证）──")
            print("   %-11s %-9s %-19s %-19s %-19s %s" % (
                "日期", "对账", "站方请求/缓存", "本地请求/缓存", "缓存差异%", "判定"))
            for d in sorted(overlap, reverse=True)[:12]:
                a = next((x for x in s["daily"] if x["site"] == site and x["day"] == d), None)
                b = hbd.get(d)
                if not a or not b:
                    continue
                sa = "%s / %s" % (fmt(a["n"]), fmt(a["cache_r"]))
                sb = "%s / %s" % (fmt(b["calls"]), fmt(b["cache_r"]))
                if a["cache_r"]:
                    dc = "%+.2f%%" % ((b["cache_r"] - a["cache_r"]) / a["cache_r"] * 100)
                else:
                    dc = "n/a"
                same = abs(a["cache_r"] - b["cache_r"]) <= max(10000, a["cache_r"] * 0.02)
                verdict = "✓ 口径吻合" if same else "✗ 有差异"
                print("   %-11s %-9s %-19s %-19s %-19s %s" % (d, "", sa, sb, dc, verdict))

        # 完整按日差异表
        alldays = sorted(set(list(hbd.keys()) + site_days), reverse=True)[:12]
        print("\n  ── 全窗口按日（找漏记日）──")
        print("   %-11s %-19s %-19s %s" % ("日期", "站方(请求/缓存)", "本地(请求/缓存)", "判定"))
        for d in alldays:
            a = next((x for x in s["daily"] if x["site"] == site and x["day"] == d), None)
            b = hbd.get(d)
            if a and b:
                verdict = "两侧都有"
                sa = "%s / %s" % (fmt(a["n"]), fmt(a["cache_r"]))
                sb = "%s / %s" % (fmt(b["calls"]), fmt(b["cache_r"]))
            elif a:
                verdict = "★仅站方有（本地漏记）"
                sa = "%s / %s" % (fmt(a["n"]), fmt(a["cache_r"]))
                sb = "-"
            else:
                verdict = "仅本地有（超出站方窗口）"
                sa = "-"
                sb = "%s / %s" % (fmt(b["calls"]), fmt(b["cache_r"]))
            print("   %-11s %-19s %-19s %s" % (d, sa, sb, verdict))

    ch.close()
    con.close()


# ────────────────── HTML 面板 ──────────────────

def html_report(path, days=None):
    if not os.path.isfile(STORE):
        print("采集库不存在，先跑 site_collect.py")
        return
    con = sqlite3.connect(STORE)
    s = site_summary(con, days)
    split = {r["site"]: r for r in s.get("split", [])}

    # ── 今日 / 全时段 关键数字（对齐用户悬浮窗口径）──
    today = datetime.now(CST).strftime("%Y-%m-%d")
    now = {}
    for site in {r["site"] for r in s["sites"]}:
        row = con.execute("""
            SELECT COUNT(*) n, SUM(in_tokens) i, SUM(cache_read) cr,
                   SUM(out_tokens) o, SUM(cost) cost
            FROM usage_flows WHERE site=? AND day=? AND %s""" % NON_FLOW,
            (site, today)).fetchone()
        brow = con.execute("""
            SELECT COUNT(*) n, SUM(in_tokens) i, SUM(cache_read) cr, SUM(cost) cost
            FROM usage_flows WHERE site=? AND day=? AND %s AND %s""" % (NON_FLOW, BUSINESS_FILTER),
            (site, today)).fetchone()
        irow = con.execute("""
            SELECT COUNT(*) n, SUM(cost) cost
            FROM usage_flows WHERE site=? AND day=? AND %s AND %s""" % (NON_FLOW, INTERNAL_FILTER),
            (site, today)).fetchone()
        # 省下的钱 = 缓存命中量 × (输入价 − 缓存价)
        saved = 0.0
        if brow and brow[2] and brow[1] is not None:
            # 有效输入单价（从 quota 反推太绕），用站方倍率：cache 价 = 输入价 × cache_ratio
            # 示例站: model_ratio=0.03, cache_ratio=0.02 → 省 98% 的输入价
            pass
        now[site] = {
            "n": (row[0] if row else 0) or 0,
            "cost": (row[4] if row else 0) or 0,
            "biz_n": (brow[0] if brow else 0) or 0,
            "int_n": (irow[0] if irow else 0) or 0,
            "int_cost": (irow[1] if irow else 0) or 0,
            "in_t": (row[1] if row else 0) or 0,
            "cache": (row[2] if row else 0) or 0,
        }
    con.close()

    EX = 7.1   # 美元→人民币，仅用于显示

    def row_cells(r, cols):
        return "".join("<td>%s</td>" % c for c in cols)

    daily_html = "".join(
        "<tr>%s</tr>" % row_cells(r, [
            r["day"], r["site"], fmt(r["n"]), fmt(r["in_t"]), fmt(r["cache_r"]),
            fmt(r["cache_w"]), fmt(r["out_t"]),
            "<b>%.1f%%</b>" % hit_rate(r["cache_r"], r["in_t"]),
            "$%.4f" % (r["cost"] or 0), "¥%.6f" % ((r["cost"] or 0) * EX)])
        for r in s["daily"][:60])
    # 按日表补一列「内部任务」不好放（daily 已过滤），改在表头上方说明

    model_html = "".join(
        "<tr>%s</tr>" % row_cells(r, [
            r["site"], r["model"] or "-", fmt(r["n"]), fmt(r["in_t"]),
            fmt(r["cache_r"]), "%.1f%%" % hit_rate(r["cache_r"], r["in_t"]),
            "$%.4f" % (r["cost"] or 0)])
        for r in s["models"])

    # 今日高亮卡片（用户最关心）
    today_cards = ""
    for r in s["sites"]:
        site = r["site"]
        t = now.get(site, {})
        sp = split.get(site, {})
        today_cards += """
        <div class="card today">
          <h3>%s <span class="tag">今日</span></h3>
          <div class="big">¥%.6f <span class="u">≈ $%.6f</span></div>
          <div class="kv">业务请求 <b>%s</b> ｜ 内部任务(审批) <b>%s</b></div>
          <div class="kv">缓存命中 <b>%s</b> ／ 输入 <b>%s</b></div>
          <div class="kv">今日命中率 <b>%.1f%%</b></div>
        </div>""" % (site, (t.get("cost") or 0) * EX, t.get("cost") or 0,
                     fmt(t.get("biz_n")), fmt(t.get("int_n")),
                     fmt(t.get("cache")), fmt(t.get("in_t")),
                     hit_rate(t.get("cache"), t.get("in_t")))

    cards = ""
    for r in s["sites"]:
        sp = split.get(r["site"], {})
        bi = sp.get("biz_in") or 0
        bc = sp.get("biz_cache") or 0
        cards += """
        <div class="card">
          <h3>%s <span class="tag dim">累计</span></h3>
          <div class="big">%s <span class="u">次请求</span></div>
          <div class="kv">业务 <b>%s</b> ｜ 内部任务 <b>%s</b></div>
          <div class="kv">业务命中率 <b>%.1f%%</b></div>
          <div class="kv">输入 %s ／ 缓存 %s</div>
          <div class="kv">输出 %s</div>
          <div class="kv">花费 <b>$%.4f</b> ≈ <b>¥%.4f</b></div>
          <div class="kv dim">%s ~ %s</div>
        </div>""" % (r["site"], fmt(r["n"]), fmt(sp.get("biz_n")), fmt(sp.get("int_n")),
                     hit_rate(bc, bi), fmt(bi), fmt(bc), fmt(r["out_t"]),
                     r["cost"] or 0, (r["cost"] or 0) * EX, r["d0"], r["d1"])

    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>中转站用量监控</title>
<style>
 body{background:#1b1b1f;color:#e8e8ea;font-family:"Microsoft YaHei",system-ui,sans-serif;
      margin:0;padding:22px 26px}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#8a8a92;font-size:12px;margin-bottom:18px}
 .cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:22px}
 .card{background:#232329;border:1px solid #33333b;border-radius:10px;padding:14px 18px;min-width:240px}
 .card.today{background:#1f2a24;border-color:#2e4a3a}
 .card h3{margin:0 0 8px;font-size:14px;color:#5aa9e0}
 .card.today h3{color:#4ec26a}
 .tag{font-size:10px;padding:1px 6px;border-radius:8px;background:#2e4a3a;color:#7ee09a;font-weight:400}
 .tag.dim{background:#2c2c33;color:#8a8a92}
 .big{font-size:26px;font-weight:700;margin-bottom:8px} .u{font-size:12px;color:#8a8a92;font-weight:400}
 .kv{font-size:12px;color:#b8b8c0;line-height:1.75} .kv b{color:#e8e8ea}
 .dim{color:#6a6a72;font-size:11px;margin-top:6px}
 table{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:24px}
 th,td{padding:6px 9px;text-align:right;border-bottom:1px solid #2c2c33}
 th{color:#8a8a92;font-weight:500;text-align:right;background:#202025}
 td:first-child,th:first-child{text-align:left}
 tr:hover{background:#26262c}
 h2{font-size:14px;color:#e0b542;margin:22px 0 9px;font-weight:600}
</style></head><body>
<h1>中转站侧用量监控</h1>
<div class="sub">生成于 __GEN__ ｜ 数据源：站方 API 直连 ｜ 独立于 Hermes 本地监控 ｜ 汇率按 7.1 折算</div>
<h2 style="margin-top:0">今日</h2>
<div class="cards">__TODAY__</div>
<h2>累计（全时段）</h2>
<div class="cards">__CARDS__</div>
<h2>按日明细（命中率仅算业务请求，已剔除智能审批）</h2>
<table><tr><th>日期</th><th>站点</th><th>业务</th><th>输入</th><th>缓存命中</th>
<th>缓存写入</th><th>输出</th><th>命中率</th><th>花费$</th><th>花费¥</th></tr>__DAILY__</table>
<h2>按模型</h2>
<table><tr><th>站点</th><th>模型</th><th>请求</th><th>输入</th><th>缓存命中</th>
<th>命中率</th><th>花费</th></tr>__MODEL__</table>
</body></html>"""
    html = (html.replace("__GEN__", datetime.now(CST).strftime("%Y-%m-%d %H:%M"))
                .replace("__TODAY__", today_cards)
                .replace("__CARDS__", cards)
                .replace("__DAILY__", daily_html)
                .replace("__MODEL__", model_html))

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成: %s" % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", help="输出 HTML 面板路径")
    ap.add_argument("--days", type=int, help="限定最近 N 天")
    ap.add_argument("--recon", action="store_true", help="只看对账")
    args = ap.parse_args()
    if args.html:
        html_report(args.html, args.days)
    else:
        console_report(args.days, args.recon)


if __name__ == "__main__":
    main()
