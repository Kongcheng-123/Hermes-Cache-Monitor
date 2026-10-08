# -*- coding: utf-8 -*-
r"""site_admin.py — 站点配置的增删改（供 8788「加站界面」调用）2026-10-07

职责边界
--------
· 只管 **sites.json**（站点配置）。凭据是 credentials.py 的事，互不越界。
· 写盘一律「先备份 → 校验 → 原子替换」，任何一步失败都不动原文件。
· 不做删站 —— 删除牵连 store.db 四张表，只提供「停用」（enabled=false）。

为什么单独一个文件
------------------
panel.py 已经 1000+ 行，写盘这种「有副作用」的逻辑塞进去不好测。
独立成模块后可以命令行直接验证，不必起面板。

关键决策
--------
1. **/v1 自动补全**：用户只填 https://api.xxx.com → 自动补成 .../v1。
   · 已经带 /v1 /v1beta 等后缀的不重复补
   · 明显不是 OpenAI 兼容形态的（含 /api/ 等）不动，原样保留
   · 判断不了就补 —— 绝大多数中转站是 OpenAI 兼容，这是概率最大的选择
2. **host 去重**：同 host 已存在则报错，不覆盖（防手滑把老站配置冲掉）
3. **探测结果缓存**：写入时把 probe 结果一并落进 _probe_cache，省得下次重探
"""

import json
import os
import re
import shutil
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SITES_PATH = os.path.join(HERE, "sites.json")

# ★ 2026-10-07 审计修复：panel.py 用 ThreadingHTTPServer，每个请求一个线程。
#   原来 add_site / set_enabled 是「读-改-写」且无锁，双击「确认加站」或
#   开两个标签页就会并发撞在一起 —— 实测 3 线程并发丢 2 条记录。
#   所有写操作整段持这把锁。
_WRITE_LOCK = threading.RLock()
BAK_PATH = SITES_PATH + ".bak"

# 已经是 API 版本后缀的形态（不用再补 /v1）
_HAS_VER = re.compile(r"/(v1beta|v1|v2|v3|api/v1|api/v2|compatible-mode|openai)\s*$", re.I)
# 端口号结尾的，如 http://1.2.3.4:8080
_ENDS_WITH_PORT = re.compile(r":\d+\s*$")


# ─────────────────────────── 读写 ───────────────────────────

def load_raw():
    """读 sites.json 原始结构（含 _probe_cache 等私有字段）。

    返回 dict；文件不存在/损坏返回 {"sites": []}。
    """
    if not os.path.isfile(SITES_PATH):
        return {"sites": []}
    try:
        with open(SITES_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"sites": []}
    # 兼容纯 list 格式
    if isinstance(d, list):
        return {"sites": d}
    if not isinstance(d.get("sites"), list):
        d["sites"] = []
    return d


def all_sites(include_disabled=True):
    """全部站点（默认含已停用的，加站界面要能看见停用站）。"""
    sites = load_raw().get("sites") or []
    out = []
    for s in sites:
        if not isinstance(s, dict):
            continue
        if not include_disabled and not s.get("enabled", True):
            continue
        out.append(s)
    return out


def find_site(host):
    """按 host 找站点（host 比对忽略大小写与首尾空白）。"""
    h = (host or "").strip().lower()
    for s in all_sites():
        if (s.get("host") or "").strip().lower() == h:
            return s
    return None


# ─────────────────────── base_url 归一化 ───────────────────────

def normalize_base_url(url):
    """把用户输入的站点地址规整成能用的 base_url。

    规则（按顺序）：
      1. 补协议：没写 http(s):// 的自动加 https://
      2. 去尾斜杠
      3. 补 /v1 —— 已有版本后缀的不补；端口结尾的直接补；
         含 /api/ 这种非 OpenAI 形态的不补（原样保留，避免拼错）

    返回 (base_url, note)；note 是人话说明（补了什么/为什么没补）。
    """
    u = (url or "").strip()
    if not u:
        return "", "地址为空"
    notes = []

    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
        notes.append("补了 https://")

    u = u.rstrip("/")

    # 已经是版本后缀 -> 不动
    if _HAS_VER.search(u):
        return u, "；".join(notes) if notes else ""

    # 路径里含 /api/ 或明显是别的形态 -> 保守，不补
    tail = u.split("://", 1)[-1]
    if "/" in tail:
        seg = "/" + tail.split("/", 1)[1]
        if re.search(r"/(api|admin|console|dashboard|user)/", seg, re.I):
            notes.append("路径含 /api/，未自动补 /v1（请自行核对）")
            return u, "；".join(notes)

    # 端口结尾 / 纯域名 -> 补 /v1
    u = u + "/v1"
    notes.append("自动补了 /v1")
    return u, "；".join(notes)


def host_from_url(url):
    """从 URL 抠 host（站点身份）。"""
    if not url:
        return ""
    s = str(url).split("://", 1)[-1]
    return s.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0].strip().lower()


# ─────────────────────────── 校验 ───────────────────────────

def validate(entry):
    """校验一条待写入的站点配置。返回 (ok, msg)。

    ★ 2026-10-07 审计加强：原来只查地址和域名格式，以下都能混进来 ——
      · quota_per_cny 传字符串 'abc' → 落盘后 site_collect 除法 TypeError，
        又被 try/except 吞成「无新数据」（就是 2026-10-06「被蒙 3 天」那个形态）
      · hermes_providers 数组里塞非字符串（[1, {...}]）→ 采集时当字典键用会炸
      · kind 传任意值 → 采集走错适配器
      · label 不限长 → 10 万字符也能落盘
    """
    base = (entry.get("base_url") or "").strip()
    if not base:
        return False, "站点地址不能为空"
    if not re.match(r"^https?://", base, re.I):
        return False, "站点地址必须以 http:// 或 https:// 开头"
    host = (entry.get("host") or "").strip()
    if not host:
        return False, "无法从地址解析出域名"
    if not re.match(r"^[a-zA-Z0-9._-]+$", host):
        return False, "域名格式不合法：%s" % host

    # hermes_providers：必须是数组，且元素全为字符串
    provs = entry.get("hermes_providers") or []
    if not isinstance(provs, list):
        return False, "hermes_providers 必须是数组"
    for x in provs:
        if not isinstance(x, str):
            return False, "hermes_providers 的元素必须是字符串（发现 %s）" % type(x).__name__
        if len(x) > 120:
            return False, "provider 名过长：%s..." % x[:40]

    # quota_per_cny：可为空（走默认），但给了就必须是正数
    q = entry.get("quota_per_cny")
    if q not in (None, ""):
        try:
            qf = float(q)
        except (TypeError, ValueError):
            return False, "quota_per_cny 必须是数字（现在是 %r）" % (q,)
        if qf <= 0:
            return False, "quota_per_cny 必须大于 0（现在是 %s）" % qf

    # kind：只认已知值（空 = 让探测决定）
    kind = (entry.get("kind") or "").strip().lower()
    if kind and kind not in ("newapi", "sub2api", "official", "unknown"):
        return False, "未知的 kind：%s（只支持 newapi / sub2api）" % kind

    # 文本长度上限（防止超长字符串落盘 + 前端渲染）
    for key, lim in (("label", 200), ("note", 2000), ("login_url", 500)):
        v = entry.get(key)
        if isinstance(v, str) and len(v) > lim:
            return False, "%s 过长（%d 字符，上限 %d）" % (key, len(v), lim)

    # ★ 2026-10-08 新增：代理地址校验（站点账号页可配，空 = 直连）
    #   允许形式：空 / 纯端口 '4512' / '127.0.0.1:4512' / 'http://127.0.0.1:4512'
    px = entry.get("proxy")
    if px not in (None, ""):
        if not isinstance(px, str):
            return False, "代理必须是字符串（现在是 %s）" % type(px).__name__
        pxs = px.strip()
        if pxs:
            if len(pxs) > 200:
                return False, "代理地址过长（上限 200 字符）"
            if pxs.isdigit():
                if not (1 <= int(pxs) <= 65535):
                    return False, "代理端口超出范围（1-65535）"
            else:
                body = pxs.split("://", 1)[-1]
                if not re.match(r"^[a-zA-Z0-9._-]+(:\d+)?$", body):
                    return False, ("代理格式不对：%s\n"
                                   "可填：4512 或 127.0.0.1:4512 或 http://127.0.0.1:4512"
                                   % pxs)

    return True, "ok"


# ─────────────────────────── 写盘 ───────────────────────────

def _backup():
    """写前备份（每次覆盖，保留最近一次的好配置）。"""
    try:
        if os.path.isfile(SITES_PATH):
            shutil.copy2(SITES_PATH, BAK_PATH)
            return True
    except Exception:
        pass
    return False


def _atomic_write(data):
    """原子替换写盘：先写 .tmp 再 os.replace（断电不会留半截文件）。"""
        # ★ 2026-10-07：tmp 名带 pid+线程 id，避免两个写者撞同一个文件
    tmp = "%s.tmp.%d.%d" % (SITES_PATH, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SITES_PATH)


def add_site(entry, probe=None):
    """新增一个站点。

    entry: {base_url, hermes_providers[], label?, kind?, note?}
    probe: 可选，探测结果 {kind, reason, root} —— 会写进 _probe_cache

    返回 (ok, msg, site_dict)

    ★ 2026-10-07：整段加锁（读-改-写必须原子）。
    """
    with _WRITE_LOCK:
        return _add_site_locked(entry, probe)


def _add_site_locked(entry, probe=None):
    s = dict(entry)
    raw = load_raw()
    sites = raw.get("sites") or []

    ok, msg = validate(s)
    if not ok:
        return False, msg, None

    host = (s.get("host") or "").strip().lower()
    # 去重：同 host 已存在 -> 拒绝（不覆盖老配置）
    if find_site(host):
        return False, "该站点已存在（%s）。如需修改请用「停用/启用」，不要重复添加。" % host, None

    # 组装完整条目
    try:
        q = float(s.get("quota_per_cny") or 500000)
    except (TypeError, ValueError):
        q = 500000.0
    row = {
        "host": host,
        "base_url": s["base_url"],
        "hermes_providers": s.get("hermes_providers") or [],
        "extra_keys": s.get("extra_keys") or [],
        "quota_per_cny": q,
        "enabled": True if s.get("enabled") is None else bool(s.get("enabled")),
        "label": (s.get("label") or "").strip() or host,
    }
    if s.get("kind"):
        row["kind"] = s["kind"]
    if s.get("note"):
        row["note"] = s["note"]
    if s.get("login_url"):
        row["login_url"] = s["login_url"]
    # ★ 2026-10-08：代理（空/未填 = 不写该字段，保持配置干净）
    _px = (s.get("proxy") or "").strip() if isinstance(s.get("proxy"), str) else ""
    if _px:
        row["proxy"] = _px
    row["_added_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    _backup()
    sites.append(row)
    raw["sites"] = sites

    # 探测结果入缓存（key 用 base_url，与 site_probe 的缓存口径一致）
    if probe and probe.get("kind"):
        cache = raw.get("_probe_cache") or {}
        cache[s["base_url"]] = {
            "kind": probe["kind"],
            "reason": probe.get("reason") or "加站时探测",
            "probed_at": int(time.time()),
        }
        raw["_probe_cache"] = cache

    _atomic_write(raw)
    return True, "已加入 %s" % host, row


def set_enabled(host, enabled):
    """启用/停用一个站点（不删除，避免牵连 store.db）。

    ★ 2026-10-07：整段加锁。
    """
    with _WRITE_LOCK:
        raw = load_raw()
        sites = raw.get("sites") or []
        h = (host or "").strip().lower()
        for s in sites:
            if isinstance(s, dict) and (s.get("host") or "").strip().lower() == h:
                _backup()
                s["enabled"] = bool(enabled)
                raw["sites"] = sites
                _atomic_write(raw)
                return True, "已%s %s" % ("启用" if enabled else "停用", host)
        return False, "找不到站点 %s" % host


def set_proxy(host, proxy):
    """★ 2026-10-08 新增：设置/清除某站的代理。

    空字符串 = 清除代理（改回直连）。
    返回 (ok, msg)。

    为什么放这儿：代理是【站点属性】（跟 base_url / kind 同类），
    存 sites.json 而不是 credentials.json —— 前者语义正确，
    且开源同步会自动带上（代理地址不是敏感信息）。
    """
    with _WRITE_LOCK:
        raw = load_raw()
        sites = raw.get("sites") or []
        h = (host or "").strip().lower()
        px = (proxy or "").strip()
        # 校验：借用 validate 的规则（构造一个最小 entry 只验代理）
        ok, msg = validate({"base_url": "http://x.com", "host": "x.com", "proxy": px})
        if not ok:
            return False, msg
        for s in sites:
            if isinstance(s, dict) and (s.get("host") or "").strip().lower() == h:
                _backup()
                if px:
                    s["proxy"] = px
                else:
                    s.pop("proxy", None)
                raw["sites"] = sites
                _atomic_write(raw)
                return True, ("已为 %s 设置代理 %s" % (host, px)) if px \
                    else ("已清除 %s 的代理（改回直连）" % host)
        return False, "找不到站点 %s" % host


def add_base_domain(host, domain):
    """★ 2026-10-09 新增：给某站补登一个入口域名（写进 sites.json 的 bases）。

    用途（用户场景）：换中转站入口后（dshapi 从 api2 换到 api9），
    Hermes 记的 provider 会退化成裸 `custom`，若 api9 又没登记在 bases 里
    → 该会话之后的流水全部归集不上（实测 1627 条 none）。
    面板的「归集健康度」条会指出没登记的域名，用户点一下补登即调本函数。

    入参 domain 可以是裸域名（api9.dshapi.icu）或完整 URL，都会归一成
    https://<host> 存进 bases。已存在则直接返回成功（幂等）。

    返回 (ok, msg)。写盘走「备份 → 原子替换」，与其它写操作一致。
    """
    with _WRITE_LOCK:
        raw = load_raw()
        sites = raw.get("sites") or []
        h = (host or "").strip().lower()
        d = (domain or "").strip()
        if not d:
            return False, "域名不能为空"
        # 归一成 https://<host> 形式
        if "://" not in d:
            d = "https://" + d
        d = d.rstrip("/")
        from urllib.parse import urlparse
        try:
            netloc = urlparse(d).netloc.lower()
        except Exception:
            netloc = ""
        if not netloc:
            return False, "域名格式不对：%s" % domain
        ok, msg = validate({"base_url": d, "host": netloc})
        if not ok:
            return False, msg

        for s in sites:
            if not isinstance(s, dict):
                continue
            if (s.get("host") or "").strip().lower() == h:
                bases = list(s.get("bases") or [])
                # 幂等：已登记就直接说一声
                for b in bases:
                    try:
                        if urlparse(b if "://" in b else "https://" + b).netloc.lower() == netloc:
                            return True, "该地址已在 %s 的备选域名里" % host
                    except Exception:
                        continue
                _backup()
                bases.append(d)
                s["bases"] = bases
                raw["sites"] = sites
                _atomic_write(raw)
                return True, "已把 %s 补登到 %s" % (netloc, host)
        return False, "找不到站点 %s" % host


def edit_site(host, bases=None, providers=None, proxy=None, label=None, note=None):
    """★ 2026-10-09 新增：编辑已有站点的配置（站点配置页用）。

    只改传入的字段（None = 不动）。用户最常用的两个：
      · bases      —— 备选入口域名（换入口后归集认不出来就在这里补）
      · providers  —— Hermes 记录的 provider 名（换地址后可能退化成裸 custom）
    写盘走「备份 → 原子替换」，返回 (ok, msg)。
    """
    with _WRITE_LOCK:
        raw = load_raw()
        sites = raw.get("sites") or []
        h = (host or "").strip().lower()
        for s in sites:
            if not isinstance(s, dict):
                continue
            if (s.get("host") or "").strip().lower() != h:
                continue
            changed = []
            _backup()
            if bases is not None:
                bl = []
                for b in (bases if isinstance(bases, list) else []):
                    b = str(b).strip()
                    if not b:
                        continue
                    if "://" not in b:
                        b = "https://" + b
                    b = b.rstrip("/")
                    if b not in bl:
                        bl.append(b)
                s["bases"] = bl
                changed.append("备选域名 %d 个" % len(bl))
            if providers is not None:
                pl = [str(p).strip() for p in (providers if isinstance(providers, list) else [])
                      if str(p).strip()]
                s["hermes_providers"] = pl
                changed.append("provider %d 个" % len(pl))
            if proxy is not None:
                px = str(proxy).strip()
                if px:
                    ok, msg = validate({"base_url": "http://x.com", "host": "x.com",
                                        "proxy": px})
                    if not ok:
                        return False, msg
                    s["proxy"] = px
                else:
                    s.pop("proxy", None)
                changed.append("代理 %s" % (px or "（直连）"))
            if label is not None:
                s["label"] = str(label).strip()
                changed.append("别名")
            if note is not None:
                s["note"] = str(note).strip()
                changed.append("备注")
            raw["sites"] = sites
            _atomic_write(raw)
            return True, "已更新 %s：%s" % (host, "、".join(changed) or "无改动")
        return False, "找不到站点 %s" % host


def clear_probe_cache(base_url):
    """改地址后清掉旧地址的探测缓存（加站不需要调这个）。"""
    with _WRITE_LOCK:
        raw = load_raw()
        cache = raw.get("_probe_cache") or {}
        if base_url in cache:
            _backup()
            del cache[base_url]
            raw["_probe_cache"] = cache
            _atomic_write(raw)
            return True
        return False


# ───────────────────── 读 Hermes provider 列表 ─────────────────────
# ★ 2026-10-07：加站界面要能「从下拉里挑 provider」—— 拿 exe 的人
#    不知道 custom:xxx 该写什么，硬让他手输等于没解决门槛问题。
#    这里从 Hermes 的 config.yaml 读出全部 custom_providers 的名字 + base_url，
#    按 base_url 跟待加的站做匹配，把「最可能是这个站」的排前面。

HERMES_CONFIG_CANDIDATES = [
    r"D:\Hermes Agent CN Desktop\data\hermes-home\config.yaml",
    os.path.expanduser("~/.hermes/config.yaml"),
]


def _find_config():
    """找 Hermes 的 config.yaml（找不到返回空串，不抛异常）。"""
    # 环境变量优先（开源版/换机器时用）
    env = os.environ.get("HERMES_CONFIG")
    if env and os.path.isfile(env):
        return env
    for p in HERMES_CONFIG_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def list_hermes_providers():
    """列出 Hermes config.yaml 里的 custom_providers。

    返回 [{"name": "custom:xxx", "base_url": "...", "label": "..."}]；
    读不到返回 []（绝不抛异常 —— 界面拿不到列表只是少个便利功能）。

    ★ 2026-10-07 重写：原来纯手写浅解析，实测有三个 bug ——
      ① `"custom:quoted"` 带引号的 provider 名整个漏掉；
      ② 缩进异常时 base_url 会串到**别的** provider 上（名和地址对错，最危险）；
      ③ 会误收 custom_providers 之外的段（如顶层 other_section 下的 custom:xxx）。
      现在优先用 pyyaml 正规解析；没有 yaml 时退回「改进版浅解析」并保守跳过可疑行。
    """
    path = _find_config()
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return []

    # ① 正规路径：pyyaml
    #   ⚠️ 段名实测是 `providers`（2026-10-07 在 CN 桌面版 config.yaml 上确认，23 个 custom: 键）。
    #      文档里写的 `custom_providers` 是**老版本字段名**，现在只剩兼容价值。
    try:
        import yaml
        data = yaml.safe_load(txt)
        if isinstance(data, dict):
            for sec in ("providers", "custom_providers"):
                provs = data.get(sec)
                if isinstance(provs, dict):
                    out = []
                    for name, val in provs.items():
                        if not isinstance(name, str) or not name.startswith("custom:"):
                            continue
                        v = val if isinstance(val, dict) else {}
                        out.append({
                            "name": name.strip(),
                            "base_url": str(v.get("base_url") or "").strip(),
                            "label": str(v.get("name") or "").strip(),
                        })
                    if out:
                        return out
    except Exception:
        pass                                   # yaml 不可用/解析失败 → 走浅解析

    # ② 兜底：浅解析（改进版）
    return _shallow_providers(txt)


def _shallow_providers(txt):
    """没有 pyyaml 时的兜底解析（保守：只在 custom_providers 段内、缩进严格时收）。

    比原版严格的地方：
      · 先定位 `custom_providers:` 这一行，只解析它下面的内容（不误收别的段）
      · provider 名的引号会被剥掉（支持 "custom:xxx" / 'custom:xxx'）
      · 只认「刚好比 custom_providers 缩进深一级」的键，缩进乱的直接跳过（宁可漏，不可对错）
    """
    lines = txt.splitlines()

    # 找 providers 段（兼容老字段名 custom_providers）
    start = None
    base_indent = None
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not s.endswith(":"):
            continue
        key = s.rstrip(":").strip()
        if key in ("providers", "custom_providers"):
            start = i
            base_indent = len(line) - len(line.lstrip())
            break
    if start is None:
        return []

    def _strip_quotes(s):
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            return s[1:-1]
        return s

    out = []
    cur = None
    for line in lines[start + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        # 出了 custom_providers 段（缩进 <= 段头）→ 停
        if indent <= base_indent:
            break
        s = line.strip()
        # provider 键：缩进恰好是段头 + 2
        if indent == base_indent + 2 and s.endswith(":") and ":" in s[:-1]:
            name = _strip_quotes(s[:-1])
            if name.startswith("custom:"):
                cur = {"name": name, "base_url": "", "label": ""}
                out.append(cur)
            else:
                cur = None                     # 不是 custom: 开头的，忽略其后子键
            continue
        # provider 属性：必须比 provider 键再深一级，且当前有 cur
        if cur is not None and indent >= base_indent + 4:
            if s.startswith("base_url:"):
                # 已拿到过就不再覆盖（防止缩进异常时被后面的值串掉）
                if not cur["base_url"]:
                    cur["base_url"] = _strip_quotes(s.split(":", 1)[1])
            elif s.startswith("name:") and not cur["label"]:
                cur["label"] = _strip_quotes(s.split(":", 1)[1])
        elif indent == base_indent + 2:
            # 同级的非 provider 键 → 结束当前项
            cur = None
    return out


def providers_for(host):
    """挑出「最可能是这个站」的 provider（按 base_url 的 host 匹配）。

    返回 (matched, others)：命中的列表 + 其余全部（供界面完整下拉）。
    """
    allp = list_hermes_providers()
    if not host:
        return [], allp
    h = host.strip().lower()
    matched, others = [], []
    for p in allp:
        bu = (p.get("base_url") or "").lower()
        if h in bu:
            matched.append(p)
        else:
            others.append(p)
    return matched, others


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="站点配置管理（自测用）")
    ap.add_argument("--list", action="store_true", help="列出全部站点")
    ap.add_argument("--norm", help="归一化一个地址（只看结果不写盘）")
    ap.add_argument("--add", help="加一个站（仅测试，会真写盘）")
    ap.add_argument("--label", default="", help="配合 --add")
    a = ap.parse_args()

    if a.norm:
        b, note = normalize_base_url(a.norm)
        print("输入 : %s" % a.norm)
        print("结果 : %s" % b)
        print("说明 : %s" % (note or "无需改动"))
        print("host : %s" % host_from_url(b))
    elif a.add:
        b, note = normalize_base_url(a.add)
        ok, msg, row = add_site({"base_url": b, "label": a.label or ""})
        print("%s  %s" % ("OK " if ok else "ERR", msg))
        if row:
            print(json.dumps(row, ensure_ascii=False, indent=2))
    else:
        for s in all_sites():
            print("%-28s %-9s %-10s %s" % (
                s.get("host", "?"), s.get("kind") or "?",
                "启用" if s.get("enabled", True) else "停用",
                s.get("label") or ""))
