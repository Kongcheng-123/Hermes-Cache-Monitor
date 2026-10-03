"""site_resolver.py — 域名/站点解析统一入口（配置驱动，代码零硬编码）

设计目的（2026-10-04）：
  之前 `dsh_flows.py` / `dsh_auth.py` / `cache_follow.py` 里散落着硬编码域名，
  别人 clone 下来必须改代码才能用。本模块把「站点是谁」全部收口到
  `sites.json`，代码一律通过这里的函数取域名。

配置来源（优先级从高到低）：
  1. 环境变量 HERMES_SITES_JSON 指向的 json
  2. <本文件目录>/sites.json
  3. <本文件目录>/sites.example.json（只读兜底，保证不崩）

函数约定：
  所有函数在读不到配置时返回空值（"" / []），绝不抛异常 —— 调用方自己判断。
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_KEY = "HERMES_SITES_JSON"
_CACHE = {"path": None, "mtime": None, "data": None}


def _candidate_paths():
    env = os.environ.get(_ENV_KEY)
    out = []
    if env:
        out.append(env)
    out.append(os.path.join(HERE, "sites.json"))
    out.append(os.path.join(HERE, "sites.example.json"))
    return out


def _load():
    """读配置（带 mtime 缓存，配置改了自动重读）。"""
    for p in _candidate_paths():
        if not os.path.isfile(p):
            continue
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = None
        if _CACHE["path"] == p and _CACHE["mtime"] == mt and _CACHE["data"] is not None:
            return _CACHE["data"]
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        # 兼容两种格式：{"sites": [...]} 或直接 [...]
        sites = raw.get("sites") if isinstance(raw, dict) else raw
        if not isinstance(sites, list):
            sites = []
        _CACHE.update(path=p, mtime=mt, data=sites)
        return sites
    _CACHE.update(path=None, mtime=None, data=[])
    return []


def all_sites():
    """全部站点配置（list of dict），过滤掉未启用的。"""
    return [s for s in _load() if isinstance(s, dict) and s.get("enabled", True)]


def sites_of_kind(kind):
    """按架构筛选：kind='newapi' / 'sub2api'。"""
    kind = (kind or "").lower()
    out = []
    for s in all_sites():
        k = (s.get("kind") or "").lower()
        if k == kind:
            out.append(s)
    return out


def default_newapi_site():
    """默认的 NewAPI 类站点（第一个启用的）。返回 dict 或 None。"""
    lst = sites_of_kind("newapi")
    if lst:
        return lst[0]
    # kind 未标时，靠 base_url 里不含 sub2api 特征来猜
    for s in all_sites():
        if "sub2api" not in (s.get("note") or "").lower():
            return s
    return None


def default_sub2api_site():
    """默认的 sub2api 类站点。返回 dict 或 None。"""
    lst = sites_of_kind("sub2api")
    if lst:
        return lst[0]
    for s in all_sites():
        if "sub2api" in (s.get("note") or "").lower():
            return s
    return None


def host_of(site):
    """站点身份（库里的 site 字段）。"""
    if not isinstance(site, dict):
        return ""
    h = site.get("host") or ""
    if h:
        return h
    # 没写 host 就从 base_url 抠
    return host_from_url(site.get("base_url") or "")


def host_from_url(url):
    """从 URL 抠主机名。"""
    if not url:
        return ""
    s = str(url).split("://", 1)[-1]
    return s.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]


def bases_of(site):
    """请求域名池（含协议）。优先 bases 字段，其次 base_url 的 origin。"""
    if not isinstance(site, dict):
        return []
    out = []
    for b in (site.get("bases") or []):
        b = str(b).strip().rstrip("/")
        if b and b not in out:
            out.append(b)
    origin = origin_of(site.get("base_url") or "")
    if origin and origin not in out:
        out.append(origin)
    return out


def origin_of(url):
    """从 URL 抠 origin（协议+主机）。"""
    if not url:
        return ""
    u = str(url).strip().rstrip("/")
    if "://" not in u:
        return ""
    scheme, rest = u.split("://", 1)
    host = rest.split("/", 1)[0]
    return "%s://%s" % (scheme, host)


def alias_map():
    """站点别名归一表。

    来源（按优先级）：
      1. host_alias.json（用户自己写的 json）
      2. host_alias.example.json（模板）
      3. host_alias.py 里的 DEFAULT_ALIAS（老格式，兼容）
    """
    for p in (os.path.join(HERE, "host_alias.json"),
              os.path.join(HERE, "host_alias.example.json")):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            m = d.get("map") if isinstance(d, dict) else d
            if isinstance(m, dict) and m:
                return m
        except Exception:
            continue
    # 回退：从 host_alias.py 读 DEFAULT_ALIAS（不 import，避免循环/副作用）
    py = os.path.join(HERE, "host_alias.py")
    if os.path.isfile(py):
        try:
            src = open(py, encoding="utf-8").read()
            m = re.search(r"DEFAULT_ALIAS\s*=\s*\{", src)
            if m:
                # 截到配对的收尾大括号，再交给 ast 解析
                i = m.end() - 1
                depth = 0
                for j in range(i, len(src)):
                    if src[j] == "{":
                        depth += 1
                    elif src[j] == "}":
                        depth -= 1
                        if depth == 0:
                            import ast
                            return ast.literal_eval(src[i:j + 1]) or {}
        except Exception:
            pass
    return {}


# ── 兼容层：给老代码用的常量（启动时求值一次）────────────────
def legacy_site():
    """sub2api 站的【站点身份】。"""
    return host_of(default_sub2api_site()) or ""


def legacy_bases():
    """sub2api 站的【请求域名池】。"""
    return bases_of(default_sub2api_site())


def legacy_newapi_host():
    """NewAPI 站的 host。"""
    return host_of(default_newapi_site()) or ""
