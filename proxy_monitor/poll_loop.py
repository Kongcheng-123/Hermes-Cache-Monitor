# -*- coding: utf-8 -*-
"""poll_loop.py — 站方用量采集守护循环

设计依据（2026-10-01 实测 + NewAPI 源码交叉验证）：
  · NewAPI /api/log/token 无分页，固定返回最近 N 条（源码 MaxRecentItems=1000）
  · 实测 example.com 未开启 CriticalRateLimit（22 次连打无 429），但官方默认
    是 20次/20分钟/按IP —— 别的站点可能开着，所以必须保守限速
  · 窗口会滚动，一旦被写满 1000 条，更早记录永久丢失

策略：默认 5 分钟一轮（远低于限流阈值），靠 request_id 幂等去重累积。
按当前用量（约 500+ 条覆盖 12 天），5 分钟间隔有极大安全余量。

用法:
  python poll_loop.py                 # 前台守护（Ctrl+C 停）
  python poll_loop.py --interval 300  # 自定义间隔秒
  python poll_loop.py --once          # 只跑一轮
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CST = timezone(timedelta(hours=8))
LOG_PATH = os.path.join(HERE, "poll.log")


def log(msg):
    line = "%s  %s" % (datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


_LAST_HERMES_ACTIVITY = None


def check_hermes_new_calls():
    """检查自上次采集以来 Hermes 本地是否有任何新 API 调用（0.1ms 本地只读检测）。"""
    global _LAST_HERMES_ACTIVITY
    db_path = r"D:\Hermes Agent CN Desktop\data\hermes-home\state.db"
    if not os.path.isfile(db_path):
        return True
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        cur = con.cursor()
        row = cur.execute("SELECT MAX(last_seen), SUM(api_call_count) FROM session_model_usage").fetchone()
        con.close()
        if not row or row[0] is None:
            return True
        activity = (row[0], row[1])
        if _LAST_HERMES_ACTIVITY is not None and activity == _LAST_HERMES_ACTIVITY:
            return False  # 本地零新调用，完全无需发起网络采集
        _LAST_HERMES_ACTIVITY = activity
        return True
    except Exception:
        return True


def _alert_login_needed(reason):
    """登录态彻底失效时兜底提醒（第 3 层）。

    · 限频：12 小时内只推一条（marker 文件记时间），免得每 5 分钟刷屏
    · 通道：若本机存在 send_weixin_text.py 就用微信推；不存在则**静默跳过**
      （不写死依赖 → 别人 clone 这个项目也能跑，只是没提醒）
    · 路径可用环境变量 DSH_ALERT_CMD 覆盖：命令里用 {msg} 占位
    """
    marker = os.path.join(HERE, ".dsh_login_alert_ts")
    now = time.time()
    try:
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8") as f:
                if now - float((f.read() or "0").strip() or 0) < 12 * 3600:
                    return
    except Exception:
        pass

    text = ("⚠️ dshapi 逐条流水的登录态需要补一次\n"
            "原因：%s\n"
            "修法：cd <项目目录>\\proxy_monitor 后跑 py dsh_auth.py --login\n"
            "（有邮箱密码可自动重登；没有就用 dsh_auth.py --import 从浏览器读一次）"
            % reason)
    cmd = os.environ.get("DSH_ALERT_CMD")
    try:
        if cmd:
            subprocess.run(cmd.replace("{msg}", text), shell=True, timeout=40)
        else:
            sender = os.path.join(os.path.dirname(HERE), "send_weixin_text.py")
            if not os.path.isfile(sender):
                return
            subprocess.run([sys.executable, sender, text], timeout=40)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(now))
        log("  已推送「登录态需补」提醒（12h 内不再重复）")
    except Exception as e:
        log("  提醒推送失败（不影响采集）：%s" % str(e)[:100])


def one_round():
    """跑一轮采集：动态载入 site_collect，避免循环导入问题"""
    if not check_hermes_new_calls():
        log("— Hermes 本地无新 API 调用，静默跳过本轮网络采集（0 流量 0 开销）")
        return 0

    import importlib
    import site_collect
    importlib.reload(site_collect)

    sites = site_collect.load_sites()
    provs = site_collect.parse_hermes_providers()
    total_new = 0
    # ★ 2026-10-08：两轮采集（用户拍板）。
    #   第一轮各站并行、每站 30 秒；失败的站记下来，末尾串行兜底再试一轮。
    #   两轮都失败 = 如实报失败，不再无限重试。
    failed = []
    for s in sites:
        if not s.get("enabled", True):
            continue
        try:
            r = site_collect.run_site(s, provs, full=False, log=lambda m: None, budget=30)
            if r:
                total_new += r.get("added") or 0
                log("%-20s 拉到 %-5d 新增 %-5d" % (
                    r["site"], r["fetched"], r["added"]))
            else:
                failed.append(s)
        except Exception as e:
            log("%-20s 采集异常 %s: %s" % (s.get("host"), type(e).__name__, str(e)[:150]))
            failed.append(s)

    # ── 第二轮：兜底重试失败的站（串行，各再给 30 秒）──
    for s in failed:
        h = s.get("host") or "?"
        try:
            r = site_collect.run_site(s, provs, full=False, log=lambda m: None, budget=30)
            if r:
                total_new += r.get("added") or 0
                log("%-20s 拉到 %-5d 新增 %-5d（重试成功）" % (
                    r["site"], r["fetched"], r["added"]))
            else:
                log("%-20s 采集失败（两轮均失败，本轮放弃）" % h)
        except Exception as e:
            log("%-20s 采集失败（两轮均失败）：%s" % (h, str(e)[:100]))

    # ── sub2api 站逐条流水（走网页端 JWT，见 dsh_flows.py）──
    # 这类站的 sk- key 接口只有聚合数据（一天一行），无法归集到对话；
    # 逐条流水必须走 /api/v1/usage（需登录态），所以这里补采。
    # ⚠️ 不做这步 → 面板「按对话」看不到 sub2api 站的数据（只有点「立即刷新」才会有）
    #
    # ★ 2026-10-07 多站改造：原来这里**写死只采一个站**（dshapi），
    #   第二个 sub2api 站（如 tryaigc）的流水永远采不到、token 也永不续期。
    #   现在遍历 sites.json 里所有 kind=sub2api 且需要登录态的站，各用各的 token。
    try:
        import importlib
        import dsh_auth
        import dsh_flows
        importlib.reload(dsh_auth)
        importlib.reload(dsh_flows)

        # 找出所有需要登录态的 sub2api 站
        s2sites = []
        for s in sites:
            if not s.get("enabled", True):
                continue
            if (s.get("kind") or "").lower() != "sub2api":
                continue
            host = s.get("host") or ""
            # 需不需要登录态：显式 session_auth 优先，否则 kind 默认（sub2api=需要）
            need = s.get("session_auth")
            if need is None:
                need = True
            if need and host:
                s2sites.append(s)

        for s in s2sites:
            host = s.get("host") or ""
            try:
                tok = dsh_auth.ensure_token(log=lambda m: None, host=host)
                if not tok:
                    log("%-18s 逐条跳过（无登录态；去站点账号页填账号密码）" % host)
                    _alert_login_needed("%s 拿不到登录态（token 失效且无法自动重登）" % host)
                    continue
                # ★ 2026-10-08 修（域名泄漏）：每个站用自己的请求域名池。
                #   原来直接读 s.get("bases")，站上没配就空 → base=None →
                #   dsh_flows.pick_base() 兜底到模块级 BASES（= 第一个 sub2api 站）
                #   → 静默把别的站的数据采回来贴本站标签。
                #   改用 site_resolver.bases_of()：没配 bases 时回退 base_url 的 origin。
                try:
                    import site_resolver as _srp
                    bases = _srp.bases_of(s)
                except Exception:
                    bases = []
                base = bases[0] if bases else None
                if not base:
                    log("%-18s 逐条跳过（缺请求域名：bases/base_url 都没配）" % host)
                    continue
                items, _t = dsh_flows.fetch_items(tok, pages=3, log=lambda m: None, base=base)
                if items:
                    norms = [dsh_flows.norm(it, host=host)
                             for it in items if it.get("request_id")]
                    con = site_collect.db()
                    added = site_collect.upsert(con, norms)
                    con.commit()
                    con.close()
                    total_new += added
                    log("%-18s 逐条拉到 %-5d 新增 %-5d" % (host, len(norms), added))
                else:
                    log("%-18s 逐条窗口内无新记录" % host)
            except Exception as e:
                log("%-18s 逐条异常 %s: %s" % (host, type(e).__name__, str(e)[:120]))
    except Exception as e:
        log("逐条流水异常 %s: %s" % (type(e).__name__, str(e)[:150]))

    # ── 每次采集后都跑一次会话归集与校准导出（确保无论是否有新流水，最新对话都能关联并导出）──
    try:
        import importlib
        import session_join
        importlib.reload(session_join)
        session_join.main_quiet()
    except Exception as e:
        log("归集异常 %s: %s" % (type(e).__name__, str(e)[:120]))

    # ── 自动导出校准文件（★ 2026-10-02 A3）──
    try:
        import importlib
        import calib_export
        importlib.reload(calib_export)
        res = calib_export.export_calibration(verbose=False)
        if res and total_new > 0:
            log("已更新校准文件: %d 个会话，%d 天日度" % (len(res.get("sessions", {})), len(res.get("daily", {}))))
    except Exception as e:
        log("导出校准文件异常 %s: %s" % (type(e).__name__, str(e)[:120]))

    # ── 无变动早退（★ 2026-10-02）──
    if total_new == 0:
        log("— 无新流水（已同步会话归集与校准数据）")
        return 0

    # ── 自动保留策略（★ 2026-10-02）──
    # 用户定：只留最近 7 天（老对话不回看）。随采集自动执行，不需额外 cron。
    # 插入点：放在归集之后，保证刚采到的新数据已归集完再判龄。
    try:
        auto_retention()
    except Exception as e:
        log("保留策略异常 %s: %s" % (type(e).__name__, str(e)[:120]))

    return total_new


# 保留天数（用户 2026-10-02 定：只留最近 7 天）
KEEP_DAYS = 7
_retention_marker = os.path.join(HERE, "last_retention.txt")


def auto_retention(days=KEEP_DAYS):
    """自动清理保留期外的数据（每天最多一次）

    设计取舍：
      · 每天只跑一次 —— 清理是重活（DELETE + VACUUM），5 分钟一轮没必要
      · VACUUM 只在真正删了东西后才跑（它需要额外磁盘空间且会锁库）
      · 失败不影响采集 —— 调用方已 try/except 包住
    """
    today = datetime.now(CST).strftime("%Y-%m-%d")
    try:
        if os.path.isfile(_retention_marker):
            with open(_retention_marker, encoding="utf-8") as f:
                if (f.read() or "").strip() == today:
                    return
    except Exception:
        pass

    import sqlite3
    cutoff = time.time() - days * 86400
    db = os.path.join(HERE, "store.db")
    if not os.path.isfile(db):
        return
    con = sqlite3.connect(db, timeout=30)
    try:
        n = con.execute("SELECT COUNT(*) FROM usage_flows WHERE ts < ?",
                        (cutoff,)).fetchone()[0]
        if not n:
            _mark_retention(today)
            return
        d1 = con.execute("DELETE FROM usage_flows WHERE ts < ?", (cutoff,)).rowcount
        d2 = con.execute("DELETE FROM flow_sessions WHERE request_id NOT IN "
                         "(SELECT request_id FROM usage_flows)").rowcount
        con.commit()
        log("保留策略           清除 %d 天前：流水 %d 行 / 归集 %d 行" % (days, d1, d2))
        try:
            before = os.path.getsize(db)
            con.execute("VACUUM")
            after = os.path.getsize(db)
            log("                   回收空间 %.1f MB -> %.1f MB"
                % (before / 1048576, after / 1048576))
        except Exception as e:
            log("                   VACUUM 跳过：%s" % str(e)[:80])
        _mark_retention(today)
    finally:
        con.close()


def _mark_retention(today):
    try:
        with open(_retention_marker, "w", encoding="utf-8") as f:
            f.write(today)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300, help="轮询间隔秒（默认 300）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    args = ap.parse_args()

    log("=" * 60)
    log("站方用量采集守护启动，间隔 %ds" % args.interval)

    if args.once:
        n = one_round()
        n_all = _count()
        log("单轮完成，新增 %d 条，库内合计 %d 条" % (n, n_all))
        return

    fails = 0
    while True:
        try:
            n = one_round()
            fails = 0
        except KeyboardInterrupt:
            log("收到中断，退出")
            break
        except Exception as e:
            fails += 1
            log("轮次异常(%d) %s: %s" % (fails, type(e).__name__, str(e)[:150]))
        # 连续失败时退避，避免打爆站点
        sleep_s = args.interval * (2 if fails >= 3 else 1)
        try:
            time.sleep(sleep_s)
        except KeyboardInterrupt:
            log("收到中断，退出")
            break


def _count():
    try:
        import sqlite3
        con = sqlite3.connect(os.path.join(HERE, "store.db"))
        return con.execute("SELECT COUNT(*) FROM usage_flows").fetchone()[0]
    except Exception:
        return -1


if __name__ == "__main__":
    main()
