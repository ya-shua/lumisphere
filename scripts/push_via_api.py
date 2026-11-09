#!/usr/bin/env python3
"""github.com:443 连不上时，改用 api.github.com 把当前 HEAD 推上去。

背景：国内网络经常出现 api.github.com 能通、github.com 的 443 超时的情况，
`git push` 就会卡在 "Could not connect to server"。
这个脚本走 GitHub 的 Git Data API（blobs → tree → commit → ref），
整条链路都在 api.github.com 上。

用法：
  python scripts/push_via_api.py                 # 推到 main
  python scripts/push_via_api.py --dry-run       # 只算不推
  python scripts/push_via_api.py --branch dev    # 推到别的分支

token 从 C:\\Users\\Lenovo\\Desktop\\token.txt 读，不打印、不落盘。
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "ya-shua"
REPO = "lumisphere"
TOKEN_FILE = Path(r"%USERPROFILE%\Desktop\token.txt")
GIT = r"D:\Git\cmd\git.exe"

API_ROOT = f"https://api.github.com/repos/{OWNER}/{REPO}"
HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "lumisphere-push",
    "Content-Type": "application/json",
}


def read_token() -> str:
    if not TOKEN_FILE.exists():
        raise SystemExit(f"找不到 token 文件：{TOKEN_FILE}")
    tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if len(tok) < 20:
        raise SystemExit("token 文件看起来是空的")
    return tok


def api(tok: str, method: str, path: str, payload: dict | None = None) -> dict:
    url = API_ROOT + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={**HEADERS_BASE, "Authorization": f"token {tok}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"{method} {path} 失败 {exc.code}：{body}") from exc


def git(*args: str) -> str:
    out = subprocess.run(
        [GIT, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout


def ensure_branch(tok: str, branch: str) -> str | None:
    """返回远端分支的当前 sha；分支不存在返回 None。"""
    try:
        return api(tok, "GET", f"/git/refs/heads/{branch}")["object"]["sha"]
    except SystemExit:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tok = read_token()
    head = git("rev-parse", "HEAD").strip()
    message = git("log", "-1", "--pretty=%B", "HEAD").strip()
    files = [f for f in git("ls-tree", "-r", "HEAD", "--name-only").splitlines() if f]
    print(f"本地 HEAD {head[:10]}，{len(files)} 个文件")

    remote_sha = ensure_branch(tok, args.branch)
    if remote_sha == head:
        print("远端已经和本地一致，不用推。")
        return 0
    print(f"远端 {args.branch}：{remote_sha[:10] if remote_sha else '（空）'}")

    if remote_sha is None:
        print("远端还是空仓库，先落一个初始提交……")
        if not args.dry_run:
            api(
                tok,
                "PUT",
                "/contents/.gitkeep",
                {"message": "init: 初始化仓库", "content": ""},
            )
        remote_sha = api(tok, "GET", f"/git/refs/heads/{args.branch}")["object"]["sha"]

    # 每个文件 → blob
    tree = []
    for path in files:
        raw = Path(path).read_bytes()
        if args.dry_run:
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": "0" * 40})
            continue
        blob = api(
            tok,
            "POST",
            "/git/blobs",
            {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"},
        )
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"  blob {blob['sha'][:8]}  {path}")

    if args.dry_run:
        print(f"[dry-run] 会推 {len(tree)} 个文件到 {args.branch}，不动远端。")
        return 0

    tree_res = api(tok, "POST", "/git/trees", {"tree": tree})
    commit = api(
        tok,
        "POST",
        "/git/commits",
        {"message": message, "tree": tree_res["sha"], "parents": [remote_sha]},
    )
    api(tok, "PATCH", f"/git/refs/heads/{args.branch}", {"sha": commit["sha"], "force": False})

    print(f"\n推上去了：{args.branch} → {commit['sha']}")
    print(f"https://github.com/{OWNER}/{REPO}/commit/{commit['sha']}")
    print("（走的是 API，所以本地 HEAD 和远端 sha 不会一样，内容是一致的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())