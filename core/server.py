"""本地控制端口：给刷视频插件 / 外部程序调用。

只监听 127.0.0.1，外部网络碰不到。
命令都通过 BrowserManager.submit() 转给浏览器线程执行。
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .settings_io import MODES, read_runtime, write_runtime
from .version import __version__

ROOT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PAGE = ROOT_DIR / "webui" / "index.html"

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
STARTED_AT = time.time()


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

    def _send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

    # ---- 安全闸门 ----
    # 这个端口的权限很大：能开网页、截屏、读正文。所以每个请求都要过一遍。
    _LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}

    @staticmethod
    def _hostname_of(header: str) -> str:
        """从 Host / Origin 头里抠主机名，去掉端口，保留 IPv6 的方括号。"""
        h = (header or "").strip().lower()
        if not h:
            return ""
        if h.startswith("["):  # [::1]:8765
            end = h.find("]")
            return h[: end + 1] if end != -1 else h
        return h.rsplit(":", 1)[0] if ":" in h else h

    def _is_local_client(self) -> bool:
        """请求是不是从本机发出来的。"""
        ip = (self.client_address[0] or "").strip()
        return ip in ("127.0.0.1", "::1") or ip.startswith("127.")

    def _guard(self, path: str = "") -> bool:
        """限流 → Host/Origin → token，任何一关不过就直接回错误。"""
        srv = self.server
        client = self.client_address[0]

        limiter = getattr(srv, "rate_limiter", None)
        if limiter is not None and not limiter.allow(client):
            self._send_json({"error": "too many requests"}, 429)
            return False

        # 防 DNS rebinding：恶意网页把自己的域名解析成 127.0.0.1，
        # 浏览器就以为是同源，能读到这里返回的内容。校验 Host 就挡住了。
        name = self._hostname_of(self.headers.get("Host"))
        if name:
            allowed = getattr(srv, "allowed_hosts", set()) or set()
            if getattr(srv, "bind_loopback", True):
                ok = name in self._LOOPBACK_NAMES or name in allowed
            else:
                ok = name in allowed or name == (getattr(srv, "bind_host", "") or "").lower()
            if not ok:
                log.warning("拒绝可疑 Host 头：%r（来自 %s）", name, client)
                self._send_json({"error": "forbidden host"}, 403)
                return False

        # 浏览器带 Origin 就说明是跨域来的，必须同源
        origin = self.headers.get("Origin")
        if origin:
            if origin.strip().lower() == "null":
                self._send_json({"error": "forbidden origin"}, 403)
                return False
            oname = self._hostname_of(urlparse(origin).netloc)
            allowed = getattr(srv, "allowed_hosts", set()) or set()
            if oname not in self._LOOPBACK_NAMES and oname not in allowed:
                log.warning("拒绝跨站 Origin：%r（来自 %s）", origin, client)
                self._send_json({"error": "forbidden origin"}, 403)
                return False

        # 设置页只开放给本机，而且不要求 token ——
        # 能坐在这台机器前的人本来就能直接改 config.yaml，加一道 token 只是让他打不开页面。
        if path == "/" or path.startswith("/api/"):
            if not self._is_local_client():
                self._send_json({"error": "设置页只允许本机访问"}, 403)
                return False
            return True

        # 本机模式不校验令牌：配了也不生效。
        # 这样从 bridge 切回 local 时，残留的 token 不会把调用方挡在外面。
        if getattr(srv, "mode", "local") == "local":
            return True

        expected = getattr(srv, "api_token", "") or ""
        if expected:
            got = self.headers.get("X-Auth-Token") or ""
            if not hmac.compare_digest(got, expected):
                self._send_json(
                    {"error": "unauthorized", "hint": "缺少或错误的 X-Auth-Token 请求头"}, 401
                )
                return False

        return True

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if not self._guard(path):
                return

            # ---- 设置页 ----
            if path == "/":
                try:
                    html = SETTINGS_PAGE.read_bytes()
                except OSError:
                    self._send_json({"error": "设置页文件缺失（webui/index.html）"}, 500)
                    return
                self._send_html(html)
                return

            if path == "/api/health":
                # 契约（docs/插件对接契约.md）：只报「进程活着」，不检查浏览器连着没
                self._send_json({
                    "ok": True,
                    "id": "lumisphere",
                    "version": __version__,
                    "uptime_s": round(time.time() - STARTED_AT, 1),
                })
                return

            if path == "/api/manifest":
                try:
                    meta = json.loads((ROOT_DIR / "metadata.json").read_text(encoding="utf-8-sig"))
                except Exception:
                    meta = {}
                self._send_json({
                    "ok": True,
                    "metadata": meta,
                    "runtime": {
                        "port": self.server.server_address[1],
                        "pid": os.getpid(),
                        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "browser_ok": bool(getattr(self.manager, "is_connected", False)),
                    },
                    "capabilities": [
                        {"name": "打开网页", "kind": "http", "how": "POST /open {\"url\": ...}"},
                        {"name": "搜索", "kind": "http", "how": "POST /search {\"q\": ...}"},
                        {"name": "抓正文", "kind": "http", "how": "POST /fetch {\"url\": ...}"},
                        {"name": "截图", "kind": "http", "how": "GET /screenshot（PNG）"},
                        {"name": "标签页管理", "kind": "http", "how": "GET /tabs、POST /tabs/clean"},
                        {"name": "视频抽帧", "kind": "http", "how": "POST /video/frames"},
                    ],
                })
                return

            if path == "/api/config":
                cfg_path = getattr(self.server, "config_path", None)
                rt = read_runtime(cfg_path) if cfg_path else {}
                self._send_json({
                    "ok": True,
                    "version": __version__,
                    "mode": str(rt.get("mode") or "local"),
                    "api_token_set": bool(str(rt.get("api_token") or "").strip()),
                    # 设置页只给本机看，所以这里直接给明文 ——
                    # 能读到它的人本来就能直接打开 config.yaml
                    "api_token": str(rt.get("api_token") or ""),
                    "host": getattr(self.server, "bind_host", ""),
                    "port": self.server.server_address[1],
                    "browser_ok": bool(getattr(self.manager, "is_connected", False)),
                })
                return

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
            if not self._guard(path):
                return
            body = self._read_json()

            # ---- 设置页的两个接口 ----
            if path == "/api/token":
                import secrets

                self._send_json({"ok": True, "token": secrets.token_urlsafe(32)})
                return

            if path == "/api/config":
                cfg_path = getattr(self.server, "config_path", None)
                if cfg_path is None:
                    self._send_json({"error": "服务没拿到配置文件路径"}, 500)
                    return

                mode = str(body.get("mode") or "").strip().lower()
                if mode not in MODES:
                    self._send_json({"error": f"不认识的模式：{mode or '（空）'}"}, 400)
                    return

                token_raw = body.get("api_token")
                token = None if token_raw is None else str(token_raw).strip()

                try:
                    write_runtime(cfg_path, mode, token)
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"error": f"写配置失败：{exc}"}, 500)
                    return

                log.info("运行模式已改为 %s（重启后生效）", mode)
                self._send_json({"ok": True, "restart": True, "mode": mode})
                return

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


class _RateLimiter:
    """按客户端 IP 的滑动窗口，防止端口被当成跳板刷请求。"""

    def __init__(self, per_minute: int = 240) -> None:
        self.per_minute = max(10, int(per_minute))
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            cutoff = now - 60.0
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            if len(self._hits) > 1024:  # 别让字典无限长
                for k in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(k, None)
            return True


def _is_loopback(host: str) -> bool:
    """这个监听地址是不是只在本机可见。"""
    h = (host or "").strip().lower()
    if h in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        return True
    try:
        return ipaddress.ip_address(h.strip("[]")).is_loopback
    except ValueError:
        return False


class ControlServer:
    def __init__(
        self, cfg: dict, manager, window_ctrl=None, config_path: Path | None = None
    ) -> None:
        s = cfg.get("server") or {}
        r = cfg.get("runtime") or {}
        self.config_path = config_path

        # 运行模式：local 只在本机；其它模式监听对外地址，必须带 token
        self.mode = str(r.get("mode") or "local").strip().lower()
        self.api_token = str(r.get("api_token") or "").strip()
        self.allowed_hosts = {
            str(x).strip().lower() for x in (r.get("allowed_hosts") or []) if str(x).strip()
        }

        self.enabled = bool(s.get("enabled", True))
        self.port = int(s.get("port", 8765))

        host = str(s.get("host") or "").strip()
        if not host:
            host = "127.0.0.1" if self.mode == "local" else "0.0.0.0"
        self.host = host
        self.bind_loopback = _is_loopback(host)

        self.rate_limiter = _RateLimiter(int(s.get("rate_limit_per_min") or 240))

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

        # 安全闸：对外监听却不设 token，等于把浏览器交给任何能连上的人
        if not self.bind_loopback and not self.api_token:
            log.error(
                "拒绝启动控制端口：监听 %s（对外）但没配 runtime.api_token。"
                "这样任何人都能开网页、截屏、读页面正文。"
                "请先设一个 token，或用 mode: local 只绑本机。",
                self.host,
            )
            return False

        handler_cls = type("BoundHandler", (_Handler,), {"manager": self.manager})
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        except OSError as exc:
            log.warning("控制端口 %s:%s 绑定失败：%s（可能已被占用）", self.host, self.port, exc)
            return False

        self._httpd.daemon_threads = True
        # handler 的安全闸门要用的东西
        self._httpd.window_ctrl = self.window_ctrl
        self._httpd.api_token = self.api_token
        self._httpd.allowed_hosts = self.allowed_hosts
        self._httpd.bind_loopback = self.bind_loopback
        self._httpd.bind_host = self.host
        self._httpd.rate_limiter = self.rate_limiter
        self._httpd.config_path = self.config_path
        self._httpd.mode = self.mode

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="lumisphere-http", daemon=True)
        self._thread.start()
        log.info(
            "控制端口已开启：%s（%s%s）",
            self.url,
            "仅本机" if self.bind_loopback else "对外开放",
            "，带 token 校验"
            if (self.mode != "local" and self.api_token)
            else "",
        )
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None