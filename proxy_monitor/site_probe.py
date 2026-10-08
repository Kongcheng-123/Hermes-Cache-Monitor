# -*- coding: utf-8 -*-
"""site_probe.py — 中转站类型自动探测（2026-10-01）

为什么需要它
------------
以前加一个站要手工填 kind（newapi / sub2api），填错就跑不通。
实测发现【网站类型可以自动判出来】，规律很稳：

    NewAPI  : GET {root}/api/status   → 返回体含 quota_per_unit
    sub2api : GET {root}/v1/sub2api/billing → 返回体含 rate_multiplier
              （未带鉴权时返回 401，但 401 本身就说明"这个路由存在"→ 也算命中）

实测（2026-10-01，18 个候选站）：12 个自动判出（7 NewAPI + 5 sub2api）。
判不出的那 6 个不是失败，是它们本来就不是标准中转站
（自建服务 / 官方 API / 路径特殊的站）。

设计原则
--------
· 探测只发**无鉴权 GET**，不消耗额度、不留痕。
· 探测失败**不抛异常**，返回 unknown + 原因，让上层决定怎么处理。
· 结果只是"建议"，sites.json 里显式写的 kind 永远优先（人工压过自动）。
"""
import json
import re
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 这些是官方站，不是中转站，没必要探
OFFICIAL_HINTS = (
    "api.deepseek.com", "openrouter.ai", "generativelanguage.googleapis.com",
    "api.anthropic.com", "dashscope.aliyuncs.com", "api.openai.com",
    "api.moonshot.cn", "open.bigmodel.cn",
)


def strip_api_suffix(base_url):
    """把 base_url 去掉 /v1 /v1beta 等 API 后缀，得到站点根

    例：https://api.xxx.com/v1  →  https://api.xxx.com
    """
    b = (base_url or "").strip().rstrip("/")
    b = re.sub(r"/(v1beta|v1|compatible-mode|api/v1)$", "", b, flags=re.I)
    return b


def _get(url, timeout=10, fast=False):
    """无鉴权 GET，返回 (status, body)；失败返回 (None, 错误描述)

    fast=True 时用更短的超时（3 秒）—— 探测场景下「没有这个路由」的站
    会硬等满超时才返回，把超时压短能显著提升体验（实测 20s → 4s）。
    探测只求「有没有特征路由」，3 秒足够；真连不上的站给再多时间也没用。
    """
    if fast:
        timeout = min(timeout, 3)
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:100])


def probe_type(base_url, timeout=10):
    """探测站点类型

    返回 dict:
      {
        "kind":   "newapi" | "sub2api" | "official" | "unknown",
        "reason": 人话依据（用于日志/报错提示）,
        "root":   站点根 URL,
      }
    """
    root = strip_api_suffix(base_url)
    if not root:
        return {"kind": "unknown", "reason": "base_url 为空", "root": ""}

    low = root.lower()
    if any(h in low for h in OFFICIAL_HINTS):
        return {"kind": "official", "reason": "官方站（非中转站），无需监控", "root": root}

    # ── 1. NewAPI 特征先探（★ 2026-10-07 调整顺序）──
    #    /api/status 是轻量只读接口，响应普遍在 1 秒内；
    #    而 /v1/sub2api/billing 在不存在的路径上会硬等满超时。
    #    实测：先探 sub2api 时，一个 NewAPI 站要等 20 秒才出结果。
    st, body = _get(root + "/api/status", timeout, fast=True)
    if st == 200 and "quota_per_unit" in (body or ""):
        return {"kind": "newapi", "reason": "/api/status 返回 quota_per_unit", "root": root}

    # ── 2. sub2api 特征：/v1/sub2api/billing 存在 ──
    st, body = _get(root + "/v1/sub2api/billing", timeout, fast=True)
    if st == 200 and "multiplier" in (body or ""):
        return {"kind": "sub2api", "reason": "/v1/sub2api/billing 返回 rate_multiplier", "root": root}
    if st == 401:
        # 401 说明路由存在、只是要鉴权 —— 这就是 sub2api 的招牌
        return {"kind": "sub2api", "reason": "/v1/sub2api/billing 返回 401（路由存在，需鉴权）", "root": root}

    # ── 3. 兜底：NewAPI 的 /api/status 有时不公开 quota_per_unit，但有 data 结构 ──
    st, body = _get(root + "/api/status", timeout, fast=True)
    if st == 200 and body:
        try:
            j = json.loads(body)
            if isinstance(j, dict) and ("data" in j or "success" in j):
                return {"kind": "newapi",
                        "reason": "/api/status 结构像 NewAPI（有 data/success，但未公开 quota_per_unit）",
                        "root": root}
        except Exception:
            pass

    return {"kind": "unknown",
            "reason": "两个特征接口都没命中（可能非标准中转站，或需单独适配）",
            "root": root}


def probe_with_key(base_url, api_key, timeout=10):
    """带 key 探测 —— 比无鉴权探测更准（有些站要登录才暴露特征）

    在无鉴权探测出 unknown 时调用。只发只读 GET，不改动任何数据。
    """
    root = strip_api_suffix(base_url)
    if not root or not api_key:
        return {"kind": "unknown", "reason": "缺 base_url 或 key", "root": root}

    hdr = {"User-Agent": UA, "Authorization": "Bearer " + api_key}

    def _g(u):
        try:
            r = urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=timeout)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception:
                return e.code, ""
        except Exception as e:
            return None, "%s: %s" % (type(e).__name__, str(e)[:100])

    # sub2api：带 key 后 billing 会直接返回倍率
    st, body = _g(root + "/v1/sub2api/billing")
    if st == 200 and "multiplier" in (body or ""):
        return {"kind": "sub2api", "reason": "带 key 访问 /v1/sub2api/billing 成功", "root": root}

    # sub2api：/v1/usage 带 key 能通（返回 balance/planName 结构）
    st, body = _g(root + "/v1/usage?days=1")
    if st == 200 and body:
        if "balance" in body or "planName" in body or "daily_usage" in body:
            return {"kind": "sub2api", "reason": "带 key 访问 /v1/usage 返回聚合结构", "root": root}

    # NewAPI：/api/log/token 是逐条流水接口
    st, body = _g(root + "/api/log/token?p=1&page_size=1")
    if st == 200 and body:
        if "data" in body or "quota" in body:
            return {"kind": "newapi", "reason": "带 key 访问 /api/log/token 成功", "root": root}

    return {"kind": "unknown", "reason": "带 key 探测也未命中", "root": root}


def normalize_site(raw, provs=None):
    """把一条「极简配置」补全成运行时的完整站点配置

    输入（用户在 sites.json 里只需写这么多）：
        {"base_url": "https://api.xxx.com/v1", "api_key": "sk-...", "label": "可省"}
    或引用 config.yaml 里的 provider：
        {"base_url": "https://api.xxx.com/v1", "hermes_providers": ["custom:xxx"]}

    输出：补上 host / kind / label / quota_per_cny 等字段的完整 dict。
    kind 若已显式给出则【不覆盖】—— 人工判断优先于自动探测。
    """
    s = dict(raw)
    base = (s.get("base_url") or "").rstrip("/")
    root = strip_api_suffix(base)
    s["base_url"] = base

    # host：优先显式，否则从 base_url 推
    if not s.get("host"):
        try:
            from urllib.parse import urlparse
            s["host"] = urlparse(root).netloc or root
        except Exception:
            s["host"] = root

    # kind：显式优先；没写才探测
    if not s.get("kind"):
        probe = probe_type(base)
        if probe["kind"] in ("unknown", "official"):
            # 无鉴权探不出 → 带 key 再试一次
            key = s.get("api_key") or ""
            if not key and provs:
                for pname in (s.get("hermes_providers") or []):
                    p = provs.get(pname)
                    if p and p.get("api_key"):
                        key = p["api_key"]
                        break
            if key:
                probe2 = probe_with_key(base, key)
                if probe2["kind"] != "unknown":
                    probe = probe2
        s["kind"] = probe["kind"]
        s["_probe"] = probe.get("reason", "")
    else:
        s["_probe"] = "显式指定"

    s.setdefault("label", s["host"])
    s.setdefault("enabled", True)
    s.setdefault("extra_keys", [])
    s.setdefault("hermes_providers", [])
    return s


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="探测中转站类型")
    ap.add_argument("urls", nargs="+", help="一个或多个 base_url")
    a = ap.parse_args()
    for u in a.urls:
        p = probe_type(u)
        print("%-46s %-9s %s" % (strip_api_suffix(u)[:46], p["kind"], p["reason"]))
