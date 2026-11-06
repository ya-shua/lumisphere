#!/usr/bin/env python3
"""隐私门禁：提交前扫一遍，别把不该出门的东西带上。

用法：
  python scripts/check_privacy.py --staged   # pre-commit 用，只看暂存区
  python scripts/check_privacy.py --all      # CI 用，看所有被跟踪的文件

命中任何一条就 exit 1，提交会被挡下来。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认是 GBK，不重设一下打中文会炸
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

# ---------------------------------------------------------------- 路径黑名单
# .gitignore 是第一道（根本不会被 add），这里是第二道：
# 万一 .gitignore 被人改了、或者用了 git add -f，也得拦住。
BANNED_PATHS = [
    (re.compile(r"(^|/)config\.yaml$"), "本地配置（含网关 token / QQ 号）"),
    (re.compile(r"(^|/)data/"), "浏览器数据（登录态、cookie、缓存）"),
    (re.compile(r"(^|/)logs/"), "运行日志与截图"),
    (re.compile(r"(^|/)\.venv/"), "Python 虚拟环境"),
    (re.compile(r"(^|/)__pycache__/"), "Python 缓存"),
    (re.compile(r"(^|/)\.env$"), "环境变量文件"),
    (re.compile(r"\.(db|sqlite3?|log)$"), "本地数据库 / 日志"),
    (re.compile(r"(^|/)bili_credential\.json$"), "B站登录凭证"),
    (re.compile(r"(^|/)\.private_values\.json$"), "本地敏感值表（真实值全在里面）"),
]

# ---------------------------------------------------------------- 内容规则
# (名字, 正则, 说明)
RULES = [
    (
        "OneBot / access token",
        re.compile(r"access[_\-]?token\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}", re.I),
        "网关的 access_token 不能进门",
    ),
    (
        "通用密钥赋值",
        re.compile(
            r"(?:password|passwd|pwd|secret|api[_\-]?key|access[_\-]?token)\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{12,}",
            re.I,
        ),
        "看起来像密钥",
    ),
    ("GitHub PAT", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"), "GitHub token"),
    ("OpenAI 风格 key", re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "sk- 开头的密钥"),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (
        "私钥",
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
        "私钥不能进门",
    ),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (
        "配置里的 QQ 号",
        re.compile(r"(?:target_id|qq|uin|user_id)\s*[:=]\s*[\"']?[1-9]\d{4,10}", re.I),
        "配置里写了 QQ 号",
    ),
    (
        "独立的 QQ 号",
        re.compile(r"(?:QQ|qq|扣扣|企鹅)\s*(?:号|是|为)?\s*[:：=]?\s*[1-9]\d{4,11}(?!\d)"),
        "写了 QQ 号",
    ),
    (
        "带标签的手机号",
        re.compile(r"(?:手机|电话|tel|phone|mobile)\s*[:：=]?\s*\+?1[3-9]\d{9}", re.I),
        "写了手机号",
    ),
    (
        "内网地址",
        re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),
        "内网 IP",
    ),
]

# ---------------------------------------------------------------- 已知真实值
# 真实值不写在这个脚本里 —— 脚本本身也是公开的，写进来等于把秘密一起公开。
# 它们放在同级目录的 .private_values.json（.gitignore 已排除），运行时加载。
# 文件不存在就只跑通用规则（CI 和别人的机器上就是这种情况，不需要也不该有它）。
PRIVATE_FILE = Path(__file__).resolve().parent / ".private_values.json"


def load_private() -> tuple[list[tuple[str, str]], list[str]]:
    """读本地敏感值表，返回 (已知真实值对, 额外敏感词)。"""
    if not PRIVATE_FILE.exists():
        return [], []
    try:
        data = json.loads(PRIVATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[门禁] {PRIVATE_FILE.name} 读不了（{exc}），只跑通用规则。")
        return [], []

    known: list[tuple[str, str]] = []
    for item in data.get("known_values") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            name, value = str(item[0]).strip(), str(item[1]).strip()
            if name and value:
                known.append((name, value))

    words = [str(w).strip() for w in (data.get("extra_words") or []) if str(w).strip()]
    return known, words


KNOWN_VALUES: list[tuple[str, str]] = []
EXTRA_WORDS: list[str] = []

# 允许出现的无害串（示例占位、文档说明）
ALLOW_PATTERNS = [
    re.compile(r"127\.0\.0\.1"),
    re.compile(r"0\.0\.0\.0"),
    re.compile(r"example\.com"),
    re.compile(r"xxxx", re.I),
]

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".ini",
    ".cfg", ".bat", ".sh", ".ps1", ".js", ".mjs", ".ts", ".html", ".css",
    ".xml", ".csv", "",
}

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".zip", ".7z", ".gz", ".exe", ".dll", ".so", ".pyd", ".pyc",
}


def git_files(staged: bool) -> list[str]:
    if staged:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        print("[门禁] 拿不到文件列表，git 说：" + (out.stderr or "").strip())
        sys.exit(2)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def check_path(path: str) -> list[str]:
    low = path.replace("\\", "/")
    hits = []
    for pattern, why in BANNED_PATHS:
        if pattern.search(low):
            hits.append(f"{path}  ← 这个路径不该进仓库（{why}）")
            break
    return hits


def check_content(path: str) -> list[str]:
    suffix = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""
    if suffix in SKIP_SUFFIXES:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="strict") as fh:
            text = fh.read(2_000_000)
    except (UnicodeDecodeError, OSError):
        return []  # 二进制或读不了，路径规则已经管过了

    hits = []
    lines = text.splitlines()

    for name, value in KNOWN_VALUES:
        if value in text:
            for no, line in enumerate(lines, 1):
                if value in line:
                    hits.append(f"{path}:{no}  出现过{name}（真实值，必须删掉）")
                    break

    known_texts = {value for _name, value in KNOWN_VALUES}
    for word in EXTRA_WORDS:
        if word in known_texts:  # 上面已经报过，别重复
            continue
        if word in text:
            for no, line in enumerate(lines, 1):
                if word in line:
                    hits.append(f"{path}:{no}  出现过敏感词「{word}」（必须删掉）")
                    break

    for no, line in enumerate(lines, 1):
        if any(p.search(line) for p in ALLOW_PATTERNS):
            continue
        for name, pattern, why in RULES:
            if pattern.search(line):
                snippet = line.strip()[:90]
                hits.append(f"{path}:{no}  {name} —— {why}\n      {snippet}")
    return hits


def main() -> int:
    global KNOWN_VALUES, EXTRA_WORDS
    KNOWN_VALUES, EXTRA_WORDS = load_private()

    staged = "--staged" in sys.argv
    files = git_files(staged)
    if not files:
        print("[门禁] 没有要检查的文件。")
        return 0

    problems: list[str] = []
    for path in files:
        problems.extend(check_path(path))
        problems.extend(check_content(path))

    if not problems:
        print(f"[门禁] 通过 —— {len(files)} 个文件没发现隐私泄露。")
        return 0

    print("=" * 60)
    print("[门禁] 拦下了，先处理这些再提交：")
    print("=" * 60)
    for p in problems:
        print("  [x] " + p)
    print("=" * 60)
    print(f"一共 {len(problems)} 处。")
    print("确实要放行（比如那只是文档里的示例）：把值换成占位符，")
    print("或者改 scripts/check_privacy.py 里的规则 —— 别用 git add -f 绕过。")
    return 1


if __name__ == "__main__":
    sys.exit(main())