#!/usr/bin/env python3
"""发版：读 core/version.py 的版本号 → 打 tag → 建 release。

用法：
  python scripts/release.py                     # 用当前版本号
  python scripts/release.py --version 0.2.0     # 顺手改版本号再发
  python scripts/release.py --notes notes.md    # 用文件当更新说明
  python scripts/release.py --dry-run

前提：代码已经推到 main（tag 指向远端最新 commit 时最稳）。
token 从 C:\\Users\\Lenovo\\Desktop\\token.txt 读。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "ya-shua"
REPO = "lumisphere"
TOKEN_FILE = Path(r"%USERPROFILE%\Desktop\token.txt")
VERSION_FILE = Path(__file__).resolve().parent.parent / "core" / "version.py"

API_ROOT = f"https://api.github.com/repos/{OWNER}/{REPO}"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "lumisphere-release",
    "Content-Type": "application/json",
}


def read_token() -> str:
    if not TOKEN_FILE.exists():
        raise SystemExit(f"找不到 token 文件：{TOKEN_FILE}")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise SystemExit("core/version.py 里找不到 __version__")
    return m.group(1)


def write_version(version: str) -> None:
    text = VERSION_FILE.read_text(encoding="utf-8")
    text = re.sub(
        r'__version__\s*=\s*["\'][^"\']+["\']',
        f'__version__ = "{version}"',
        text,
        count=1,
    )
    VERSION_FILE.write_text(text, encoding="utf-8")
    print(f"版本号已改成 {version}（记得提交这一行再推）")


def api(tok: str, method: str, path: str, payload: dict | None = None) -> dict:
    url = API_ROOT + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={**HEADERS, "Authorization": f"token {tok}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"{method} {path} 失败 {exc.code}：{body}") from exc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", help="顺手把 core/version.py 改成这个版本")
    ap.add_argument("--notes", help="更新说明文件（不填就用默认的一句话）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tok = read_token()

    if args.version:
        write_version(args.version)

    version = args.version or read_version()
    tag = f"v{version}"
    print(f"准备发 {tag}")

    head = api(tok, "GET", "/git/refs/heads/main")["object"]["sha"]
    print(f"main 当前：{head[:10]}")

    # tag 已存在就只补 release
    try:
        api(tok, "GET", f"/git/refs/tags/{tag}")
        print("tag 已存在，跳过创建")
    except SystemExit:
        if args.dry_run:
            print(f"[dry-run] 会创建 tag {tag} → {head[:10]}")
        else:
            tag_obj = api(
                tok,
                "POST",
                "/git/tags",
                {"tag": tag, "message": f"{tag} — 见 release 说明", "object": head, "type": "commit"},
            )
            api(tok, "POST", "/git/refs", {"ref": f"refs/tags/{tag}", "sha": tag_obj["sha"]})
            print(f"tag {tag} 已建")

    body = "（这次的更新说明还没写）"
    if args.notes:
        body = Path(args.notes).read_text(encoding="utf-8")
    elif (Path.cwd() / "RELEASE_NOTES.md").exists():
        body = (Path.cwd() / "RELEASE_NOTES.md").read_text(encoding="utf-8")

    if args.dry_run:
        print(f"[dry-run] 会创建 release {tag}")
        print("---- 说明 ----")
        print(body[:500])
        return 0

    # release 已存在就更新它
    try:
        existing = api(tok, "GET", f"/releases/tags/{tag}")
        rel = api(
            tok,
            "PATCH",
            f"/releases/{existing['id']}",
            {"name": tag, "body": body, "draft": False, "prerelease": False},
        )
        print("release 已更新：" + rel["html_url"])
    except SystemExit:
        rel = api(
            tok,
            "POST",
            "/releases",
            {"tag_name": tag, "name": tag, "body": body, "draft": False, "prerelease": False},
        )
        print("release 已建：" + rel["html_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())