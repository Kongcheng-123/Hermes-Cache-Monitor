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
_last_refresh = {"at": 0, "msg": "", "running": False}


def q(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def collect_now(target_sites=None):
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
        for s in sites:
            if not s.get("enabled", True):
                continue
            host = s.get("host")
            if target_sites and host not in target_sites:
                continue
            try:
                r = site_collect.run_site(s, provs, full=False, log=lambda m: None)
                if r:
                    total_new += r.get("added") or 0
                    msgs.append("%s +%d" % (r["site"], r["added"]))
            except Exception as e:
                msgs.append("%s 失败:%s" % (s.get("host"), str(e)[:60]))
        # 采集后重跑会话归集（数据变了，归集要跟上）
        try:
            import importlib
            import session_join
            importlib.reload(session_join)
            session_join.main_quiet()
        except Exception:
            pass
        # dshapi 逐条流水（走网页端 JWT，见 dsh_flows.py）
        # 若指定了 target_sites 且不包含 dsh 相关域名，跳过拉取以实现秒级加速
        need_dsh = True
        if target_sites:
            dsh_domains = {"api.dshapi.icu", "api2.dshapi.icu", "api4.dshapi.icu"}
            need_dsh = any(h in dsh_domains for h in target_sites)
        if need_dsh:
            try:
                import importlib
                import dsh_flows
                import dsh_auth
                importlib.reload(dsh_auth)
                importlib.reload(dsh_flows)
                tok = dsh_auth.ensure_token(log=lambda m: None)
                if tok:
                    items, _total = dsh_flows.fetch_items(tok, pages=3, log=lambda m: None)
                    if items:
                        norms = [dsh_flows.norm(it) for it in items if it.get("request_id")]
                        con2 = site_collect.db()
                        added2 = site_collect.upsert(con2, norms)
                        con2.commit()
                        con2.close()
                        total_new += added2
                        msgs.append("dsh流水 +%d" % added2)
                        session_join.main_quiet()   # 新流水也归到对话
            except Exception as e:
                msgs.append("dsh流水 失败:%s" % str(e)[:50])
        # 刷新完成后自动导出校准数据（供缓存监控使用）
        try:
            import calib_export
            calib_export.export_calibration(verbose=False)
        except Exception:
            pass
        _last_refresh["at"] = int(time.time())
        _last_refresh["msg"] = "新增 %d 条（%s）" % (total_new, "、".join(msgs))
        return {"ok": True, "added": total_new, "detail": _last_refresh["msg"]}
    except Exception as e:
        _last_refresh["msg"] = "失败: %s" % str(e)[:150]
        return {"ok": False, "msg": _last_refresh["msg"]}
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
        out["sessions"] = q(con, """
            SELECT f.session_id, COUNT(*) n,
                   SUM(u.in_tokens) i, SUM(u.cache_read) cr, SUM(u.out_tokens) o,
                   SUM(u.cost) c, AVG(f.confidence) conf,
                   MAX(u.ts) last_ts
            FROM flow_sessions f JOIN usage_flows u
              ON u.site=f.site AND u.request_id=f.request_id
            WHERE f.session_id IS NOT NULL
            GROUP BY f.session_id ORDER BY c DESC LIMIT 30""")
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
        elif self.path.startswith("/api/refresh"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            target = None
            if "sites" in qs:
                target = [s.strip() for s in qs["sites"][0].split(",") if s.strip()]
            elif "site" in qs:
                target = [s.strip() for s in qs["site"][0].split(",") if s.strip()]
            r = collect_now(target_sites=target)
            self._send(200, json.dumps(r, ensure_ascii=False))
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
                    note=body.get("note") or "")
                self._send(200, json.dumps({"ok": True, "row": row}, ensure_ascii=False))
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
.btn.sm{padding:3px 10px;font-size:11px}
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
h2.sec{font-size:14px;color:var(--ylw);margin:20px 0 8px;font-weight:600;
    cursor:pointer;user-select:none;display:flex;align-items:center;gap:7px}
h2.sec:hover{color:#f0d060}
h2.sec .arw{display:inline-block;transition:transform .18s;font-size:11px;color:var(--dim)}
h2.sec.fold .arw{transform:rotate(-90deg)}
h2.sec .cnt{font-size:11px;color:var(--dim);font-weight:400;margin-left:auto}
.foldbox.hidden{display:none}
</style></head><body>
<div class="top">
  <h1>中转站用量监控</h1>
  <button class="btn" id="rf" onclick="doRefresh()">⟳ 立即刷新</button>
  <span class="msg" id="msg"></span>
</div>
<div class="sub" id="sub">加载中…</div>
<div class="tabs">
  <button class="tab on" id="tab-usage" onclick="switchTab('usage')">用量</button>
  <button class="tab" id="tab-price" onclick="switchTab('price')">模型定价</button>
</div>
<div id="root"></div>
<div id="priceroot" style="display:none"></div>

<script>
const EX = __EX__;
function fmt(n){ return (n||0).toLocaleString('en-US'); }
function hitCls(h){ return h>=90?'hit':(h>=60?'hit low':'hit bad'); }
function esc(s){ return (s||'').replace(/[<>&]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])); }

function render(st){
  if(st.error){ document.getElementById('sub').textContent = st.error; return; }
  const lr = st.last_refresh||{};
  const lrTxt = lr.at ? new Date(lr.at*1000).toLocaleTimeString('zh-CN') : '尚未手动刷新';
  document.getElementById('sub').textContent =
    `数据时间 ${st.now} ｜ 上次手动刷新 ${lrTxt} ｜ 自动采集每 5 分钟 ｜ 汇率按 ${EX} 折算`;

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

  let sess = (st.sessions||[]).map(r=>{
    const t=(r.i||0)+(r.cr||0), h=t?(r.cr||0)/t*100:0;
    const last = r.last_ts ? new Date(r.last_ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
    return `<tr><td>${esc(r.session_id)}</td><td>${fmt(r.n)}</td>
      <td>${fmt(r.i)}</td><td>${fmt(r.cr)}</td>
      <td class="${hitCls(h)}">${h.toFixed(1)}%</td>
      <td>¥${((r.c||0)*EX).toFixed(6)}</td><td>${(r.conf||0).toFixed(2)}</td>
      <td>${last}</td></tr>`;
  }).join('');

  document.getElementById('root').innerHTML = `
    <h2 class="sec" onclick="fold(this)"><span class="arw">▼</span>今日
      <span class="cnt">${st.sites.length} 个站点</span></h2>
    <div class="foldbox"><div class="cards">${todayCards}</div></div>
    <h2 class="sec" onclick="fold(this)"><span class="arw">▼</span>累计
      <span class="cnt">${st.sites.length} 个站点</span></h2>
    <div class="foldbox"><div class="cards">${totalCards}</div></div>
    <h2 class="sec" onclick="fold(this)"><span class="arw">▼</span>按对话（站方数据归集）
      <span class="cnt">${(st.sessions||[]).length} 条</span></h2>
    <div class="foldbox"><table><tr><th>会话</th><th>请求</th><th>未命中</th><th>缓存命中</th><th>命中率</th><th>花费¥</th><th>置信</th><th>最后活动</th></tr>${sess}</table></div>
    <h2 class="sec" onclick="fold(this)"><span class="arw">▼</span>按日明细（命中率仅算业务请求，已剔除智能审批）
      <span class="cnt">${(st.daily||[]).length} 行</span></h2>
    <div class="foldbox"><table><tr><th>日期</th><th>站点</th><th>业务</th><th>输入</th><th>缓存命中</th><th>输出</th><th>命中率</th><th>¥</th></tr>${daily}</table></div>`;
}

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
  const isU = which === 'usage';
  document.getElementById('tab-usage').classList.toggle('on', isU);
  document.getElementById('tab-price').classList.toggle('on', !isU);
  document.getElementById('root').style.display = isU ? '' : 'none';
  document.getElementById('priceroot').style.display = isU ? 'none' : '';
  if(!isU) loadPrices();
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
function pv(id){ const e = document.getElementById(id); return e ? e.value : '0'; }
function num(v){ const n = parseFloat(v); return isNaN(n) ? 0 : n; }

async function savePrice(host, model){
  const key = (host+'|'+model).replace(/[^a-zA-Z0-9|._-]/g,'_');
  const body = {
    action: 'save', host: host, model: model,
    in:    num(pv('pi_'+key)), out: num(pv('po_'+key)), cache: num(pv('pc_'+key)),
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

function renderPrices(){
  const el = document.getElementById('priceroot');
  const cov = (PRICE_DATA && PRICE_DATA.covered) || [];
  const ent = (PRICE_DATA && PRICE_DATA.entries) || [];

  // ── 库中出现过的模型：一行一个，可直接填价 ──
  let rows = cov.map(r=>{
    const key = (r.site+'|'+r.model).replace(/[^a-zA-Z0-9|._-]/g,'_');
    const p = r.price || {};
    const badge = r.priced
      ? `<span class="pill ok">已定价${r.matched && r.matched.indexOf('*')>=0 ? '（通配 '+esc(r.matched)+'）' : ''}</span>`
      : `<span class="pill no">★ 未定价</span>`;
    return `<tr class="${r.priced?'':'miss'}">
      <td>${esc(r.site)}</td>
      <td>${esc(r.model)}${badge}</td>
      <td>${fmt(r.n)}</td>
      <td>${fmt(r.in)}</td>
      <td>${fmt(r.cache)}</td>
      <td>${fmt(r.out)}</td>
      <td>¥${(r.site_cost||0).toFixed(4)}</td>
      <td><input class="pf" id="pi_${key}" value="${p.in!=null?p.in:''}" placeholder="输入价"></td>
      <td><input class="pf" id="po_${key}" value="${p.out!=null?p.out:''}" placeholder="输出价"></td>
      <td><input class="pf" id="pc_${key}" value="${p.cache!=null?p.cache:''}" placeholder="缓存价"></td>
      <td><input class="pf txt" id="pt_${key}" value="${esc(p.tag||'')}" placeholder="标签(可空)"></td>
      <td style="white-space:nowrap">
        <button class="btn sm" onclick="savePrice('${esc(r.site)}','${esc(r.model)}')">保存</button>
        <button class="btn sm gray" onclick="autofill(${JSON.stringify(r).replace(/"/g,'&quot;')})">估算</button>
      </td></tr>`;
  }).join('');

  // ── 全部价格条目（含通配/已不用但仍保留的） ──
  let all = ent.map(e=>{
    const key = (e.host+'|'+e.model).replace(/[^a-zA-Z0-9|._-]/g,'_');
    return `<tr>
      <td>${esc(e.host)}</td><td>${esc(e.model)}</td>
      <td><input class="pf" id="pi_${key}" value="${e.in!=null?e.in:''}"></td>
      <td><input class="pf" id="po_${key}" value="${e.out!=null?e.out:''}"></td>
      <td><input class="pf" id="pc_${key}" value="${e.cache!=null?e.cache:''}"></td>
      <td><input class="pf txt" id="pt_${key}" value="${esc(e.tag||'')}"></td>
      <td><input class="pf txt" id="pn_${key}" value="${esc(e.note||'')}" placeholder="备注"></td>
      <td>${esc(e.updated||'')}</td>
      <td style="white-space:nowrap">
        <button class="btn sm" onclick="savePrice('${esc(e.host)}','${esc(e.model)}')">保存</button>
        <button class="btn sm red" onclick="delPrice('${esc(e.host)}','${esc(e.model)}')">删</button>
      </td></tr>`;
  }).join('');

  el.innerHTML = `
    <div class="sect">单价单位：元 / 百万 token。改完点「保存」立即生效（无需重启）。
      「估算」会按站方实付反推一个参考价填进输入框，请核对后再存。</div>
    <h2>库中出现过的模型（按用量排序）</h2>
    <table>
      <tr><th>站点</th><th>模型</th><th>请求</th><th>未命中</th><th>缓存</th><th>输出</th>
          <th>站方实付¥</th><th>输入价</th><th>输出价</th><th>缓存价</th><th>标签</th><th>操作</th></tr>
      ${rows || '<tr><td colspan="12" style="text-align:center;color:#8a8a92">暂无数据</td></tr>'}
    </table>
    <h2>已配置的全部价格</h2>
    <table>
      <tr><th>站点</th><th>模型</th><th>输入价</th><th>输出价</th><th>缓存价</th><th>标签</th><th>备注</th><th>更新</th><th>操作</th></tr>
      ${all || '<tr><td colspan="9" style="text-align:center;color:#8a8a92">还没有配过价格</td></tr>'}
    </table>
    <h2>新增一条价格</h2>
    <table>
      <tr><th>站点</th><th>模型</th><th>输入价</th><th>输出价</th><th>缓存价</th><th>标签</th><th>备注</th><th></th></tr>
      <tr>
        <td><input class="pf txt" id="np_host" placeholder="如 api.dshapi.icu 或 *"></td>
        <td><input class="pf txt" id="np_model" placeholder="模型名，* = 全部"></td>
        <td><input class="pf" id="np_in" placeholder="0.08"></td>
        <td><input class="pf" id="np_out" placeholder="0.32"></td>
        <td><input class="pf" id="np_cache" placeholder="0.0016"></td>
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
    tag: pv('np_tag').trim(), note: pv('np_note').trim() };
  const m = document.getElementById('msg');
  try{
    const r = await fetch('/api/prices', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    const d = await r.json();
    if(d.error) throw new Error(d.error);
    m.className='msg'; m.textContent = '✓ 已加入 '+host+' / '+model;
    ['np_host','np_model','np_in','np_out','np_cache','np_tag','np_note']
      .forEach(id=>{ const e=document.getElementById(id); if(e) e.value=''; });
    await loadPrices();
  }catch(e){ m.className='msg err'; m.textContent='加入失败: '+e; }
}

async function doRefresh(){
  const b = document.getElementById('rf'), m = document.getElementById('msg');
  b.disabled = true; b.classList.add('spin'); m.className='msg'; m.textContent='正在采集…';
  try{
    const r = await fetch('/api/refresh');
    const d = await r.json();
    m.className = d.ok ? 'msg' : 'msg err';
    m.textContent = d.ok ? ('✓ ' + (d.detail||'完成')) : ('✗ ' + (d.msg||'失败'));
    await tick();
  }catch(e){ m.className='msg err'; m.textContent='✗ '+e; }
  b.disabled = false; b.classList.remove('spin');
  setTimeout(()=>{ m.textContent=''; }, 8000);
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
