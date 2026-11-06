"""本地控制端口：给刷视频插件 / 外部程序调用。

只监听 127.0.0.1，外部网络碰不到。
命令都通过 BrowserManager.submit() 转给浏览器线程执行。
"""
from __future__ import annotations

import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024


class _Handler(BaseHTTPRequestHandler):
    manager = None  # 由 Server.attach 注入
    server_version = "LumiSphere/0.1"

    # ---- 工具 ----
    def log_message(self, fmt, *args):  # 收敛默认日志噪音
        log.debug("HTTP " + fmt, *args)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_png(self, png: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.end_headers()
        self.wfile.write(png)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _run(self, coro):
        return self.manager.submit(coro)

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/status":
                self._send_json(self._run(self.manager.status()))
                return
            if path == "/screenshot":
                png = self._run(self.manager.screenshot())
                self._send_png(png)
                return
            if path == "/screenshot/base64":
                png = self._run(self.manager.screenshot())
                self._send_json({"png_base64": base64.b64encode(png).decode("ascii")})
                return
            if path == "/tabs":
                self._send_json({"tabs": self._run(self.manager.tabs_info())})
                return
            if path == "/login/detect":
                self._send_json(self._run(self.manager.detect_login()))
                return
            if path == "/text":
                from urllib.parse import parse_qs, urlparse as _u

                qs = parse_qs(_u(self.path).query)
                try:
                    mc = int((qs.get("max_chars") or ["20000"])[0])
                except Exception:
                    mc = 20000
                self._send_json(self._run(self.manager.page_text(mc)))
                return
            if path == "/video/probe":
                self._send_json(self._run(self.manager.video_probe()))
                return
            self._send_json({"error": "not found", "path": path}, 404)
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s 失败", self.path)
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self):  # noqa: N802
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            body = self._read_json()

            if path == "/open":
                url = str(body.get("url") or "").strip()
                if not url:
                    self._send_json({"error": "缺少 url"}, 400)
                    return
                self._send_json({"ok": True, "url": self._run(self.manager.goto(url))})
                return

            if path == "/search":
                kw = str(body.get("keyword") or "").strip()
                if not kw:
                    self._send_json({"error": "缺少 keyword"}, 400)
                    return
                try:
                    num = int(body.get("num") or 10)
                except Exception:
                    num = 10
                engine = str(body.get("engine") or "bing")
                self._send_json({"ok": True, **self._run(self.manager.search(kw, num, engine))})
                return

            if path == "/fetch":
                target = str(body.get("url") or "").strip()
                if not target:
                    self._send_json({"error": "缺少 url"}, 400)
                    return
                try:
                    mc = int(body.get("max_chars") or 20000)
                except Exception:
                    mc = 20000
                self._send_json({"ok": True, **self._run(self.manager.fetch_text(target, mc))})
                return

            if path == "/browser/start":
                if not self.manager.is_connected:
                    self.manager.relaunch()
                self._send_json({"ok": True, "connected": self.manager.is_connected})
                return

            if path == "/tabs/clean":
                self._send_json({"ok": True, "closed": self._run(self.manager.close_extra_tabs())})
                return

            if path == "/login/notify":
                note = str(body.get("text") or "")
                self._send_json(self._run(self.manager.notify_login_now(note)))
                return

            if path == "/shot/send":
                note = str(body.get("text") or "")
                self._send_json(self._run(self.manager.send_shot(note)))
                return

            if path == "/video/frames":
                try:
                    count = int(body.get("count") or 3)
                except Exception:
                    count = 3
                ratios = body.get("ratios")
                if ratios is not None and not isinstance(ratios, list):
                    ratios = None
                self._send_json(self._run(self.manager.video_frames(count, ratios)))
                return

            if path == "/login/wait":
                try:
                    t = float(body.get("timeout") or 120.0)
                except Exception:
                    t = 120.0
                t = max(5.0, min(300.0, t))
                self._send_json(
                    self.manager.submit(self.manager.wait_login(t), timeout=t + 20)
                )
                return

            if path == "/window/hide":
                wc = getattr(self.server, "window_ctrl", None)
                pids = self.manager.pids
                n = wc.hide(pids) if wc else 0
                self._send_json({
                    "ok": True,
                    "hidden": n,
                    "pids": sorted(pids),
                    "cached": wc.cached if wc else [],
                })
                return

            if path == "/window/show":
                n = self.server.window_ctrl.show(self.manager.pids) if self.server.window_ctrl else 0
                self._send_json({"ok": True, "shown": n})
                return

            if path == "/quit":
                self._send_json({"ok": True})
                threading.Thread(target=self.manager.request_quit, daemon=True).start()
                return

            self._send_json({"error": "not found", "path": path}, 404)
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s 失败", self.path)
            self._send_json({"error": str(exc)}, 500)


class ControlServer:
    def __init__(self, cfg: dict, manager, window_ctrl=None) -> None:
        s = cfg.get("server") or {}
        self.enabled = bool(s.get("enabled", True))
        self.host = str(s.get("host", "127.0.0.1"))
        self.port = int(s.get("port", 8765))
        self.manager = manager
        self.window_ctrl = window_ctrl
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> bool:
        if not self.enabled:
            log.info("控制端口已关闭（config.server.enabled = false）")
            return False

        handler_cls = type("BoundHandler", (_Handler,), {"manager": self.manager})
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        except OSError as exc:
            log.warning("控制端口 %s:%s 绑定失败：%s（可能已被占用）", self.host, self.port, exc)
            return False

        self._httpd.daemon_threads = True
        # 让 handler 能拿到窗口控制器
        self._httpd.window_ctrl = self.window_ctrl
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="lumisphere-http", daemon=True)
        self._thread.start()
        log.info("控制端口已开启：%s", self.url)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None