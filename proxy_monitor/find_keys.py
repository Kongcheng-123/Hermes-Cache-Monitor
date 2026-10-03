# -*- coding: utf-8 -*-
"""find_keys.py — 检查站点账号下有哪些 key 在跑

用途：站方网页统计的是「账号级」，而我们按单个 key 采集，会漏掉其他 key 的流量。
      本脚本帮你摸清有几个 key、各自用多少，从而知道还差哪些没纳入。
"""
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERMES_CONFIG = r"D:\Hermes Agent CN Desktop\data\hermes-home\config.yaml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def providers(path=HERMES_CONFIG):
    out = {}
    raw = open(path, encoding="utf-8").read()
    lines = raw.splitlines()
    inprov = False
    cur = None
    buf = {}
    for ln in lines:
        if re.match(r"^providers:\s*$", ln):
            inprov = True
            continue
        if inprov and re.match(r"^\S", ln):
            break
        if not inprov:
            continue
        m = re.match(r"^  ([A-Za-z0-9_:.\-]+):\s*$", ln)
        if m:
            if cur:
                out[cur] = buf
            cur = m.group(1)
            buf = {}
            continue
        if cur:
            m = re.match(r"^    ([a-z_]+):\s*(.+?)\s*$", ln)
            if m:
                buf[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    if cur:
        out[cur] = buf
    return out


def fetch(root, key, path):
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            root + path, headers={"Authorization": "Bearer " + key,
                                  "User-Agent": UA}), timeout=60)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return None, str(e)[:200]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "example.com"
    provs = providers()
    root = "https://" + host

    # 找该站所有配置的 key
    keys = {}
    for name, p in provs.items():
        u = p.get("base_url", "")
        if host in u and p.get("api_key"):
            keys[name] = p["api_key"]

    if not keys:
        print("在 hermes config 里没找到 %s 的 key" % host)
        return

    today0 = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    print("站点: %s   配置里的 key 数: %d" % (host, len(keys)))
    print("=" * 78)

    per_key = {}
    for name, k in keys.items():
        st, b = fetch(root, k, "/api/log/token?p=0&page_size=100")
        if st != 200:
            print("  %-26s -> HTTP %s %s" % (name, st, b[:100]))
            continue
        rows = json.loads(b).get("data") or []
        n_today = t_tok = t_q = 0
        tnames = Counter()
        for r in rows:
            if (r.get("created_at") or 0) >= today0:
                n_today += 1
                t_tok += (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
                t_q += r.get("quota") or 0
            tnames[r.get("token_name") or "?"] += 1
        per_key[name] = {"window": len(rows), "today_n": n_today,
                         "today_tok": t_tok, "today_q": t_q, "names": dict(tnames)}
        print("  key: %s" % name)
        print("      窗口 %d 条 ｜ 今日 %d 次 ｜ %s token ｜ quota %d ｜ ¥%.6f" % (
            len(rows), n_today, format(t_tok, ","), t_q, t_q / 500000))
        print("      token_name 分布: %s" % dict(tnames))

    print()
    print("=" * 78)
    print("汇总（本机配置的 key）")
    print("=" * 78)
    sn = sum(v["today_n"] for v in per_key.values())
    st_ = sum(v["today_tok"] for v in per_key.values())
    sq = sum(v["today_q"] for v in per_key.values())
    print("  今日合计: %d 次 ｜ %s token ｜ ¥%.6f" % (sn, format(st_, ","), sq / 500000))
    print()
    print("  ⚠️ 若站方网页显示的次数/token 明显多于上面这个数，")
    print("     说明账号下还有【未配置到 hermes 的 key】在跑。")
    print("     去站方网页的「API 密钥」页可以看到全部 key 的用量。")
    print()
    print("  想让它们也纳入监控：把那些 key 加到 sites.json 的")
    print("     「extra_keys」数组（或建成 hermes provider）即可。")


if __name__ == "__main__":
    main()
