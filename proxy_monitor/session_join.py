# -*- coding: utf-8 -*-
"""按对话归集站方请求 v2 —— 最近邻分配

v1 的教训（2026-10-01）：
  ✗ 会话级 MIN/MAX 宽窗口 → 一个 9 小时对话把期间所有请求都吸走
  ✗ 5 分钟间隙切片段 → 切出 1472 个碎片，请求落在碎片空隙里够不到（76.6% 失配）

v2 策略：最近邻
  对每个站方请求（有精确到秒的 UTC 时刻，来自 request_id 前 14 位），
  在 Hermes 全部消息时间戳里二分查找最近的几条，归给所属会话。
  再用「距离」判定置信度：距离越小越可信。

为什么可行：
  · Hermes 每条 assistant 消息落库 ≈ 该轮 API 调用刚结束 → 时间高度贴近
  · 消息间隔中位数仅 3~6 秒 → 最近邻几乎总能命中同一会话
  · 并行会话时，谁的消息离得近就归谁（比「窗口重叠」精确得多）
"""
import bisect
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UTC = timezone.utc
HERMES_DB = r"D:\Hermes Agent CN Desktop\data\hermes-home\state.db"
STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store.db")

# 距离阈值（秒）：超过则认为该请求不属于任何已知对话
MAX_DIST = 1800.0
# 值得信任的距离（秒）
GOOD_DIST = 120.0


def req_ts(rid, fallback):
    """request_id 前 14 位是 UTC 时刻；返回 UTC 时间戳（秒）"""
    if rid and len(rid) >= 14:
        try:
            t = datetime.strptime(rid[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            return int(t.timestamp())
        except Exception:
            pass
    return fallback


def main():
    r = run(verbose=True)


def main_quiet():
    """静默跑一次归集（供 panel.py 采集后自动调用）"""
    return run(verbose=False)


def run(verbose=True):
    def log(*a):
        if verbose:
            print(*a)
    hcon = sqlite3.connect("file:%s?mode=ro" % HERMES_DB.replace("\\", "/"), uri=True)
    hcon.row_factory = sqlite3.Row

    # 全部消息时间戳（标准 UTC epoch），排序后用于二分
    rows = hcon.execute("""SELECT session_id, timestamp FROM messages
                           WHERE timestamp > 0 ORDER BY timestamp""").fetchall()
    if not rows:
        log("messages 表无数据")
        return
    times = []
    sids = []
    for r in rows:
        times.append(r["timestamp"])   # 标准 UTC epoch
        sids.append(r["session_id"])
    log("Hermes 消息数: %d，会话数: %d" % (len(times), len(set(sids))))
    log("时间范围: %s ~ %s" % (
        datetime.fromtimestamp(times[0], CST).strftime("%Y-%m-%d %H:%M"),
        datetime.fromtimestamp(times[-1], CST).strftime("%Y-%m-%d %H:%M")))

    scon = sqlite3.connect(STORE)
    scon.row_factory = sqlite3.Row
    # ⚠️ 归集只处理【逐条流水】（有 request_id + 精确时刻）：
    #   · 排除 sub2api-model:（按模型聚合，day 为空，无时刻，归集无意义）
    #   · 排除 sub2api-daily:（按日聚合，ts 是当天零点，归集会全部错配到同一会话）
    #   dshapi 的逐条行走 client: 前缀（见 dsh_flows.py），自带毫秒时刻，可正常归集。
    flows = scon.execute("""SELECT request_id, ts, site FROM usage_flows
                            WHERE request_id NOT LIKE 'sub2api-%'
                            ORDER BY ts""").fetchall()
    log("站方流水数: %d" % len(flows))

    scon.executescript("""
    CREATE TABLE IF NOT EXISTS flow_sessions (
        site        TEXT,
        request_id  TEXT,
        session_id  TEXT,
        match_kind  TEXT,
        confidence  REAL,
        dist_sec    REAL,
        PRIMARY KEY (site, request_id)
    );
    CREATE INDEX IF NOT EXISTS idx_fs_session ON flow_sessions(session_id);

    DROP TABLE IF EXISTS flow_sessions_tmp;
    CREATE TABLE flow_sessions_tmp (
        site        TEXT,
        request_id  TEXT,
        session_id  TEXT,
        match_kind  TEXT,
        confidence  REAL,
        dist_sec    REAL,
        PRIMARY KEY (site, request_id)
    );
    """)

    stats = defaultdict(int)
    conf_sum = 0.0
    batch = []

    for f in flows:
        ts = req_ts(f["request_id"], f["ts"])
        i = bisect.bisect_left(times, ts)
        # 看左右各 3 条候选中哪条最近
        best_d = None
        best_sid = None
        for j in range(max(0, i - 3), min(len(times), i + 4)):
            d = abs(times[j] - ts)
            if best_d is None or d < best_d:
                best_d = d
                best_sid = sids[j]

        if best_d is None or best_d > MAX_DIST:
            stats["far"] += 1
            batch.append((f["site"], f["request_id"], None, "none", 0.0, best_d or -1))
            continue

        if best_d <= GOOD_DIST:
            kind, conf = "near", 1.0 - best_d / (GOOD_DIST * 4)
            stats["near"] += 1
        elif best_d <= MAX_DIST:
            kind, conf = "far_ok", 0.5
            stats["far_ok"] += 1
        else:
            kind, conf = "none", 0.0
        conf = max(0.1, min(1.0, conf))
        conf_sum += conf
        batch.append((f["site"], f["request_id"], best_sid, kind, conf, best_d))

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

    tot = sum(stats.values())
    log()
    log("=== 归集结果（最近邻）===")
    labels = {"near": "近距离(≤120s)", "far_ok": "中距离(≤30min)", "far": "超远(丢弃)"}
    for k in ("near", "far_ok", "far"):
        v = stats.get(k, 0)
        log("  %-18s %-7d (%.1f%%)" % (labels[k], v, v / tot * 100 if tot else 0))
    ok = stats.get("near", 0) + stats.get("far_ok", 0)
    log("  可归属 %d / %d = %.1f%%" % (ok, tot, ok / tot * 100 if tot else 0))
    log("  平均置信度 %.2f" % (conf_sum / tot if tot else 0))

    log()
    log("=== 按会话汇总（按花费降序）===")
    q = """
        SELECT f.session_id, COUNT(*) n,
               SUM(u.in_tokens) i, SUM(u.cache_read) cr, SUM(u.out_tokens) o,
               SUM(u.quota) q, SUM(u.cost) c,
               MIN(f.confidence) cmin, AVG(f.confidence) cavg
        FROM flow_sessions f JOIN usage_flows u
          ON u.site=f.site AND u.request_id=f.request_id
        WHERE f.session_id IS NOT NULL
        GROUP BY f.session_id ORDER BY c DESC LIMIT 20"""
    log("  %-32s %-5s %-11s %-12s %-7s %-10s %-10s %s" % (
        "会话", "请求", "输入", "缓存命中", "命中率", "花费$", "花费¥", "置信"))
    for r in scon.execute(q):
        hit = (r["cr"] or 0) / ((r["i"] or 0) + (r["cr"] or 0)) * 100 if (r["i"] or 0) + (r["cr"] or 0) else 0
        log("  %-32s %-5d %-11s %-12s %-6.1f%% %-10.6f %-10.6f %.2f" % (
            (r["session_id"] or "")[:32], r["n"],
            "{:,}".format(r["i"] or 0), "{:,}".format(r["cr"] or 0),
            hit, r["c"] or 0, (r["c"] or 0) * 7.1, r["cavg"] or 0))

    scon.close()
    hcon.close()


if __name__ == "__main__":
    main()
