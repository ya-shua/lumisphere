"""把二维码/截图推送到 QQ。

走 SnowLuma 的 OneBot v11 HTTP 接口，直接用标准库，不引入新依赖。

为什么不用现成的聊天工具发？因为那些只有"发文字"的接口，
发图片需要自己拼 OneBot 的 image 段 —— 就是这里干的事。
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class QQNotifier:
    def __init__(self, cfg: dict | None = None):
        c = cfg or {}
        self.enabled = bool(c.get("enabled", False))
        self.base_url = str(c.get("onebot_url") or "http://127.0.0.1:3000").rstrip("/")
        self.token = str(c.get("access_token") or "").strip()
        self.target_type = str(c.get("target_type") or "private").lower()
        try:
            self.target_id = int(c.get("target_id") or 0)
        except Exception:
            self.target_id = 0
        self.cooldown = max(0, int(c.get("cooldown_sec") or 600))
        self._last_sent: dict[str, float] = {}

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.target_id)

    # ------------------------------------------------------------------
    def _post(self, action: str, payload: dict, timeout: float = 30.0) -> dict:
        url = f"{self.base_url}/{action}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"status": "bad_json", "raw": raw[:300]}

    def _build_message(self, segments: list[dict]) -> dict:
        if self.target_type == "group":
            return {"group_id": self.target_id, "message": segments}
        return {"user_id": self.target_id, "message": segments}

    def _action(self) -> str:
        return "send_group_msg" if self.target_type == "group" else "send_private_msg"

    # ------------------------------------------------------------------
    def send_text(self, text: str) -> bool:
        if not self.ready or not text.strip():
            return False
        try:
            r = self._post(self._action(), self._build_message([{"type": "text", "data": {"text": text}}]))
            ok = r.get("status") == "ok"
            if not ok:
                log.warning("QQ 发送文字失败：%s", r)
            return ok
        except Exception as exc:
            log.warning("QQ 发送文字异常：%s", exc)
            return False

    def send_image(self, png: bytes, text: str | None = None) -> bool:
        """发一张图（可附一句文字）。"""
        if not self.ready or not png:
            return False
        b64 = base64.b64encode(png).decode("ascii")
        segments: list[dict] = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        segments.append({"type": "image", "data": {"file": f"base64://{b64}"}})
        try:
            r = self._post(self._action(), self._build_message(segments), timeout=60.0)
            ok = r.get("status") == "ok"
            if ok:
                log.info("已推送图片到 QQ（%s %s，%d KB）", self.target_type, self.target_id, len(png) // 1024)
            else:
                log.warning("QQ 发送图片失败：%s", r)
            return ok
        except Exception as exc:
            log.warning("QQ 发送图片异常：%s", exc)
            return False

    # ------------------------------------------------------------------
    def should_send(self, key: str) -> bool:
        """同一个网址在冷却期内只发一次，避免刷屏。"""
        import time

        now = time.time()
        last = self._last_sent.get(key)
        if last and now - last < self.cooldown:
            return False
        self._last_sent[key] = now
        # 顺手清理过期的记录
        if len(self._last_sent) > 200:
            cutoff = now - self.cooldown * 2
            self._last_sent = {k: v for k, v in self._last_sent.items() if v >= cutoff}
        return True