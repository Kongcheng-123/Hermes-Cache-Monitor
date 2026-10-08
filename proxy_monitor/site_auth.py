# -*- coding: utf-8 -*-
r"""site_auth.py — 站点登录态统一抽象层（按 kind 自动识别）

设计目标（2026-10-06 用户拍板）：
  · **自动识别**，不硬编码站名。谁需要登录态，看 sites.json 的 session_auth 字段。
  · newapi / sub2api 两种 kind 都支持，行为按 kind 分派。
  · 现在只有 sub2api 需要登录态；但 newapi 的路径也做齐，
    将来遇到需要登录的 NewAPI 变体，改配置即可，不用改代码。

判定规则（优先级）：
  1. sites.json 里显式写了 session_auth: true / false → 以此为准
  2. 没写 → 按 kind 默认：newapi=不需要，sub2api=需要

对外函数：
  list_sites()               → 全部站点 + 各自登录态状态（供 UI 列表）
  site_info(host)            → 单个站点的状态
  login(host, email, pwd)    → 用凭据登录一次（目前仅 sub2api 实现）
  needs_login(site)          → 该站是否需要登录态
"""

import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 各 kind 的默认"是否需要登录态"
KIND_DEFAULT_NEEDS_LOGIN = {
    "sub2api": True,
    "newapi": False,
}


def _load_sites():
    """读 sites.json（走 site_resolver，拿到解析后的站点列表）。"""
    try:
        from . import site_resolver as _sr
    except ImportError:
        import site_resolver as _sr
    return _sr.all_sites() or []


def needs_login(site):
    """该站是否需要登录态。显式配置优先，否则按 kind 默认。"""
    if not isinstance(site, dict):
        return False
    if "session_auth" in site and site.get("session_auth") is not None:
        return bool(site.get("session_auth"))
    return KIND_DEFAULT_NEEDS_LOGIN.get(site.get("kind") or "", False)


def _dsh_status(host=None):
    """sub2api 侧的登录态（复用 dsh_auth.cred_status）。

    ★ 2026-10-07：支持按站取 —— 多站改造后每个站有独立 token 槽位。
    """
    try:
        try:
            from . import dsh_auth
        except ImportError:
            import dsh_auth
        return dsh_auth.cred_status(host=host)
    except Exception as e:
        return {"error": str(e)[:150]}


def _dsh_host():
    """dsh_auth 当前管着哪个站（用于把状态映射回站点）。"""
    try:
        try:
            from . import dsh_auth
        except ImportError:
            import dsh_auth
        return dsh_auth._site_host()
    except Exception:
        return ""


def site_info(site, dsh_st=None):
    """组装单个站点的展示信息（脱敏）。"""
    host = site.get("host") or ""
    kind = site.get("kind") or "?"
    need = needs_login(site)
    info = {
        "host": host,
        "label": site.get("label") or "",
        "kind": kind,
        "enabled": bool(site.get("enabled", True)),
        "needs_login": need,
        "login_url": site.get("login_url") or "",
        # ★ 2026-10-08：代理配置（站点账号页显示/编辑用）。空 = 直连。
        "proxy": site.get("proxy") or site.get("proxy_url") or "",
        # 默认：不需要登录态的站直接标记为「无需登录」
        "status": "no_login_needed" if not need else "unknown",
        "status_text": "无需登录态（用 API key 采集）" if not need else "未配置",
        "token_ok": None,
        "expires_in_h": None,
        "can_autorelogin": None,
        "email": "",
        "cred_source": "",
        "last_refresh": "",
    }
    if not need:
        return info

    # 需要登录态 → 取对应实现的状态
    if kind == "sub2api":
        # ★ 2026-10-07：按站取自己的 token 状态（多站改造后互不干扰）
        if dsh_st is None:
            st = _dsh_status(host=host)
        elif host and host in (dsh_st or {}):
            st = dsh_st[host]
        else:
            st = dsh_st if isinstance(dsh_st, dict) else {}
        if st.get("error"):
            info["status"] = "error"
            info["status_text"] = "状态读取失败：" + st["error"]
            return info
        # 该站有没有自己的 token 槽位？没有 = 还没登录过
        try:
            try:
                from . import dsh_auth
            except ImportError:
                import dsh_auth
            own = dsh_auth.load(host=host) if host else {}
        except Exception:
            own = {}
        if not own.get("access_token"):
            info["status"] = "no_cred"
            info["status_text"] = "尚未登录（这个站还没填过账号密码）"
            return info
        info["token_ok"] = bool(st.get("token_ok"))
        info["expires_in_h"] = st.get("expires_in_h")
        info["can_autorelogin"] = bool(st.get("can_autorelogin"))
        info["email"] = st.get("email") or ""
        info["cred_source"] = st.get("cred_source") or ""
        info["last_refresh"] = st.get("last_refresh") or ""
        if st.get("token_ok"):
            info["status"] = "ok"
            info["status_text"] = "登录态正常　剩 %.1f 小时" % (st.get("expires_in_h") or 0)
        else:
            info["status"] = "expired"
            info["status_text"] = "登录态失效"
        return info

    # newapi：当前实现不需要登录态；若配置要求，则标记为「未实现」而非假装正常
    if kind == "newapi":
        info["status"] = "unsupported"
        info["status_text"] = "该站配置要求登录态，但 NewAPI 登录适配尚未实现"
        return info

    info["status"] = "unsupported"
    info["status_text"] = "该站 kind=%s，暂无登录适配" % kind
    return info


def list_sites():
    """全部站点 + 登录态（供「站点账号」页列表）。"""
    sites = _load_sites()
    # ★ 2026-10-07：多站改造后每个站有自己的 token 状态，一次性取全
    per_site = {}
    if any(needs_login(s) for s in sites):
        try:
            try:
                from . import dsh_auth
            except ImportError:
                import dsh_auth
            for h, st in (dsh_auth.all_sites() or {}).items():
                per_site[h] = dsh_auth.cred_status(host=h)
        except Exception:
            per_site = {}
    return [site_info(s, per_site or None) for s in sites]


def find_site(host):
    """按 host 找站点配置。"""
    for s in _load_sites():
        if (s.get("host") or "") == host:
            return s
    return None


def login(host, email, password):
    """用凭据登录一次（目前仅 sub2api 实现）。

    返回 (ok: bool, msg: str, status: dict)

    ★ 2026-10-07：多站改造 —— 把 host 透传给 dsh_auth.login()，
      每个站的 token 各存各的（以前只有一个槽位，第二个站登不了）。
    """
    site = find_site(host)
    if not site:
        return False, "找不到站点 %s" % host, {}
    kind = site.get("kind") or ""
    if not needs_login(site):
        return False, "该站不需要登录态（kind=%s，用 API key 采集）" % kind, site_info(site)
    if kind == "sub2api":
        try:
            try:
                from . import dsh_auth
            except ImportError:
                import dsh_auth
            # ★ 2026-10-08 修（域名泄漏）：按 host 反查出本站域名再登录。
            #   不传 base 时 dsh_auth 也会自己按 host 反查；这里显式给出更直观。
            _base = None
            try:
                try:
                    from . import site_resolver as _sr3
                except ImportError:
                    import site_resolver as _sr3
                _b = _sr3.bases_for_host(host)
                _base = _b[0] if _b else None
            except Exception:
                _base = None
            if not _base:
                return False, "站点 %s 在 sites.json 里没有可用域名（bases/base_url）" % host, \
                       site_info(site)
            st = dsh_auth.login(email, password, log=lambda m: None,
                                host=host, base=_base)
            if st and st.get("access_token"):
                return True, "已保存并登录成功，逐条流水采集恢复", site_info(site)
            return False, "登录验证失败（邮箱/密码是否正确？）", site_info(site)
        except Exception as e:
            return False, "登录异常：%s" % str(e)[:120], site_info(site)
    return False, "kind=%s 的登录适配尚未实现" % kind, site_info(site)


if __name__ == "__main__":
    import json as _j
    print("=== 全部站点登录态 ===")
    for s in list_sites():
        print(_j.dumps(s, ensure_ascii=False, indent=2))
