# -*- coding: utf-8 -*-
"""dsh_auth.py — dshapi（sub2api 站）网页端登录态管理

背景（2026-10-01 实测）：
  · dshapi 有两套接口：
      ① sk- key 直连 /v1/usage   → 只有【聚合】数据（按日+按模型），无逐条 → 无法按对话归集
      ② 网页端 /api/v1/usage     → 【逐条流水】，带毫秒时刻 + request_id → 可归集 ★
    ② 需要登录态 JWT，不是 sk- key。
  · 认证流程（从网页 JS 里挖出来的真实逻辑）：
      POST /api/v1/auth/refresh  {"refresh_token": "rt_xxx"}
      → {code:0, data:{access_token, refresh_token, expires_in, token_type}}
      access_token 有效期 86400 秒（24 小时）
  · ⚠️ refresh_token 是【一次性】的（强轮换）：用一次就作废，必须立刻存下新值。
    实测：同一个 refresh_token 第二次调用 → 401 invalid refresh token。

本模块职责：持有 dshapi 的 access_token / refresh_token，
         自动续期，落盘持久化，供采集器复用。

首次使用需要「喂一次」凭据：运行 python dsh_auth.py --import
  （会从浏览器 localStorage 读，走 Kimi WebBridge；或手填）
之后完全自持，不再需要浏览器。

⚠️ 与浏览器共用同一账号时注意：本模块刷新会作废浏览器里的 refresh_token，
   因此刷新后会把新 token 反向写回浏览器，保证两边一致（sync_to_browser）。
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "dsh_auth.json")
# ⚠️ 主站域名故障切换（2026-10-01 实测）
#   主站 api.dshapi.icu 曾整站 TLS 握手失败，副站 api2.dshapi.icu 正常。
#   两者是【同一账号的不同入口】，凭据与接口完全通用 → 做成域名池，谁通用谁。
#   顺序即优先级：首选配置里排第一的（当前在用），失败回退下一个。
#
# 2026-10-04 改造：域名从 sites.json 读，代码零硬编码。
try:
    from . import site_resolver as _sr
except ImportError:
    import site_resolver as _sr

_S2 = _sr.default_sub2api_site()
BASES = _sr.bases_of(_S2) or ["https://api.example.com"]
BASE = BASES[0]          # 兼容旧引用；实际请求走 _base()
SITE_HOST = _sr.host_from_url(BASES[0]) or "example.com"   # 仅用于提示文字
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# access_token 提前续期的余量（秒）——剩不到这么多就先换
RENEW_MARGIN = 3600


def _base():
    """探测哪个域名当前可用（结果缓存 5 分钟，避免每轮都探）"""
    now = time.time()
    if _base.cache and now - _base.cache[1] < 300:
        return _base.cache[0]
    for b in BASES:
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(b + "/", headers={"User-Agent": UA}), timeout=8)
            if r.status < 500:
                _base.cache = (b, now)
                return b
        except Exception:
            continue
    _base.cache = (BASES[0], now)      # 都不通也给首选，让上层拿到明确错误
    return BASES[0]


_base.cache = None


def _post(path, body, tok=None, timeout=30, base=None, proxy=None):
    """POST 到指定站点域名。

    ★ 2026-10-08 修（域名泄漏，最后一层封堵）：
      base 不再允许省略。原来 `(base or _base())` 会静默回退到模块级 BASES
      （= 第一个 sub2api 站），任何漏传 base 的调用都会打到那个站上。
      现在缺 base 直接抛错 —— 宁可报错，绝不把 A 站的请求发到 B 站。
      （_base() 保留给「没有站点上下文」的探测场景，不再作为默认值使用。）

    ★ 2026-10-08 加 proxy（按站代理）：部分站直连不通必须挂梯子。
      空/None = 直连，向后兼容。
    """
    if not base:
        raise ValueError(
            "_post 缺少 base（本站请求域名）。"
            "请用 _resolve_base(host) 解析后再调用；绝不兜底到别的站。")
    hdr = {"User-Agent": UA, "Content-Type": "application/json"}
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(base.rstrip("/") + path,
                                 data=json.dumps(body).encode(),
                                 headers=hdr, method="POST")
    try:
        r = _open(req, timeout=timeout, proxy=proxy)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])


def _get(path, tok, timeout=30, base=None, proxy=None):
    """GET 指定站点域名（同样：base 必填，缺了直接抛错）。"""
    if not base:
        raise ValueError(
            "_get 缺少 base（本站请求域名）。"
            "请用 _resolve_base(host) 解析后再调用；绝不兜底到别的站。")
    hdr = {"User-Agent": UA, "Authorization": "Bearer " + tok}
    try:
        r = _open(urllib.request.Request(base.rstrip("/") + path, headers=hdr),
                  timeout=timeout, proxy=proxy)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])


def _open(req, timeout=30, proxy=None):
    """统一的「带可选代理」请求出口。proxy 为空则直连。"""
    p = ""
    if proxy:
        try:
            import site_collect as _sc
            p = _sc._norm_proxy(proxy)
        except Exception:
            p = str(proxy).strip()
    if p:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p, "https": p}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _site_from_base(base):
    """从【请求域名】反查它属于哪个站点 host。查不到返回 ''。

    ★ 2026-10-08：fetch_items 只拿到 base（请求域名），要据此找回该站的
      代理配置 —— 因为同一站的 bases 可能有多个域名，得逐个比对。
    """
    if not base:
        return ""
    try:
        from . import site_resolver as _sr
    except ImportError:
        try:
            import site_resolver as _sr
        except Exception:
            return ""
    want = (base or "").strip().lower().rstrip("/")
    try:
        for s in _sr.all_sites():
            for b in _sr.bases_of(s):
                if (b or "").strip().lower().rstrip("/") == want:
                    return _sr.host_of(s)
            # 再比一次裸主机名（base 可能只给了域名没给协议）
            if _sr.host_from_url(want) and \
               _sr.host_from_url(want) == (_sr.host_of(s) or "").lower():
                return _sr.host_of(s)
    except Exception:
        pass
    return ""


def _site_proxy(host):
    """取某站的代理配置（从 sites.json 读）。没配返回 ''。

    ★ 2026-10-08：sub2api 站的代理要**两处都生效** ——
      聚合采集走 site_collect，逐条/登录态走这里。只加一处会出现
      「聚合走了代理、逐条没走」的半吊子状态。
    """
    try:
        from . import site_resolver as _sr
    except ImportError:
        try:
            import site_resolver as _sr
        except Exception:
            return ""
    try:
        site = None
        for s in _sr.all_sites():
            if _sr.host_of(s).strip().lower() == (host or "").strip().lower():
                site = s
                break
        if site is None:
            return ""
        v = site.get("proxy") or site.get("proxy_url") or ""
        if not v:
            return ""
        try:
            import site_collect as _sc
            return _sc._norm_proxy(v)
        except Exception:
            return str(v).strip()
    except Exception:
        return ""


def _migrate(d):
    """把老格式（平铺的单站 token）迁移成分站格式。

    ★ 2026-10-07 多站改造：以前一份 dsh_auth.json 只装一个站的 token，
      第二个 sub2api 站进来时没有位置放。现在改成：

          { "sites": { "<host>": {...}, ... }, "active": "<host>" }

      读到老格式（顶层直接是 access_token/refresh_token）时，
      按「当时它管的是哪个站」搬进 sites.<host>，并写回磁盘。
      迁移是**幂等**的：已经是新格式就原样返回。

    返回 (new_dict, migrated: bool)
    """
    if not isinstance(d, dict):
        return {"sites": {}, "active": ""}, False
    if isinstance(d.get("sites"), dict):
        return d, False                      # 已是新格式
    if not d.get("access_token") and not d.get("refresh_token"):
        return {"sites": {}, "active": ""}, False   # 空文件
    host = _legacy_host()
    new = {"sites": {host: d}, "active": host}
    return new, True


def _legacy_host():
    """老格式没有站名 —— 推测它属于哪个站（取 sites.json 里第一个 sub2api 站）。

    这个推测跟改造前 _site_host() 的取值逻辑完全一致，所以迁移不会串站。
    """
    return _site_host()


def load(host=None):
    """读 token 存储。

    host=None（默认）→ 返回 **active 那个站**的 store（平铺 dict），
                       完全兼容改造前的调用方式与返回结构。
    host="xxx"       → 返回该站的 store（没有则 {}）。

    ⚠️ 老格式文件会在首次 load 时**自动迁移并写盘**（只发生一次）。

    ★ 2026-10-07 自愈：active 若被写到「非配置默认的 sub2api 站」上，
      这里自动纠回来。背景：早期版本 login() 会改 active，导致给 tryaigc
      登录一次就把当前站切成 tryaigc，而 poll_loop / dsh_flows 靠
      load() 拿 token 去打 DSH 接口 —— 实际串站。现在 login 已不改 active，
      但历史数据可能已被写歪，所以读的时候顺手纠偏。
    """
    if not os.path.isfile(STORE):
        return {}
    try:
        raw = json.load(open(STORE, encoding="utf-8"))
    except Exception:
        return {}
    data, migrated = _migrate(raw)
    sites = data.get("sites") or {}

    # 自愈：active 跟随配置（_site_host 决定谁是「默认 sub2api 站」）
    if sites:
        want = _site_host()
        cur = data.get("active") or ""
        if want and want in sites and cur != want:
            data["active"] = want
            migrated = True

    if migrated:
        try:
            _write_raw(data)
        except Exception:
            pass
    sites = data.get("sites") or {}
    if host:
        return sites.get(host) or {}
    act = data.get("active") or ""
    if act and act in sites:
        return sites[act]
    # active 没写或指向不存在的站 → 任取一个（老行为：只有一个站）
    for k in sites:
        return sites[k]
    return {}


def save(d, host=None, activate=False):
    """写 token。

    host=None      → 写入 active 站（老调用方式：整个文件就一个站）。
    host="x"       → 写入该站的槽位。
    activate=True  → 同时把 active 切到该站（只在用户显式操作时用）。

    ⚠️ 默认**不改 active** —— 否则给 A 站续期会把 B 站变成当前站，
       老代码（site_collect 等）拿 load() 就会串到别的站去（实测踩过）。
    """
    data = {}
    if os.path.isfile(STORE):
        try:
            raw = json.load(open(STORE, encoding="utf-8"))
            data, _ = _migrate(raw)
        except Exception:
            data = {"sites": {}, "active": ""}
    if not isinstance(data.get("sites"), dict):
        data = {"sites": {}, "active": ""}

    h = host or data.get("active") or _site_host()
    d = dict(d)
    d["saved_at"] = int(time.time())
    data["sites"][h] = d
    if activate or not data.get("active"):
        data["active"] = h
    _write_raw(data)
    return d


def _write_raw(data):
    """原子写盘（写 .tmp 再 os.replace）。"""
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)


def all_sites():
    """全部站的 token 状态（供「站点账号」页多站展示）。"""
    if not os.path.isfile(STORE):
        return {}
    try:
        raw = json.load(open(STORE, encoding="utf-8"))
    except Exception:
        return {}
    data, _ = _migrate(raw)
    return data.get("sites") or {}


def set_active(host):
    """把某个站设为 active（影响 host=None 的调用走哪个站）。"""
    if not os.path.isfile(STORE):
        return False
    try:
        raw = json.load(open(STORE, encoding="utf-8"))
    except Exception:
        return False
    data, _ = _migrate(raw)
    if host not in (data.get("sites") or {}):
        return False
    data["active"] = host
    _write_raw(data)
    return True


def expiring(store, margin=RENEW_MARGIN):
    """access_token 是否即将/已经过期"""
    exp = store.get("expires_at") or 0
    return (not store.get("access_token")) or (time.time() + margin >= exp)


def refresh(store=None, log=print, host=None, base=None, proxy=None):
    """用 refresh_token 换新 token（会作废旧 refresh_token）。成功返回新 store。

    ★ 2026-10-07：host 指定是哪个站的 token（多站改造）。不传 = active 站。
    ★ 2026-10-08 修（域名泄漏，**假 token 的源头**）：
      原来 _post() 没传 base → 掉回 _base() → 模块级 BASES（= 第一个 sub2api 站，
      即 dshapi）。给 tryaigc 续期时请求实际打到 dshapi，而两个站同邮箱同密码，
      dshapi 认得 → 返回一张 **dshapi 签发的 token** 存进 tryaigc 槽位。
      之后拿这张票去打 dshapi 就是 200，采回 dshapi 的流水贴上 tryaigc 标签
      （实测 434 条全串，JWT 里 user_id=96 而真 tryaigc 是 1514）。
      现在 base 不传就按 host 反查本站域名；查不到直接失败，绝不兜底。
    """
    store = store or load(host=host)
    rt = store.get("refresh_token")
    if not rt:
        log("  ✗ 无 refresh_token，需先 --import 喂一次凭据")
        return None
    base = _resolve_base(host, base, log=log)
    if not base:
        log("  ✗ 站点 %s 反查不到请求域名，拒绝对其他站发起请求" % (host or "?"))
        return None
    px = proxy if proxy is not None else _site_proxy(host)
    st, body = _post("/api/v1/auth/refresh", {"refresh_token": rt}, base=base, proxy=px)
    try:
        j = json.loads(body)
    except Exception:
        log("  ✗ refresh 响应无法解析：HTTP %s %s" % (st, body[:160]))
        return None
    if j.get("code") != 0 or not j.get("data"):
        log("  ✗ refresh 失败（HTTP %s）：%s" % (st, j.get("message")))
        return None
    d = j["data"]
    store = {
        "access_token": d["access_token"],
        "refresh_token": d["refresh_token"],
        "expires_in": d.get("expires_in"),
        "expires_at": int(time.time()) + int(d.get("expires_in") or 86400),
        "refreshed_at": int(time.time()),
        "account": store.get("account", ""),
    }
    save(store, host=host)
    log("  ✓ token 已续期（expires_in=%s 秒）" % d.get("expires_in"))
    return store


def _resolve_base(host=None, base=None, log=None):
    """★ 2026-10-08 新增：解析「这次请求该打到哪个域名」。

    优先级：
      ① 显式传入的 base（调用方已经明确知道）
      ② 按 host 反查 sites.json 里该站的域名池首项
      ③ 都没有 → 返回 None（调用方必须失败，**绝不回退到别的站**）

    为什么不能有 ③ 兜底：模块级 BASES 是「第一个 sub2api 站」，把它当默认值
    会让 A 站的请求静默打到 B 站上（实测串了 434 条数据）。
    """
    if base:
        b = str(base).strip().rstrip("/")
        if b:
            return b
    h = host or _site_host()
    try:
        from . import site_resolver as _sr2
    except ImportError:
        try:
            import site_resolver as _sr2
        except Exception:
            return None
    try:
        lst = _sr2.bases_for_host(h)
    except Exception:
        lst = []
    if lst:
        return lst[0]
    if log:
        log("  ⚠ 站点 %s 在 sites.json 里找不到可用域名" % h)
    return None


def login(email, password, log=print, save_password=False, host=None, base=None, proxy=None):
    """用邮箱+密码登录，换一对新 token（★会新建一个独立的「会话家族」）。

    ★ 2026-10-07：host 指定这个 token 属于哪个站（多站改造后按站存）；
      不传则存到 active 站（老行为）。

    ★ 2026-10-08 修（域名泄漏）：登录请求必须打到【本站】域名。
      原来 _post() 没传 base → 掉回 _base() → dshapi。两个站同邮箱同密码，
      dshapi 会正常返回一张它自己签发的 token，于是「给 tryaigc 登录」却拿到
      dshapi 的身份（实测 JWT user_id=96；真 tryaigc 是 1514）。
      这是本次串数据的**源头**。

    ⚠️ 为什么这是稳定链路（2026-10-03 读 sub2api 源码确认，auth_service.go）：
      · 每次 login → GenerateTokenPair(ctx, user, "") → familyID 为空 → 服务端
        **新生成一个随机 familyID**，token 分别挂在「用户集合」和「家族集合」下；
      · 而 refresh → RefreshTokenPair(同一 familyID) + DeleteRefreshToken(旧值)
        → **同家族强制轮转**，谁先刷谁就把对方手里的旧票作废。
      ⇒ 所以：
        - **借**浏览器那张票去 refresh（共享家族）→ 必然与浏览器分叉 → 断链；
        - 脚本**自己 login**（独立家族）→ 浏览器怎么刷都影响不到我们，反之亦然。
      另：撤销是分级的（RevokeRefreshToken 单条 / RevokeSessionFamily 单家族 /
      RevokeAllUserSessions 全部），所以脚本登录**不会**把浏览器踢下线。
    """
    base = _resolve_base(host, base, log=log)
    if not base:
        log("  ✗ 站点 %s 反查不到请求域名，拒绝对其他站发起登录" % (host or "?"))
        return None
    px = proxy if proxy is not None else _site_proxy(host)
    st, body = _post("/api/v1/auth/login",
                     {"email": email, "password": password}, base=base, proxy=px)
    try:
        j = json.loads(body)
    except Exception:
        log("  ✗ 登录响应无法解析：HTTP %s %s" % (st, (body or "")[:160]))
        return None
    if j.get("code") not in (0, None) or not j.get("data"):
        log("  ✗ 登录失败（HTTP %s）：%s" % (st, j.get("message")))
        return None
    d = j["data"]
    if d.get("requires_2fa"):
        log("  ✗ 该账号开了两步验证（2FA），脚本没法自动登录，请改用 --import 喂凭据")
        return None
    store = {
        "access_token": d.get("access_token"),
        "refresh_token": d.get("refresh_token") or "",
        "expires_at": int(time.time()) + int(d.get("expires_in") or 86400),
        "account": (d.get("user") or {}).get("email") or email,
        "email": email,
        "source": "login",
        "refreshed_at": int(time.time()),
    }
    old = load(host=host)
    if save_password:
        store["password"] = password
    elif old.get("password"):
        store["password"] = old["password"]      # 不因一次未带标志的登录把它抹掉
    # ★ 2026-10-07 修：**不要** activate。
    #   原来写 activate=bool(host)，导致「给 tryaigc 登录一次」就把 active
    #   切成 tryaigc，而 poll_loop / dsh_flows 那些老代码靠 load()（=active 站）
    #   拿 token 去打 DSH 的接口 —— 直接串站。
    #   active 只应由配置（_site_host）或用户显式 set_active() 决定。
    save(store, host=host)
    log("  ✓ 登录成功（独立会话家族，expires_in=%s 秒）" % d.get("expires_in"))
    return store


def _cred_lookup(store, site=None):
    """取该站凭据：优先 credentials.json（独立凭据文件），回退 dsh_auth.json。

    2026-10-06 改造（用户拍板方案 B）：密码从 dsh_auth.json 挪到
    credentials.json，这样 sites.json / dsh_auth.json 都可以分享/同步而不漏密码。

    2026-10-07：site 参数支持按站取（多站改造后每个站一套凭据）。
    """
    site = site or _site_host()
    try:
        import credentials as _c
        c = _c.get(site)
        if c.get("email") and c.get("password"):
            return c["email"], c["password"], "credentials.json"
    except Exception:
        pass
    # 回退：老位置（兼容未迁移的情况）
    if store.get("email") and store.get("password"):
        return store["email"], store["password"], "dsh_auth.json"
    return None, None, None


def _site_host():
    """本 token 对应的站点 host（从 sites.json 的 session_auth 站读）。"""
    try:
        from . import site_resolver as _sr
    except ImportError:
        try:
            import site_resolver as _sr
        except Exception:
            return "api.dshapi.icu"
    try:
        s = _sr.default_sub2api_site()
        return _sr.host_of(s) or "api.dshapi.icu"
    except Exception:
        return "api.dshapi.icu"


def cred_status(host=None):
    """给界面看的凭据状态（脱敏，不含明文密码）。

    ★ 2026-10-07：host 指定看哪个站；不传则看 active 站（老行为）。
    """
    store = load(host=host)
    site = host or _site_host()
    email, pwd, src = _cred_lookup(store, site=site)
    left = (store.get("expires_at") or 0) - time.time()
    return {
        "site": site,
        "token_ok": bool(store.get("access_token")) and left > 0,
        "expires_in_h": round(left / 3600.0, 2),
        "has_refresh": bool(store.get("refresh_token")),
        "can_autorelogin": bool(email and pwd),
        "cred_source": src or "无",
        "email": (email[:2] + "***" + email[email.find("@"):]) if email and "@" in email else (email or ""),
        "last_refresh": time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(store.get("refreshed_at") or 0)),
    }


def ensure_token(log=print, host=None):
    """拿可用的 access_token。三级降级：

      ① 本地 access_token 还没到期（留 RENEW_MARGIN 余量）→ 直接用（零动作）
      ② 到期了 → 用 refresh_token 续（同家族轮转）
      ③ 续不动了 → 有邮箱密码就**重新登录**（新建家族，浏览器不受影响）
    返回 None = 彻底拿不到，调用方应提示用户补一次凭据（别静默）。

    凭据来源（2026-10-06）：credentials.json 优先，dsh_auth.json 回退。

    ★ 2026-10-07：host 指定管哪个站（多站改造）。不传 = active 站（老行为）。
      这样每个 sub2api 站都能各自续期，不会出现「B 站 token 永不过期检查」。

    ★ 2026-10-08 修（域名泄漏）：续期/重登都必须在【本站】域名上做 ——
      否则拿到的是别的站签发的 token（详见 refresh / login 的注释）。
    """
    store = load(host=host)
    if store.get("access_token") and not expiring(store):
        return store["access_token"]

    base = _resolve_base(host, None, log=log)
    if not base:
        log("  ✗ 站点 %s 反查不到请求域名，无法续期/登录" % (host or _site_host()))
        return None
    # ★ 2026-10-08：该站若配了代理，续期/登录也走它（否则需要梯子的站永远登不上）
    px = _site_proxy(host)

    if store.get("refresh_token"):
        log("  token 快过期/已过期，自动续期…")
        new = refresh(store, log=log, host=host, base=base, proxy=px)
        if new and new.get("access_token"):
            return new["access_token"]

    email, pwd, _src = _cred_lookup(store, site=host)
    if email and pwd:
        log("  续期失败，改用账号密码重新登录（会新建独立家族）…")
        new = login(email, pwd, log=log, host=host, base=base, proxy=px)
        if new and new.get("access_token"):
            return new["access_token"]

    return None


def verify(tok, host=None, base=None, proxy=None):
    """验证 token 是否可用。

    ★ 2026-10-08 修（域名泄漏）：必须校验在【本站】域名上可用。
      原来固定打 _base()（= dshapi），于是「用 dshapi 签发的假 token 校验 tryaigc」
      会返回 200，误判为「登录态正常」——串数据能长期不被发现就靠这个。
    ★ 2026-10-08 加 proxy：需挂梯子的站，校验也要走同一条路，
      否则「直连校验失败、采集其实能用」或反之，判断会失真。
    """
    b = _resolve_base(host, base)
    if not b:
        return False
    px = proxy if proxy is not None else _site_proxy(host)
    st, body = _get("/api/v1/usage?page=1&page_size=1", tok, timeout=20,
                    base=b, proxy=px)
    return st == 200


def sync_to_browser(log=print):
    """把当前 token 反向写回 Kimi WebBridge 控制的浏览器 localStorage，
       保证「我们刷新」之后浏览器也不会被登出。"""
    store = load()
    if not store.get("access_token"):
        return False
    import urllib.request as u2
    code = ("localStorage.setItem('auth_token',%s);"
            "localStorage.setItem('refresh_token',%s);"
            "localStorage.setItem('token_expires_at',String(%d));'ok'"
            % (json.dumps(store["access_token"]),
               json.dumps(store.get("refresh_token", "")),
               (store.get("expires_at") or 0) * 1000))
    body = json.dumps({"action": "evaluate",
                       "args": {"code": code},
                       "session": "dsh-auth"}).encode()
    try:
        r = u2.urlopen(u2.Request("http://127.0.0.1:10086/command", data=body,
                                  headers={"Content-Type": "application/json"}),
                       timeout=20)
        ok = json.loads(r.read().decode()).get("ok")
        log("  %s 已同步回浏览器" % ("✓" if ok else "✗"))
        return bool(ok)
    except Exception as e:
        log("  ✗ 同步浏览器失败（不影响采集）：%s" % str(e)[:80])
        return False


def import_from_browser(log=print):
    """首次喂凭据：从 WebBridge 浏览器 localStorage 读 token

    ⚠️ 2026-10-01 改版（踩坑）：
      旧逻辑读到的 refresh_token 立刻拿去 /auth/refresh 换长效 token。
      但实测踩到两种情形：
        ① refresh_token 已被前端轮换过（它是一次性的）→ 401 invalid refresh token
        ② 换域名期间（主站 TLS 挂 / 切副站）同一个 rt 也会 401
      而此时浏览器里的 access_token 往往【还有 20+ 小时有效期、且能正常调接口】。
      所以改为：只要 access_token 还有效就【直接用】，不强行刷新——
      刷新是消耗品，不该在未知有效性时盲目烧掉一次。
      只有 access_token 也失效了，才退回 refresh 尝试。
    """
    import urllib.request as u2
    code = ("JSON.stringify({a:localStorage.getItem('auth_token')||'',"
            "r:localStorage.getItem('refresh_token')||'',"
            "e:localStorage.getItem('token_expires_at')||''})")
    body = json.dumps({"action": "evaluate",
                       "args": {"code": code},
                       "session": "dsh-auth"}).encode()
    try:
        r = u2.urlopen(u2.Request("http://127.0.0.1:10086/command", data=body,
                                  headers={"Content-Type": "application/json"}),
                       timeout=25)
        j = json.loads(r.read().decode())
        v = (j.get("data") or {}).get("value") or ""
        o = json.loads(v)
    except Exception as e:
        log("✗ 从浏览器读取失败：%s" % str(e)[:120])
        log("  确认 Kimi WebBridge 已连、且浏览器已登录 api2.dshapi.icu")
        return None

    at = o.get("a") or ""
    rt = o.get("r") or ""
    # 浏览器给的过期时间戳是毫秒
    exp = 0
    try:
        exp = int(int(o.get("e") or 0) / 1000)
    except Exception:
        exp = 0

    if not rt and not at:
        log("✗ 浏览器里既无 access_token 也无 refresh_token，请先登录")
        return None

    # 首选：access_token 还有效 → 直接存下来用（不烧 refresh_token）
    # ★ 2026-10-08：导入也要落到正确的站槽位（默认 active 站）。
    _host = _site_host()
    if at and exp > time.time() + 60:
        store = {"access_token": at, "refresh_token": rt, "expires_at": exp,
                 "account": "", "source": "browser-import"}
        save(store, host=_host)
        left = (exp - time.time()) / 3600.0
        log("✓ 已从浏览器导入 access_token（剩余 %.1f 小时），直接使用，未消耗 refresh_token"
            % left)
        return store

    # 退路：access_token 没了，才尝试用 refresh_token 换
    if not rt:
        log("✗ access_token 已过期且无 refresh_token，请重新登录浏览器")
        return None
    log("  access_token 已过期，尝试用 refresh_token 换新…")
    store = {"refresh_token": rt, "access_token": "", "expires_at": 0, "account": ""}
    got = refresh(store, log=log, host=_host)
    if not got:
        log("✗ 换新失败（refresh_token 多半已被轮换作废）。")
        log("  → 请在浏览器重新登录 api2.dshapi.icu，再跑 python dsh_auth.py --import")
    return got


def main():
    ap = argparse.ArgumentParser(description="dshapi 登录态管理")
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="从浏览器喂一次凭据（首次用）")
    ap.add_argument("--login", action="store_true",
                    help="★用邮箱+密码登录（推荐：建独立会话家族，不影响浏览器）")
    ap.add_argument("--email", help="配合 --login：账号邮箱")
    ap.add_argument("--password", help="配合 --login：密码（不填则交互输入，更安全）")
    ap.add_argument("--save-password", action="store_true",
                    help="把密码一并存进 dsh_auth.json（换来「换 IP / 家族被撤」后能自愈）")
    ap.add_argument("--refresh", action="store_true", help="手动续期")
    ap.add_argument("--status", action="store_true", help="看当前状态")
    ap.add_argument("--sync", action="store_true", help="把 token 写回浏览器")
    ap.add_argument("--test", action="store_true", help="验证 token 可用")
    # ★ 2026-10-08 新增：多站改造后必须能指定「对哪个站操作」。
    #   不指定时用 active 站（老行为）。登录/续期/校验都受它影响 ——
    #   不指定就去 dshapi，正是本次串数据的成因。
    ap.add_argument("--host", help="对哪个站操作（站点身份，如 api.tryaigc.cn）")
    a = ap.parse_args()
    host = (a.host or "").strip() or None

    if a.do_import:
        import_from_browser()
        return
    if a.login:
        email = (a.email or "").strip()
        if not email:
            try:
                email = input("dshapi 账号邮箱: ").strip()
            except EOFError:
                print("✗ 没拿到邮箱")
                return
        pwd = a.password
        if not pwd:
            try:
                pwd = getpass.getpass("密码（不回显）: ")
            except EOFError:
                print("✗ 没拿到密码")
                return
        if not email or not pwd:
            print("✗ 邮箱/密码不能为空")
            return
        st = login(email, pwd, save_password=a.save_password, host=host)
        if st:
            _b = _resolve_base(host)
            _ok = bool(_b) and verify(st["access_token"], host=host, base=_b)
            print("  ✓ token 可用（域名 %s）" % _b if _ok else "  ⚠ 拿到了 token 但校验失败")
            if not a.save_password:
                print("  提示：未存密码（默认）。想让「换 IP / 家族被撤」后也能自愈，"
                      "加 --save-password 重跑一次。")
        return
    if a.refresh:
        refresh(host=host)
        return
    if a.sync:
        sync_to_browser()
        return
    if a.test:
        tok = ensure_token(host=host)
        if not tok:
            print("✗ 无可用 token")
            return
        _b = _resolve_base(host)
        print("✓ token 可用（域名 %s）" % _b if ( _b and verify(tok, host=host, base=_b))
              else "✗ token 已失效")
        return

    # 默认：状态
    st = load(host=host)
    if not st:
        print("尚无凭据。首次请运行： python dsh_auth.py --import")
        return
    left = (st.get("expires_at") or 0) - time.time()
    print("dshapi 登录态")
    print("  access_token : %s…（%d 字符）" % (st.get("access_token", "")[:12],
                                             len(st.get("access_token") or "")))
    print("  refresh_token: %s…" % (st.get("refresh_token", "") or "")[:12])
    print("  剩余有效     : %.1f 小时" % (left / 3600))
    print("  上次续期     : %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.get("refreshed_at") or 0))))
    print("  可自动续期   : %s" % ("是" if st.get("refresh_token") else "否"))
    print("  可自动重登   : %s" % ("是（已存邮箱+密码）" if (st.get("email") and st.get("password"))
                                  else "否 —— 想更稳可跑 --login --save-password"))
    print("  凭据来源     : %s" % (st.get("source") or "?"))
    if st.get("email"):
        print("  账号         : %s" % st["email"])


if __name__ == "__main__":
    main()
