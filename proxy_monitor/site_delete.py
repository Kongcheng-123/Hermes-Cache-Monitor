# -*- coding: utf-8 -*-
r"""site_delete.py — 站点彻底删除（配置 + 全部数据）

★ 2026-10-08 新增（用户拍板）。

为什么单独一个模块：
  「删除站点」牵动 4 张数据库表 + 4 个 json 文件，散在好几个模块里。
  收口到一个地方，才能保证**删干净**（不留孤儿数据）且**可预览**（删前
  先把影响范围列给用户看，而不是弹个「确定吗」就删）。

删除范围（实测确认，以 yt.19851117.xyz 为例）：
  store.db
    · usage_flows     逐条流水（用量页/模型定价页的卡片数据源）
    · flow_sessions   归集关系（哪条流水属于哪个对话）
    · collect_state   采集状态
    · site_accounts   账号信息（余额/套餐/倍率）
  json
    · sites.json              站点配置
    · credentials.json        邮箱密码
    · dsh_auth.json           登录态 token
    · proxy_calibration.json  校准文件里的 valid_sites / sites_meta
  prices.json  —— 按 host 清（有则删，无则跳过）

安全设计：
  1. 删前 **preview()** 报出「将删多少行、哪些文件有它」—— 前端拿这个弹确认框
  2. 删前自动整体备份到 backups/<日期>-删站-<host>/
  3. 删后 **verify()** 复核，确认四处都没残留
  4. active 指向被删站时，自动改指到另一个存在的 sub2api 站
"""

import json
import os
import shutil
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "store.db")
SITES = os.path.join(HERE, "sites.json")
CREDS = os.path.join(HERE, "credentials.json")
DSH_AUTH = os.path.join(HERE, "dsh_auth.json")
CALIB = os.path.join(HERE, "proxy_calibration.json")
PRICES = os.path.join(HERE, "prices.json")

# store.db 里所有带 site 字段的表（删站要逐张清）
TABLES = ("usage_flows", "flow_sessions", "collect_state", "site_accounts")


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def preview(host):
    """删前预览：这个站牵连了什么。返回 dict（给确认框显示）。"""
    host = (host or "").strip()
    out = {"host": host, "db": {}, "db_total": 0, "files": [], "calib": False,
           "ok": False}
    if not host:
        return out
    if not os.path.isfile(STORE):
        return out

    try:
        con = sqlite3.connect("file:%s?mode=ro" % STORE.replace("\\", "/"), uri=True)
        for t in TABLES:
            try:
                n = con.execute("SELECT COUNT(*) FROM %s WHERE site=?" % t,
                                (host,)).fetchone()[0]
            except Exception:
                n = 0
            out["db"][t] = n
            out["db_total"] += n
        con.close()
    except Exception:
        pass

    if any((s.get("host") or "").strip() == host
           for s in (_read_json(SITES, {}).get("sites") or [])):
        out["files"].append("sites.json（站点配置）")
    if host in (_read_json(CREDS, {}) or {}):
        out["files"].append("credentials.json（邮箱密码）")
    if host in ((_read_json(DSH_AUTH, {}).get("sites") or {})):
        out["files"].append("dsh_auth.json（登录态）")
    if any((p.get("host") or "").strip() == host
           for p in (_read_json(PRICES, []) or [])):
        out["files"].append("prices.json（价格配置）")
    cal = _read_json(CALIB, {})
    if host in (cal.get("valid_sites") or []) or host in (cal.get("sites_meta") or {}):
        out["calib"] = True
        out["files"].append("proxy_calibration.json（校准数据）")

    out["ok"] = True
    return out


def delete(host, backup=True, log=print):
    """彻底删除一个站点的全部数据。

    返回 (ok: bool, msg: str, detail: dict)
    """
    host = (host or "").strip()
    if not host:
        return False, "缺少站点 host", {}

    pv = preview(host)
    if not pv.get("ok"):
        return False, "读不到站点数据（store.db 有问题？）", {}

    # ── 1. 备份（可回滚）──
    bkdir = ""
    if backup:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S").replace(":", "")[:15]
        safe = host.replace("/", "_").replace("\\", "_").replace(":", "_")
        bkdir = os.path.join(os.path.dirname(HERE), "backups",
                             "%s-删站-%s" % (time.strftime("%Y-%m-%d"), safe))
        try:
            os.makedirs(bkdir, exist_ok=True)
            for src in (STORE, SITES, CREDS, DSH_AUTH, CALIB, PRICES):
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(bkdir, os.path.basename(src)))
            log("  已备份到 %s" % bkdir)
        except Exception as e:
            return False, "备份失败，已中止删除：%s" % str(e)[:120], {}

    detail = {"db": {}, "files": [], "backup_dir": bkdir}

    # ── 2. 清数据库（4 张表）──
    con = sqlite3.connect(STORE, timeout=30)
    try:
        for t in TABLES:
            try:
                n = con.execute("DELETE FROM %s WHERE site=?" % t, (host,)).rowcount
            except Exception as e:
                n = 0
                log("  ⚠ 清 %s 失败：%s" % (t, str(e)[:80]))
            detail["db"][t] = n
        con.commit()
    finally:
        con.close()

    # ── 3. 清 sites.json ──
    raw = _read_json(SITES, {})
    sites = raw.get("sites") or []
    before = len(sites)
    sites = [s for s in sites if (s.get("host") or "").strip() != host]
    if len(sites) != before:
        # 顺手清探测缓存（base_url 为键）
        cache = raw.get("_probe_cache") or {}
        for k in list(cache.keys()):
            pass        # 探测缓存按 base_url 存，站点没了就都没用了；保守起见不动
        raw["sites"] = sites
        _atomic_json(SITES, raw)
        detail["files"].append("sites.json")

    # ── 4. 清 credentials.json ──
    c = _read_json(CREDS, {})
    if host in c:
        del c[host]
        _atomic_json(CREDS, c)
        detail["files"].append("credentials.json")

    # ── 5. 清 dsh_auth.json（含 active 纠偏）──
    da = _read_json(DSH_AUTH, {})
    slots = da.get("sites") or {}
    if host in slots:
        del slots[host]
        da["sites"] = slots
        # active 指向被删站 → 改指到任意一个还存在的槽位
        if (da.get("active") or "") == host:
            rest = list(slots.keys())
            da["active"] = rest[0] if rest else ""
            log("  active 原指向被删站，已改为 %s" % (da["active"] or "(空)"))
        _atomic_json(DSH_AUTH, da)
        detail["files"].append("dsh_auth.json")

    # ── 6. 清 prices.json ──
    pr = _read_json(PRICES, [])
    if isinstance(pr, list):
        left = [p for p in pr if (p.get("host") or "").strip() != host]
        if len(left) != len(pr):
            _atomic_json(PRICES, left)
            detail["files"].append("prices.json")

    # ── 7. 清 proxy_calibration.json ──
    cal = _read_json(CALIB, {})
    if isinstance(cal, dict) and cal:
        changed = False
        vs = cal.get("valid_sites") or []
        if host in vs:
            cal["valid_sites"] = [x for x in vs if x != host]
            changed = True
        sm = cal.get("sites_meta") or {}
        if host in sm:
            del sm[host]
            cal["sites_meta"] = sm
            changed = True
        if changed:
            _atomic_json(CALIB, cal)
            detail["files"].append("proxy_calibration.json")

    # ── 8. 删后复核 ──
    import importlib
    import site_resolver
    importlib.reload(site_resolver)          # 让配置缓存失效
    rest = verify(host)
    detail["verify"] = rest

    total = sum(detail["db"].values())
    if rest.get("clean"):
        return True, "已彻底删除 %s（清理 %d 行数据 + %d 个配置文件）" % (
            host, total, len(detail["files"])), detail
    return True, "已删除 %s，但仍有残留：%s" % (host, rest.get("leftover")), detail


def verify(host):
    """删后复核：四处都不该再有它。"""
    host = (host or "").strip()
    left = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STORE.replace("\\", "/"), uri=True)
        for t in TABLES:
            try:
                n = con.execute("SELECT COUNT(*) FROM %s WHERE site=?" % t,
                                (host,)).fetchone()[0]
                if n:
                    left.append("%s=%d行" % (t, n))
            except Exception:
                pass
        con.close()
    except Exception as e:
        left.append("db读取失败:%s" % str(e)[:40])
    if any((s.get("host") or "").strip() == host
           for s in (_read_json(SITES, {}).get("sites") or [])):
        left.append("sites.json")
    if host in (_read_json(CREDS, {}) or {}):
        left.append("credentials.json")
    if host in ((_read_json(DSH_AUTH, {}).get("sites") or {})):
        left.append("dsh_auth.json")
    if any((p.get("host") or "").strip() == host
           for p in (_read_json(PRICES, []) or [])):
        left.append("prices.json")
    return {"clean": not left, "leftover": "、".join(left) if left else ""}


def _atomic_json(path, data):
    """原子写 json（写 .tmp 再 replace）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python site_delete.py <host>            # 预览")
        print("      python site_delete.py <host> --do       # 真删")
        sys.exit(0)
    h = sys.argv[1]
    if "--do" in sys.argv:
        ok, msg, d = delete(h)
        print(("✓ " if ok else "✗ ") + msg)
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(preview(h), ensure_ascii=False, indent=2))
