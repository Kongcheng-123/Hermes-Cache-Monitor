# -*- coding: utf-8 -*-
"""host_alias.py — 站点 host 归一映射（2026-10-02）

为什么需要它
------------
Hermes 库里 `billing_base_url` 的 hostname **不稳定**：同一个中转站会有多个域名入口，
于是被当成不同站点，导致：

  · 价格表要为一站配多份（`api.dshapi.icu` / `api2.` / `api4.` 各配一遍）
  · 统计被劈碎（同一个站显示成 3 个）
  · 历史数据因为域名变更而对不上

实测（2026-10-02）：dsh 站的 `api.` / `api2.` / `api4.` 三个域名，
余额、用量、倍率完全一致（19.39215461 × 3）→ 确认是**同一账号的多个入口**。

设计
----
· 一张 **别名 → 规范名** 的映射表（本文件内置默认值，用户可在 alias.json 覆盖）
· `canon(host)` 把任意别名归一成规范名；无映射的原样返回
· **幂等**：`canon(canon(x)) == canon(x)`
· 归一方向永远指向「**库里历史数据最多的那个域名**」（避免改名导致历史对不上）

规范名的选择原则
----------------
选**历史数据最多**的域名当规范名，这样绝大多数历史数据不用动。
例：dsh 的 `api.dshapi.icu` 有 122 行，`api2.`/`api4.` 各 4 行 →
   规范名取 `api.dshapi.icu`。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ALIAS_PATH = os.path.join(HERE, "host_alias.json")

# 内置默认映射：别名 -> 规范名
# （用户可在 host_alias.json 里补充/覆盖，见 load()）
DEFAULT_ALIAS = {
    # dsh 站：同一账号的多个域名入口（2026-10-02 实测余额一致）
    "api.dshapi.icu": "api.dshapi.icu",
    "api2.dshapi.icu": "api.dshapi.icu",
    "api4.dshapi.icu": "api.dshapi.icu",
    "api3.dshapi.icu": "api.dshapi.icu",
}


def _load_user():
    """用户自定义映射（可选）。文件不存在返回 {}"""
    try:
        with open(ALIAS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return {str(k): str(v) for k, v in d.items() if k and v}
    except Exception:
        pass
    return {}


def mapping():
    """合并后的映射表（用户配置覆盖内置）"""
    m = dict(DEFAULT_ALIAS)
    m.update(_load_user())
    return m


def canon(host):
    """把 host 归一成规范名。

    · 空值 -> ""
    · 命中映射 -> 规范名
    · 未命中 -> 原样返回（大小写不敏感地查，但返回映射里的原始写法）
    """
    if not host:
        return ""
    h = str(host).strip()
    m = mapping()
    if h in m:
        return m[h]
    # 大小写不敏感兜底
    low = h.lower()
    for k, v in m.items():
        if k.lower() == low:
            return v
    return h


def same(a, b):
    """两个 host 是否属于同一个站"""
    return canon(a) == canon(b)


def alias_groups():
    """返回 {规范名: [别名...]}，供文档/界面展示"""
    groups = {}
    for k, v in mapping().items():
        groups.setdefault(v, []).append(k)
    return groups


def ensure_default_file():
    """首次运行时落一份可编辑的映射文件（方便用户手改）"""
    if os.path.isfile(ALIAS_PATH):
        return False
    try:
        with open(ALIAS_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "_note": "站点域名归一映射：别名 -> 规范名。"
                         "同一个中转站的多个域名入口写在这里，就会被当成一个站。",
                "_hint": "左侧是库里实际出现的 hostname，右侧是你想统一成的名字。"
                         "建议规范名取『历史数据最多的那个域名』。",
                "map": DEFAULT_ALIAS,
            }, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    ensure_default_file()
    print("站点归一分组：")
    for canon_name, aliases in sorted(alias_groups().items()):
        if len(aliases) > 1:
            print("  %s" % canon_name)
            for a in sorted(aliases):
                mark = "  ← 规范名" if a == canon_name else ""
                print("      %s%s" % (a, mark))
        else:
            print("  %s（无别名）" % canon_name)
    print()
    print("映射文件：%s" % ALIAS_PATH)
