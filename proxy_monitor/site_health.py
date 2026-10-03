# -*- coding: utf-8 -*-
"""site_health.py — 站方监控体检（2026-10-01）

回答的问题：「这套东西现在健康吗？站变多了要不要动手？」

一条命令看全：
  · 站点清单与类型（探测结果）
  · 各站数据量与最近采集时间
  · 采集耗时外推 —— 离守护周期（5 分钟）还有多少余量
  · 数据陈旧度 —— 哪个站悄悄不采了
  · 库体积与增长速率外推
  · 异常提示（采集失败 / 类型未识别 / 数据过旧）

用法：
  python site_health.py            # 体检
  python site_health.py --json     # 机器可读输出
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "store.db")
SITES = os.path.join(HERE, "sites.json")
CST = timezone(timedelta(hours=8))
POLL_INTERVAL = 300          # 守护周期（秒），与 poll_loop 默认一致
STALE_WARN_H = 2             # 超过这么久没采到新数据 → 提醒
DB_SCALE_WARN_MB = 500       # 库超过这个体积 → 提醒考虑归档
TIME_USAGE_WARN = 0.6        # 采集耗时占守护周期比例超过此值 → 提醒并发


def ts_str(ts):
    """ts → 可读时间

    ⚠️ 时区语义（2026-10-01 踩坑）：
      库里 ts 有两种来源，语义不同：
        · NewAPI 站（NewAPI 类站）：ts 是【标准 epoch】，用 CST 格式化即得北京时间
        · dsh 站（dsh_flows.py）：它把 ts 统一存成【北京时间戳】（epoch + 8h），
          见 dsh_flows.norm() 的注释 —— 因为 session_join.req_ts() 约定
          「回退分支的 ts 已是北京时间戳，会 +8h」。
      所以 dsh 的 ts 若再按 CST(+8h) 格式化，会多出 8 小时、显示成"未来时间"。
      处理：dsh 的 ts 直接按 UTC 格式化即可还原北京钟点。
    """
    if not ts:
        return "-"
    ts = int(ts)
    # 北京时间戳（> 现在 + 1h 的值只可能来自 +8h 补偿） → 按 UTC 格式化还原
    if ts > time.time() + 3600:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")
    return datetime.fromtimestamp(ts, CST).strftime("%m-%d %H:%M")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def collect(con):
    out = {"sites": [], "warn": [], "info": []}

    # ── 站点配置 ──
    sites_cfg = []
    try:
        d = json.load(open(SITES, encoding="utf-8"))
        sites_cfg = d.get("sites", [])
        cache = d.get("_probe_cache") or {}
    except Exception as e:
        out["warn"].append("sites.json 读取失败：%s" % e)
        cache = {}

    # ── 每站统计 ──
    rows = list(con.execute("""
        SELECT site, COUNT(*) n, MIN(ts) t0, MAX(ts) t1, SUM(cost) cost, MAX(day) day
        FROM usage_flows GROUP BY site ORDER BY n DESC"""))
    stats = {r[0]: {"n": r[1], "t0": r[2], "t1": r[3], "cost": r[4] or 0, "day": r[5]} for r in rows}

    # ── 采集状态 ──
    state = {}
    try:
        for r in con.execute("SELECT site,last_run,last_ok,last_msg,complete,ok_keys,total_keys "
                             "FROM collect_state"):
            state[r[0]] = {"last_run": r[1], "last_ok": r[2], "last_msg": r[3],
                           "complete": r[4], "ok_keys": r[5], "total_keys": r[6]}
    except Exception:
        # 老库可能没有 complete 等列 —— 退化为只读基础字段
        try:
            for r in con.execute("SELECT site,last_run,last_ok,last_msg FROM collect_state"):
                state[r[0]] = {"last_run": r[1], "last_ok": r[2], "last_msg": r[3],
                               "complete": None, "ok_keys": None, "total_keys": None}
        except Exception:
            pass

    now = time.time()
    for s in sites_cfg:
        host = s.get("host") or ""
        base = s.get("base_url") or ""
        st = stats.get(host, {})
        sc = state.get(host, {})
        last = st.get("t1")
        age_h = ((now - last) / 3600.0) if last else None
        out["sites"].append({
            "host": host,
            "kind": s.get("kind") or "(未探测)",
            "kind_src": (cache.get(base) or {}).get("reason", ""),
            "enabled": s.get("enabled", True),
            "flows": st.get("n", 0),
            "cost": round(st.get("cost", 0), 4),
            "last_ts": last,
            "last_str": ts_str(last),
            "age_h": round(age_h, 1) if age_h is not None else None,
            "last_run": ts_str(sc.get("last_run")),
            "last_ok": sc.get("last_ok"),
            "last_msg": (sc.get("last_msg") or "")[:70],
        })
        if not s.get("enabled", True):
            continue
        if s.get("kind") == "unknown":
            out["warn"].append("%s 类型未识别 —— 需在 sites.json 显式写 kind" % host)
        if sc and not sc.get("last_ok"):
            out["warn"].append("%s 上次采集失败：%s" % (host, (sc.get("last_msg") or "")[:60]))
        if age_h is not None and age_h > STALE_WARN_H:
            # 按日聚合的站本来就天天有数据，超 2 小时没新值未必是故障
            out["info"].append("%s 已 %.1f 小时没有新流水（可能只是没使用）" % (host, age_h))

    # 库里存在但配置里没有的站（历史残留）
    cfg_hosts = set(s.get("host") for s in sites_cfg)
    for h in stats:
        if h and h not in cfg_hosts:
            out["info"].append("库中有历史站 %s（%d 条），已不在监控列表" % (h, stats[h]["n"]))

    # ── 库体积与外推 ──
    try:
        sz = os.path.getsize(STORE)
    except Exception:
        sz = 0
    total_n = sum(v["n"] for v in stats.values())
    per_row = (sz / total_n) if total_n else 0
    days = sorted(set(r[0] for r in con.execute(
        "SELECT DISTINCT day FROM usage_flows WHERE day != ''")))
    per_day_rows = (total_n / len(days)) if days else 0
    out["db"] = {
        "size": sz, "size_str": human(sz), "rows": total_n,
        "days": len(days), "rows_per_day": round(per_day_rows, 1),
        "bytes_per_row": round(per_row, 1),
        "size_1y_str": human(per_row * per_day_rows * 365),
        "size_5y_str": human(per_row * per_day_rows * 365 * 5),
    }
    if sz > DB_SCALE_WARN_MB * 1048576:
        out["warn"].append("库已 %.0f MB，考虑归档历史数据" % (sz / 1048576))

    # ── 采集耗时余量 ──
    n_sites = sum(1 for s in sites_cfg if s.get("enabled", True))
    # 实测经验：NewAPI 站约 20s/站（1000 条窗口），sub2api 聚合站约 4s/站
    est = sum(20 if (s.get("kind") == "newapi") else 4 for s in sites_cfg
              if s.get("enabled", True))
    ratio = est / float(POLL_INTERVAL)
    out["perf"] = {"sites": n_sites, "est_seconds": est,
                   "interval": POLL_INTERVAL, "ratio": round(ratio, 2)}
    if ratio > TIME_USAGE_WARN:
        out["warn"].append(
            "采集耗时估算 %ds 已占守护周期 %ds 的 %.0f%% —— 建议改并发采集"
            % (est, POLL_INTERVAL, ratio * 100))
    return out


def report(o):
    print("=" * 74)
    print("站方用量监控 · 体检  %s" % datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 74)

    print("\n【站点】")
    if not o["sites"]:
        print("  （无配置站点）")
    for s in o["sites"]:
        flag = "" if s["enabled"] else "  [已停用]"
        print("  %-22s %-9s %6d 条  ¥%-9.4f  最近 %s%s"
              % (s["host"], s["kind"], s["flows"], s["cost"], s["last_str"], flag))
        if s["kind_src"]:
            print("  %-22s   └ 判定依据：%s" % ("", s["kind_src"][:60]))
        if s["last_msg"]:
            print("  %-22s   └ 上次采集：%s  %s"
                  % ("", "ok" if s["last_ok"] else "失败", s["last_msg"]))

    d = o["db"]
    print("\n【库】")
    print("  体积 %s ｜ %d 条 ｜ 覆盖 %d 天 ｜ 约 %.0f 条/天 ｜ %.0f 字节/条"
          % (d["size_str"], d["rows"], d["days"], d["rows_per_day"], d["bytes_per_row"]))
    print("  按当前速率外推：一年 %s ｜ 五年 %s" % (d["size_1y_str"], d["size_5y_str"]))

    p = o["perf"]
    print("\n【采集余量】")
    print("  启用 %d 站 ｜ 单轮估算 %ds ｜ 守护周期 %ds ｜ 占用 %.0f%%"
          % (p["sites"], p["est_seconds"], p["interval"], p["ratio"] * 100))
    if p["ratio"] <= 0.3:
        print("  ✓ 余量充足")
    elif p["ratio"] <= 0.6:
        print("  ○ 尚可，站再多建议改并发")
    else:
        print("  ⚠ 接近上限，建议改并发采集")

    if o["warn"]:
        print("\n【需要注意】")
        for w in o["warn"]:
            print("  ⚠ %s" % w)
    if o["info"]:
        print("\n【提示】")
        for i in o["info"]:
            print("  · %s" % i)
    if not o["warn"]:
        print("\n✓ 整体健康")


def main():
    ap = argparse.ArgumentParser(description="站方监控体检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()
    if not os.path.isfile(STORE):
        print("✗ 找不到 store.db：%s" % STORE)
        return
    con = sqlite3.connect(STORE, timeout=10)
    o = collect(con)
    con.close()
    if a.json:
        print(json.dumps(o, ensure_ascii=False, indent=2))
    else:
        report(o)


if __name__ == "__main__":
    main()
