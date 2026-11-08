"""检查更新 + 一键更新。

只覆盖代码文件；config.yaml、data/、logs/、.venv/ 一律不动 ——
更新不该弄丢你的登录态和配置。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .version import REPO, __version__

API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
UA = {"User-Agent": "LumiSphere-Updater"}


def _git_blob_sha(data: bytes) -> str:
    """算 git blob 的 sha：sha1("blob <长度>\\0" + 内容)。

    GitHub 的 tree 接口给的就是这个值，拿它比对就知道下下来的文件
    是不是同一个东西 —— 中间被人换了内容就对不上。
    """
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _fetch_tree_shas(tag: str, timeout: float) -> dict[str, str]:
    """取某个 tag 对应提交的 {路径: git blob sha}。"""
    with urllib.request.urlopen(
        urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{tag}", headers=UA),
        timeout=timeout,
    ) as resp:
        commit = json.loads(resp.read().decode("utf-8"))
    tree_sha = commit["commit"]["tree"]["sha"]

    with urllib.request.urlopen(
        urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/git/trees/{tree_sha}?recursive=1", headers=UA
        ),
        timeout=timeout,
    ) as resp:
        tree = json.loads(resp.read().decode("utf-8"))

    return {t["path"]: t["sha"] for t in tree.get("tree", []) if t.get("type") == "blob"}

# 更新时只认这几个（其余一律不碰）
UPDATE_FILES = ("main.py", "mcp_server.py", "requirements.txt", "README.md", "LICENSE")
UPDATE_DIRS = ("core", "scripts", "docs", "webui", ".githooks", ".github")

# 就算 zip 里有，也绝对不覆盖
PROTECTED = {"config.yaml", "config.example.yaml", "data", "logs", ".venv", ".git"}


def _ver_tuple(text: str) -> tuple[int, ...]:
    """把 "v0.2.1" 之类的变成 (0, 2, 1)，比不出来就当成 0。"""
    cleaned = (text or "").strip().lstrip("vV")
    parts = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def check(timeout: float = 8.0) -> dict:
    """问一次 GitHub：有没有新版本。网络不通就返回 ok=False，不抛异常。"""
    req = urllib.request.Request(API_LATEST, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # 网络问题不该影响主程序
        return {"ok": False, "reason": f"检查更新失败：{exc}", "current": __version__}

    latest = (data.get("tag_name") or "").lstrip("vV")
    has_new = _ver_tuple(latest) > _ver_tuple(__version__)
    return {
        "ok": True,
        "current": __version__,
        "latest": latest or "?",
        "has_new": has_new,
        "name": data.get("name") or "",
        "url": data.get("html_url") or f"https://github.com/{REPO}/releases",
        "notes": (data.get("body") or "").strip()[:1200],
        "zip_url": data.get("zipball_url") or "",
        "tag": data.get("tag_name") or "",
    }


def _safe_extract(zip_path: Path, target: Path) -> None:
    """解压，但不许解到目录外面去（zip slip）。"""
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            dest = (target / member.filename).resolve()
            if not str(dest).startswith(str(root)):
                raise RuntimeError("压缩包里有可疑路径，已中止：" + member.filename)
        zf.extractall(target)


def apply_update(
    project_dir: Path, zip_url: str = "", timeout: float = 60.0, tag: str = ""
) -> dict:
    """下载最新版并覆盖代码文件。返回 {ok, copied, note}。

    调用方负责在成功后重启程序。传了 tag 就会做完整性校验。
    """
    project_dir = Path(project_dir).resolve()

    if not zip_url:
        info = check()
        if not info.get("ok"):
            return {"ok": False, "reason": info.get("reason")}
        if not info.get("has_new"):
            return {"ok": False, "reason": f"已经是最新版（{__version__}）"}
        zip_url = info["zip_url"]
        tag = tag or (info.get("tag") or "")

    work = Path(tempfile.mkdtemp(prefix="lumisphere-update-"))
    try:
        archive = work / "release.zip"
        req = urllib.request.Request(zip_url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(archive, "wb") as fh:
            shutil.copyfileobj(resp, fh)

        unpacked = work / "unpacked"
        _safe_extract(archive, unpacked)

        tops = [p for p in unpacked.iterdir() if p.is_dir()]
        if len(tops) != 1:
            return {"ok": False, "reason": "压缩包结构不认识（应该只有一个顶层目录）"}
        src = tops[0]

        # 完整性校验：逐个文件比对 GitHub 记录的指纹。
        # 中间被人篡改过（或者下到半截），这里就对不上，立刻中止不覆盖。
        if tag:
            try:
                expected = _fetch_tree_shas(tag, timeout)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": f"取不到校验信息，不敢覆盖：{exc}"}

            bad: list[str] = []
            for rel, want in expected.items():
                f = src / rel
                if not f.is_file():
                    continue
                try:
                    if _git_blob_sha(f.read_bytes()) != want:
                        bad.append(rel)
                except OSError:
                    continue
            if bad:
                shown = "、".join(bad[:5]) + ("…" if len(bad) > 5 else "")
                return {
                    "ok": False,
                    "reason": f"下载的包和 GitHub 记录对不上（{len(bad)} 个文件），已中止：{shown}",
                }

        copied = 0
        for name in UPDATE_FILES:
            source = src / name
            if source.is_file():
                shutil.copy2(source, project_dir / name)
                copied += 1

        for name in UPDATE_DIRS:
            source = src / name
            if not source.is_dir():
                continue
            for item in source.rglob("*"):
                if item.is_dir():
                    continue
                rel = item.relative_to(src)
                if rel.parts and rel.parts[0] in PROTECTED:
                    continue
                dest = project_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                copied += 1

        return {
            "ok": True,
            "copied": copied,
            "note": "代码已更新。config.yaml 和 data/ 没动，重启程序生效。",
        }
    except Exception as exc:
        return {"ok": False, "reason": f"更新失败：{exc}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)