# -*- coding: utf-8 -*-
"""归集健康检查（2026-10-09 新增）。

## 为什么有这个东西

用户换中转站入口地址后（例：dshapi 从 api2 换到 api9），Hermes 记录的
`billing_provider` 会**退化成裸 `custom`**，而站点配置里登记的是
`custom:your-provider` → provider 映射不上、bases 里又没 api9
→ 该会话之后的流水**全部归集不上**，面板数字一直停在换站那一刻。

实测证据（2026-10-09）：换站后 1627 条流水 match_kind=none，
面板最新记录卡在 03:13，而采集其实一直是好的（库里 05:04 还有新流水）。

## 这个模块干什么

只看两件事，都用大白话回答用户：
  ① 今天采集到的流水，有多少条认得出属于哪个对话？
  ② 有哪些 API 地址认不出来？（= 应该补登到站点配置的）

面板顶部据此显示一条状态：
    ✅ 用量归集正常 47/47
    ⚠ 有 12 个对话的用量没统计上 → 原因：地址 api9.dshapi.icu 没登记 → [补登]

## 注意

这是**只读**诊断，不改任何数据。补登走 site_admin.add_base_domain()。
"""
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "store.db")
SITES = os.path.join(HERE, "sites.json")
HERMES_DB = r"D:\Hermes Agent CN Desktop\data\hermes-home\state.db"

CST = timezone(timedelta(hours=8))

# 归集率低于这个值就报警（其余情况只提示"正常"）
WARN_RATIO = 0.90


def _conn(path, ro=True):
    if ro:
        return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    return sqlite3.connect(path)


def _known_domains():
    """站点配置里已登记的所有主域名 → 站点 host。"""
    out = {}
    try:
        cfg = json.load(open(SITES, encoding="utf-8"))
    except Exception:
        return out
    sites = cfg.get("sites") if isinstance(cfg, dict) else cfg
    for s in (sites or []):
        if not isinstance(s, dict):
            continue
        host = s.get("host") or ""
        for u in [s.get("base_url")] + list(s.get("bases") or []):
            d = _main_domain(u)
            if d:
                out[d] = host
    return out


def _main_domain(u):
    s = str(u or "").strip().lower()
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    h = s.split("/")[0].split(":")[0]
    if not h:
        return ""
    import re
    if h in ("localhost", "127.0.0.1") or re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
        return h
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def report(day=None):
    """归集健康度报告。返回 dict（面板直接渲染）。"""
    if not day:
        day = datetime.now(CST).strftime("%Y-%m-%d")

    out = {
        "day": day,
        "ok": True,                     # 是否健康
        "total": 0,                     # 今日流水总条数
        "joined": 0,                    # 能归属到对话的条数
        "ratio": 1.0,
        "sites": [],                    # [{site, total, joined, ratio}]
        "unknown": [],                  # [{domain, count, hint}] 认不出的地址
        "msg": "",
    }
    if not os.path.isfile(STORE):
        out["ok"] = False
        out["msg"] = "找不到流水库（store.db）"
        return out

    known = _known_domains()
    try:
        con = _conn(STORE)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT f.site, f.request_id, f.raw_json,
                   CASE WHEN fs.session_id IS NOT NULL THEN 1 ELSE 0 END AS j
            FROM usage_flows f
            LEFT JOIN flow_sessions fs ON f.request_id = fs.request_id
            WHERE f.day = ? AND f.request_id NOT LIKE 'sub2api-%'
        """, (day,)).fetchall()
        con.close()
    except Exception as e:
        out["ok"] = False
        out["msg"] = "读流水库失败：%s" % e
        return out

    if not rows:
        out["msg"] = "今天还没有采集到流水"
        return out

    per_site = {}
    unknown = Counter()
    unknown_site = {}
    for r in rows:
        site = r["site"] or "?"
        a = per_site.setdefault(site, {"total": 0, "joined": 0})
        a["total"] += 1
        a["joined"] += r["j"]
        out["total"] += 1
        out["joined"] += r["j"]
        if not r["j"]:
            # 未归属 —— 从 raw_json 里挖出请求地址，看是不是"没登记的新域名"
            burl = ""
            try:
                rj = json.loads(r["raw_json"] or "{}")
                burl = (rj.get("base_url") or rj.get("api_base")
                        or rj.get("url") or "")
            except Exception:
                pass
            dm = _main_domain(burl) if burl else ""
            key = dm or site
            unknown[key] += 1
            unknown_site.setdefault(key, site)

    out["ratio"] = (float(out["joined"]) / out["total"]) if out["total"] else 1.0
    for site, a in sorted(per_site.items(), key=lambda kv: -kv[1]["total"]):
        a["site"] = site
        a["ratio"] = (float(a["joined"]) / a["total"]) if a["total"] else 1.0
        out["sites"].append(a)

    for dm, cnt in unknown.most_common(10):
        hint = ""
        if dm in known:
            hint = "域名已登记（归集时被时间窗/配额挡下，稍后会自动补）"
        else:
            hint = "这个地址还没登记到站点配置"
        out["unknown"].append({"domain": dm, "count": cnt,
                               "site": unknown_site.get(dm, ""), "hint": hint})

    out["ok"] = out["ratio"] >= WARN_RATIO
    if out["ok"]:
        out["msg"] = "用量归集正常 %d/%d" % (out["joined"], out["total"])
    else:
        miss = out["total"] - out["joined"]
        out["msg"] = "有 %d 条流水的用量没统计上（共 %d 条）" % (miss, out["total"])
    return out


def main():
    import sys
    r = report()
    print("=== 归集健康度（%s）===" % r["day"])
    print("  %s" % r["msg"])
    print("  归集率 %.1f%%" % (r["ratio"] * 100))
    if r["sites"]:
        print("\n  各站：")
        for s in r["sites"]:
            print("    %-24s %4d 条，归属 %4d (%.0f%%)"
                  % (s["site"], s["total"], s["joined"], s["ratio"] * 100))
    if r["unknown"]:
        print("\n  没归属上的地址：")
        for u in r["unknown"]:
            print("    %-26s %4d 条  %s" % (u["domain"], u["count"], u["hint"]))


if __name__ == "__main__":
    main()
