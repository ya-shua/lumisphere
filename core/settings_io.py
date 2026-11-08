"""读写 config.yaml 里的 runtime 段。

只做最小的文本改动 —— 用户的 config.yaml 往往是手写带注释的，
整份 yaml.safe_dump 回去会把注释全冲掉，那是很讨厌的行为。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

MODES = ("local", "bridge", "cloud")

_HEAD = re.compile(r"^runtime\s*:")
_KEY = re.compile(r"^(\s+)([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def read_runtime(path: Path) -> dict:
    """读配置里的 runtime 段；没有就返回空 dict。"""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    return data.get("runtime") or {}


def _block_end(lines: list[str], start: int) -> int:
    """runtime 段到哪一行为止（下一个顶格的非空行）。"""
    for i in range(start + 1, len(lines)):
        raw = lines[i]
        if raw.strip() and not raw[:1].isspace():
            return i
    return len(lines)


def _set_key(block: list[str], key: str, value: str) -> list[str]:
    """在缩进块里把 key 的值换掉；没有这行就插到段首之下。"""
    for i, line in enumerate(block):
        m = _KEY.match(line)
        if m and m.group(2) == key:
            block[i] = f'{m.group(1)}{key}: "{value}"'
            return block

    indent = "  "
    for line in block[1:]:
        m = _KEY.match(line)
        if m:
            indent = m.group(1)
            break
    block.insert(1, f'{indent}{key}: "{value}"')
    return block


def write_runtime(path: Path, mode: str, api_token: str | None = None) -> None:
    """写回 runtime 段。

    api_token 传 None 表示『不动这一项』，传空字符串表示『清空』。
    """
    if mode not in MODES:
        raise ValueError(f"不认识的模式：{mode}")

    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()

    start = next((i for i, line in enumerate(lines) if _HEAD.match(line)), None)

    if start is None:
        new_block = [
            "runtime:",
            "  # local 只在本机用 | bridge 机器人云端·浏览器本地 | cloud 全部在云端",
            f'  mode: "{mode}"',
        ]
        if api_token is not None:
            new_block.append(f'  api_token: "{api_token}"')
        new_block.append("")

        anchor = next((i for i, line in enumerate(lines) if re.match(r"^server\s*:", line)), None)
        if anchor is None:
            lines.extend(new_block)
        else:
            lines[anchor:anchor] = new_block
    else:
        end = _block_end(lines, start)
        block = lines[start:end]
        block = _set_key(block, "mode", mode)
        if api_token is not None:
            block = _set_key(block, "api_token", api_token)
        lines[start:end] = block

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    path.write_text(body, encoding="utf-8")