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


def _post(path, body, tok=None, timeout=30, base=None):
    hdr = {"User-Agent": UA, "Content-Type": "application/json"}
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    req = urllib.request.Request((base or _base()) + path,
                                 data=json.dumps(body).encode(),
                                 headers=hdr, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])


def _get(path, tok, timeout=30, base=None):
    hdr = {"User-Agent": UA, "Authorization": "Bearer " + tok}
    try:
        r = urllib.request.urlopen(
            urllib.request.Request((base or _base()) + path, headers=hdr),
            timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])


def load():
    if not os.path.isfile(STORE):
        return {}
    try:
        return json.load(open(STORE, encoding="utf-8"))
    except Exception:
        return {}


def save(d):
    d["saved_at"] = int(time.time())
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)          # 原子替换，防写坏


def expiring(store, margin=RENEW_MARGIN):
    """access_token 是否即将/已经过期"""
    exp = store.get("expires_at") or 0
    return (not store.get("access_token")) or (time.time() + margin >= exp)


def refresh(store=None, log=print):
    """用 refresh_token 换新 token（会作废旧 refresh_token）。成功返回新 store。"""
    store = store or load()
    rt = store.get("refresh_token")
    if not rt:
        log("  ✗ 无 refresh_token，需先 --import 喂一次凭据")
        return None
    st, body = _post("/api/v1/auth/refresh", {"refresh_token": rt})
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
    save(store)
    log("  ✓ token 已续期（expires_in=%s 秒）" % d.get("expires_in"))
    return store


def login(email, password, log=print, save_password=False):
    """用邮箱+密码登录，换一对新 token（★会新建一个独立的「会话家族」）。

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
    st, body = _post("/api/v1/auth/login",
                     {"email": email, "password": password})
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
    old = load()
    if save_password:
        store["password"] = password
    elif old.get("password"):
        store["password"] = old["password"]      # 不因一次未带标志的登录把它抹掉
    save(store)
    log("  ✓ 登录成功（独立会话家族，expires_in=%s 秒）" % d.get("expires_in"))
    return store


def ensure_token(log=print):
    """拿可用的 access_token。三级降级：

      ① 本地 access_token 还没到期（留 RENEW_MARGIN 余量）→ 直接用（零动作）
      ② 到期了 → 用 refresh_token 续（同家族轮转）
      ③ 续不动了 → 有邮箱密码就**重新登录**（新建家族，浏览器不受影响）
    返回 None = 彻底拿不到，调用方应提示用户补一次凭据（别静默）。
    """
    store = load()
    if store.get("access_token") and not expiring(store):
        return store["access_token"]

    if store.get("refresh_token"):
        log("  token 快过期/已过期，自动续期…")
        new = refresh(store, log=log)
        if new and new.get("access_token"):
            return new["access_token"]

    if store.get("email") and store.get("password"):
        log("  续期失败，改用账号密码重新登录（会新建独立家族）…")
        new = login(store["email"], store["password"], log=log)
        if new and new.get("access_token"):
            return new["access_token"]

    return None


def verify(tok):
    st, body = _get("/api/v1/usage?page=1&page_size=1", tok, timeout=20)
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
    if at and exp > time.time() + 60:
        store = {"access_token": at, "refresh_token": rt, "expires_at": exp,
                 "account": "", "source": "browser-import"}
        save(store)
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
    got = refresh(store, log=log)
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
    a = ap.parse_args()

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
        st = login(email, pwd, save_password=a.save_password)
        if st:
            print("  ✓ token 可用" if verify(st["access_token"]) else "  ⚠ 拿到了 token 但校验失败")
            if not a.save_password:
                print("  提示：未存密码（默认）。想让「换 IP / 家族被撤」后也能自愈，"
                      "加 --save-password 重跑一次。")
        return
    if a.refresh:
        refresh()
        return
    if a.sync:
        sync_to_browser()
        return
    if a.test:
        tok = ensure_token()
        if not tok:
            print("✗ 无可用 token")
            return
        print("✓ token 可用" if verify(tok) else "✗ token 已失效")
        return

    # 默认：状态
    st = load()
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
