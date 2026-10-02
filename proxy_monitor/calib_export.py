# -*- coding: utf-8 -*-
"""calib_export.py — 站方校准数据导出模块（2026-10-02）

功能：
  1. 检查各站采集状态（collect_state），严格遵循 T4 红线：complete != 1 的站点坚决不参与校准，防止漏算数据误导对账。
  2. 会话级汇总：从 flow_sessions + usage_flows 提取最近 7 天各会话的真实调用量与费用（用于悬浮窗会话对账）。
  3. 日度级汇总：从 usage_flows 提取按天 + 规范化站点（host_alias.canon）+ 模型的真实用量（用于日账本统计窗口对账）。
  4. 原子写入：生成 proxy_calibration.json，同时输出到本地 proxy_monitor 目录与 %APPDATA%\\HermesCacheMonitor\\ 目录。
"""

import os
import sys
import json
import sqlite3
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import host_alias

CST = timezone(timedelta(hours=8))
DB_PATH = os.path.join(HERE, "store.db")

APPDATA_DIR = os.path.join(os.environ.get("APPDATA") or "", "HermesCacheMonitor")
TARGET_APPDATA = os.path.join(APPDATA_DIR, "proxy_calibration.json")
TARGET_LOCAL = os.path.join(HERE, "proxy_calibration.json")


def _atomic_write_json(path, data):
    """原子写入 JSON 文件（写 .tmp 然后原子 replace）。"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("[calib_export] 写入 %s 失败: %s" % (path, e))
        return False


def export_calibration(days_limit=7, verbose=False):
    """执行校准数据导出。

    返回 dict: 导出的校准数据结构，或者 None（导出失败）。
    """
    if not os.path.exists(DB_PATH):
        if verbose:
            print("[calib_export] 未找到 store.db：%s" % DB_PATH)
        return None

    now = datetime.now(CST)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    now_ts = int(now.timestamp())
    cutoff_date = (now - timedelta(days=days_limit)).strftime("%Y-%m-%d")

    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        # 1. 站点健康状态检查（T4 红线）
        states = cur.execute(
            "SELECT site, complete, last_msg, last_run, last_ok FROM collect_state"
        ).fetchall()

        valid_sites = []
        excluded_sites = {}
        sites_meta = {}

        for s in states:
            site = s["site"]
            sites_meta[site] = {
                "complete": s["complete"],
                "last_msg": s["last_msg"],
                "last_run": s["last_run"],
                "last_ok": s["last_ok"],
            }
            if s["complete"] == 1:
                valid_sites.append(site)
            else:
                excluded_sites[site] = "采集未完成(complete=0): %s" % (s["last_msg"] or "未知")

        if not valid_sites:
            if verbose:
                print("[calib_export] 无有效站点（所有站点 complete != 1），跳过导出")
            con.close()
            return None

        placeholders = ",".join("?" for _ in valid_sites)

        # 过滤重叠聚合（与 panel.py 保持完全一致：有逐条流水时只算逐条，无逐条时回退按日聚合）
        non_flow_cond = (
            "(uf.request_id NOT LIKE 'sub2api-model:%' "
            " AND (uf.request_id NOT LIKE 'sub2api-daily:%' "
            "      OR NOT EXISTS (SELECT 1 FROM usage_flows x "
            "                     WHERE x.site = uf.site "
            "                       AND x.request_id LIKE 'client:%' "
            "                       AND x.day = uf.day)))"
        )

        # 2. 会话级汇总（按 session_id 归集，只取 confidence >= 0.5 且 site 在有效站点中）
        q_sess = """
        SELECT fs.session_id,
               COUNT(*) as calls,
               SUM(uf.in_tokens) as in_tokens,
               SUM(uf.cache_read) as cache_read,
               SUM(uf.out_tokens) as out_tokens,
               SUM(uf.cost) as cost,
               GROUP_CONCAT(DISTINCT fs.site) as sites,
               MAX(uf.ts) as last_ts
        FROM flow_sessions fs
        JOIN usage_flows uf ON fs.site = uf.site AND fs.request_id = uf.request_id
        WHERE fs.session_id IS NOT NULL
          AND fs.confidence >= 0.5
          AND uf.site IN (%s)
          AND uf.day >= ?
        GROUP BY fs.session_id
        """ % placeholders

        sess_rows = cur.execute(q_sess, valid_sites + [cutoff_date]).fetchall()
        sessions = {}
        for r in sess_rows:
            sid = r["session_id"]
            site_list = sorted(list(set(r["sites"].split(",")))) if r["sites"] else []
            sessions[sid] = {
                "calls": r["calls"],
                "in_tokens": r["in_tokens"] or 0,
                "cache_read": r["cache_read"] or 0,
                "out_tokens": r["out_tokens"] or 0,
                "cost": round(r["cost"] or 0.0, 6),
                "sites": site_list,
                "last_ts": r["last_ts"] or 0,
            }

        # 2.5 关联子会话：如果子代理会话有站方用量，合并归入父会话（支持悬浮窗父子统一对账）
        hermes_db = r"D:\Hermes Agent CN Desktop\data\hermes-home\state.db"
        if os.path.isfile(hermes_db):
            try:
                hcon = sqlite3.connect("file:%s?mode=ro" % hermes_db.replace("\\", "/"), uri=True)
                child_rows = hcon.execute("SELECT id, parent_session_id FROM sessions WHERE parent_session_id IS NOT NULL").fetchall()
                hcon.close()
                for cid, pid in child_rows:
                    if cid in sessions and pid:
                        c_data = sessions[cid]
                        p_data = sessions.setdefault(pid, {
                            "calls": 0, "in_tokens": 0, "cache_read": 0, "out_tokens": 0,
                            "cost": 0.0, "sites": [], "last_ts": 0,
                        })
                        p_data["calls"] += c_data["calls"]
                        p_data["in_tokens"] += c_data["in_tokens"]
                        p_data["cache_read"] += c_data["cache_read"]
                        p_data["out_tokens"] += c_data["out_tokens"]
                        p_data["cost"] = round(p_data["cost"] + c_data["cost"], 6)
                        p_data["sites"] = sorted(list(set(p_data["sites"] + c_data["sites"])))
                        p_data["last_ts"] = max(p_data["last_ts"], c_data["last_ts"])
            except Exception:
                pass

        # 3. 日度级汇总（按 day + 规范化 host + model 归集）
        q_daily = """
        SELECT uf.day, uf.site, uf.model,
               COUNT(*) as calls,
               SUM(uf.in_tokens) as in_tokens,
               SUM(uf.cache_read) as cache_read,
               SUM(uf.out_tokens) as out_tokens,
               SUM(uf.cost) as cost
        FROM usage_flows uf
        WHERE uf.site IN (%s)
          AND uf.day >= ?
          AND %s
        GROUP BY uf.day, uf.site, uf.model
        ORDER BY uf.day DESC
        """ % (placeholders, non_flow_cond)

        daily_rows = cur.execute(q_daily, valid_sites + [cutoff_date]).fetchall()
        daily = {}
        for r in daily_rows:
            day = r["day"]
            canon_site = host_alias.canon(r["site"])
            model = r["model"] or "?"

            day_dict = daily.setdefault(day, {})
            host_dict = day_dict.setdefault(canon_site, {
                "calls": 0,
                "in_tokens": 0,
                "cache_read": 0,
                "out_tokens": 0,
                "cost": 0.0,
                "models": {},
            })

            host_dict["calls"] += r["calls"]
            host_dict["in_tokens"] += (r["in_tokens"] or 0)
            host_dict["cache_read"] += (r["cache_read"] or 0)
            host_dict["out_tokens"] += (r["out_tokens"] or 0)
            host_dict["cost"] = round(host_dict["cost"] + (r["cost"] or 0.0), 6)

            m_dict = host_dict["models"].setdefault(model, {
                "calls": 0,
                "in_tokens": 0,
                "cache_read": 0,
                "out_tokens": 0,
                "cost": 0.0,
            })
            m_dict["calls"] += r["calls"]
            m_dict["in_tokens"] += (r["in_tokens"] or 0)
            m_dict["cache_read"] += (r["cache_read"] or 0)
            m_dict["out_tokens"] += (r["out_tokens"] or 0)
            m_dict["cost"] = round(m_dict["cost"] + (r["cost"] or 0.0), 6)

        # 3.5 查询账号与套餐真实金额（消灭抽象 quota 概念）
        accts = cur.execute("SELECT site, plan_title, plan_total, plan_used, plan_remain, updated_at FROM site_accounts").fetchall()
        accounts = {}
        for a in accts:
            st = a["site"]
            p_title = a["plan_title"] or ""
            p_tot = a["plan_total"] or 0
            p_usd = a["plan_used"] or 0
            p_rem = a["plan_remain"] or 0
            # 若大于 10000 说明是站方内部 quota，除以 500000 换算为真实人民币
            tot_cny = round(p_tot / 500000.0, 2) if p_tot > 10000 else float(p_tot)
            used_cny = round(p_usd / 500000.0, 4) if p_usd > 10000 else float(p_usd)
            rem_cny = round(p_rem / 500000.0, 2) if p_rem > 10000 else float(p_rem)
            accounts[st] = {
                "plan_title": p_title,
                "total_cny": tot_cny,
                "used_cny": used_cny,
                "remain_cny": rem_cny,
                "updated_at": a["updated_at"],
            }

        con.close()

        # 4. 组装校准数据包
        payload = {
            "version": 1,
            "generated_at": now_str,
            "generated_ts": now_ts,
            "days_limit": days_limit,
            "cutoff_date": cutoff_date,
            "valid_sites": valid_sites,
            "excluded_sites": excluded_sites,
            "sites_meta": sites_meta,
            "sessions": sessions,
            "daily": daily,
            "accounts": accounts,
        }

        # 5. 原子写入到目标路径
        ok1 = _atomic_write_json(TARGET_LOCAL, payload)
        ok2 = _atomic_write_json(TARGET_APPDATA, payload)

        if verbose:
            print("[calib_export] 导出成功:")
            print("  生成时间: %s" % now_str)
            print("  有效站点: %s" % ", ".join(valid_sites))
            if excluded_sites:
                print("  排除站点: %s" % excluded_sites)
            print("  校准会话数: %d 条" % len(sessions))
            print("  校准日度天数: %d 天" % len(daily))
            print("  写入本地: %s (%s)" % (TARGET_LOCAL, "✓" if ok1 else "✗"))
            print("  写入 AppData: %s (%s)" % (TARGET_APPDATA, "✓" if ok2 else "✗"))

        return payload

    except Exception as e:
        if verbose:
            print("[calib_export] 导出发生异常: %s" % e)
        return None


if __name__ == "__main__":
    export_calibration(days_limit=7, verbose=True)
