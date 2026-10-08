# -*- coding: utf-8 -*-
"""panel.py — 中转站用量监控面板（本地小服务）

设计（2026-10-01 定稿）：
  · 后台采集 5 分钟一轮（保底，流量 12 MB/天）
  · 页面提供「立即刷新」按钮 → 触发一次即时采集（按需，不浪费）
  · 页面每 3 秒轮询本地状态 → 看起来是「活的」，但只有点刷新才联网
  · 只监听 127.0.0.1，不对外暴露

流量账（实测，gzip 后）：
  · 单次拉取 42.8 KB
  · 5 分钟一轮 = 12 MB/天
  · 每次手动刷新 = 43 KB

用法:
  python panel.py              # 启动，浏览器开 http://127.0.0.1:8788
  python panel.py --port 9000
  python panel.py --open       # 启动并自动开浏览器
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import concurrent.futures
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
STORE = os.path.join(HERE, "store.db")
HERMES_DB = r"D:\Hermes Agent CN Desktop\data\hermes-home\state.db"
# ── 金额口径（用户 2026-10-01 定：1 美元 = 1 人民币）──
# 页面显示直接用库里的 cost 字段（元），不再乘汇率。
# 若将来某站是「美元计费」，在 sites.json 给该站加 fx_to_cny 即可。
CST = timezone(timedelta(hours=8))
EX = 1.0

# 内部任务过滤（智能审批等：输出≤10、无缓存、输入<1500）
INTERNAL = "(out_tokens <= 10 AND cache_read = 0 AND total_prompt < 1500)"
BUSINESS = "(NOT (out_tokens <= 10 AND cache_read = 0 AND total_prompt < 1500))"
# ⚠️ 非流水行过滤（2026-10-01 修，10-01 晚更新为「优先逐条」）
#   dshapi(sub2api) 站的同一笔用量会在库里留下多种行，是同一笔账的不同切面：
#     · client:<uuid>                逐条流水（网页端 /api/v1/usage）★最细，优先
#     · sub2api-daily:<日期>         按日汇总（sk- key /v1/usage）
#     · sub2api-model:<模型>         按模型汇总（同上）
#   三者 token/cost 会重叠。全表 SUM 会把同一笔钱算多次 → 金额虚高数倍。
#   策略：有逐条数据时【只认逐条】（最精确、可归集到对话）；无逐条时退回按日聚合。
NON_FLOW = ("(request_id NOT LIKE 'sub2api-model:%' "
            " AND (request_id NOT LIKE 'sub2api-daily:%' "
            "      OR NOT EXISTS (SELECT 1 FROM usage_flows x "
            "                     WHERE x.site = usage_flows.site "
            "                       AND x.request_id LIKE 'client:%' "
            "                       AND x.day = usage_flows.day)))")

_refresh_lock = threading.Lock()
_last_refresh = {"at": 0, "msg": "", "running": False, "level": "ok"}
# level: ok=一切正常 / warn=有站采集不完整 / error=需要用户处理（如凭据失效）
# 2026-10-06 新增：以前只有一句"新增 0 条"，看不出是"站方没新数据"还是
# "我们登录态挂了" —— 用户因此被蒙了 3 天。现在分级 + 说人话。



# ★ 2026-10-07 异步刷新进度
#   为什么要它：/api/refresh 原来是同步的，串行采所有站。只要有站网络卡住
#   （example.com SSL 超时实测卡 9 分钟），整个请求不返回，前端一直显示
#   「正在采集」且无法取消 —— 用户实测踩到。
#   现在采集跑后台线程，这里记录实时进度供前端轮询 /api/refresh/status。
_refresh_prog_lock = threading.Lock()
_refresh_prog = {
    "running": False,
    "started_at": 0,
    "elapsed": 0,
    "current": "",        # 当前正在采的站
    "done": [],           # 已完成的 [{site, added, msg}]
    "total": 0,           # 本轮计划采的站数
    "finished": False,
    "result": None,       # 完成后的汇总（同原 /api/refresh 返回）
}


def _prog_set(**kw):
    with _refresh_prog_lock:
        _refresh_prog.update(kw)

def _prog_push_done(site, added, msg):
    """记录一个站采集完成（供前端显示进度条）"""
    with _refresh_prog_lock:
        _refresh_prog["done"].append(
            {"site": site, "added": added, "msg": msg})

def _refresh_worker():
    """后台跑一次全量采集，期间更新 _refresh_prog 供前端轮询。"""
    import importlib
    import site_collect
    importlib.reload(site_collect)
    try:
        planned = [x.get('host') for x in site_collect.load_sites()
                   if x.get('enabled', True)]
    except Exception:
        planned = []
    _prog_set(running=True, started_at=int(time.time()), elapsed=0,
              current='', done=[], total=len(planned),
              finished=False, result=None)
    try:
        r = collect_now(progress=True)
    except Exception as e:
        r = {'ok': False, 'msg': '失败: %s' % str(e)[:150], 'level': 'error'}
    _prog_set(running=False, finished=True, current='', result=r)

def q(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def collect_now(target_sites=None, progress=False):
    """触发一次即时采集（后台线程调用，带锁防并发；支持 target_sites 定向快速刷新）"""
    if not _refresh_lock.acquire(blocking=False):
        return {"ok": False, "msg": "已有刷新在进行中"}
    try:
        _last_refresh["running"] = True
        import importlib
        import site_collect
        importlib.reload(site_collect)
        sites = site_collect.load_sites()
        provs = site_collect.parse_hermes_providers()
        total_new = 0
        msgs = []
        need_action = False      # ★ 2026-10-06：是否有需要用户处理的问题

        # ★ 2026-10-07 并行采集
        #   原来串行采：一个站卡住（如 example.com TLS 抖动 90 秒），
        #   后面的站只能排队干等 —— 3 个站最坏接近 5 分钟，用户以为卡死。
        #   现在各站各起一个线程并发采，总耗时 = 最慢那个站（而非累加）。
        #   每个站仍有独立的 90 秒预算（site_collect 里用 thread-local 存，
        #   不会互相覆盖）。
        todo = []
        for s in sites:
            if not s.get("enabled", True):
                continue
            host = s.get("host")
            if target_sites and host not in target_sites:
                continue
            todo.append(s)

        if progress:
            _prog_set(current="（%d 个站并行采集）" % len(todo))

        _rlock = threading.Lock()

        def _one(site_cfg, budget=None):
            h = site_cfg.get("host") or "?"
            _t0 = time.time()
            try:
                r = site_collect.run_site(site_cfg, provs, full=False,
                                          log=lambda m: None, budget=budget)
                return (h, r, time.time() - _t0, None)
            except Exception as e:
                return (h, None, time.time() - _t0, e)

        # ── 第一轮：所有站并行采，每站 30 秒预算 ──
        # ★ 2026-10-08 用户拍板：单站 30 秒扫不到就跳过，别拖死整轮刷新。
        #   失败的站不立刻放弃 —— 留到第二轮兜底再试（见下）。
        failed_sites = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_one, s, 30): s for s in todo}
            for fut in concurrent.futures.as_completed(futs):
                h, r, dt, err = fut.result()
                if err is not None:
                    with _rlock:
                        failed_sites.append(futs[fut])
                elif r:
                    with _rlock:
                        total_new += r.get("added") or 0
                        msgs.append("%s +%d" % (r["site"], r.get("added") or 0))
                else:
                    # run_site 返回 None = 该站没采到（网络/凭据问题）→ 待重试
                    with _rlock:
                        failed_sites.append(futs[fut])
                if progress:
                    _prog_push_done(h, (r or {}).get("added") or 0,
                                   "%.0fs%s" % (dt, "" if err is None and r else " 待重试"))

        # ── 第二轮：只兜底重试第一轮失败的站（串行，各再给 30 秒）──
        #   为什么串行：失败站通常就是「连不上」，并行重试只会一起超时；
        #   串行还能让每站拿满自己的预算。站点数一般 1-2 个，代价可接受。
        if failed_sites:
            if progress:
                _prog_set(current="重试 %d 个失败的站…" % len(failed_sites))
            for s in failed_sites:
                h = s.get("host") or "?"
                if progress:
                    _prog_set(current="%s（重试）" % h)
                hh, r, dt, err = _one(s, 30)
                if err is None and r:
                    with _rlock:
                        total_new += r.get("added") or 0
                        msgs.append("%s +%d（重试成功）" % (r["site"], r.get("added") or 0))
                else:
                    # 两轮都失败 → 如实报失败（不再无限重试）
                    with _rlock:
                        need_action = True
                        why = str(err)[:40] if err is not None else "30秒内无响应"
                        msgs.append("⚠ %s 采集失败（重试后仍失败：%s）" % (h, why))
        # 采集后重跑会话归集（数据变了，归集要跟上）
        try:
            import importlib
            import session_join
            importlib.reload(session_join)
            session_join.main_quiet()
        except Exception:
            pass
        # sub2api 站逐条流水（走网页端 JWT，见 dsh_flows.py）
        # ★ 2026-10-07 多站改造：原来写死 dshapi 那几个域名 + 单个 token，
        #   第二个 sub2api 站（tryaigc）的流水永远采不到。
        #   现在遍历所有启用的 sub2api 站，各用各的 token 与域名池。
        #   若指定了 target_sites，只处理其中的 sub2api 站（秒级加速）。
        s2sites = []
        try:
            import site_collect as _sc
            import site_resolver as _sr
            for s in _sc.load_sites():
                if not s.get("enabled", True):
                    continue
                if (s.get("kind") or "").lower() != "sub2api":
                    continue
                h = s.get("host") or ""
                if not h:
                    continue
                need = s.get("session_auth")
                if need is None:
                    need = True
                if not need:
                    continue
                if target_sites and h not in target_sites:
                    continue
                s2sites.append(s)
        except Exception:
            s2sites = []

        for s in s2sites:
            host = s.get("host") or ""
            if progress:
                _prog_set(current="%s（逐条流水）" % host)
            try:
                import importlib
                import dsh_flows
                import dsh_auth
                importlib.reload(dsh_auth)
                importlib.reload(dsh_flows)
                # ★ 2026-10-07：流水采集也要设预算。
                #   这里不走 run_site，所以之前没被预算保护 ——
                #   实测 example.com 在这段卡了 176 秒以上还没出来。
                # ★ 2026-10-08：跟聚合采集统一收到 30 秒（用户拍板）。
                try:
                    import site_collect as _scb
                    _scb.set_site_budget(30)
                except Exception:
                    pass
                tok = dsh_auth.ensure_token(log=lambda m: None, host=host)
                if tok:
                    # ★ 2026-10-08 修（域名泄漏）：原来直接读 s.get("bases")，
                    #   站上没配 bases 时取到空列表 → base=None → 往下掉进
                    #   dsh_flows.pick_base() 的模块级 BASES（= 第一个 sub2api 站）→
                    #   拿 A 站的流水贴上 B 站标签（tryaigc 实测 434 条全串）。
                    #   改用统一解析器：没配 bases 时它回退到 base_url 的 origin。
                    try:
                        import site_resolver as _srb
                        _bases = _srb.bases_of(s)
                    except Exception:
                        _bases = []
                    base = _bases[0] if _bases else None
                    if not base:
                        # 缺域名 = 配置残缺，绝不兜底到别的站
                        need_action = True
                        msgs.append("⚠ %s 缺请求域名（bases/base_url 都没配），已跳过" % host)
                        continue
                    items, _total = dsh_flows.fetch_items(
                        tok, pages=3, log=lambda m: None, base=base)
                    if items:
                        norms = [dsh_flows.norm(it, host=host)
                                 for it in items if it.get("request_id")]
                        con2 = site_collect.db()
                        added2 = site_collect.upsert(con2, norms)
                        con2.commit()
                        con2.close()
                        total_new += added2
                        msgs.append("%s流水 +%d" % (host, added2))
                        session_join.main_quiet()   # 新流水也归到对话
                    else:
                        msgs.append("%s流水 无新数据" % host)
                else:
                    # ★ 2026-10-06：拿不到 token = 逐条流水中断，必须让用户看见
                    need_action = True
                    msgs.append("⚠ %s 登录态失效，逐条流水已中断" % host)
            except Exception as e:
                need_action = True
                msgs.append("⚠ %s 流水失败:%s" % (host, str(e)[:50]))
        # 刷新完成后自动导出校准数据（供缓存监控使用）
        try:
            import calib_export
            calib_export.export_calibration(verbose=False)
        except Exception:
            pass
        _last_refresh["at"] = int(time.time())
        # ★ 2026-10-06：分级 + 说人话。以前只有"新增 N 条"，看不出是不是我们挂了。
        if need_action:
            _last_refresh["level"] = "error"
            _last_refresh["msg"] = "、".join(msgs)
        elif total_new == 0:
            _last_refresh["level"] = "warn"
            _last_refresh["msg"] = "无新数据（%s）" % "、".join(msgs) if msgs else "无新数据"
        else:
            _last_refresh["level"] = "ok"
            _last_refresh["msg"] = "新增 %d 条（%s）" % (total_new, "、".join(msgs))
        return {"ok": True, "added": total_new, "detail": _last_refresh["msg"],
                "level": _last_refresh["level"]}
    except Exception as e:
        _last_refresh["level"] = "error"
        _last_refresh["msg"] = "失败: %s" % str(e)[:150]
        return {"ok": False, "msg": _last_refresh["msg"], "level": "error"}
    finally:
        _last_refresh["running"] = False
        _refresh_lock.release()


def build_state():
    """读出当前状态（纯本地，不联网）"""
    if not os.path.isfile(STORE):
        return {"error": "采集库不存在，先跑 site_collect.py"}
    con = sqlite3.connect(STORE, timeout=10)
    today = datetime.now(CST).strftime("%Y-%m-%d")
    out = {"now": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
           "today": today, "sites": [], "sessions": [], "daily": [],
           "last_refresh": _last_refresh}
    # ★ 2026-10-06：站点登录凭据状态（供顶部告警条用）
    # ★ 2026-10-08 修：原来取 cred_status()（= active 站），多站后只反映一个站。
    #   现在遍历所有 sub2api 站各自的登录态，谁的快过期就说谁。
    try:
        import dsh_auth
        import site_auth
        _per = []
        for _h, _st in (dsh_auth.all_sites() or {}).items():
            try:
                _c = dsh_auth.cred_status(host=_h)
                _c["host"] = _h
                _per.append(_c)
            except Exception:
                pass
        # 兼容旧前端：仍给一个 creds 字段（取最紧急的那个，没有就第一个）
        if _per:
            _per.sort(key=lambda x: (x.get("expires_in_h") or 999))
            out["creds"] = _per[0]
            out["creds_all"] = _per
        else:
            out["creds"] = {"token_ok": False, "error": "无任何站点登录态"}
            out["creds_all"] = []
    except Exception as e:
        out["creds"] = {"error": str(e)[:120]}
        out["creds_all"] = []

    for srow in q(con, """SELECT site, COUNT(*) n, MIN(day) d0, MAX(day) d1,
                                 SUM(cost) cost FROM usage_flows
                          WHERE %s
                          GROUP BY site ORDER BY n DESC""" % NON_FLOW):
        site = srow["site"]
        t = con.execute("""SELECT COUNT(*) n, SUM(in_tokens) i, SUM(cache_read) cr,
                                  SUM(out_tokens) o, SUM(cost) c
                           FROM usage_flows WHERE site=? AND day=? AND %s""" % NON_FLOW,
                        (site, today)).fetchone()
        bt = con.execute("""SELECT COUNT(*) n FROM usage_flows
                            WHERE site=? AND day=? AND %s AND %s""" % (NON_FLOW, BUSINESS),
                         (site, today)).fetchone()
        it = con.execute("""SELECT COUNT(*) n, SUM(cost) c FROM usage_flows
                            WHERE site=? AND day=? AND %s AND %s""" % (NON_FLOW, INTERNAL),
                         (site, today)).fetchone()
        # 业务请求（排除智能审批）的缓存指标
        bt2 = con.execute("""SELECT COUNT(*) n,
                                    SUM(in_tokens) i, SUM(cache_read) cr,
                                    SUM(out_tokens) o
                             FROM usage_flows
                             WHERE site=? AND day=? AND %s AND %s""" % (NON_FLOW, BUSINESS),
                          (site, today)).fetchone()
        bi = (bt2[1] or 0) if bt2 else 0
        bc = (bt2[2] or 0) if bt2 else 0
        biz_hit = (bc / (bi + bc) * 100) if (bi + bc) else 0
        acc = q(con, "SELECT * FROM site_accounts WHERE site=?", (site,))
        ti = (t[1] or 0) + (t[2] or 0)
        out["sites"].append({
            "site": site, "total_n": srow["n"], "d0": srow["d0"], "d1": srow["d1"],
            "total_cost": srow["cost"] or 0,
            "today_cost": t[4] or 0, "today_in": t[1] or 0,
            "today_cache": t[2] or 0, "today_out": t[3] or 0,
            "today_hit": ((t[2] or 0) / ti * 100) if ti else 0,
            "today_biz_hit": biz_hit,
            "today_biz": bt[0] or 0, "today_internal": it[0] or 0,
            "today_internal_cost": (it[1] or 0) if it else 0,
            "account": acc[0] if acc else None,
        })

    out["daily"] = q(con, """
        SELECT day, site, COUNT(*) n, SUM(in_tokens) i, SUM(cache_read) cr,
               SUM(out_tokens) o, SUM(cost) c
        FROM usage_flows WHERE %s AND day != '' AND %s
        GROUP BY day, site ORDER BY day DESC, site LIMIT 40""" % (BUSINESS, NON_FLOW))

    try:
        # ⚠️ 排序按「最后活动」倒序（最近的在最前），不再按花费倒序
        #    （2026-10-03 用户要求）。
        #
        # ★ 2026-10-07 修：原来 GROUP BY 只按 session_id + 全局 LIMIT 30，
        #   导致「会话多的站把会话少的站挤光」—— 实测 tryaigc 只有 3 个会话，
        #   而 d1/dshapi 各有 20 上下，全局 top30 里 tryaigc 占 0 条，
        #   用户完全看不到它的按对话数据（实测反馈）。
        #   现在：**每个站各取最近 N 条**，用窗口函数保证每站都有份。
        per_site = 15
        out["sessions"] = q(con, """
            SELECT site, session_id, n, i, cr, o, c, conf, last_ts FROM (
              SELECT f.site site, f.session_id session_id, COUNT(*) n,
                     SUM(u.in_tokens) i, SUM(u.cache_read) cr, SUM(u.out_tokens) o,
                     SUM(u.cost) c, AVG(f.confidence) conf,
                     MAX(u.ts) last_ts,
                     ROW_NUMBER() OVER (
                       PARTITION BY f.site ORDER BY MAX(u.ts) DESC
                     ) rn
              FROM flow_sessions f JOIN usage_flows u
                ON u.site=f.site AND u.request_id=f.request_id
              WHERE f.session_id IS NOT NULL
              GROUP BY f.site, f.session_id
            ) WHERE rn <= %d
            ORDER BY site, last_ts DESC""" % per_site)
    except Exception:
        # 老 SQLite 不支持窗口函数（< 3.25）→ 退回旧查询，至少不崩
        try:
            out["sessions"] = q(con, """
                SELECT f.site AS site, f.session_id, COUNT(*) n,
                       SUM(u.in_tokens) i, SUM(u.cache_read) cr, SUM(u.out_tokens) o,
                       SUM(u.cost) c, AVG(f.confidence) conf,
                       MAX(u.ts) last_ts
                FROM flow_sessions f JOIN usage_flows u
                  ON u.site=f.site AND u.request_id=f.request_id
                WHERE f.session_id IS NOT NULL
                GROUP BY f.site, f.session_id ORDER BY last_ts DESC LIMIT 60""")
        except Exception:
            out["sessions"] = []

    con.close()
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            try:
                self._send(200, json.dumps(build_state(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/refresh/status"):
            # ★ 2026-10-07 新增：异步刷新的进度查询。
            #   前端点「立即刷新」后立刻拿到受理，然后每 1 秒来这里问进度。
            #   elapsed **实时算**，不靠写入更新 —— 否则卡在网络等待时数字不动。
            with _refresh_prog_lock:
                out = dict(_refresh_prog)
            if out.get("running") and out.get("started_at"):
                out["elapsed"] = int(time.time()) - out["started_at"]
            self._send(200, json.dumps(out, ensure_ascii=False))
        elif self.path.startswith("/api/refresh"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            target = None
            if "sites" in qs:
                target = [s.strip() for s in qs["sites"][0].split(",") if s.strip()]
            elif "site" in qs:
                target = [s.strip() for s in qs["site"][0].split(",") if s.strip()]
            # ★ 2026-10-07 重构：改成**异步**。
            #   原来这里是同步调 collect_now()，串行采所有站 —— 只要有一个站
            #   网络卡住（如 example.com SSL 握手超时，实测卡 9 分钟），整个 HTTP
            #   请求就一直不返回，前端永远停在「正在采集」（实测踩过）。
            #   现在：立刻受理并返回，采集丢后台线程；前端轮询 /api/refresh/status。
            if target:
                # 指定站点时仍走同步（加站后立刻采一次这种场景，等得起，也要拿结果）
                r = collect_now(target_sites=target)
                self._send(200, json.dumps(r, ensure_ascii=False))
                return
            if _refresh_lock.locked():
                with _refresh_prog_lock:
                    cur = dict(_refresh_prog)
                self._send(200, json.dumps({
                    "ok": True, "async": True, "already": True,
                    "msg": "已在采集中", "prog": cur,
                }, ensure_ascii=False))
                return
            t = threading.Thread(target=_refresh_worker, daemon=True)
            t.start()
            self._send(200, json.dumps({
                "ok": True, "async": True, "msg": "已开始采集，进度见页面提示",
            }, ensure_ascii=False))
        elif self.path.startswith("/api/prices"):
            # ★ 2026-10-02：模型定价接口
            try:
                import prices
                self._send(200, json.dumps({
                    "entries": prices.all_entries(),
                    "covered": prices.covered_models(),
                }, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/creds"):
            # ★ 2026-10-06：站点登录态（自动识别 kind，列出全部站）
            #   默认脱敏；?reveal=1 返回明文（供"小眼睛"，仅本机回环）
            #   ?site=<host> 指定站点
            #   ?relogin=1  → 只重登拿 token（轻量，1-2 秒；不走全量采集）
            try:
                import importlib
                import site_auth
                importlib.reload(site_auth)
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                want_site = (qs.get("site") or [""])[0]
                reveal = (qs.get("reveal") or [""])[0] == "1"
                want_relogin = (qs.get("relogin") or [""])[0] == "1"

                sites = site_auth.list_sites()
                target = None
                if want_site:
                    target = next((s for s in sites if s["host"] == want_site), None)
                if target is None:
                    target = next((s for s in sites if s.get("needs_login")), None)
                if target is None and sites:
                    target = sites[0]

                # ★ 2026-10-07：专用重登路径 —— 只拿 token，不做全量采集。
                #   原来前端「重新登录」按钮走 /api/refresh（全量采集 30 秒），
                #   30 秒静默期会让用户以为失败。这里拆开。
                if want_relogin and target and target.get("needs_login"):
                    import credentials
                    importlib.reload(credentials)
                    c = credentials.get(target["host"])
                    if not (c.get("email") and c.get("password")):
                        self._send(200, json.dumps({
                            "ok": False, "action": "relogin",
                            "msg": "没有已存凭据，请先填写账号密码",
                        }, ensure_ascii=False))
                        return
                    ok, msg, st = site_auth.login(target["host"], c["email"], c["password"])
                    self._send(200, json.dumps({
                        "ok": ok, "action": "relogin", "msg": msg, "status": st,
                    }, ensure_ascii=False))
                    return

                out = {"sites": sites, "site": (target or {}).get("host", "")}
                if target:
                    out.update({k: v for k, v in target.items() if k != "host"})
                if reveal and target and target.get("needs_login"):
                    # ★ 2026-10-07 修串站：必须按 target 的 host 取凭据，
                    #   不能 load() 默认（那只拿 active 站，多站时会串）。
                    import dsh_auth
                    importlib.reload(dsh_auth)
                    thost = target.get("host") or ""
                    email, pwd, _src = dsh_auth._cred_lookup(
                        dsh_auth.load(host=thost), site=thost)
                    out["email_plain"] = email or ""
                    out["password_plain"] = pwd or ""
                self._send(200, json.dumps(out, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/join_health"):
            # ★ 2026-10-09 新增：归集健康度（有没有会话没统计上）
            #   GET /api/join_health → 今日各站归集率 + 认不出的地址
            #   （补登是写操作，走 do_POST 分支）
            try:
                import importlib
                import join_health
                importlib.reload(join_health)
                self._send(200, json.dumps(join_health.report(),
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/sites"):
            # ★ 2026-10-07：站点配置读写（加站界面）
            #   GET  /api/sites              → 全部站点（含停用）+ 探测缓存
            #   GET  /api/sites?probe=<url>  → 只探测，不写盘（探测可能等 10 秒）
            try:
                import importlib
                import site_admin
                importlib.reload(site_admin)
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)

                # 探测模式：只读操作，不碰磁盘
                want_probe = (qs.get("probe") or [""])[0]
                if want_probe:
                    import site_probe
                    importlib.reload(site_probe)
                    # ★ 2026-10-07 审计修复：限制输入长度。
                    #   超长输入本身不慢（100 万字符实测 5ms），但探测是**同步阻塞
                    #   网络调用**（最多 3 次 × 3 秒 = 9 秒），没有闸门时并发打
                    #   慢主机能让线程数暴涨。加个长度上限过滤明显异常输入。
                    if len(want_probe) > 500:
                        self._send(200, json.dumps({
                            "ok": False,
                            "msg": "地址过长（%d 字符，上限 500）—— 是不是粘错内容了？"
                                   % len(want_probe),
                        }, ensure_ascii=False))
                        return
                    base, note = site_admin.normalize_base_url(want_probe)
                    if not base:
                        self._send(200, json.dumps(
                            {"ok": False, "msg": note}, ensure_ascii=False))
                        return
                    p = site_probe.probe_type(base, timeout=10)
                    host = site_admin.host_from_url(base)
                    dup = site_admin.find_site(host)
                    # ★ 顺便把 Hermes 里跟这个域名相关的 provider 挑出来，
                    #   供界面下拉选择（拿 exe 的人不知道 custom:xxx 写什么）
                    matched, others = site_admin.providers_for(host)
                    self._send(200, json.dumps({
                        "ok": True,
                        "input": want_probe,
                        "base_url": base,
                        "norm_note": note,
                        "host": host,
                        "kind": p.get("kind"),
                        "reason": p.get("reason"),
                        "root": p.get("root"),
                        "duplicate": (dup.get("label") or dup.get("host")) if dup else "",
                        "providers_matched": matched,
                        "providers_others": [x["name"] for x in others],
                    }, ensure_ascii=False))
                    return

                sites = site_admin.all_sites(include_disabled=True)
                raw = site_admin.load_raw()
                self._send(200, json.dumps({
                    "sites": sites,
                    "cache": raw.get("_probe_cache") or {},
                    "count": len(sites),
                }, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        """写操作：模型定价的新增/修改/删除"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            body = json.loads(raw or "{}")
        except Exception as e:
            self._send(400, json.dumps({"error": "bad json: %s" % e}, ensure_ascii=False))
            return

        if self.path.startswith("/api/join_health"):
            # ★ 2026-10-09 新增：补登域名（归集健康度条上那个「补登」按钮）
            #   写操作：往 sites.json 的 bases 里加一个入口域名
            try:
                import importlib
                import site_admin
                importlib.reload(site_admin)
                ok, msg = site_admin.add_base_domain(
                    body.get("host") or "", body.get("domain") or "")
                # 补登成功后顺手重跑一次归集，让数字立刻对
                if ok:
                    try:
                        import session_join
                        importlib.reload(session_join)
                        session_join.main_quiet()
                    except Exception:
                        pass
                self._send(200, json.dumps({"ok": ok, "msg": msg},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
            return

        if self.path.startswith("/api/prices"):
            try:
                import prices
                act = body.get("action") or "save"
                if act == "delete":
                    ok = prices.delete(body.get("host") or "", body.get("model") or "")
                    self._send(200, json.dumps({"ok": ok}, ensure_ascii=False))
                    return
                row = prices.upsert(
                    body.get("host") or "",
                    body.get("model") or "*",
                    body.get("in"), body.get("out"), body.get("cache"),
                    cur=body.get("cur") or "¥",
                    tag=body.get("tag") or "",
                    note=body.get("note") or "",
                    per_call=body.get("per_call"))
                self._send(200, json.dumps({"ok": True, "row": row}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/creds"):
            # ★ 2026-10-06：录入/更新站点账号密码 → 按 kind 自动分派登录
            try:
                import importlib
                import credentials
                import site_auth
                importlib.reload(credentials)
                importlib.reload(site_auth)
                act = body.get("action") or "save"
                site = body.get("site") or ""
                if not site:
                    self._send(400, json.dumps({"error": "缺少 site"}, ensure_ascii=False))
                    return
                # ★ 2026-10-08 新增：删除站点（连带清空用量/归集/账号/凭据/
                #   登录态/价格/校准 —— 见 site_delete.py）。
                #   ?preview 只报「将删什么」不真删，供前端弹确认框。
                if act in ("delete_site", "preview_delete"):
                    import importlib as _il3
                    import site_delete as _sd
                    _il3.reload(_sd)
                    if act == "preview_delete":
                        pv = _sd.preview(site)
                        self._send(200, json.dumps(
                            {"ok": pv.get("ok"), "action": "preview_delete",
                             "preview": pv}, ensure_ascii=False))
                        return
                    ok, msg, detail = _sd.delete(site, log=lambda m: None)
                    # 删除后让前端刷新（配置缓存已在 site_delete 里失效）
                    self._send(200, json.dumps({
                        "ok": ok, "action": "delete_site", "msg": msg,
                        "detail": detail,
                    }, ensure_ascii=False))
                    return
                if act == "delete":
                    ok = credentials.delete(site)
                    self._send(200, json.dumps({"ok": ok}, ensure_ascii=False))
                    return
                # ★ 2026-10-08 新增：设置/清除代理（前端「保存代理」按钮走这里）。
                #   必须放在 email/password 校验**之前** —— 设代理不需要账号密码，
                #   否则会被下面的「邮箱和密码都不能为空」拦掉（实测踩过）。
                if act == "set_proxy":
                    px = (body.get("proxy") or "").strip()
                    # 先探测代理可用性（当场告知填对没有，不用等下一轮采集）
                    chk_ok, chk_msg = True, ""
                    if px:
                        try:
                            import site_collect as _sc2
                            importlib.reload(_sc2)
                            chk_ok, chk_msg = _sc2.check_proxy(px)
                        except Exception as e:
                            chk_ok, chk_msg = False, "探测失败：%s" % str(e)[:100]
                    # 探测不通也允许保存（可能只是暂时抖动），但如实告知
                    import site_admin as _sadm2
                    importlib.reload(_sadm2)
                    ok, msg = _sadm2.set_proxy(site, px)
                    self._send(200, json.dumps({
                        "ok": ok, "msg": msg, "action": "set_proxy",
                        "proxy_check_ok": chk_ok, "proxy_check_msg": chk_msg,
                    }, ensure_ascii=False))
                    return
                email = (body.get("email") or "").strip()
                pwd = body.get("password") or ""
                if not email or not pwd:
                    self._send(400, json.dumps({"error": "邮箱和密码都不能为空"}, ensure_ascii=False))
                    return
                # ★ 2026-10-07 修正：先校验站点是否支持/需要登录，通过了才写盘。
                #   原实现是「先 save 再 login」，导致给无需登录的站（或错密码）
                #   也会把无效凭据写进 credentials.json —— 实测已复现并清理。
                site_cfg = site_auth.find_site(site)
                if not site_cfg:
                    self._send(200, json.dumps({
                        "ok": False, "msg": "找不到站点 %s" % site,
                    }, ensure_ascii=False))
                    return
                if not site_auth.needs_login(site_cfg):
                    self._send(200, json.dumps({
                        "ok": False,
                        "msg": "该站不需要登录态（kind=%s，用 API key 采集），未写入凭据"
                               % (site_cfg.get("kind") or "?"),
                    }, ensure_ascii=False))
                    return
                # 校验凭据可用 → 成功才落盘
                ok, msg, st = site_auth.login(site, email, pwd)
                if ok:
                    credentials.save(site, email, pwd)
                self._send(200, json.dumps({
                    "ok": ok, "msg": msg, "status": st,
                }, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path.startswith("/api/sites"):
            # ★ 2026-10-07：加站 / 启停（有副作用 —— 写 sites.json，带备份+校验+原子写）
            try:
                import importlib
                import site_admin
                importlib.reload(site_admin)
                act = (body.get("action") or "add").lower()

                if act in ("enable", "disable"):
                    ok, msg = site_admin.set_enabled(
                        body.get("host") or "", act == "enable")
                    self._send(200, json.dumps({"ok": ok, "msg": msg},
                                               ensure_ascii=False))
                    return

                if act == "edit":
                    # ★ 2026-10-09 新增：站点配置页用 —— 改 provider / 备选域名 / 代理
                    ok, msg = site_admin.edit_site(
                        body.get("host") or "",
                        bases=body.get("bases"),
                        providers=body.get("hermes_providers"),
                        proxy=body.get("proxy"),
                        label=body.get("label"),
                        note=body.get("note"))
                    self._send(200, json.dumps({"ok": ok, "msg": msg},
                                               ensure_ascii=False))
                    return

                if act == "add":
                    raw_url = (body.get("base_url") or "").strip()
                    base, note = site_admin.normalize_base_url(raw_url)
                    if not base:
                        self._send(200, json.dumps(
                            {"ok": False, "msg": "站点地址不能为空"},
                            ensure_ascii=False))
                        return
                    # 探测结果（前端探测过就带上，避免后端重复联网）
                    probe_kind = (body.get("probe_kind") or "").strip()
                    probe = None
                    if probe_kind and probe_kind != "unknown":
                        probe = {"kind": probe_kind,
                                 "reason": body.get("probe_reason") or "加站时探测"}
                    entry = {
                        "base_url": base,
                        "host": site_admin.host_from_url(base),
                        "hermes_providers": body.get("hermes_providers") or [],
                        "label": (body.get("label") or "").strip(),
                        # ★ 探测出的 kind 一并落进站点配置（unknown 不写，
                        #   留给 site_probe 在加载时重探，别把「探不出」固化）
                        "kind": probe_kind if probe_kind and probe_kind != "unknown" else "",
                        "note": (body.get("note") or "").strip(),
                        # ★ 2026-10-08 新增：加站时可选填代理（空 = 直连）
                        "proxy": (body.get("proxy") or "").strip(),
                    }
                    # ★ 2026-10-09 新增：备选地址（同一家的其它入口域名）真正落进 bases。
                    #   用户换入口后（api2 → api9）归集会认不出来，这里预先把镜像域名
                    #   登记好，换入口就不用回来改配置了。
                    _bases = body.get("bases") or []
                    if isinstance(_bases, list) and _bases:
                        entry["bases"] = [str(b).strip() for b in _bases if str(b).strip()]
                    ok, msg, row = site_admin.add_site(entry, probe=probe)
                    self._send(200, json.dumps({
                        "ok": ok, "msg": msg, "site": row, "norm_note": note,
                    }, ensure_ascii=False))
                    return

                self._send(400, json.dumps(
                    {"ok": False, "msg": "未知操作 %s" % act},
                    ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


PAGE = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>中转站用量监控</title><style>
:root{--bg:#1b1b1f;--card:#232329;--line:#33333b;--fg:#e8e8ea;--dim:#8a8a92;
      --grn:#4ec26a;--ylw:#e0b542;--blu:#5aa9e0}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:"Microsoft YaHei",system-ui,sans-serif;
     margin:0;padding:20px 24px}
.top{display:flex;align-items:center;gap:14px;margin-bottom:6px}
h1{font-size:19px;margin:0}
.btn{background:#2e4a3a;color:#7ee09a;border:1px solid #3a5c48;border-radius:7px;
     padding:7px 15px;font-size:13px;cursor:pointer;font-family:inherit;transition:.15s}
.btn:hover{background:#365a45;color:#a0f0b8}
.btn:disabled{opacity:.5;cursor:default}
.btn.spin{animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.55}}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.msg{font-size:12px;color:var(--grn);margin-left:4px}
.msg.err{color:#e05a5a}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:13px 17px;min-width:250px}
.card.today{background:#1f2a24;border-color:#2e4a3a}
.card h3{margin:0 0 7px;font-size:13px;color:var(--blu)}
.card.today h3{color:var(--grn)}
.big{font-size:25px;font-weight:700;margin-bottom:7px}
.big .u{font-size:12px;color:var(--dim);font-weight:400}
.kv{font-size:12px;color:#b8b8c0;line-height:1.72}
.kv b{color:var(--fg)}
.dimtxt{color:#6a6a72;font-size:11px;margin-top:5px}
table{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:22px}
th,td{padding:6px 9px;text-align:right;border-bottom:1px solid #2c2c33;white-space:nowrap}
th{color:var(--dim);font-weight:500;background:#202025}
td:first-child,th:first-child{text-align:left}
tr:hover{background:#26262c}
h2{font-size:14px;color:var(--ylw);margin:20px 0 8px;font-weight:600}
.hit{color:var(--grn);font-weight:600}
.hit.low{color:#e0b542}
.hit.bad{color:#e05a5a}
.tag{font-size:10px;padding:1px 6px;border-radius:8px;background:#2e4a3a;color:#7ee09a;font-weight:400}
.tabs{display:flex;gap:6px;margin-bottom:16px;border-bottom:1px solid var(--line)}
.tab{padding:7px 16px;font-size:13px;color:var(--dim);cursor:pointer;border:none;
     background:none;font-family:inherit;border-bottom:2px solid transparent;transition:.15s}
.tab:hover{color:var(--fg)}
.tab.on{color:var(--grn);border-bottom-color:var(--grn);font-weight:600}
input.pf{background:#1b1b1f;border:1px solid var(--line);color:var(--fg);border-radius:5px;
     padding:4px 7px;font-size:12px;font-family:inherit;width:82px}
input.pf:focus{outline:none;border-color:var(--grn)}
input.pf.txt{width:150px}
input.pf.txt.wide{width:420px}
/* ★ 2026-10-09 站点配置页 */
.cfgnote{background:var(--card);border:1px solid var(--line);border-radius:6px;
  padding:10px 12px;margin:0 0 12px;font-size:12.5px;line-height:1.7;color:var(--dim)}
.cfgnote b{color:var(--fg)}
.cfgcard{background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:12px 14px;margin:0 0 12px}
.cfghd{display:flex;align-items:center;gap:10px;margin-bottom:8px;
  padding-bottom:8px;border-bottom:1px solid var(--line)}
.cfghd b{font-size:14px;color:var(--fg)}
.cfgrow{display:flex;align-items:center;gap:10px;margin:7px 0}
.cfgrow label{width:76px;color:var(--dim);font-size:12.5px;flex:none}
.cfghint{margin:0 0 8px 86px;font-size:11.5px;line-height:1.65;color:var(--dim)}
.cfghint code{background:#1b1b1f;padding:1px 5px;border-radius:3px;font-size:11px}
.cfgbtns{margin-top:10px;padding-top:10px;border-top:1px solid var(--line);
  display:flex;gap:8px}
.btn.sm{padding:3px 10px;font-size:11px}
/* ★ 2026-10-09 归集健康度条 */
.joinbar{margin:0 0 10px;padding:8px 12px;border-radius:6px;font-size:12.5px;
  line-height:1.6;border:1px solid var(--line)}
.joinbar.dim{color:var(--dim);background:var(--card)}
.joinbar.ok{color:var(--grn);background:rgba(78,194,106,.08);
  border-color:rgba(78,194,106,.3)}
.joinbar.warn{color:var(--ylw);background:rgba(224,181,66,.08);
  border-color:rgba(224,181,66,.35)}
.joinlist{margin-top:6px;padding-top:6px;border-top:1px dashed rgba(224,181,66,.3)}
.joinrow{margin:3px 0}
.joinrow b{color:var(--fg)}
.btn.gray{background:#2c2c33;color:#a8a8b0;border-color:#3a3a44}
.btn.gray:hover{background:#35353d;color:#d0d0d8}
.btn.red{background:#3a2626;color:#e08a8a;border-color:#5a3a3a}
.btn.red:hover{background:#4a2e2e;color:#f0a0a0}
tr.miss{background:#2a2418}
tr.miss:hover{background:#332b1c}
.pill{display:inline-block;font-size:10px;padding:1px 7px;border-radius:8px;margin-left:6px}
.pill.ok{background:#2e4a3a;color:#7ee09a}
.pill.no{background:#4a3a1e;color:#e0b542}
.sect{font-size:12px;color:var(--dim);margin:16px 0 6px}
/* ★ 2026-10-06 凭据告警条 */
.alert{display:none;align-items:center;gap:12px;margin:0 0 14px;padding:11px 15px;
       border-radius:8px;font-size:13px;line-height:1.5}
.alert.show{display:flex}
.alert.err{background:#3a2222;border:1px solid #6a3838;color:#f0a8a8}
.alert.warn{background:#3a3222;border:1px solid #6a5a38;color:#f0d0a0}
.alert .atxt{flex:1}
.alert b{color:#fff}
.credbox{margin:14px 0;padding:13px 16px;background:#232329;border:1px solid var(--line);
         border-radius:8px;font-size:12px;display:none}
.credbox.show{display:block}
.credbox h3{margin:0 0 10px;font-size:13px;color:var(--ylw)}
.credbox input{background:#1b1b1f;border:1px solid var(--line);color:var(--fg);
               border-radius:5px;padding:5px 8px;font-size:12px;font-family:inherit;
               width:210px;margin-right:8px}
.credbox input:focus{outline:none;border-color:var(--grn)}
.credline{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:6px 0}
.credhint{color:#6a6a72;font-size:11px;margin-top:8px;line-height:1.6}
h2.sec{font-size:14px;color:var(--ylw);margin:20px 0 8px;font-weight:600;
    cursor:pointer;user-select:none;display:flex;align-items:center;gap:7px}
h2.sec:hover{color:#f0d060}
h2.sec .arw{display:inline-block;transition:transform .18s;font-size:11px;color:var(--dim)}
h2.sec.fold .arw{transform:rotate(-90deg)}
h2.sec .cnt{font-size:11px;color:var(--dim);font-weight:400;margin-left:auto}
.foldbox.hidden{display:none}

/* ═══════════ ★ 2026-10-07 UI 优化 + 加站界面 ═══════════ */

/* 数字等宽：金额/百分数右对齐时位对齐，扫一眼就能比大小 */
td, .big, .kv b, th{font-variant-numeric:tabular-nums}
td:not(:first-child){font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:11.5px}
th{position:sticky;top:0;z-index:2}
/* 长表格横向滚动（窄屏不撑破布局） */
table{display:block;overflow-x:auto;white-space:nowrap}
table tr{display:table-row}
table thead,table tbody{display:table;width:100%}

/* 按钮补强 */
.btn:active{transform:translateY(1px)}
.btn.blue{background:#243a4a;color:#7ec8f0;border-color:#35566e}
.btn.blue:hover{background:#2c475a;color:#a0dcff}

/* 空状态 */
.empty{padding:26px 20px;text-align:center;color:#6a6a72;font-size:13px;
  border:1px dashed var(--line);border-radius:10px;margin:14px 0;line-height:1.9}
.empty .ic{font-size:26px;display:block;margin-bottom:8px;opacity:.6}
.empty b{color:var(--ylw)}

/* 加载骨架 */
.skel{height:52px;border-radius:9px;margin:8px 0;
  background:linear-gradient(90deg,#232329 25%,#2b2b33 50%,#232329 75%);
  background-size:400% 100%;animation:sk 1.3s ease-in-out infinite}
@keyframes sk{0%{background-position:100% 0}100%{background-position:0 0}}

/* 加站盒子 */
.addbox{display:none;margin:14px 0;padding:14px 16px;background:#20262a;
  border:1px solid #2e4a3a;border-radius:10px;font-size:12px}
.addbox.show{display:block}
.addbox h3{margin:0 0 12px;font-size:13px;color:var(--grn);display:flex;
  align-items:center;gap:8px}
.stepnum{display:inline-flex;align-items:center;justify-content:center;
  width:18px;height:18px;border-radius:50%;background:#2c2c33;color:#8a8a92;
  font-size:11px;font-weight:600}
.stepnum.on{background:#2e4a3a;color:#7ee09a}
.proberes{margin:10px 0 4px;padding:11px 13px;border-radius:8px;font-size:12px;
  line-height:1.75;border:1px solid var(--line);background:#25252b}
.proberes.ok{border-color:#3a5c48;background:#1f2a24}
.proberes.warn{border-color:#6a5a38;background:#2a2418}
.proberes.err{border-color:#6a3838;background:#2a1f1f}
.proberes .kindline{font-size:14px;font-weight:600;margin-bottom:5px}
.proberes .why{color:#8a8a92;font-size:11.5px}
.proberes code{background:#1b1b1f;padding:1px 5px;border-radius:4px;
  color:#7ec8f0;font-size:11px}
.dupwarn{margin-top:7px;padding:6px 10px;border-radius:6px;
  background:#3a2626;color:#f0a8a8;font-size:11.5px}
/* ★ 2026-10-07 按对话按站分组 */
.sessblock{margin:10px 0 16px}
.sesshead{font-size:12.5px;color:var(--fg);font-weight:600;padding:7px 0 5px;
  display:flex;align-items:center;gap:8px;border-bottom:1px solid #2c2c33}
.sesshead .sesssum{color:#6a6a72;font-weight:400;font-size:11px;margin-left:auto}
/* 站点卡片上的停用/启用按钮 */
.sitectl{margin-left:auto;display:flex;gap:6px}
.credbox h3{display:flex;align-items:center;gap:7px;flex-wrap:wrap}

/* provider 多选（★ 2026-10-07） */
.provbox{display:flex;flex-wrap:wrap;gap:6px;max-width:560px}
.provbox .pv{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;
  border-radius:7px;border:1px solid var(--line);background:#1b1b1f;
  font-size:11.5px;cursor:pointer;transition:.15s;user-select:none}
.provbox .pv:hover{border-color:#3a5c48}
.provbox .pv.on{background:#2e4a3a;border-color:#3a5c48;color:#a0f0b8}
.provbox .pv.miss{opacity:.5}
.provbox .pv input{width:auto;margin:0;accent-color:#4ec26a}
.provbox .pv .bl{color:#8a8a92;font-size:10.5px}
.provbox .pv.on .bl{color:#7ee09a}
.provwarn{margin:4px 0 8px 62px;padding:6px 10px;border-radius:6px;
  background:#3a3222;color:#f0d0a0;font-size:11.5px}
.provbox .none{color:#6a6a72;font-size:11.5px;padding:4px 0}
</style></head><body>
<div class="top">
  <h1>中转站用量监控</h1>
  <button class="btn" id="rf" onclick="doRefresh()">⟳ 立即刷新</button>
  <button class="btn blue" id="addbtn" onclick="openAddSite()">＋ 加站</button>
  <span class="msg" id="msg"></span>
</div>
<div class="sub" id="sub">加载中…</div>
<div class="alert err" id="alert"><span class="atxt" id="alerttxt"></span>
  <button class="btn" id="alertbtn" onclick="switchTab('site')">去站点账号页</button></div>
<div class="tabs">
  <button class="tab on" id="tab-usage" onclick="switchTab('usage')">用量</button>
  <button class="tab" id="tab-price" onclick="switchTab('price')">模型定价</button>
  <button class="tab" id="tab-site" onclick="switchTab('site')">站点账号</button>
  <button class="tab" id="tab-siteset" onclick="switchTab('siteset')">站点配置</button>
</div>
<div id="root"></div>
<div id="priceroot" style="display:none"></div>
<div id="siteroot" style="display:none"></div>
<div id="sitesetroot" style="display:none"></div>

<!-- ★ 2026-10-07 加站表单（默认收起，点「+ 加站」展开） -->
<div id="addsite" class="addbox">
  <h3>添加站点 <span class="stepnum on">1</span>
      <span style="color:#8a8a92;font-weight:400;font-size:12px">填地址 → 探测 → 确认</span>
      <button class="btn gray sm" style="margin-left:auto" onclick="closeAddSite()">收起</button></h3>

  <div class="credline">
    <span style="width:56px">站点地址</span>
    <input id="ns_url" type="text" placeholder="api.xxx.com 或 https://api.xxx.com"
           autocomplete="off" style="width:300px" onkeydown="if(event.key==='Enter')doProbe()">
    <button class="btn blue" id="ns_probe_btn" onclick="doProbe()">探测 →</button>
    <span class="msg" id="ns_msg"></span>
  </div>
  <div class="credhint" style="margin:2px 0 0 62px">
    只填域名就行，程序会自动补 <code>https://</code> 和 <code>/v1</code>。
    带路径的按你写的原样保留。
  </div>

  <!-- 探测结果区（探测后出现） -->
  <div id="ns_result" class="proberes" style="display:none"></div>

  <!-- 第二步：补充信息 + 确认 -->
  <div id="ns_step2" style="display:none">
    <div class="credline">
      <span style="width:56px">别名</span>
      <input id="ns_label" type="text" placeholder="如：D1 站（主力）；留空用域名" style="width:300px">
    </div>
    <div class="credline" style="align-items:flex-start">
      <span style="width:56px;padding-top:5px">provider</span>
      <div style="flex:1">
        <div id="ns_prov_box" class="provbox"></div>
        <input id="ns_prov" type="text" placeholder="也可以手输，多个用逗号分隔"
               style="width:300px;margin-top:6px" oninput="checkProvWarn()">
      </div>
    </div>
    <div class="credhint" style="margin:0 0 6px 62px">
      从 Hermes 的 <code>config.yaml</code> 自动读出来的，<b>带 ✓ 的是域名匹配上的</b>，通常全选即可。<br>
      <b style="color:#e0b542">留空的后果</b>：采集器拿不到 API key，这个站采不到数据，用量页不会显示。
    </div>
    <div id="ns_provwarn" class="provwarn" style="display:none">
      ⚠ 没有选任何 provider —— 这个站将无法采集数据。确认要这样加吗？
    </div>
    <div class="credline">
      <span style="width:56px">备选地址</span>
      <input id="ns_bases" type="text" placeholder="可空。多个镜像域名用逗号分隔" style="width:300px">
    </div>
    <div class="credline">
      <span style="width:56px">代理</span>
      <input id="ns_proxy" type="text" placeholder="可空。留空 = 直连；如 4512 或 http://127.0.0.1:4512" style="width:300px">
    </div>
    <div class="credhint" style="margin:0 0 6px 62px">
      该站直连不通（超时/被墙）时填代理，只影响本站。留空即直连。
    </div>
    <div style="margin:10px 0 0 62px;display:flex;gap:8px;align-items:center">
      <button class="btn" id="ns_save_btn" onclick="doAddSite()">✓ 确认加站</button>
      <button class="btn gray" onclick="resetAddSite()">← 重填</button>
      <label style="font-size:12px;color:#b8b8c0;display:flex;align-items:center;gap:5px">
        <input type="checkbox" id="ns_refresh" checked style="width:auto"> 加完立刻采一次
      </label>
    </div>
  </div>
</div>

<script>
const EX = __EX__;
function fmt(n){ return (n||0).toLocaleString('en-US'); }
/* ★ 2026-10-07 审计修复：原来只转 <>&，不转引号 —— 而 esc() 被用在
   title="${esc(x)}" / value="${esc(x)}" 这类双引号属性里，x 含一个双引号
   就能注入 onmouseover 等事件属性（真浏览器实测可触发）。现在一并转 " 和 '，
   文本上下文多转义这两个字符是安全的。 */
function esc(s){
  var t = String(s == null ? '' : s);
  t = t.replace(/&/g, '&amp;');
  t = t.replace(/</g, '&lt;');
  t = t.replace(/>/g, '&gt;');
  t = t.replace(/"/g, '&quot;');
  t = t.replace(/'/g, '&#39;');
  return t;
}
function hitCls(h){ return h>=90?'hit':(h>=60?'hit low':'hit bad'); }
/* 塞进 HTML 属性（title= / value=）时用：转 & < > " ' */
/* 从 data-* 读值（配合 data-site / data-model 使用） */
/* 从按钮自己的 data-host 读值（按钮写成 onclick="fn(dh(this))"） ——
   这样 host 只经 HTML 属性转义，不进 JS 字符串，杜绝单引号逃逸。 */
function dh(el){
  if(!el) return '';
  var a = el.getAttribute ? el.getAttribute('data-host') : null;
  return a == null ? '' : a;
}
function ds(el){ return el && el.getAttribute ? (el.getAttribute('data-site') || '') : ''; }
function dm(el){ return el && el.getAttribute ? (el.getAttribute('data-model') || '') : ''; }

function attr(s){
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}


function render(st){
  if(st.error){ document.getElementById('sub').textContent = st.error; return; }
  const lr = st.last_refresh||{};
  const lrTxt = lr.at ? new Date(lr.at*1000).toLocaleTimeString('zh-CN') : '尚未手动刷新';
  const lv = lr.level||'ok';
  const lvTag = lv==='error' ? ' ｜ <span style="color:#e08a8a">⚠ ' : (lv==='warn' ? ' ｜ <span style="color:#e0b542">' : ' ｜ ');
  const lvTxt = lr.msg ? (lvTag + esc(lr.msg) + '</span>') : '';
  document.getElementById('sub').innerHTML =
    `数据时间 ${st.now} ｜ 上次手动刷新 ${lrTxt} ｜ 自动采集每 5 分钟 ｜ 汇率按 ${EX} 折算${lvTxt}`;
  renderCredAlert(st);

  let todayCards = '', totalCards = '';
  for(const s of st.sites){
    const acc = s.account||{};
    const rate = (acc.rate_multiplier||0);
    const accRow = acc.plan_total
      ? `<div class="kv">套餐 ${fmt(acc.plan_used)}/${fmt(acc.plan_total)} (${(acc.plan_used/acc.plan_total*100).toFixed(2)}%)</div>`
      : (acc.plan_remain ? `<div class="kv">余额 <b>$${(acc.plan_remain/500000).toFixed(4)}</b>${rate?`　倍率 ×${rate}`:''}</div>` : '');
    todayCards += `<div class="card today">
      <h3>${esc(s.site)} <span class="tag">今日</span></h3>
      <div class="big">¥${s.today_cost.toFixed(6)}</div>
      <div class="kv">请求合计 <b>${fmt(s.today_biz + s.today_internal)}</b>
        （业务 ${fmt(s.today_biz)} + 审批 ${fmt(s.today_internal)}）</div>
      <div class="kv">其中审批花费 <b>¥${s.today_internal_cost.toFixed(6)}</b>
        <span style="color:#8a8a92">（占比 ${s.today_cost?((s.today_internal_cost/s.today_cost)*100).toFixed(1):'0.0'}%）</span></div>
      <div class="kv">缓存命中 <b>${fmt(s.today_cache)}</b> ／ 未命中 <b>${fmt(s.today_in)}</b></div>
      <div class="kv">整体命中率 <span class="${hitCls(s.today_hit)}">${s.today_hit.toFixed(1)}%</span>
        　业务命中率 <span class="${hitCls(s.today_biz_hit)}">${s.today_biz_hit.toFixed(1)}%</span></div>
    </div>`;
    totalCards += `<div class="card">
      <h3>${esc(s.site)} <span class="tag" style="background:#2c2c33;color:#8a8a92">累计</span></h3>
      <div class="big">${fmt(s.total_n)} <span class="u">次请求</span></div>
      <div class="kv">总花费 <b>¥${(s.total_cost*EX).toFixed(4)}</b></div>
      ${accRow}
      <div class="dimtxt">${s.d0||''} ~ ${s.d1||''}</div>
    </div>`;
  }

  let daily = st.daily.map(r=>{
    const t=(r.i||0)+(r.cr||0), h=t?(r.cr||0)/t*100:0;
    return `<tr><td>${r.day}</td><td>${esc(r.site)}</td><td>${fmt(r.n)}</td>
      <td>${fmt(r.i)}</td><td>${fmt(r.cr)}</td><td>${fmt(r.o)}</td>
      <td class="${hitCls(h)}">${h.toFixed(1)}%</td>
      <td>¥${((r.c||0)*EX).toFixed(5)}</td></tr>`;
  }).join('');

  // ★ 2026-10-07：按站分组渲染「按对话」—— 原来全部混在一张表里，
  //   会话多的站（d1/dshapi 各 20 上下）会把会话少的站（tryaigc 3 个）挤光，
  //   用户完全看不到 tryaigc 的数据（实测反馈）。
  let sessGroups = '';
  const sessAll = st.sessions || [];
  const bySite = {};
  for(const r of sessAll){
    const k = r.site || '(未知站点)';
    (bySite[k] = bySite[k] || []).push(r);
  }
  const sessSites = Object.keys(bySite).sort();
  for(const site of sessSites){
    const rows = bySite[site].map(r=>{
      const t=(r.i||0)+(r.cr||0), h=t?(r.cr||0)/t*100:0;
      const last = r.last_ts ? new Date(r.last_ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
      return `<tr><td>${esc(r.session_id)}</td><td>${fmt(r.n)}</td>` +
        `<td>${fmt(r.i)}</td><td>${fmt(r.cr)}</td>` +
        `<td class="${hitCls(h)}">${h.toFixed(1)}%</td>` +
        `<td>¥${((r.c||0)*EX).toFixed(6)}</td><td>${(r.conf||0).toFixed(2)}</td>` +
        `<td>${last}</td></tr>`;
    }).join('');
    const sumN = bySite[site].reduce((a,x)=>a+(x.n||0),0);
    const sumC = bySite[site].reduce((a,x)=>a+(x.c||0),0);
    sessGroups += `<div class="sessblock">` +
      `<div class="sesshead">${esc(site)}` +
      `<span class="tag">${bySite[site].length} 个对话</span>` +
      `<span class="sesssum">合计 ${fmt(sumN)} 次请求 ｜ ¥${(sumC*EX).toFixed(6)}</span></div>` +
      `<table><tr><th>会话</th><th>请求</th><th>未命中</th><th>缓存命中</th><th>命中率</th><th>花费¥</th><th>置信</th><th>最后活动</th></tr>${rows}</table>` +
      `</div>`;
  }
  if(!sessGroups){
    sessGroups = '<div class="sect">暂无按对话数据（需要登录态采集逐条流水）</div>';
  }
  // ★ 2026-10-09 新增：归集健康度条（用户要求"以后自己也能改"）
  //   平时只显示一行「✅ 用量归集正常 N/N」；认不出的地址列出来，点一下补登。
  let joinBarHTML = '<div class="joinbar dim" id="joinbar">归集状态检查中…</div>';
  (async function(){
    let jh = null;
    try{
      const r = await fetch('/api/join_health', {cache:'no-store'});
      jh = await r.json();
    }catch(e){ }
    const el = document.getElementById('joinbar');
    if(!el) return;
    if(!jh || jh.error){
      el.className = 'joinbar dim';
      el.textContent = '归集状态未知（接口读不到）';
      return;
    }
    if(jh.ok){
      el.className = 'joinbar ok';
      el.innerHTML = '✅ ' + esc(jh.msg || '用量归集正常');
      return;
    }
    // 有问题：列出没登记的地址，给「补登」按钮
    el.className = 'joinbar warn';
    const unk = (jh.unknown || []).filter(u => u.hint && u.hint.indexOf('还没登记') >= 0);
    let html = '⚠ ' + esc(jh.msg || '有用量没统计上');
    if(unk.length){
      html += '<div class="joinlist">';
      for(const u of unk){
        html += '<div class="joinrow">地址 <b>' + esc(u.domain) + '</b> 没登记'
              + '（' + u.count + ' 条）';
        if(u.site){
          html += ' <button class="btn sm" onclick="registerDomain('
                + JSON.stringify(u.domain) + ',' + JSON.stringify(u.site) + ')">'
                + '补登到 ' + esc(u.site) + '</button>';
        }else{
          html += ' → 请先到「站点账号」页确认它属于哪个站的'
                + ' <button class="btn sm" onclick="switchTab(\'site\')">去站点账号页</button>';
        }
        html += '</div>';
      }
      html += '</div>';
    }else{
      html += '<div class="joinlist">域名都登记了 —— 可能是时间窗/配额挡下的，'
            + '稍后会自动补。持续不降请点「⟳ 立即刷新」。</div>';
    }
    el.innerHTML = html;
  })();

  document.getElementById('root').innerHTML = `
    ${joinBarHTML}
    <h2 class="sec" onclick="fold(this)"><span class="arw">▼</span>今日
      <span class="cnt">${st.sites.length} 个站点</span></h2>
    <div class="foldbox"><div class="cards">${todayCards}</div></div>
    <h2 class="sec" onclick="fold(this)"><span class="arw">▼</span>累计
      <span class="cnt">${st.sites.length} 个站点</span></h2>
    <div class="foldbox"><div class="cards">${totalCards}</div></div>
    <h2 class="sec" onclick="fold(this)"><span class="arw">▼</span>按对话（站方数据归集，按站点分组）
      <span class="cnt">${sessSites.length} 个站 / ${sessAll.length} 条</span></h2>
    <div class="foldbox">${sessGroups}</div>
    <h2 class="sec" onclick="fold(this)"><span class="arw">▼</span>按日明细（命中率仅算业务请求，已剔除智能审批）
      <span class="cnt">${(st.daily||[]).length} 行</span></h2>
    <div class="foldbox"><table><tr><th>日期</th><th>站点</th><th>业务</th><th>输入</th><th>缓存命中</th><th>输出</th><th>命中率</th><th>¥</th></tr>${daily}</table></div>`;
}

/* ★ 2026-10-09 新增：把没登记的地址补登到站点配置 */
async function registerDomain(domain, host){
  if(!confirm('把这个地址补登到站点配置？\n\n地址：' + domain + '\n站点：' + host +
              '\n\n补登后归集会重新跑一遍，这个站的用量就能统计上了。')) return;
  try{
    const r = await fetch('/api/join_health', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'register', domain: domain, host: host})});
    const d = await r.json();
    if(d.error || !d.ok){ alert(d.error || d.msg || '补登失败'); return; }
    alert('✓ ' + (d.msg || '已补登') + '\n\n正在重新归集，几秒后刷新看看。');
    // 触发一次归集 + 刷新
    try{
      await fetch('/api/refresh', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:'{}'});
    }catch(e){}
    setTimeout(()=>location.reload(), 1500);
  }catch(e){ alert('补登失败：' + e); }
}

/* ===== ★ 2026-10-09 新增：站点配置页 =====
   用户需求原话：「不止要让你帮我改这次问题，以后再有这种情况我希望我自己也能改」
   所以这一页把「归集出问题时要改什么」全部摆在明面上，每个字段配一句人话说明。
   主流程：只改「备选域名」和「Provider」两项，其余都是高级选项。
*/
let SITE_SET = null;

async function loadSiteSet(){
  const el = document.getElementById('sitesetroot');
  el.innerHTML = '<div class="skel"></div>';
  try{
    const r = await fetch('/api/sites', {cache:'no-store'});
    SITE_SET = await r.json();
    renderSiteSet();
  }catch(e){
    el.innerHTML = '<div class="sect" style="color:#e08a8a">读取失败：'+esc(String(e))+'</div>';
  }
}

function renderSiteSet(){
  const data = SITE_SET || {};
  const sites = data.sites || [];
  const el = document.getElementById('sitesetroot');
  if(!sites.length){
    el.innerHTML = '<div class="empty"><span class="ic">📡</span>还没有站点</div>';
    return;
  }
  let rows = '';
  for(const s of sites){
    const h = s.host || '';
    const bases = (s.bases || []).join(', ');
    const provs = (s.hermes_providers || []).join(', ');
    const on = s.enabled !== false;
    rows += `<div class="cfgcard">
      <div class="cfghd">
        <b>${esc(s.label || h)}</b>
        <span class="dimtxt">${esc(h)}</span>
        <span class="tag">${on ? '启用中' : '已停用'}</span>
      </div>
      <div class="cfgrow">
        <label>备选域名</label>
        <input class="pf txt wide" data-k="bases" data-host="${attr(h)}"
               value="${attr(bases)}" placeholder="可空。多个用英文逗号分隔">
      </div>
      <div class="cfghint">
        同一家的其它入口地址（比如 <code>api9.dshapi.icu</code>）。
        <b style="color:#e0b542">换了新的 API 地址后，用量统计不上，多半就是这里缺了它</b>
        —— 补上、保存，回用量页点「⟳ 立即刷新」就会重算。
      </div>
      <div class="cfgrow">
        <label>Provider</label>
        <input class="pf txt wide" data-k="hermes_providers" data-host="${attr(h)}"
               value="${attr(provs)}" placeholder="可空。多个用英文逗号分隔">
      </div>
      <div class="cfghint">
        Hermes 记在账上的内部名字（如 <code>custom:your-provider</code>）。
        <b>平时不用管</b>；只有在「域名也对、用量还是统计不上」时，才去
        Hermes 的 <code>config.yaml</code> 里看这个站新用的名字，粘在这里。
      </div>
      <div class="cfgrow">
        <label>代理</label>
        <input class="pf txt" data-k="proxy" data-host="${attr(h)}"
               value="${attr(s.proxy || '')}" placeholder="可空 = 直连，如 4512">
      </div>
      <div class="cfgrow">
        <label>别名</label>
        <input class="pf txt" data-k="label" data-host="${attr(h)}"
               value="${attr(s.label || '')}" placeholder="显示名，如 DSH 站">
      </div>
      <div class="cfgbtns">
        <button class="btn" onclick="saveSiteSet(this)">保存本页修改</button>
        <button class="btn gray sm" onclick="resetSiteSet()">放弃修改</button>
      </div>
    </div>`;
  }
  el.innerHTML =
    `<h2 class="sec"><span class="arw">▼</span>站点配置
       <span class="cnt">${sites.length} 个站点</span></h2>
     <div class="cfgnote">
       <b>这一页是干什么的？</b>
       用量页的数字如果<b>卡住不动</b>、或者提示「有用量没统计上」，
       答案通常就在这一页 —— 让面板能认出你的 API 地址。
       <br>改错了不要紧：每次保存都会自动备份，改回来的方法就是再改一次。
     </div>
     ${rows}`;
}

async function saveSiteSet(btn){
  const card = btn.closest('.cfgcard');
  const inputs = card.querySelectorAll('input[data-k]');
  const host = inputs.length ? inputs[0].getAttribute('data-host') : '';
  const payload = {action:'edit', host: host};
  inputs.forEach(inp=>{
    const k = inp.getAttribute('data-k');
    const v = (inp.value||'').trim();
    if(k === 'proxy'){
      payload.proxy = v;                       // 空 = 清除代理
    }else if(k === 'label'){
      payload.label = v;
    }else{
      payload[k] = v ? v.split(',').map(x=>x.trim()).filter(Boolean) : [];
    }
  });
  btn.disabled = true;
  try{
    const r = await fetch('/api/sites', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    if(d.error || !d.ok){ alert(d.error || d.msg || '保存失败'); btn.disabled = false; return; }
    alert('✓ ' + (d.msg || '已保存'));
    loadSiteSet();
  }catch(e){ alert('保存失败：' + e); btn.disabled = false; }
}

function resetSiteSet(){ loadSiteSet(); }

/* 折叠/展开（点标题切换；状态存在 localStorage，刷新后保持） */
function fold(h){
  const box = h.nextElementSibling;
  if(!box) return;
  const hiding = !box.classList.contains('hidden');
  box.classList.toggle('hidden', hiding);
  h.classList.toggle('fold', hiding);
  try{
    const k = 'fold:' + (h.textContent||'').trim().replace(/\s+/g,' ').slice(0,30);
    localStorage.setItem(k, hiding ? '1' : '0');
  }catch(e){}
}

/* 恢复上次的折叠状态 */
function restoreFolds(){
  document.querySelectorAll('h2.sec').forEach(h=>{
    try{
      const k = 'fold:' + (h.textContent||'').trim().replace(/\s+/g,' ').slice(0,30);
      if(localStorage.getItem(k) === '1'){
        const box = h.nextElementSibling;
        if(box){ box.classList.add('hidden'); h.classList.add('fold'); }
      }
    }catch(e){}
  });
}

async function tick(){
  try{
    const r = await fetch('/api/state', {cache:'no-store'});
    render(await r.json());
    restoreFolds();                 // ★ 渲染后恢复上次的折叠状态
  }catch(e){ document.getElementById('sub').textContent = '读取失败: '+e; }
}

/* ═══════════ 模型定价页（2026-10-02 加）═══════════ */
let PRICE_DATA = null;

function switchTab(which){
  const isU = which === 'usage', isP = which === 'price',
        isS = which === 'site', isSS = which === 'siteset';
  document.getElementById('tab-usage').classList.toggle('on', isU);
  document.getElementById('tab-price').classList.toggle('on', isP);
  document.getElementById('tab-site').classList.toggle('on', isS);
  document.getElementById('tab-siteset').classList.toggle('on', isSS);
  document.getElementById('root').style.display = isU ? '' : 'none';
  document.getElementById('priceroot').style.display = isP ? '' : 'none';
  document.getElementById('siteroot').style.display = isS ? '' : 'none';
  document.getElementById('sitesetroot').style.display = isSS ? '' : 'none';
  if(isP) loadPrices();
  if(isS) loadSiteCreds();
  if(isSS) loadSiteSet();
}

/* ===== ★ 2026-10-06 站点账号页（常驻）===== */
let SITE_CREDS = null;
// ★ 2026-10-07：SHOW_PWD 从全局改成「按站的状态表」——
//   多张卡片各自独立，点 A 的眼睛不该影响 B 的输入框。
let SHOW_PWD_MAP = {};
function isShown(host){ return !!SHOW_PWD_MAP[host]; }

async function loadSiteCreds(){
  const el = document.getElementById('siteroot');
  el.innerHTML = '<div class="skel"></div><div class="skel"></div>';
  try{
    const r = await fetch('/api/creds', {cache:'no-store'});
    SITE_CREDS = await r.json();
    renderSiteCreds();
  }catch(e){
    el.innerHTML = '<div class="sect" style="color:#e08a8a">读取失败：'+esc(String(e))+'</div>';
  }
}

function renderSiteCreds(){
  const data = SITE_CREDS || {};
  const sites = data.sites || [];
  const cur = data.site || '';
  const el = document.getElementById('siteroot');

  if(!sites.length){
    el.innerHTML = '<h2 class="sec"><span class="arw">▼</span>站点登录凭据</h2>' +
                   '<div class="empty"><span class="ic">📡</span>' +
                   '还没有配置任何站点<br>点右上角 <b>+ 加站</b> 开始</div>';
    return;
  }

  let blocks = '';
  for(const s of sites){
    const isCur = s.host === cur;
    blocks += renderOneSite(s, isCur && data);
  }
  el.innerHTML = `
    <h2 class="sec"><span class="arw">▼</span>站点登录凭据
      <span class="tag">按站点类型自动识别</span></h2>
    <div class="credhint" style="margin:0 0 12px">
      需要登录态的站点（如 sub2api）才显示账号密码框；用 API key 采集的站点（如 NewAPI）无需填写。
    </div>` + blocks;

  // ★ 2026-10-07：重渲染后，把「已展开明文」的那些站恢复成明文
  for(const host of Object.keys(SHOW_PWD_MAP)){
    if(SHOW_PWD_MAP[host]) revealPwd(host);
  }
}

function renderOneSite(s, data){
  const need = !!s.needs_login;
  const st = s.status || '';
  let badge;
  if(st === 'ok') badge = `<span class="pill ok">${esc(s.status_text)}</span>`;
  else if(st === 'no_login_needed') badge = `<span class="pill ok">${esc(s.status_text)}</span>`;
  else if(st === 'expired') badge = `<span class="pill no">${esc(s.status_text)}</span>`;
  else if(st === 'unsupported' || st === 'not_managed') badge = `<span class="pill no">${esc(s.status_text)}</span>`;
  else badge = `<span class="pill no">${esc(s.status_text || '未知')}</span>`;

  const head = `<h3>${esc(s.host)}${s.label ? '　<span style="color:#8a8a92;font-weight:400">'+esc(s.label)+'</span>' : ''}
      <span class="tag">${esc(s.kind)}</span> ${badge}
      ${s.enabled ? '' : '<span class="pill no">已停用</span>'}
      <span class="sitectl">
        ${s.enabled
          ? '<button class="btn gray sm" data-host="' + attr(s.host) + '" onclick="toggleSite(dh(this),false)">停用</button>'
          : '<button class="btn sm" data-host="' + attr(s.host) + '" onclick="toggleSite(dh(this),true)">启用</button>'}
        <button class="btn red sm" data-host="${attr(s.host)}" onclick="askDeleteSite(dh(this))">🗑 删除站点</button>
      </span></h3>`;

  // ① 不需要登录态 → 只展示说明，不给输入框
  if(!need){
    return `<div class="credbox show" style="margin:10px 0">
      ${head}
      <div class="credhint" style="margin:0">
        该站通过 API key 直接采集逐条流水，无需账号密码。若采集异常，看顶部告警或 poll.log。
      </div>
      ${proxyRow(s)}
    </div>`;
  }

  // ② 需要登录态但当前拿不到（槽位被占 / kind 不支持）→ 说清真实原因
  if(st === 'unsupported' || st === 'not_managed'){
    const why = s.blocked_by
      ? '站点 <b>' + esc(s.blocked_by) + '</b> 已占用登录态槽位。<br>' +
        '目前登录态存储只支持<b>一个站</b>，新站要用得先做多站改造。'
      : esc(s.status_text) + '<br>如需支持，请告知开发补该 kind 的登录适配。';
    return `<div class="credbox show" style="margin:10px 0">
      ${head}
      <div class="credhint" style="margin:0;color:#e0b542">
        ${why}<br>
        <span style="color:#8a8a92">该站仍可采集（用 API key 拿聚合数），只是拿不到逐条流水。</span>
      </div>
    </div>`;
  }

  // ③ sub2api 等需登录 → 给完整表单
  const auto = s.can_autorelogin
    ? `<span class="pill ok">已存凭据 · 可自动重登</span>`
    : `<span class="pill no">未存凭据 · 无法自动重登</span>`;
  const src = s.cred_source ? `　来源：<code>${esc(s.cred_source)}</code>` : '';
  // ★ 2026-10-07 修：多站页面上每张卡片必须有**自己的 id**，
  //   否则 getElementById 只认第一个（DSH 那张），在 try 卡片点保存
  //   会把 DSH 的输入框当成本站的值 —— 实测串站事故。
  const k = sid(s.host);                       // id 后缀：host 净化版
  const shown = isShown(s.host);
  const emailVal = shown ? esc(s.email_plain || s.email || '') : esc(s.email || '');
  // 密码：显示态用明文；遮罩态若有已存凭据则用圆点占位（不泄露长度）
  const pwdVal = shown ? esc(s.password_plain || '')
                       : (s.can_autorelogin ? '••••••••••••' : '');

  return `<div class="credbox show" style="margin:10px 0">
    ${head}
    <div class="credline" style="margin-top:8px">${auto}</div>
    <div class="credline">
      <span style="width:38px">邮箱</span>
      <input id="ci_email_${k}" type="text" placeholder="账号邮箱" autocomplete="off" value="${emailVal}">
    </div>
    <div class="credline">
      <span style="width:38px">密码</span>
      <input id="ci_pwd_${k}" type="${shown?'text':'password'}" placeholder="密码"
             autocomplete="new-password" style="width:210px" value="${pwdVal}"
             data-saved="${s.can_autorelogin ? '1' : '0'}">
      <button class="btn gray sm" id="eye_${k}" data-host="${attr(s.host)}" onclick="togglePwd(dh(this))">${shown?'🙈 隐藏':'👁 显示'}</button>
      <button class="btn" data-host="${attr(s.host)}" onclick="saveSiteCred(dh(this))">保存</button>
      <button class="btn gray" data-host="${attr(s.host)}" onclick="relogin(dh(this))">重新登录</button>
      <button class="btn red" data-host="${attr(s.host)}" onclick="delSiteCred(dh(this))">清除凭据</button>
    </div>
    <div class="credhint" id="credhint_${k}">
      上次成功登录：${esc(s.last_refresh || '—')}${src}<br>
      保存后会立即用它登录验证。凭据存本机 <code>proxy_monitor/credentials.json</code>（已排除在开源同步外），不走外网。
    </div>
    ${proxyRow(s)}
  </div>`;
}

/* ★ 2026-10-08 新增：站点代理行（两种卡片共用）
   空 = 直连；填写则该站的所有请求（聚合 + 逐条 + 登录）都走这个代理。
   填法：4512 或 127.0.0.1:4512 或 http://127.0.0.1:4512 */
function proxyRow(s){
  const k = sid(s.host);
  const px = s.proxy || '';
  const active = !!px;
  const badge = active
    ? `<span class="pill ok">走代理</span>`
    : `<span class="pill" style="opacity:.6">直连</span>`;
  return `<div class="credline" style="margin-top:8px;border-top:1px dashed #3a3a44;padding-top:8px">
    <span style="width:38px">代理</span>
    <input id="ci_proxy_${k}" type="text" placeholder="留空 = 直连；如 4512"
           autocomplete="off" style="width:210px" value="${esc(px)}">
    <button class="btn gray sm" data-host="${attr(s.host)}" onclick="saveProxy(dh(this))">保存代理</button>
    ${active ? `<button class="btn gray sm" data-host="${attr(s.host)}" onclick="clearProxy(dh(this))">清除</button>` : ''}
    ${badge}
  </div>
  <div class="credhint" id="proxyhint_${k}" style="margin-top:2px">
    该站直连不通时填代理（如 <code>4512</code>）。只影响本站，其他站不受影响。
  </div>`;
}

/* ★ 2026-10-08 新增：删除站点（先预览「将删什么」，再确认）
   设计意图：删除是不可逆的，不能只弹「确定吗」。
   先向后端要一份 preview，把影响范围列清楚给用户看。 */
async function askDeleteSite(host){
  let pv = null;
  try{
    const r = await fetch('/api/creds', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'preview_delete', site: host})
    });
    const j = await r.json();
    pv = j.preview || null;
  }catch(e){ pv = null; }

  const db = (pv && pv.db) || {};
  const names = {
    usage_flows:   '用量流水（用量页 / 模型定价页的卡片数据）',
    flow_sessions: '对话归集关系（哪条流水属于哪个对话）',
    collect_state: '采集状态',
    site_accounts: '账号信息（余额 / 套餐 / 倍率）',
  };
  let rows = '';
  let tot = 0;
  for(const k of ['usage_flows','flow_sessions','collect_state','site_accounts']){
    const n = db[k] || 0;
    tot += n;
    if(n) rows += `<li>${esc(names[k] || k)}：<b>${n}</b> 行</li>`;
  }
  if(!tot) rows = '<li>（该站在库内暂无数据）</li>';
  const files = (pv && pv.files) || [];
  const flist = files.length
    ? '<li>' + files.map(f=>esc(f)).join('</li><li>') + '</li>'
    : '<li>（无关联配置文件）</li>';

  const html = `
    <div style="font-size:13px;line-height:1.7">
      <div style="color:#e08a8a;font-weight:600;margin-bottom:8px">
        删除站点是<b>不可逆</b>操作 —— 会连数据一起清掉。
      </div>
      <div style="margin-bottom:6px">将删除 <b style="color:#e8e8ea">${esc(host)}</b>：</div>
      <div style="color:#8a8a92;margin:6px 0 2px">数据库（共 <b style="color:#e0b542">${tot}</b> 行）</div>
      <ul style="margin:2px 0 8px 18px;padding:0;color:#b8b8c0">${rows}</ul>
      <div style="color:#8a8a92;margin:6px 0 2px">配置文件</div>
      <ul style="margin:2px 0 8px 18px;padding:0;color:#b8b8c0">${flist}</ul>
      <div style="color:#8a8a92;font-size:12px">
        删前会自动整体备份到 <code>backups/&lt;日期&gt;-删站-&lt;域名&gt;/</code>，出问题可回滚。<br>
        用量页与模型定价页里该站的卡片会随之消失。
      </div>
      <div style="margin-top:10px;color:#e0b542;font-size:12px">
        ⚠ 只想「不采集但保留数据」请用「停用」，不要用删除。
      </div>
    </div>`;

  showConfirmDialog('删除站点 ' + host, html, async () => {
    try{
      const r = await fetch('/api/creds', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'delete_site', site: host})
      });
      const j = await r.json();
      if(j.ok){
        toast(j.msg || ('已删除 ' + host));
        loadSiteCreds();
        // 用量页的卡片也要跟着消失
        try{ tick(); }catch(e){}
      }else{
        toast('删除失败：' + (j.msg || '未知错误'));
      }
    }catch(e){ toast('删除失败：' + e); }
  });
}

/* 通用确认弹窗（居中模态，带遮罩；确认按钮红色） */
function showConfirmDialog(title, html, onOk){
  const old = document.getElementById('cfm_ov');
  if(old) old.remove();
  const ov = document.createElement('div');
  ov.id = 'cfm_ov';
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;'
    + 'display:flex;align-items:center;justify-content:center';
  ov.innerHTML = `
    <div style="background:#232329;border:1px solid #4a3a3a;border-radius:10px;
                padding:18px 20px;max-width:520px;max-height:80vh;overflow:auto;
                box-shadow:0 12px 40px rgba(0,0,0,.6)">
      <div style="font-size:15px;font-weight:600;margin-bottom:10px;color:#e8e8ea">${esc(title)}</div>
      <div>${html}</div>
      <div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end">
        <button class="btn gray" id="cfm_no">取消</button>
        <button class="btn red" id="cfm_yes">确认删除</button>
      </div>
    </div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  document.getElementById('cfm_no').onclick = close;
  ov.addEventListener('click', e => { if(e.target === ov) close(); });
  document.getElementById('cfm_yes').onclick = async () => {
    close();
    await onOk();
  };
}

/* 轻提示 */
function toast(msg){
  const old = document.getElementById('toast_box');
  if(old) old.remove();
  const d = document.createElement('div');
  d.id = 'toast_box';
  d.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);'
    + 'background:#2e4a3a;color:#a0f0b8;border:1px solid #3a5c48;border-radius:8px;'
    + 'padding:10px 18px;font-size:13px;z-index:10000;box-shadow:0 6px 20px rgba(0,0,0,.5)';
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 4000);
}

/* 保存代理（先探测再用） */
async function saveProxy(host){
  const f = document.getElementById('ci_proxy_' + sid(host));
  const hint = document.getElementById('proxyhint_' + sid(host));
  const px = f ? f.value.trim() : '';
  if(hint) hint.innerHTML = '<span style="color:#e0b542">正在探测代理…</span>';
  try{
    const r = await fetch('/api/creds', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'set_proxy', site: host, proxy: px})
    });
    const j = await r.json();
    if(!j.ok){
      if(hint) hint.innerHTML = '<span style="color:#e08a8a">✗ '+esc(j.msg||'保存失败')+'</span>';
      return;
    }
    const cm = j.proxy_check_msg || '';
    const cok = j.proxy_check_ok;
    if(hint){
      hint.innerHTML = (cok ? '<span style="color:#7ee09a">✓ ' : '<span style="color:#e0b542">⚠ ')
        + esc(j.msg) + (cm ? '　'+esc(cm) : '') + '</span>';
    }
    loadSiteCreds();     // 重渲染，让徽标/清除按钮跟上
  }catch(e){
    if(hint) hint.innerHTML = '<span style="color:#e08a8a">✗ '+esc(String(e))+'</span>';
  }
}

/* 清除代理（改回直连） */
async function clearProxy(host){
  const f = document.getElementById('ci_proxy_' + sid(host));
  if(f) f.value = '';
  await saveProxy(host);
}

/* host → 可用作 DOM id 的后缀（把 . : / 等换成 _） */
function sid(host){
  return (host||'').replace(/[^a-zA-Z0-9_-]/g, '_');
}
/* 取某站的表单元素（按 host 唯一定位，不会串到别的卡片） */
function credEl(host, kind){
  const k = sid(host);
  return document.getElementById(kind + '_' + k);
}

async function revealPwd(host){
  try{
    // ★ 2026-10-07：reveal 要指明是哪个站，否则拿到的是 active 站的明文
    const q = host ? ('&site=' + encodeURIComponent(host)) : '';
    const r = await fetch('/api/creds?reveal=1' + q, {cache:'no-store'});
    const c = await r.json();
    if(host && c.site !== host) return;          // 回的站不对，别往上写
    const f = credEl(host, 'ci_pwd');
    if(f && c.password_plain !== undefined) f.value = c.password_plain;
    const e = credEl(host, 'ci_email');
    if(e && c.email_plain) e.value = c.email_plain;
    // 把明文也存进列表数据，重渲染时不会丢
    const lst = (SITE_CREDS && SITE_CREDS.sites) || [];
    const row = lst.find(x => x.host === host);
    if(row){
      row.email_plain = c.email_plain || '';
      row.password_plain = c.password_plain || '';
    }
  }catch(err){}
}

async function togglePwd(host){
  const f = credEl(host, 'ci_pwd');
  const eye = credEl(host, 'eye');
  if(!f) return;
  const nowShown = !isShown(host);
  SHOW_PWD_MAP[host] = nowShown;               // 只影响这一张卡片
  if(nowShown){
    await revealPwd(host);                     // 先拿到明文再切类型
    f.type = 'text';
    if(eye) eye.textContent = '🙈 隐藏';
  }else{
    f.type = 'password';
    if(f.dataset.saved) f.value = '••••••••••••';
    if(eye) eye.textContent = '👁 显示';
  }
}

async function saveSiteCred(host){
  // ★ 2026-10-07 修串站：site 用传进来的 host（而不是全局 SITE_CREDS.site），
  //   元素按 host 唯一定位。以前两者都指向 active 站，导致在 try 卡片
  //   点保存却写给了 DSH。
  const site = host || (SITE_CREDS && SITE_CREDS.site) || '';
  const emailEl = credEl(site, 'ci_email');
  const pwdEl   = credEl(site, 'ci_pwd');
  const hint    = credEl(site, 'credhint');
  if(!emailEl || !pwdEl){
    alert('找不到 ' + site + ' 的输入框，请刷新页面重试');
    return;
  }
  const email = (emailEl.value||'').trim();
  let pwd = pwdEl.value || '';
  // 用户没改密码（还是遮罩占位）→ 不覆盖已存密码
  if(pwdEl.dataset.saved && /^[•*]+$/.test(pwd)){
    hint.innerHTML = '<span style="color:#e0b542">密码未改动 —— 若要只更新邮箱，请先点「显示」再确认密码</span>';
    return;
  }
  if(!email || !pwd){
    hint.innerHTML = '<span style="color:#e08a8a">邮箱和密码都不能为空</span>'; return;
  }
  hint.textContent = '正在登录验证…';
  try{
    const r = await fetch('/api/creds', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({site: site, email, password: pwd})
    });
    const j = await r.json();
    if(j.error){ hint.innerHTML = '<span style="color:#e08a8a">'+esc(j.error)+'</span>'; return; }
    hint.innerHTML = j.ok ? '<span style="color:#4ec26a">✓ '+esc(j.msg||'成功')+'</span>'
                          : '<span style="color:#e0b542">'+esc(j.msg||'失败')+'</span>';
    if(j.ok){ setTimeout(()=>loadSiteCreds(), 900); }
  }catch(e){
    hint.innerHTML = '<span style="color:#e08a8a">请求失败：'+esc(String(e))+'</span>';
  }
}

async function relogin(host){
  const site = host || (SITE_CREDS && SITE_CREDS.site) || '';
  const hint = credEl(site, 'credhint');
  if(!hint) return;
  hint.textContent = '正在重新登录（只换 token，不做全量采集）…';
  try{
    // ★ 2026-10-07：改走专用重登入口（1-2 秒），不再用 /api/refresh（全量采集 30 秒）
    const r = await fetch('/api/creds?relogin=1&site=' + encodeURIComponent(site), {cache:'no-store'});
    const j = await r.json();
    if(j.error){ hint.innerHTML = '<span style="color:#e08a8a">'+esc(j.error)+'</span>'; return; }
    hint.innerHTML = j.ok
      ? '<span style="color:#4ec26a">✓ '+esc(j.msg||'重登成功')+'</span>'
      : '<span style="color:#e0b542">'+esc(j.msg||'重登失败')+'</span>';
    setTimeout(()=>loadSiteCreds(), 700);
  }catch(e){ hint.innerHTML = '<span style="color:#e08a8a">请求失败：'+esc(String(e))+'</span>'; }
}

async function delSiteCred(host){
  const site = host || (SITE_CREDS && SITE_CREDS.site) || '';
  if(!confirm('确定清除 ' + site + ' 的账号密码？清除后将无法自动重登。')) return;
  const hint = credEl(site, 'credhint');
  try{
    const r = await fetch('/api/creds', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({site: site, action:'delete'})
    });
    const j = await r.json();
    if(hint){
      hint.innerHTML = j.ok ? '<span style="color:#4ec26a">✓ 已清除</span>'
                            : '<span style="color:#e08a8a">清除失败</span>';
    }
    setTimeout(()=>loadSiteCreds(), 700);
  }catch(e){ if(hint) hint.innerHTML = '<span style="color:#e08a8a">'+esc(String(e))+'</span>'; }
}

/* ═══════════ ★ 2026-10-07 加站界面 ═══════════ */
let NS_PROBE = null;          // 最近一次探测结果

function openAddSite(){
  const box = document.getElementById('addsite');
  box.classList.add('show');
  document.getElementById('ns_url').focus();
}
function closeAddSite(){
  document.getElementById('addsite').classList.remove('show');
}
function resetAddSite(){
  NS_PROBE = null;
  document.getElementById('ns_result').style.display = 'none';
  document.getElementById('ns_step2').style.display = 'none';
  document.getElementById('ns_msg').textContent = '';
  document.querySelectorAll('#addsite .stepnum').forEach(e=>e.classList.remove('on'));
  document.querySelector('#addsite .stepnum').classList.add('on');
}

async function doProbe(){
  const url = (document.getElementById('ns_url').value||'').trim();
  const m = document.getElementById('ns_msg');
  const btn = document.getElementById('ns_probe_btn');
  const res = document.getElementById('ns_result');

  if(!url){ m.className='msg err'; m.textContent='请先填站点地址'; return; }

  btn.disabled = true; btn.classList.add('spin');
  m.className = 'msg'; m.textContent = '探测中…（最多几秒）';
  res.style.display = 'none';
  document.getElementById('ns_step2').style.display = 'none';

  try{
    const r = await fetch('/api/sites?probe=' + encodeURIComponent(url), {cache:'no-store'});
    const d = await r.json();
    if(d.error){ m.className='msg err'; m.textContent = d.error; return; }
    if(!d.ok){ m.className='msg err'; m.textContent = d.msg || '探测失败'; return; }

    NS_PROBE = d;
    m.className = 'msg'; m.textContent = '';
    renderProbe(d);
  }catch(e){
    m.className='msg err'; m.textContent = '请求失败：' + e;
  }finally{
    btn.disabled = false; btn.classList.remove('spin');
  }
}

function renderProbe(d){
  const res = document.getElementById('ns_result');
  const kind = d.kind || 'unknown';
  let cls = 'warn', icon = '⚠', title = '无法识别站点类型';

  if(kind === 'newapi'){ cls='ok'; icon='✅'; title='识别为 NewAPI 架构'; }
  else if(kind === 'sub2api'){ cls='ok'; icon='✅'; title='识别为 sub2api 架构'; }
  else if(kind === 'official'){ cls='err'; icon='🚫'; title='这是官方站，不是中转站'; }

  let extra = '';
  if(kind === 'sub2api'){
    extra = '<div class="why" style="margin-top:6px;color:#e0b540">' +
            '这个站需要<b>账号密码</b>才能拿到逐条流水 —— 加完后到站点账号页填。</div>';
  }else if(kind === 'newapi'){
    extra = '<div class="why" style="margin-top:6px">用 API key 采集即可，<b>不需要账号密码</b>。</div>';
  }else if(kind === 'official'){
    extra = '<div class="why" style="margin-top:6px;color:#f0a8a8">' +
            '官方站（DeepSeek / OpenAI / OpenRouter 等）用量在各自后台看，本工具只监控中转站。</div>';
  }else{
    // unknown：给两条出路
    extra = '<div class="why" style="margin-top:8px">' +
            '可能是：① 非标准中转站；② 地址写错了（检查域名拼写）；③ 站点要登录才暴露特征。</div>' +
            '<div class="credline" style="margin-top:8px">' +
            '  <span style="width:56px">手动指定</span>' +
            '  <select id="ns_kind" style="background:#1b1b1f;border:1px solid #33333b;color:#e8e8ea;' +
            '    border-radius:5px;padding:5px 8px;font-size:12px;font-family:inherit">' +
            '    <option value="">先不确定（留空，可后补）</option>' +
            '    <option value="newapi">NewAPI</option>' +
            '    <option value="sub2api">sub2api</option>' +
            '  </select>' +
            '</div>';
  }

  const dup = d.duplicate
    ? '<div class="dupwarn">⚠ 这个站已经在监控里了：<b>' + esc(d.duplicate) +
      '</b>。重复添加会被拒绝。</div>'
    : '';

  const norm = d.norm_note
    ? '<div class="why" style="margin-top:6px">地址已规范化 → <code>' + esc(d.base_url) + '</code>' +
      '<br><span style="color:#6a6a72">（' + esc(d.norm_note) + '）</span></div>'
    : '<div class="why" style="margin-top:6px">地址 → <code>' + esc(d.base_url) + '</code></div>';

  res.className = 'proberes ' + cls;
  res.innerHTML = '<div class="kindline">' + icon + ' ' + title + '</div>' +
                  '<div class="why">判定依据：' + esc(d.reason || '—') + '</div>' +
                  norm + extra + dup;
  res.style.display = '';

  // 官方站 + 重复站：不给下一步
  const blocked = (kind === 'official') || !!d.duplicate;
  if(!blocked){
    document.querySelectorAll('#addsite .stepnum').forEach(e=>e.classList.add('on'));
    document.getElementById('ns_step2').style.display = '';
    renderProvBox(d);
  }
}

/* ★ 2026-10-07 provider 多选框：从 Hermes config.yaml 读出来的 */
function renderProvBox(d){
  const box = document.getElementById('ns_prov_box');
  const matched = d.providers_matched || [];
  const others = d.providers_others || [];
  let html = '';

  if(!matched.length && !others.length){
    box.innerHTML = '<span class="none">' +
      '读不到 Hermes 配置（config.yaml 找不到或没有 custom provider）—— 请手工填写。' +
      '</span>';
    checkProvWarn();
    return;
  }

  // 域名匹配上的排前面，默认勾选
  for(const p of matched){
    const short = p.base_url ? p.base_url.replace(/^https?:\/\//,'').split('/')[0] : '';
    html += `<label class="pv on" title="${esc(p.base_url||'')}">` +
            `<input type="checkbox" value="${esc(p.name)}" checked onchange="syncProvInput()">` +
            `<span>${esc(p.name)}</span>` +
            (short ? `<span class="bl">${esc(short)}</span>` : '') +
            `</label>`;
  }
  // 其余的全部列出（低亮，供手动勾）
  for(const name of others){
    html += `<label class="pv miss">` +
            `<input type="checkbox" value="${esc(name)}" onchange="syncProvInput()">` +
            `<span>${esc(name)}</span></label>`;
  }
  box.innerHTML = html;
  syncProvInput();
}

/* 勾选框 ↔ 手输框 双向同步 */
function syncProvInput(){
  const box = document.getElementById('ns_prov_box');
  const inp = document.getElementById('ns_prov');
  const chosen = [...box.querySelectorAll('input:checked')].map(c=>c.value);
  inp.value = chosen.join(', ');
  box.querySelectorAll('.pv').forEach(l=>{
    const c = l.querySelector('input');
    l.classList.toggle('on', !!(c && c.checked));
  });
  checkProvWarn();
}

/* 手工输入后，反向同步勾选框状态 */
function syncProvFromInput(){
  const box = document.getElementById('ns_prov_box');
  const inp = document.getElementById('ns_prov');
  const typed = (inp.value||'').split(/[,，\s]+/).filter(Boolean);
  box.querySelectorAll('.pv').forEach(l=>{
    const c = l.querySelector('input');
    if(!c) return;
    const on = typed.indexOf(c.value) >= 0;
    c.checked = on;
    l.classList.toggle('on', on);
  });
  checkProvWarn();
}

/* 没选 provider 时给警告（不阻止，只是提醒） */
function checkProvWarn(){
  const w = document.getElementById('ns_provwarn');
  if(!w) return;
  const chosen = provsFromUI();
  const isSub2 = NS_PROBE && NS_PROBE.kind === 'sub2api';
  // sub2api 可以用登录态采逐条流水，空 provider 后果轻一些
  if(chosen.length){ w.style.display = 'none'; return; }
  w.style.display = '';
  w.textContent = isSub2
    ? '⚠ 没选 provider —— 该站是 sub2api，还能靠登录态采集；但仍建议选上以拿到余额/倍率。'
    : '⚠ 没有选任何 provider —— 这个站将无法采集数据（拿不到 API key）。确认要这样加吗？';
}

/* 从界面读出最终 provider 列表 */
function provsFromUI(){
  const box = document.getElementById('ns_prov_box');
  const inp = document.getElementById('ns_prov');
  const typed = (inp.value||'').split(/[,，\s]+/).filter(Boolean);
  if(typed.length) return typed;
  if(!box) return [];
  return [...box.querySelectorAll('input:checked')].map(c=>c.value);
}

async function doAddSite(){
  const m = document.getElementById('ns_msg');
  const btn = document.getElementById('ns_save_btn');
  if(!NS_PROBE){ m.className='msg err'; m.textContent='请先探测'; return; }

  // unknown 时用下拉里选手动 kind（没选就留空，交给后端重探）
  const sel = document.getElementById('ns_kind');
  let kind = NS_PROBE.kind || '';
  if(kind === 'unknown' && sel && sel.value) kind = sel.value;

  const provs = provsFromUI();
  const basesRaw = (document.getElementById('ns_bases').value||'').trim();
  // ★ 2026-10-09：备选地址真正解析成数组传给后端（原来只塞进备注文字，填了不生效）
  const basesList = basesRaw ? basesRaw.split(',').map(s=>s.trim())
                                         .filter(Boolean).map(s=>{
    return (s.indexOf('://') >= 0) ? s : ('https://' + s);
  }) : [];
  const proxyRaw = (document.getElementById('ns_proxy').value||'').trim();

  const body = {
    action: 'add',
    base_url: NS_PROBE.base_url || document.getElementById('ns_url').value,
    label: (document.getElementById('ns_label').value||'').trim(),
    hermes_providers: provs,
    probe_kind: kind === 'unknown' ? '' : kind,
    probe_reason: NS_PROBE.reason || '',
    proxy: proxyRaw,                       // ★ 2026-10-08：加站即可选填代理
    bases: basesList,                      // ★ 2026-10-09：备选地址真正写进 bases
    note: (document.getElementById('ns_note')||{}).value || '',
  };
  // ★ 2026-10-09 修正：原来这里只把备选地址写进 note 文字（"请在 sites.json 的
  //   bases 字段补充"），等于填了不生效 —— 用户还得手改文件。现在直接传 bases。

  btn.disabled = true;
  m.className = 'msg'; m.textContent = '正在写入配置…';
  try{
    const r = await fetch('/api/sites', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    if(d.error){ m.className='msg err'; m.textContent = d.error; return; }
    if(!d.ok){ m.className='msg err'; m.textContent = d.msg || '加站失败'; return; }

    m.className='msg'; m.textContent = '✓ ' + (d.msg || '已加入') +
      (d.norm_note ? '（' + d.norm_note + '）' : '');

    // 加完立刻采一次（可选）
    const wantRefresh = document.getElementById('ns_refresh').checked;
    if(wantRefresh){
      const host = (d.site && d.site.host) || '';
      m.textContent = '✓ 已加入，正在采集…（几秒到十几秒）';
      try{
        await fetch('/api/refresh?site=' + encodeURIComponent(host), {cache:'no-store'});
        m.textContent = '✓ 已加入并采集完成：' + host;
      }catch(e){ m.textContent = '✓ 已加入（采集没跑成，稍后自动采）'; }
    }

    setTimeout(()=>{
      resetAddSite();
      closeAddSite();
      loadSiteCreds();          // 刷新站点账号页（新站会出现）
    }, 1200);
  }catch(e){
    m.className='msg err'; m.textContent = '请求失败：' + e;
  }finally{
    btn.disabled = false;
  }
}

/* 启用/停用站点 */
async function toggleSite(host, enable){
  const verb = enable ? '启用' : '停用';
  if(!confirm('确定' + verb + '站点 ' + host + ' ？\n（停用后不再采集，数据保留，可随时恢复）')) return;
  const m = document.getElementById('msg');
  try{
    const r = await fetch('/api/sites', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action: enable?'enable':'disable', host: host})});
    const d = await r.json();
    m.className = d.ok ? 'msg' : 'msg err';
    m.textContent = (d.ok ? '✓ ' : '✗ ') + (d.msg || '');
    loadSiteCreds();
    setTimeout(()=>{ m.textContent=''; }, 6000);
  }catch(e){ m.className='msg err'; m.textContent='请求失败：'+e; }
}

async function loadPrices(){
  const el = document.getElementById('priceroot');
  el.innerHTML = '<div class="sub">加载中…</div>';
  try{
    const r = await fetch('/api/prices', {cache:'no-store'});
    PRICE_DATA = await r.json();
    renderPrices();
  }catch(e){ el.innerHTML = '<div class="sub">读取失败: '+e+'</div>'; }
}

/* 输入框取值（不存在则返回 0） */
// ⚠ 找不到元素必须返回 ''，不能返回 '0'：
//   历史上返回 '0' 导致 covered 表（无备注框）点保存时把备注写成字符串 "0"。
function pv(id){ const e = document.getElementById(id); return e ? e.value : ''; }
function num(v){ const n = parseFloat(v); return isNaN(n) ? 0 : n; }

// ★ 2026-10-08：covered 表（第一张）专用的保存。
//   它没有备注框，且带 cov 前缀的 id —— 绝不能走 savePrice（那会用
//   pi_/po_/pc_ 前缀，跟下方「已配置」表的重复 id 撞车，写错数据）。
async function saveCov(btn){
  const host = ds(btn), model = dm(btn);
  const key = (host+'|'+model).replace(/[^a-zA-Z0-9|._-]/g,'_');
  const body = {
    action: 'save', host: host, model: model,
    in:    num(pv('covi_'+key)), out: num(pv('covo_'+key)), cache: num(pv('covc_'+key)),
    per_call: num(pv('covpc_'+key)),
    tag:   pv('covt_'+key).trim(), note: ''
  };
  const m = document.getElementById('msg');
  m.className = 'msg'; m.textContent = '保存中… '+host+' / '+model;
  try{
    const r = await fetch('/api/prices', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    const d = await r.json();
    if(d.error) throw new Error(d.error);
    m.textContent = '✓ 已保存 '+host+' / '+model;
    await loadPrices();
  }catch(e){ m.className='msg err'; m.textContent = '保存失败: '+e; }
}

async function savePrice(host, model){
  const key = (host+'|'+model).replace(/[^a-zA-Z0-9|._-]/g,'_');
  const body = {
    action: 'save', host: host, model: model,
    in:    num(pv('pi_'+key)), out: num(pv('po_'+key)), cache: num(pv('pc_'+key)),
    per_call: num(pv('ppc_'+key)),            // ★ 2026-10-08 按次收费
    tag:   pv('pt_'+key).trim(), note: pv('pn_'+key).trim()
  };
  const m = document.getElementById('msg');
  m.className = 'msg'; m.textContent = '保存中… '+host+' / '+model;
  try{
    const r = await fetch('/api/prices', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    const d = await r.json();
    if(d.error) throw new Error(d.error);
    m.textContent = '✓ 已保存 '+host+' / '+model;
    await loadPrices();
  }catch(e){ m.className='msg err'; m.textContent = '保存失败: '+e; }
}

async function delPrice(host, model){
  if(!confirm('删除价格：'+host+' / '+model+' ?')) return;
  const m = document.getElementById('msg');
  try{
    const r = await fetch('/api/prices', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'delete', host:host, model:model})});
    const d = await r.json();
    m.className='msg'; m.textContent = d.ok ? ('✓ 已删除 '+host+' / '+model) : '（没找到该条目）';
    await loadPrices();
  }catch(e){ m.className='msg err'; m.textContent='删除失败: '+e; }
}

/* 用「站方实际花费」反推单价，填进输入框（仅参考，需用户核对） */
function autofill(row){
  const key = (row.site+'|'+row.model).replace(/[^a-zA-Z0-9|._-]/g,'_');
  const tot = (row.in||0) + (row.cache||0) + (row.out||0);
  if(!tot || !row.site_cost){ return; }
  const set = (id, v) => { const e = document.getElementById(id); if(e) e.value = v.toFixed(4); };
  // 按「该档 token 占比 × 站方实付 ÷ 该档 token 数」反推单价（元/百万 token）
  if(row.in)    set('pi_'+key, (row.site_cost * ((row.in||0)/tot))    * 1e6 / row.in);
  if(row.out)   set('po_'+key, (row.site_cost * ((row.out||0)/tot))   * 1e6 / row.out);
  if(row.cache) set('pc_'+key, (row.site_cost * ((row.cache||0)/tot)) * 1e6 / row.cache);
  const m = document.getElementById('msg');
  m.className='msg'; m.textContent = '已按站方实付估算填入，请核对后点「保存」';
}

/**
 * ★ 2026-10-07 新增：原 autofill(row) 是把整个 row 对象 JSON 拼进 onclick，
 *   既容易坏也开注入面。改成按 site/model 从 PRICE_DATA 里取回那一行。
 */
function autofill2(el){
  var site = ds(el), model = dm(el);
  var rows = (PRICE_DATA && PRICE_DATA.covered) || [];
  var row = rows.find(function(x){ return x.site === site && x.model === model; });
  if(!row){
    var m0 = document.getElementById('msg');
    if(m0){ m0.className = 'msg err'; m0.textContent = '找不到 ' + site + ' / ' + model + ' 的用量数据'; }
    return;
  }
  autofill(row);
}

function renderPrices(){
  const el = document.getElementById('priceroot');
  const cov = (PRICE_DATA && PRICE_DATA.covered) || [];
  const ent = (PRICE_DATA && PRICE_DATA.entries) || [];

  // ── 库中出现过的模型：一行一个，可直接填价 ──
  let rows = cov.map(r=>{
    const key = (r.site+'|'+r.model).replace(/[^a-zA-Z0-9|._-]/g,'_');
    const p = r.price || {};
    const isPC = !!(r.per_call && r.per_call > 0);   // ★ 按次收费
    const badge = r.priced
      ? (isPC
         ? `<span class="pill ok">按次 ¥${r.per_call}/次</span>`
         : `<span class="pill ok">已定价${r.matched && r.matched.indexOf('*')>=0 ? '（通配 '+esc(r.matched)+'）' : ''}</span>`)
      : `<span class="pill no">★ 未定价</span>`;
    return `<tr class="${r.priced?'':'miss'}">
      <td>${esc(r.site)}</td>
      <td>${esc(r.model)}${badge}</td>
      <td>${fmt(r.n)}</td>
      <td>${fmt(r.in)}</td>
      <td>${fmt(r.cache)}</td>
      <td>${fmt(r.out)}</td>
      <td>¥${(r.site_cost||0).toFixed(4)}</td>
      <td><input class="pf" id="covi_${key}" value="${p.in!=null?p.in:''}" placeholder="输入价"${isPC?' disabled style="opacity:.4"':''}></td>
      <td><input class="pf" id="covo_${key}" value="${p.out!=null?p.out:''}" placeholder="输出价"${isPC?' disabled style="opacity:.4"':''}></td>
      <td><input class="pf" id="covc_${key}" value="${p.cache!=null?p.cache:''}" placeholder="缓存价"${isPC?' disabled style="opacity:.4"':''}></td>
      <td><input class="pf" id="covpc_${key}" value="${r.per_call!=null?r.per_call:''}" placeholder="—" title="每次收费（元）；填了走按次计价"></td>
      <td><input class="pf txt" id="covt_${key}" value="${esc(p.tag||'')}" placeholder="标签(可空)"></td>
      <td style="white-space:nowrap">
        <button class="btn sm" data-site="${attr(r.site)}" data-model="${attr(r.model)}" onclick="saveCov(this)">保存</button>
        <button class="btn sm gray" data-site="${attr(r.site)}" data-model="${attr(r.model)}" onclick="autofill2(this)">估算</button>
      </td></tr>`;
  }).join('');

  // ── 全部价格条目（含通配/已不用但仍保留的） ──
  let all = ent.map(e=>{
    const key = (e.host+'|'+e.model).replace(/[^a-zA-Z0-9|._-]/g,'_');
    const isPC = !!(e.per_call && e.per_call > 0);
    // ★ 2026-10-08：按次收费的条目，token 单价灰掉（不参与计价），
    //   单独一列显示「¥X/次」；界面上要一眼能看出这站是按次算的。
    return `<tr${isPC?' style="background:#2a2a22"':''}>
      <td>${esc(e.host)}</td><td>${esc(e.model)}</td>
      <td><input class="pf" id="pi_${key}" value="${e.in!=null?e.in:''}"${isPC?' disabled style="opacity:.4"':''}></td>
      <td><input class="pf" id="po_${key}" value="${e.out!=null?e.out:''}"${isPC?' disabled style="opacity:.4"':''}></td>
      <td><input class="pf" id="pc_${key}" value="${e.cache!=null?e.cache:''}"${isPC?' disabled style="opacity:.4"':''}></td>
      <td><input class="pf" id="ppc_${key}" value="${e.per_call!=null?e.per_call:''}"
                 placeholder="—" title="每次收费（元）；填了就走按次计价，token 单价忽略"></td>
      <td><input class="pf txt" id="pt_${key}" value="${esc(e.tag||'')}"></td>
      <td><input class="pf txt" id="pn_${key}" value="${esc(e.note||'')}" placeholder="备注"></td>
      <td>${esc(e.updated||'')}</td>
      <td style="white-space:nowrap">
        <button class="btn sm" data-site="${attr(e.host)}" data-model="${attr(e.model)}" onclick="savePrice(ds(this),dm(this))">保存</button>
        <button class="btn sm red" data-site="${attr(e.host)}" data-model="${attr(e.model)}" onclick="delPrice(ds(this),dm(this))">删</button>
      </td></tr>`;
  }).join('');

  el.innerHTML = `
    <div class="sect">单价单位：元 / 百万 token。<b>「每次收费」填了就改走按次计价</b>（不看 token，
      成本 = 调用次数 × 每次费用；缓存/未命中仍会显示但不参与计算）。改完点「保存」立即生效（无需重启）。
      「估算」会按站方实付反推一个参考价填进输入框，请核对后再存。</div>
    <h2>库中出现过的模型（按用量排序）</h2>
    <table>
      <tr><th>站点</th><th>模型</th><th>请求</th><th>未命中</th><th>缓存</th><th>输出</th>
          <th>站方实付¥</th><th>输入价</th><th>输出价</th><th>缓存价</th><th>每次收费</th><th>标签</th><th>操作</th></tr>
      ${rows || '<tr><td colspan="13" style="text-align:center;color:#8a8a92">暂无数据</td></tr>'}
    </table>
    <h2>已配置的全部价格</h2>
    <table>
      <tr><th>站点</th><th>模型</th><th>输入价</th><th>输出价</th><th>缓存价</th>
          <th>每次收费</th><th>标签</th><th>备注</th><th>更新</th><th>操作</th></tr>
      ${all || '<tr><td colspan="10" style="text-align:center;color:#8a8a92">还没有配过价格</td></tr>'}
    </table>
    <h2>新增一条价格</h2>
    <table>
      <tr><th>站点</th><th>模型</th><th>输入价</th><th>输出价</th><th>缓存价</th>
          <th>每次收费</th><th>标签</th><th>备注</th><th></th></tr>
      <tr>
        <td><input class="pf txt" id="np_host" placeholder="如 api.dshapi.icu 或 *"></td>
        <td><input class="pf txt" id="np_model" placeholder="模型名，* = 全部"></td>
        <td><input class="pf" id="np_in" placeholder="0.08"></td>
        <td><input class="pf" id="np_out" placeholder="0.32"></td>
        <td><input class="pf" id="np_cache" placeholder="0.0016"></td>
        <td><input class="pf" id="np_percall" placeholder="如 0.08" title="填了就走按次计价；此时上面三个单价留空"></td>
        <td><input class="pf txt" id="np_tag" placeholder="可空 / 免费"></td>
        <td><input class="pf txt" id="np_note" placeholder="备注"></td>
        <td><button class="btn sm" onclick="addPrice()">加入</button></td>
      </tr>
    </table>`;
}

async function addPrice(){
  const host = pv('np_host').trim(), model = pv('np_model').trim() || '*';
  if(!host){ const m=document.getElementById('msg'); m.className='msg err'; m.textContent='站点不能为空'; return; }
  const body = { action:'save', host:host, model:model,
    in: num(pv('np_in')), out: num(pv('np_out')), cache: num(pv('np_cache')),
    per_call: num(pv('np_percall')),          // ★ 2026-10-08 按次收费
    tag: pv('np_tag').trim(), note: pv('np_note').trim() };
  const m = document.getElementById('msg');
  try{
    const r = await fetch('/api/prices', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    const d = await r.json();
    if(d.error) throw new Error(d.error);
    m.className='msg'; m.textContent = '✓ 已加入 '+host+' / '+model;
    ['np_host','np_model','np_in','np_out','np_cache','np_percall','np_tag','np_note']
      .forEach(id=>{ const e=document.getElementById(id); if(e) e.value=''; });
    await loadPrices();
  }catch(e){ m.className='msg err'; m.textContent='加入失败: '+e; }
}

/* ===== ★ 2026-10-06 站点凭据：告警 + 录入 ===== */
let CREDSITE = '';

function renderCredAlert(st){
  const c = st.creds || {};
  const all = st.creds_all || (c.host ? [c] : []);
  const box = document.getElementById('alert');
  const txt = document.getElementById('alerttxt');
  const btn = document.getElementById('alertbtn');
  CREDSITE = c.site || '';
  if(c.error){ box.className='alert warn show'; txt.textContent='凭据状态读取失败：'+c.error;
               btn.style.display='none'; return; }

  // ★ 2026-10-08 多站：失效的站优先报（红），其次报快过期的（黄），都正常则隐藏。
  const dead = all.filter(x => x && x.token_ok === false && x.has_refresh !== false);
  const soon = all.filter(x => x && x.token_ok && (x.expires_in_h || 99) < 6);
  if(dead.length){
    box.className='alert err show';
    const names = dead.map(x => `<b>${esc(x.host || x.site || '?')}</b>`).join('、');
    txt.innerHTML = `⚠ 站点 ${names} 登录态已失效，<b>逐条流水采集中断</b> → 会话列表不会更新。` +
                    `已存凭据的可点刷新自动重登；仍失败请去「站点账号」页更新。`;
    btn.style.display='';
    btn.textContent = '去站点账号页';
    btn.onclick = ()=>{ switchTab('site'); };
    return;
  }
  if(soon.length){
    box.className='alert warn show';
    const s0 = soon[0];
    txt.innerHTML = `站点 <b>${esc(s0.host || s0.site)}</b> 登录态将在 ${(s0.expires_in_h||0).toFixed(1)} 小时后过期` +
                    (s0.can_autorelogin ? '，届时会自动重登，无需操作。' : '，且未存账号密码，请提前录入。');
    btn.style.display = s0.can_autorelogin ? 'none' : '';
    if(!s0.can_autorelogin){ btn.textContent='去站点账号页'; btn.onclick=()=>{switchTab('site');}; }
    return;
  }
  box.className='alert';                    // 全部正常 → 整条隐藏
}

/* ★ 2026-10-07 重构：改成异步 —— 点「立即刷新」立刻受理，然后轮询进度。
   原来这里是 await fetch('/api/refresh') 同步干等，只要有一个站网络卡住
   （example.com SSL 超时实测卡 9 分钟），请求就不返回，界面永远停在
   「正在采集」且无法取消。用户实测踩到。 */
let REFRESH_POLL = null;

async function doRefresh(){
  const b = document.getElementById('rf'), m = document.getElementById('msg');
  if(REFRESH_POLL) return;                    // 已在轮询中，别重复点
  b.disabled = true; b.classList.add('spin');
  m.className='msg'; m.textContent='正在采集…';
  try{
    const r = await fetch('/api/refresh');
    const d = await r.json();
    if(d.error){ m.className='msg err'; m.textContent=d.error; finishRefresh(b); return; }
    if(!d.async){                              // 定向同步模式（指定站点）
      showRefreshResult(m, d);
      await tick();
      finishRefresh(b);
      return;
    }
    // 异步模式：开始轮询进度
    pollRefresh(b, m);
  }catch(e){
    m.className='msg err'; m.textContent='✗ ' + e;
    finishRefresh(b);
  }
}

function finishRefresh(b){
  b.disabled = false; b.classList.remove('spin');
  REFRESH_POLL = null;
  setTimeout(function(){
    const m = document.getElementById('msg');
    if(m) m.textContent = '';
  }, 8000);
}

function showRefreshResult(m, d){
  const lv = d.level || (d.ok ? 'ok' : 'error');
  m.className = lv==='error' ? 'msg err' : 'msg';
  m.style.color = lv==='error' ? '#e08a8a' : (lv==='warn' ? '#e0b542' : '');
  const head = lv==='error' ? '⚠ ' : (lv==='warn' ? '· ' : '✓ ');
  m.textContent = head + (d.detail || d.msg || '完成');
}

function pollRefresh(b, m){
  const t0 = Date.now();
  REFRESH_POLL = setInterval(async function(){
    try{
      const r = await fetch('/api/refresh/status', {cache:'no-store'});
      const p = await r.json();
      const secs = Math.round((Date.now() - t0) / 1000);
      if(p.finished){
        clearInterval(REFRESH_POLL); REFRESH_POLL = null;
        showRefreshResult(m, p.result || {msg:'完成'});
        await tick();
        finishRefresh(b);
        return;
      }
      // 显示实时进度：当前站 + 已完成数
      const doneN = (p.done||[]).length;
      const totN = p.total || 0;
      let txt = '⏳ 正在采集';
      if(p.current) txt += ' ' + p.current;
      if(totN) txt += '（' + doneN + '/' + totN + '）';
      txt += ' ｜ 已 ' + secs + ' 秒';
      if(secs >= 20) txt += ' ｜ 慢站会拖时间，可关闭页面，采集在后台继续';
      m.className = 'msg';
      m.style.color = '#e0b542';
      m.textContent = txt;
    }catch(e){
      // 轮询失败不致命，继续试
    }
  }, 1000);
}

tick();
setInterval(tick, 3000);   // 只轮询本地状态，不联网采集
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--open", action="store_true", help="启动后自动开浏览器")
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    # 注入汇率
    global PAGE
    PAGE = PAGE.replace("__EX__", str(EX))
    url = "http://127.0.0.1:%d" % args.port
    print("中转站用量面板已启动: %s" % url)
    print("  · 自动采集保持每 5 分钟一轮（另需 poll_loop.py 在跑）")
    print("  · 点页面上的「立即刷新」= 触发一次即时采集（43 KB）")
    print("  · 页面每 3 秒读一次本地库，不产生网络流量")
    print("  Ctrl+C 停止")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
