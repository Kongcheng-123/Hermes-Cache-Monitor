# -*- coding: utf-8 -*-
"""prices.py — 模型单价表（可按「站 × 模型」手填，网页上编辑）

设计要点（2026-10-02 建）
------------------------
· 数据文件：`proxy_monitor/prices.json`（本目录，随项目走，便于备份/迁移）
· 匹配规则：先精确匹配 (host, model)，再退到通配 (host, "*")，最后 (global, "*")。
  这样「某站给个默认价 + 个别模型单独定价」都能表达。
· 单位：元 / 百万 token（与缓存监控 cache_prices.json 口径一致，便于互换）
· 原子写：改价走 tmp + os.replace，防写坏。
· 热重载：每次读都看 mtime，文件一变就用新值（网页改完立即生效，无需重启）。

字段说明
--------
  host   站点标识（用 sites.json 里的 host，如 "api.dshapi.icu"；"*" = 所有站）
  model  模型名（"*" = 该站全部模型）
  in     输入（未命中缓存）单价，元/百万token
  out    输出单价
  cache  缓存命中单价
  cur    币种标记，默认 "¥"
  tag    可选标签，如 "免费"
  note   可选备注
  updated 最后修改日期（自动写）
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "prices.json")

_cache = {"mtime": None, "data": []}


def _load_raw():
    """读文件（带 mtime 热重载）。文件不存在返回空列表。"""
    try:
        mt = os.path.getmtime(PATH)
    except OSError:
        _cache["mtime"], _cache["data"] = None, []
        return []
    if _cache["mtime"] == mt:
        return _cache["data"]
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, list):
            d = []
    except Exception:
        d = []
    _cache["mtime"], _cache["data"] = mt, d
    return d


def all_entries():
    """全部价格条目（原样）"""
    return _load_raw()


def _norm(s):
    return (s or "").strip().lower()


def find(host, model):
    """按 站×模型 找价。匹配优先级：
       ① (host, model) 精确
       ② (host, "*")
       ③ ("*", model)
       ④ ("*", "*")
       返回条目 dict 或 None
    """
    entries = _load_raw()
    h, m = _norm(host), _norm(model)
    exact = wild_h = wild_m = wild_all = None
    for e in entries:
        eh, em = _norm(e.get("host")), _norm(e.get("model"))
        if eh == h and em == m:
            exact = e
        elif eh == h and em == "*":
            wild_h = e
        elif eh == "*" and em == m:
            wild_m = e
        elif eh == "*" and em == "*":
            wild_all = e
    return exact or wild_h or wild_m or wild_all


def unit_price(host, model):
    """返回 (in, out, cache) 单价三元组；无匹配返回 None"""
    e = find(host, model)
    if not e:
        return None
    try:
        return (float(e.get("in") or 0), float(e.get("out") or 0),
                float(e.get("cache") or 0))
    except Exception:
        return None


def is_free(host, model):
    e = find(host, model)
    return bool(e and (e.get("tag") == "免费" or
                       (not e.get("in") and not e.get("out") and not e.get("cache"))))


def compute_cost(host, model, in_tokens, cache_tokens, out_tokens):
    """按单价算钱（元）。无价返回 None（调用方据此显示「未配价」）。"""
    p = unit_price(host, model)
    if p is None:
        return None
    pi, po, pc = p
    return ((in_tokens or 0) * pi + (out_tokens or 0) * po +
            (cache_tokens or 0) * pc) / 1e6


def upsert(host, model, in_price, out_price, cache_price, cur="¥",
           tag="", note=""):
    """新增或更新一条价格（原子写）"""
    entries = _load_raw()

    def num(v):
        try:
            return float(v)
        except Exception:
            return None

    row = {
        "host": (host or "").strip(),
        "model": (model or "*").strip() or "*",
        "cur": cur or "¥",
        "in": num(in_price),
        "out": num(out_price),
        "cache": num(cache_price),
        "updated": time.strftime("%Y-%m-%d"),
    }
    for k in ("in", "out", "cache"):
        if row[k] is None:
            row[k] = 0.0
    if tag:
        row["tag"] = tag
    if note:
        row["note"] = note

    hit = False
    for i, e in enumerate(entries):
        if _norm(e.get("host")) == _norm(row["host"]) and \
           _norm(e.get("model")) == _norm(row["model"]):
            entries[i] = row
            hit = True
            break
    if not hit:
        entries.append(row)
    _write(entries)
    return row


def delete(host, model):
    """删除一条价格"""
    entries = _load_raw()
    n0 = len(entries)
    entries = [e for e in entries
               if not (_norm(e.get("host")) == _norm(host) and
                       _norm(e.get("model")) == _norm(model))]
    if len(entries) != n0:
        _write(entries)
        return True
    return False


def _write(entries):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)
    _cache["mtime"] = None          # 让下次读重新加载


def covered_models(sites=None):
    """返回库中出现过的 (site, model) 组合 + 当前定价状态

    用于网页「模型定价」页：列出所有见过的模型，标出哪些还没配价。
    """
    import sqlite3
    db = os.path.join(HERE, "store.db")
    out = []
    if not os.path.isfile(db):
        return out
    con = sqlite3.connect(db, timeout=10)
    try:
        # 排除聚合行（按模型聚合的 * 行不是真实模型）
        rows = con.execute("""
            SELECT site, model, COUNT(*) n,
                   SUM(in_tokens) i, SUM(cache_read) cr, SUM(out_tokens) o,
                   SUM(cost) cost
            FROM usage_flows
            WHERE request_id NOT LIKE 'sub2api-model:%'
              AND request_id NOT LIKE 'sub2api-daily:%'
              AND model != '' AND model != '*'
            GROUP BY site, model
            ORDER BY n DESC""").fetchall()
    except Exception:
        rows = []
    finally:
        con.close()
    for site, model, n, i, cr, o, cost in rows:
        e = find(site, model)
        out.append({
            "site": site, "model": model, "n": n or 0,
            "in": i or 0, "cache": cr or 0, "out": o or 0,
            "site_cost": cost or 0,                 # 站方给的实际花费
            "priced": e is not None,
            "price": ({"in": e.get("in"), "out": e.get("out"),
                       "cache": e.get("cache"), "cur": e.get("cur", "¥"),
                       "tag": e.get("tag", "")} if e else None),
            "matched": (("%s/%s" % (e.get("host"), e.get("model"))) if e else ""),
        })
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="模型单价表")
    ap.add_argument("--list", action="store_true", help="列出全部价格")
    ap.add_argument("--covered", action="store_true", help="列出库中出现过的模型及定价状态")
    ap.add_argument("--set", nargs=4, metavar=("HOST", "MODEL", "IN", "OUT"),
                    help="设置价格（缓存价默认按 输入×0.02 估）")
    ap.add_argument("--cache", type=float, default=None, help="配合 --set 指定缓存价")
    ap.add_argument("--del", nargs=2, metavar=("HOST", "MODEL"), dest="delete")
    a = ap.parse_args()

    if a.list:
        for e in all_entries():
            print("  %-22s %-26s in=%-7s out=%-7s cache=%-7s %s"
                  % (e.get("host"), e.get("model"), e.get("in"), e.get("out"),
                     e.get("cache"), e.get("tag") or ""))
    elif a.covered:
        for c in covered_models():
            mark = "已定价" if c["priced"] else "★未定价"
            print("  %-18s %-24s %5d条  站方¥%-9.4f  %s"
                  % (c["site"], c["model"], c["n"], c["site_cost"], mark))
    elif a.set:
        h, m, i, o = a.set
        cp = a.cache if a.cache is not None else round(float(i) * 0.02, 6)
        row = upsert(h, m, i, o, cp)
        print("✓ 已写入:", json.dumps(row, ensure_ascii=False))
    elif a.delete:
        print("✓ 已删除" if delete(*a.delete) else "（没找到该条目）")
    else:
        ap.print_help()
