# -*- coding: utf-8 -*-
r"""credentials.py — 站点登录凭据独立存储（不进开源仓库）

为什么单独一个文件（2026-10-06 用户拍板方案 B）：
  · sites.json 是**站点配置**（域名、key 别名），可能要分享/同步 → 不能放密码
  · 本文件是**凭据**，只在本机存在，已加入 .gitignore
  · 一个文件管全部站 → 以后加第二个需登录的站也放这里

存储形态（明文，本机本地）：
  {
    "example.com": {
      "email": "<账号邮箱>",
      "password": "<密码>",
      "saved_at": 1791279630
    }
  }

⚠️ 安全边界：
  · 本文件**永不**同步到 opensource/（publish_sync.py 的 FILE_MAP 里没有它，
    且 .gitignore 已加 credentials.json）
  · 8788 网页端录入走 127.0.0.1 本地回环，不出网
  · 若哪天把 8788 暴露到局域网，明文密码传输就有风险 —— 别那么干
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "credentials.json")


def load():
    """读全部凭据；文件不存在或坏了返回 {}（绝不抛异常）。"""
    if not os.path.isfile(STORE):
        return {}
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def get(site_host):
    """取某站的凭据 dict；没有返回 {}。"""
    return load().get(site_host) or {}


def has_creds(site_host):
    """该站有可用的邮箱+密码吗。"""
    c = get(site_host)
    return bool(c.get("email") and c.get("password"))


def save(site_host, email, password):
    """写入/更新某站凭据（原子替换，防写坏）。"""
    d = load()
    d[site_host] = {
        "email": (email or "").strip(),
        "password": password or "",
        "saved_at": int(time.time()),
    }
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)
    _harden()
    return d[site_host]


def delete(site_host):
    """删掉某站凭据（用户想彻底清除时）。"""
    d = load()
    if site_host in d:
        del d[site_host]
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE)
        return True
    return False


def masked(site_host):
    """给界面看的脱敏视图（绝不回传明文密码）。"""
    c = get(site_host)
    if not c:
        return {"has": False}
    em = c.get("email") or ""
    if "@" in em:
        a, b = em.split("@", 1)
        em = (a[:2] + "***@" + b) if len(a) > 2 else ("***@" + b)
    return {
        "has": bool(c.get("email") and c.get("password")),
        "email": em,
        "saved_at": c.get("saved_at") or 0,
    }


def _harden():
    """尽力收紧文件权限（Windows 上 chmod 效果有限，尽力而为）。"""
    try:
        os.chmod(STORE, 0o600)
    except Exception:
        pass


if __name__ == "__main__":
    d = load()
    print("凭据文件:", STORE)
    print("存在:", os.path.isfile(STORE))
    for k in d:
        print("  %s -> %s" % (k, masked(k)))
