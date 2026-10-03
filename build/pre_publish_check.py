#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pre_publish_check.py — 发布前隐私/规范体检

用途：推到 GitHub 之前跑一遍，确保没有把个人数据带上公网。
      CI 或 .git/hooks/pre-push 也可以调它。

用法：
    python build/pre_publish_check.py            # 检查工作区（默认）
    python build/pre_publish_check.py --staged   # 只检查已 git add 的内容

退出码：0 = 通过，1 = 有问题
"""

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ────────────────────────────────────────────────────────
# 检查规则
#   BLOCK  : 发现即拒绝发布（真隐私）
#   WARN   : 只提醒（可能是注释里的正常叙述）
# ────────────────────────────────────────────────────────
BLOCK = [
    (r"\bsk-[A-Za-z0-9]{16,}", "API Key（sk- 开头）"),
    (r"Bearer\s+[A-Za-z0-9._\-]{20,}", "Bearer token"),
    (r"eyJ[A-Za-z0-9_\-]{20,}\.", "JWT token"),
    (r'password\s*[=:]\s*["\'][^"\']{3,}', "硬编码密码"),
    (r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.(com|cn|net|org)\b", "邮箱地址"),
    (r"D:[\\/]+Hermes works", "绝对路径（个人目录）"),
    (r"C:[\\/]+Users[\\/]+[A-Za-z]", "C 盘用户目录"),
    (r"empty\s+city", "Windows 用户名"),
]

WARN = [
    (r"[a-z0-9\-]+\.[a-z]{2,}\s*(站|域名)", "疑似私有站域名"),
    (r"\bcustom:[a-z0-9\-]{6,}", "个人 provider 别名"),
    (r"[\u4e00-\u9fa5]{0,4}实测[^\n]{0,40}(我|本机|个人)", "个人实测叙述"),
]

# 绝不该进版本库的文件
FORBIDDEN_FILES = [
    "sites.json", "dsh_auth.json", "host_alias.json", "cache_prices.json",
    "cost_ledger.json", "proxy_calibration.json", "store.db", ".env",
    "collect_state.json",
]

SCAN_EXT = (".py", ".md", ".json", ".spec", ".txt", ".yml", ".yaml", ".ps1", ".bat")


def _run_git(args):
    try:
        r = subprocess.run(["git"] + args, cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="ignore")
        return r.stdout.splitlines() if r.returncode == 0 else []
    except Exception:
        return []


def collect_files(staged_only=False):
    if staged_only:
        files = _run_git(["diff", "--cached", "--name-only"])
    else:
        files = _run_git(["ls-files", "--cached", "--others", "--exclude-standard"])
    out = []
    # 排除体检脚本自身（否则它自己的规则定义会被自己命中）
    self_rel = os.path.relpath(os.path.abspath(__file__), ROOT).replace("\\", "/")
    for f in files:
        if f.replace("\\", "/") == self_rel:
            continue
        if f.endswith(SCAN_EXT):
            out.append(f)
    return out


def scan(path, rules):
    """返回 [(行号, 类型, 片段)]"""
    hits = []
    full = os.path.join(ROOT, path)
    if not os.path.isfile(full):
        return hits
    try:
        with open(full, encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f, 1):
                for pat, name in rules:
                    m = re.search(pat, line)
                    if m:
                        hits.append((i, name, line.strip()[:80]))
                        break
    except Exception:
        pass
    return hits


def main():
    ap = argparse.ArgumentParser(description="发布前隐私体检")
    ap.add_argument("--staged", action="store_true",
                    help="只检查已 git add 的内容（pre-commit 用）")
    a = ap.parse_args()

    print("=" * 64)
    print("发布前体检 —— %s" % ("暂存区" if a.staged else "工作区"))
    print("项目根目录: %s" % ROOT)
    print("=" * 64)

    files = collect_files(a.staged)
    print("\n待检查文件：%d 个\n" % len(files))

    blocked, warned = [], []

    # ① 禁止文件
    for f in files:
        base = os.path.basename(f)
        if base in FORBIDDEN_FILES:
            blocked.append((f, 0, "禁止提交的私有文件", base))
        # 数据库变体
        if re.search(r"\.db(-wal|-shm)?$", base):
            blocked.append((f, 0, "禁止提交的数据库文件", base))

    # ② 内容扫描
    for f in files:
        for ln, name, frag in scan(f, BLOCK):
            blocked.append((f, ln, name, frag))
        for ln, name, frag in scan(f, WARN):
            warned.append((f, ln, name, frag))

    if blocked:
        print("❌ 严重问题（%d 处）—— 必须处理，不能发布：\n" % len(blocked))
        for f, ln, name, frag in blocked:
            loc = "L%d" % ln if ln else "-"
            print("  %-42s %-6s [%s]" % (f, loc, name))
            print("      %s" % frag)
        print()
    else:
        print("✅ 无严重问题\n")

    if warned:
        print("⚠️  提醒（%d 处）—— 请确认是否要保留：\n" % len(warned))
        for f, ln, name, frag in warned[:25]:
            print("  %-42s L%-5d [%s] %s" % (f, ln, name, frag))
        if len(warned) > 25:
            print("  ... 还有 %d 处" % (len(warned) - 25))
        print()

    print("=" * 64)
    if blocked:
        print("结论：❌ 不通过 —— 先处理上面 %d 处严重问题" % len(blocked))
        return 1
    print("结论：✅ 通过 —— 可以发布")
    if warned:
        print("        （有 %d 处提醒，建议人工扫一眼）" % len(warned))
    return 0


if __name__ == "__main__":
    sys.exit(main())
