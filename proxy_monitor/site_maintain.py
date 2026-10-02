# -*- coding: utf-8 -*-
"""site_maintain.py — 采集库维护（保留策略 / 归档 / 压缩）

背景（2026-10-02）
------------------
库按高峰用量约 5 MB/天增长，5 年可达 9 GB。其中 raw_json 占 69%
（平均 1644 字节/行），是原始数据备份，只在排查/挖字段时有用。

用户的保留策略（2026-10-02 定）
------------------------------
**只留最近 7 天**（更早的对话基本不会回看）。

设计原则
--------
· 默认【只报告不动作】—— 所有清除操作必须显式下命令（用户对破坏性操作谨慎）
· 删除前自动备份（导出成 JSON 再删，可回溯）
· 支持 dry-run（--dry 只显示会删什么，不真删）
· 删完自动 VACUUM 回收空间（SQLite 删数据不会自动缩小文件）

用法
----
  python site_maintain.py --report           # 看现状（大小/各年龄段行数）
  python site_maintain.py --trim --days 7    # 删除 7 天前的流水（自动备份）
  python site_maintain.py --trim --days 7 --dry   # 先看会删什么
  python site_maintain.py --vacuum           # 回收空间
  python site_maintain.py --backup           # 只备份不删
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "store.db")
BACKUP_DIR = os.path.join(HERE, "archive")
CST = timezone(timedelta(hours=8))

# 保留天数（默认 7，见用户 2026-10-02 决定）
KEEP_DAYS = 7


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f TB" % n


def ts_str(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M")


def db_size():
    try:
        return os.path.getsize(STORE)
    except OSError:
        return 0


def count_rows(con, where, args=()):
    try:
        return con.execute("SELECT COUNT(*) FROM usage_flows WHERE " + where, args).fetchone()[0]
    except Exception:
        return 0


def report(con):
    sz = db_size()
    print("=" * 72)
    print("采集库维护报告  %s" % datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 72)
    print("\n【库体积】%s   %s" % (human(sz), STORE))

    total = count_rows(con, "1=1")
    print("【总行数】%d" % total)

    # 按年龄分段
    now = time.time()
    segs = [
        ("近 7 天", "ts >= ?", (now - 7 * 86400,)),
        ("7~30 天", "ts < ? AND ts >= ?", (now - 7 * 86400, now - 30 * 86400)),
        ("30~90 天", "ts < ? AND ts >= ?", (now - 30 * 86400, now - 90 * 86400)),
        ("90 天以上", "ts < ?", (now - 90 * 86400,)),
    ]
    print("\n【按年龄】")
    for name, where, args in segs:
        n = count_rows(con, where, args)
        pct = 100.0 * n / total if total else 0
        print("  %-10s %6d 行  (%.1f%%)" % (name, n, pct))

    # raw_json 占用
    try:
        r = con.execute("SELECT SUM(LENGTH(raw_json)), COUNT(*) FROM usage_flows "
                        "WHERE raw_json IS NOT NULL AND raw_json != ''").fetchone()
        rj, cnt = (r[0] or 0), (r[1] or 0)
        print("\n【raw_json】(原始数据备份，占大头)")
        print("  有值行数 %d，合计 %s，平均 %.0f 字节/行"
              % (cnt, human(rj), rj / max(1, cnt)))
        if rj:
            print("  ⚠️ raw_json 占库体积约 %.0f%%" % (100.0 * rj / max(1, sz)))
    except Exception:
        pass

    # 其他表
    print("\n【其他表】")
    for t in ("flow_sessions", "site_accounts", "collect_state"):
        try:
            n = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            print("  %-16s %6d 行" % (t, n))
        except Exception:
            pass

    # 保留策略预览
    old_total = count_rows(con, "ts < ?", (now - KEEP_DAYS * 86400,))
    print("\n【保留策略】只留最近 %d 天" % KEEP_DAYS)
    if old_total:
        print("  将清除 %d 行（占总量 %.0f%%）" % (old_total, 100.0 * old_total / max(1, total)))
        print("  命令：python site_maintain.py --trim --days %d" % KEEP_DAYS)
    else:
        print("  ✓ 无需清除（所有数据都在保留期内）")

    print("\n💡 删完后记得跑 --vacuum 回收空间（SQLite 删数据不会自动缩小文件）")
    print("   或直接：python site_maintain.py --trim --days %d --vacuum" % KEEP_DAYS)


def backup_rows(con, where, args, tag):
    """把待删行导出成 JSON 文件（删前备份）"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, "trimmed-%s.json" % tag)
    rows = con.execute("SELECT * FROM usage_flows WHERE " + where, args).fetchall()
    cols = [d[0] for d in con.execute("SELECT * FROM usage_flows LIMIT 1").description]
    data = [dict(zip(cols, r)) for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path, len(data)


def trim(con, days=KEEP_DAYS, do_vacuum=False, dry=False):
    cutoff = time.time() - days * 86400
    where = "ts < ?"
    args = (cutoff,)
    n = count_rows(con, where, args)
    print("保留最近 %d 天（截止 %s）" % (days, ts_str(cutoff)))
    print("待清除：%d 行" % n)
    if not n:
        print("✓ 无需清除")
        return

    if dry:
        print("\n[--dry] 只显示，不真删。样本（最先 5 行）：")
        for r in con.execute("SELECT site, request_id, day, model, cost FROM usage_flows "
                             "WHERE " + where + " ORDER BY ts ASC LIMIT 5", args):
            print("  %-18s %-30s %-11s ¥%s" % (r[0], (r[1] or "")[:30], r[2] or "-", r[4]))
        print("\n去掉 --dry 即真删（会先自动备份）")
        return

    # 先备份
    tag = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    try:
        path, cnt = backup_rows(con, where, args, tag)
        print("✓ 已备份 %d 行 -> %s" % (cnt, path))
    except Exception as e:
        print("✗ 备份失败，中止删除：%s" % e)
        return

    # 删 usage_flows + 连带 flow_sessions
    print("正在删除…")
    d1 = con.execute("DELETE FROM usage_flows WHERE " + where, args).rowcount
    d2 = con.execute("DELETE FROM flow_sessions WHERE request_id NOT IN "
                     "(SELECT request_id FROM usage_flows)").rowcount
    con.commit()
    print("✓ usage_flows 删 %d 行，flow_sessions 删 %d 行" % (d1, d2))
    print("  库体积现在 %s" % human(db_size()))

    if do_vacuum:
        vacuum(con)


def vacuum(con):
    before = db_size()
    print("回收空间（VACUUM）…")
    t0 = time.time()
    con.execute("VACUUM")
    after = db_size()
    print("✓ 完成，耗时 %.1fs" % (time.time() - t0))
    print("  %s -> %s（省下 %s）" % (human(before), human(after), human(max(0, before - after))))


def main():
    ap = argparse.ArgumentParser(description="采集库维护")
    ap.add_argument("--report", action="store_true", help="看现状（默认动作）")
    ap.add_argument("--trim", action="store_true", help="删除保留期外的数据")
    ap.add_argument("--days", type=int, default=KEEP_DAYS, help="保留天数（默认 %d）" % KEEP_DAYS)
    ap.add_argument("--dry", action="store_true", help="只显示会删什么，不真删")
    ap.add_argument("--vacuum", action="store_true", help="回收空间")
    ap.add_argument("--backup", action="store_true", help="只备份不删")
    a = ap.parse_args()

    if not os.path.isfile(STORE):
        print("✗ 找不到 store.db：%s" % STORE)
        return
    con = sqlite3.connect(STORE, timeout=20)

    if a.trim:
        trim(con, days=a.days, do_vacuum=a.vacuum, dry=a.dry)
    elif a.vacuum:
        vacuum(con)
    elif a.backup:
        tag = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
        path, cnt = backup_rows(con, "ts < ?", (time.time() - a.days * 86400,), tag)
        print("✓ 已备份 %d 行 -> %s" % (cnt, path))
    else:
        report(con)
    con.close()


if __name__ == "__main__":
    main()
