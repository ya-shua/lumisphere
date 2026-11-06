"""浏览器管理器：一个使用独立 profile 的 Chromium 实例。

设计要点
--------
1. 独立 profile：数据目录固定在项目下 data/profile，和用户日常浏览器完全隔离，
   平台不会把两边合并成一个实例。
2. 后台线程 + 独立事件循环：Playwright 的异步 API 全部跑在一条专属线程里，
   主线程留给托盘（Windows 上托盘必须占住主线程）。
3. 线程安全：外部通过 submit() 把协程丢进那个事件循环执行，结果同步返回。
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Coroutine, TypeVar

from playwright.async_api import BrowserContext, Page, async_playwright

log = logging.getLogger(__name__)

T = TypeVar("T")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BrowserManager:
    def __init__(self, cfg: dict, root: Path) -> None:
        b = cfg.get("browser") or {}
        self._root = root
        self._profile_dir = (root / b.get("profile_dir", "data/profile")).resolve()
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        # 用哪个浏览器内核："" = Playwright 自带 Chromium；chrome / msedge = 系统已装的
        self._channel: str = (b.get("channel") or "").strip()
        self._start_url: str = (b.get("start_url") or "about:blank").strip()
        self._width = int(b.get("width", 1366))
        self._height = int(b.get("height", 860))
        self._extra_args: list[str] = list(b.get("args") or [])

        # 标签页限额：防止 AI 一口气开几十个视频页把电脑拖死
        lim = cfg.get("limits") or {}
        self._max_tabs = max(1, int(lim.get("max_tabs", 6)))
        self._one_per_domain = bool(lim.get("one_per_domain", True))
        self._max_per_domain = max(1, int(lim.get("max_per_domain", 2)))
        self._limit_task: asyncio.Task | None = None
        self._closed_by_limit: int = 0

        # 搜索 / 抓正文专用的后台页面，不参与用户浏览
        self._aux: Page | None = None
        self._aux_lock: asyncio.Lock | None = None

        # 登录二维码推送到 QQ（由 main 注入 QQNotifier）
        self.notifier = None
        self.on_login_needed = None
        self._login_prompted = 0
        self._login_pending = False
        self._login_pending_at = 0.0

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._boot_error: BaseException | None = None

        self._pw = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._pid: int | None = None
        # 本次启动产生的所有浏览器进程（Chromium 是多进程架构，
        # 窗口往往挂在子进程上，只认主 pid 会找不到窗口）
        self._pids: set[int] = set()
        # 浏览器是否还连着（用户关掉最后一个窗口时 Chromium 会整个退出）
        self._connected = False
        # 正在主动关闭（用来区分"用户关窗口"和"我们自己关"）
        self._closing = False
        # 最后一次停留的网址：窗口被关掉后重新打开时用它恢复
        self._last_url: str | None = None

        # 由 main.py 注入：外部（/quit 接口）请求退出时调用
        self.request_quit = lambda: None
        # 由 main.py 注入：浏览器断开时回调（用来刷新托盘状态）
        self.on_disconnected = lambda: None

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def profile_dir(self) -> Path:
        return self._profile_dir

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def pids(self) -> set[int]:
        return set(self._pids)

    @property
    def is_running(self) -> bool:
        return self._connected and self._context is not None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self, timeout: float = 120.0) -> None:
        self._thread = threading.Thread(
            target=self._thread_main, name="lumisphere-browser", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("浏览器启动超时（可能 Chromium 内核未安装）")
        if self._boot_error is not None:
            raise self._boot_error

    def relaunch(self, timeout: float = 120.0) -> None:
        """浏览器被关掉后，从托盘里重新拉起来。"""
        if self._loop is None:
            raise RuntimeError("浏览器线程未启动")
        self.submit(self._relaunch(), timeout)

    def submit(self, coro: Coroutine[Any, Any, T], timeout: float = 90.0) -> T:
        """把协程提交到浏览器线程执行，并同步等结果。"""
        if self._loop is None:
            raise RuntimeError("浏览器线程未启动")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def shutdown(self, timeout: float = 30.0) -> None:
        if self._loop is None:
            return
        try:
            self.submit(self._close(), timeout)
        except Exception:
            log.exception("关闭浏览器时出错")
        finally:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
            if self._thread is not None:
                self._thread.join(timeout=10)

    # ------------------------------------------------------------------
    # 线程 / 事件循环
    # ------------------------------------------------------------------
    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._boot())
        except BaseException as exc:  # noqa: BLE001
            self._boot_error = exc
            log.exception("浏览器启动失败")
        finally:
            self._ready.set()

        if self._boot_error is None:
            try:
                self._loop.run_forever()
            except Exception:
                log.exception("事件循环异常")

        try:
            self._loop.run_until_complete(self._close())
        except Exception:
            pass
        try:
            self._loop.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 启动 / 关闭
    # ------------------------------------------------------------------
    def _clean_stale_locks(self) -> None:
        """清掉上次没退干净留下的锁。

        直接关机、蓝屏、任务管理器强杀之后，Chromium 来不及删掉这几个文件，
        下次启动就会报"数据目录被占用"或者干脆起不来。
        """
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            path = self._profile_dir / name
            try:
                if path.is_symlink() or path.exists():
                    path.unlink()
                    log.info("清理残留锁文件：%s", name)
            except Exception:
                log.debug("清理锁文件失败：%s", name, exc_info=True)

    # ------------------------------------------------------------------
    # 标签页限额（防止一口气开几十个视频页把电脑拖死）
    # ------------------------------------------------------------------
    @staticmethod
    def _host_of(url: str) -> str:
        """取出域名，用来判断"是不是同一个站"。"""
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    async def _limit_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return
            try:
                await self._enforce_limits()
            except asyncio.CancelledError:
                return
            except Exception:
                log.debug("标签页限额检查失败", exc_info=True)

    async def _enforce_limits(self) -> int:
        """关掉多余的标签页，返回关掉了几个。"""
        if self._context is None:
            return 0

        pages = [p for p in self._context.pages if not p.is_closed()]
        if len(pages) <= 1:
            return 0

        # 正在用的那个永远不关
        protected: set[int] = set()
        if self._page is not None and not self._page.is_closed():
            protected.add(id(self._page))
        if self._aux is not None and not self._aux.is_closed():
            protected.add(id(self._aux))

        close_ids: set[int] = set()

        # 规则一：同一个域名超额 → 关掉旧的，留最新的那个
        per_host_keep = 1 if self._one_per_domain else self._max_per_domain
        grouped: dict[str, list] = {}
        for p in pages:
            host = self._host_of(p.url)
            if host:
                grouped.setdefault(host, []).append(p)
        for group in grouped.values():
            if len(group) <= per_host_keep:
                continue
            keep_page = None
            for p in group:
                if id(p) in protected:
                    keep_page = p
                    break
            if keep_page is None:
                keep_page = group[-1]
            for p in group:
                if p is not keep_page:
                    close_ids.add(id(p))

        # 规则二：总数还是超 → 从最旧的开始关
        remaining = [p for p in pages if id(p) not in close_ids]
        while len(remaining) > self._max_tabs:
            victim = None
            for p in remaining:
                if id(p) not in protected:
                    victim = p
                    break
            if victim is None:
                break
            close_ids.add(id(victim))
            remaining.remove(victim)

        close_ids -= protected

        closed = 0
        for p in pages:
            if id(p) not in close_ids:
                continue
            try:
                await p.close()
                closed += 1
            except Exception:
                log.debug("关闭标签页失败", exc_info=True)

        if closed:
            self._closed_by_limit += closed
            log.info(
                "标签页限额：关掉了 %d 个，现在剩 %d 个（上限 %d，同域最多 %d）",
                closed,
                len([p for p in self._context.pages if not p.is_closed()]),
                self._max_tabs,
                per_host_keep,
            )
        return closed

    async def _boot(self, start_url: str | None = None) -> None:
        self._pw = await async_playwright().start()

        # 用 Edge 内核时进程名是 msedge.exe，Chromium/Chrome 是 chrome.exe
        image_name = "msedge.exe" if self._channel == "msedge" else "chrome.exe"
        before = await asyncio.to_thread(_list_browser_pids, image_name)

        launch_kwargs = {
            "user_data_dir": str(self._profile_dir),
            "headless": False,
            "no_viewport": True,
            "args": [
                f"--window-size={self._width},{self._height}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=Translate,AcceptCHFrame,MediaRouter",
                *self._extra_args,
            ],
        }
        if self._channel:
            launch_kwargs["channel"] = self._channel

        # 硬关机 / 崩溃之后 Chromium 会在数据目录里留下锁文件，不清理会起不来
        self._clean_stale_locks()

        try:
            self._context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            # 再清一次锁、再来一遍：关机重启后最常见的失败就是锁没清干净
            self._clean_stale_locks()
            try:
                self._context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
            except Exception:
                channel_hint = (
                    f"\n当前配置的内核：{self._channel}（config.yaml → browser.channel）"
                    if self._channel
                    else "\n当前使用 Playwright 自带 Chromium，如未安装请执行："
                    "\n.venv\\Scripts\\playwright.exe install chromium"
                )
                raise RuntimeError(
                    f"浏览器启动失败。\n\n数据目录：{self._profile_dir}\n"
                    "如果上次没正常退出，请先结束残留的浏览器进程，或删掉该目录下的 "
                    f"SingletonLock / SingletonSocket 后重试。{channel_hint}"
                ) from None

        after = await asyncio.to_thread(_list_browser_pids, image_name)
        new_pids = sorted(after - before)
        self._pids = set(new_pids)
        self._pid = new_pids[0] if new_pids else None
        log.info("浏览器进程：%s（主 pid=%s）", sorted(self._pids), self._pid)

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        url = start_url or self._start_url
        if url and url != "about:blank":
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                log.warning("首页加载失败：%s", url, exc_info=True)

        # 用户关掉最后一个窗口时 Chromium 会整个退出，这里要能察觉
        try:
            self._context.on("close", self._on_context_close)
            self._context.on("page", self._track_page)
        except Exception:
            log.debug("注册 close 事件失败", exc_info=True)

        self._track_page(self._page)

        self._connected = True
        if self._limit_task is None or self._limit_task.done():
            self._limit_task = asyncio.create_task(self._limit_loop())
        log.info("浏览器已启动 pid=%s profile=%s", self._pid, self._profile_dir)

    def _track_page(self, page) -> None:
        """记住页面最后停在哪，窗口被关掉后好恢复。"""
        try:
            page.on("framenavigated", self._on_navigated)
        except Exception:
            log.debug("注册导航事件失败", exc_info=True)

    def _on_navigated(self, frame) -> None:
        try:
            page = frame.page
            if frame == page.main_frame:
                url = page.url
                if url.startswith("http"):
                    self._last_url = url
        except Exception:
            log.debug("记录网址失败", exc_info=True)

    def _on_context_close(self, *_args) -> None:
        if self._closing:
            return
        self._connected = False
        log.info("浏览器已断开（窗口被关掉了）")
        try:
            self.on_disconnected()
        except Exception:
            log.debug("断开回调失败", exc_info=True)

    async def _relaunch(self) -> None:
        """关掉再开：用户把窗口关了以后，从托盘重新拉起。

        尽量回到关掉之前那一页，这样"关窗口"用起来就像"隐藏"。
        """
        await self._close()
        await self._boot(self._last_url)

    async def _close(self) -> None:
        self._closing = True
        if self._limit_task is not None:
            self._limit_task.cancel()
            self._limit_task = None
        try:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    log.debug("关闭 context 失败", exc_info=True)
                self._context = None
                self._page = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    log.debug("关闭 playwright 失败", exc_info=True)
                self._pw = None
        finally:
            self._connected = False
            self._closing = False
        log.info("浏览器已关闭")

    # ------------------------------------------------------------------
    # 常用操作（供 server / 托盘 调用）
    # ------------------------------------------------------------------
    async def live_page(self) -> Page:
        """取一个当前活着的页面。

        Edge 在运行中可能重建标签页（首次运行引导、恢复会话等），
        启动时抓到的那个 page 对象可能已经失效，所以每次都重新找。
        """
        if self._context is None:
            raise RuntimeError("浏览器未运行")
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            page = await self._context.new_page()
            self._page = page
            return page
        self._page = pages[0]
        return self._page

    def ensure_page(self) -> Page:
        page = self._page
        if page is None or page.is_closed():
            raise RuntimeError("浏览器页面不可用")
        return page

    async def goto(self, url: str) -> str:
        page = await self.live_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return page.url

    # ------------------------------------------------------------------
    # 登录识别：卡在扫码登录页时，把二维码拍到 QQ 里让人扫
    # ------------------------------------------------------------------
    LOGIN_URL_HINTS = (
        "login", "passport", "signin", "sign-in", "auth/",
        "account/login", "sso.", "/oauth", "checklogin",
    )
    LOGIN_TEXT_HINTS = (
        "扫码登录", "二维码登录", "扫一扫登录", "请登录", "登录后查看",
        "手机扫码", "微信扫码", "打开app扫码", "打开APP扫码", "扫描二维码",
    )

    _JS_LOGIN_SCAN = r"""
    () => {
      const sx = window.scrollX || 0, sy = window.scrollY || 0;
      const boxes = [];
      document.querySelectorAll('img,canvas').forEach(el => {
        const r = el.getBoundingClientRect();
        const w = r.width || el.width || 0;
        const h = r.height || el.height || 0;
        if (w < 100 || h < 100) return;
        if (Math.abs(w - h) / Math.max(w, h) > 0.15) return;
        const vh = window.innerHeight || 900;
        if (r.bottom < 0 || r.top > vh) return;
        boxes.push({
          x: Math.round(r.x + sx), y: Math.round(r.y + sy),
          w: Math.round(w), h: Math.round(h),
          src: (el.src || el.getAttribute('data-src') || '').slice(0, 200),
          tag: el.tagName
        });
      });
      return {
        boxes: boxes,
        text: document.body ? (document.body.innerText || '').slice(0, 4000) : '',
        title: document.title || '',
        url: location.href
      };
    }
    """

    async def detect_login(self, page: Page | None = None) -> dict:
        """判断当前页是不是卡在扫码登录。宁可漏报，不误报。"""
        page = page or await self.live_page()
        data = await page.evaluate(self._JS_LOGIN_SCAN) or {}
        url = (data.get("url") or "")
        url_l = url.lower()
        text = data.get("text") or ""
        boxes = data.get("boxes") or []

        url_hit = any(h in url_l for h in self.LOGIN_URL_HINTS)
        text_hit = any(h in text for h in self.LOGIN_TEXT_HINTS)

        # 找二维码，分三档（可信度从高到低）：
        #   1. src 里带 qr / code 的，是明牌二维码
        #   2. src 是 data: / blob: 的，通常是前端现场生成的二维码（B站就是这种）
        #   3. 其余必须够大 —— 否则抖音首页那种 128x123 的按钮背景图会被误认
        qr = None
        for b in boxes:
            src = (b.get("src") or "").lower()
            if ("qr" in src or "code" in src) and b["w"] >= 100:
                qr = b
                break
        if qr is None:
            for b in boxes:
                src = (b.get("src") or "").lower()
                if (src.startswith("data:") or src.startswith("blob:")) and b["w"] >= 120:
                    qr = b
                    break
        if qr is None:
            for b in boxes:
                if b["w"] >= 150 and b["h"] >= 150:
                    qr = b
                    break

        # 判定标准：只认"网址就是登录页"。
        # 光看有没有二维码不可靠 —— 抖音首页这种正常浏览的页面里也挂着
        # 一个"扫一扫"的引导二维码（data: 图片），会被误判成卡在登录页。
        need = bool(url_hit)
        reasons = []
        if url_hit:
            reasons.append("网址像登录页")
        if text_hit:
            reasons.append("页面出现扫码/登录字样")
        if qr is not None:
            reasons.append("找到二维码 %dx%d" % (qr["w"], qr["h"]))

        return {
            "need_login": need,
            "url": url,
            "title": data.get("title") or "",
            "reason": "、".join(reasons) or "没有登录迹象",
            "has_qr": qr is not None,
            "qr": qr,
        }

    async def qr_screenshot(self, page: Page | None = None, pad: int = 24) -> bytes:
        """优先只截二维码那一小块（清晰、体积小），找不到就整页。"""
        page = page or await self.live_page()
        info = await self.detect_login(page)
        qr = info.get("qr")
        if qr:
            vp = page.viewport_size or {"width": 1366, "height": 860}
            x = max(0, qr["x"] - pad)
            y = max(0, qr["y"] - pad)
            w = min(qr["w"] + pad * 2, max(50, vp["width"] - x))
            h = min(qr["h"] + pad * 2, max(50, vp["height"] - y))
            try:
                return await page.screenshot(
                    type="jpeg",
                    quality=88,
                    clip={"x": x, "y": y, "width": w, "height": h},
                    timeout=15000,
                )
            except Exception:
                log.debug("二维码裁剪失败，退回整页", exc_info=True)
        return await page.screenshot(type="jpeg", quality=70, timeout=20000)

    # 二维码过期后页面上常见的提示，出现这些就要先点"刷新"
    EXPIRED_HINTS = ("已过期", "已失效", "点击刷新", "刷新二维码", "重新获取", "二维码失效")

    _JS_CLICK_REFRESH = r"""
    () => {
      const words = ['点击刷新', '刷新二维码', '重新获取', '刷新', '点击重新获取'];
      const nodes = Array.from(document.querySelectorAll('button, a, div, span, p, i'));
      for (const el of nodes) {
        const t = (el.innerText || '').trim();
        if (!t || t.length > 24) continue;
        if (!words.some(w => t.includes(w))) continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        try { el.click(); return t; } catch (e) {}
      }
      return '';
    }
    """

    async def qr_expired(self, page: Page | None = None) -> bool:
        """页面上是不是已经写着"二维码已过期"。"""
        page = page or await self.live_page()
        try:
            text = await page.evaluate(
                "() => document.body ? (document.body.innerText || '').slice(0, 2000) : ''"
            )
        except Exception:
            return False
        return any(h in (text or "") for h in self.EXPIRED_HINTS)

    async def click_refresh_qr(self, page: Page | None = None) -> str:
        """点掉"点击刷新"，让二维码重新生成。返回点中的文字（空=没找到）。"""
        page = page or await self.live_page()
        try:
            hit = await page.evaluate(self._JS_CLICK_REFRESH)
        except Exception:
            log.debug("点击刷新失败", exc_info=True)
            return ""
        if hit:
            log.info("二维码过期，已点「%s」刷新", hit)
        return hit or ""

    async def notify_login_now(self, text: str = "") -> dict:
        """手动推一次：有二维码就裁那一块，没有就发整页。

        主动调用就代表"现在想让人看这一页"，所以不再拦 need_login。
        text 留空 = 只发图；重发时建议带一句"刚才那张过期了"。
        """
        page = await self.live_page()
        info = await self.detect_login(page)

        # 二维码过期了就先点刷新，否则截到的还是旧码
        refreshed = ""
        if await self.qr_expired(page):
            refreshed = await self.click_refresh_qr(page)
            if refreshed:
                await asyncio.sleep(1.8)  # 等新码渲染出来
                info = await self.detect_login(page)

        notifier = self.notifier
        if notifier is None or not getattr(notifier, "ready", False):
            return {"ok": False, "reason": "QQ 通知未启用或没配收件人", **info}
        img = await self.qr_screenshot(page)
        note = (text or "").strip()
        ok = notifier.send_image(img, note or None)  # 默认只发图
        if ok:
            self._login_prompted += 1
        out = dict(info)
        if len(out.get("url") or "") > 200:
            out["url"] = out["url"][:200] + "…"
        return {
            "ok": ok,
            "bytes": len(img),
            "hint": "图已发出，等对方扫码。可以调 POST /login/wait 阻塞等待结果。",
            **out,
        }

    async def wait_login(self, timeout: float = 120.0, interval: float = 3.0) -> dict:
        """等扫码完成：页面不再是登录页就算成功。

        AI 发完二维码后调这个，就不必自己反复轮询了。
        """
        import time as _t

        start = _t.time()
        while True:
            try:
                page = await self.live_page()
                info = await self.detect_login(page)
                if not info.get("need_login"):
                    return {
                        "ok": True,
                        "logged_in": True,
                        "waited": round(_t.time() - start, 1),
                        "url": info.get("url"),
                        "title": info.get("title"),
                    }
            except Exception:
                log.debug("等待登录时检查失败", exc_info=True)
            if _t.time() - start >= timeout:
                return {
                    "ok": True,
                    "logged_in": False,
                    "waited": round(_t.time() - start, 1),
                    "reason": "超时，页面还停在登录页（对方可能没扫/没扫成功/二维码过期）",
                }
            await asyncio.sleep(interval)

    async def video_probe(self) -> dict:
        """看看当前页上有没有能播的视频。"""
        page = await self.live_page()
        data = await page.evaluate(
            r"""
            () => {
              const vids = Array.from(document.querySelectorAll('video'));
              return {
                found: vids.length,
                videos: vids.map(v => ({
                  src: (v.currentSrc || v.src || '').slice(0, 140),
                  duration: Math.round((v.duration || 0) * 10) / 10,
                  width: v.videoWidth || 0,
                  height: v.videoHeight || 0,
                  paused: !!v.paused,
                  readyState: v.readyState
                })),
                iframes: document.querySelectorAll('iframe').length,
                url: location.href,
                title: document.title
              };
            }
            """
        )
        return data or {}

    _JS_SEEK = r"""
    (ratio) => {
      const v = document.querySelector('video');
      if (!v || !isFinite(v.duration) || v.duration <= 0) return null;
      v.scrollIntoView({block: 'center', inline: 'center'});
      const t = Math.max(0, Math.min(v.duration * ratio, v.duration - 0.2));
      v.currentTime = t;
      try { v.pause(); } catch (e) {}
      return {t: t, duration: v.duration};
    }
    """

    _JS_VIDEO_BOX = r"""
    () => {
      const v = document.querySelector('video');
      if (!v) return null;
      const r = v.getBoundingClientRect();
      if (r.width < 80 || r.height < 60) return null;
      return {
        x: Math.round(r.x + (window.scrollX || 0)),
        y: Math.round(r.y + (window.scrollY || 0)),
        w: Math.round(r.width),
        h: Math.round(r.height)
      };
    }
    """

    async def video_frames(
        self,
        count: int = 3,
        ratios: list[float] | None = None,
        settle: float = 1.0,
    ) -> dict:
        """按时间比例抽几帧，存成 jpg，返回文件路径列表。

        画面交给视觉模型去读，这里只负责"把那一刻拍下来"。
        """
        n = max(1, min(6, int(count or 3)))
        if ratios:
            use = [max(0.0, min(0.99, float(r))) for r in ratios][:6]
        else:
            use = [round(i / (n + 1), 3) for i in range(1, n + 1)]

        page = await self.live_page()
        probe = await self.video_probe()
        if not probe.get("found"):
            return {
                "ok": False,
                "reason": "这个页面上没找到 <video> 元素（可能是 iframe 里的播放器）",
                "probe": probe,
            }

        out_dir = self._profile_dir.parent.parent / "logs" / "frames"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        frames: list[dict] = []

        for idx, ratio in enumerate(use, 1):
            seek = await page.evaluate(self._JS_SEEK, ratio)
            if not seek:
                continue
            await asyncio.sleep(settle)
            box = await page.evaluate(self._JS_VIDEO_BOX)
            try:
                if box:
                    img = await page.screenshot(
                        type="jpeg",
                        quality=82,
                        clip={"x": box["x"], "y": box["y"], "width": box["w"], "height": box["h"]},
                        timeout=15000,
                    )
                else:
                    img = await page.screenshot(type="jpeg", quality=72, timeout=15000)
            except Exception:
                log.debug("抽帧截图失败 ratio=%s", ratio, exc_info=True)
                continue
            path = out_dir / f"{stamp}_{idx}.jpg"
            path.write_bytes(img)
            frames.append(
                {
                    "index": idx,
                    "ratio": ratio,
                    "time": round(float(seek["t"]), 1),
                    "duration": round(float(seek["duration"]), 1),
                    "path": str(path),
                    "bytes": len(img),
                }
            )

        return {
            "ok": bool(frames),
            "url": probe.get("url"),
            "title": probe.get("title"),
            "duration": (frames[0]["duration"] if frames else (probe.get("videos") or [{}])[0].get("duration")),
            "count": len(frames),
            "frames": frames,
            "dir": str(out_dir),
        }

    async def send_shot(self, text: str = "") -> dict:
        """把当前页面截图发到 QQ（托盘和接口共用）。"""
        notifier = self.notifier
        if notifier is None or not getattr(notifier, "ready", False):
            return {"ok": False, "reason": "QQ 推送未启用或没配收件人"}
        try:
            page = await self.live_page()
            img = await page.screenshot(type="jpeg", quality=82, timeout=20000)
            url = page.url
            title = await page.title()
        except Exception as exc:
            return {"ok": False, "reason": f"截图失败：{exc}"}
        note = (text or "").strip()
        ok = notifier.send_image(img, note or None)
        return {"ok": ok, "bytes": len(img), "url": url, "title": title}

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        page = await self.live_page()
        return await page.screenshot(type="png", full_page=full_page, timeout=20000)

    async def status(self) -> dict:
        page = None
        url = title = None
        tabs = 0
        if self._context is not None:
            try:
                tabs = len([p for p in self._context.pages if not p.is_closed()])
            except Exception:
                tabs = 0
            try:
                page = await self.live_page()
                url = page.url
                title = await page.title()
            except Exception:
                log.debug("读取页面状态失败", exc_info=True)
        return {
            "running": self._connected and self._context is not None,
            "connected": self._connected,
            "pid": self._pid,
            "url": url,
            "title": title,
            "tabs": tabs,
            "closed_by_limit": self._closed_by_limit,
            "login_prompted": self._login_prompted,
            "profile_dir": str(self._profile_dir),
        }

    async def tabs_info(self) -> list[dict]:
        """列出当前所有标签页（方便排查"怎么开了这么多"）。"""
        out: list[dict] = []
        if self._context is None:
            return out
        for i, p in enumerate(self._context.pages):
            if p.is_closed():
                continue
            try:
                out.append({"index": i, "url": p.url, "title": await p.title()})
            except Exception:
                out.append({"index": i, "url": p.url, "title": None})
        return out

    async def close_extra_tabs(self) -> int:
        """手动触发一次限额清理。"""
        return await self._enforce_limits()

    # ------------------------------------------------------------------
    # 联网：搜索 / 抓正文
    # ------------------------------------------------------------------
    SEARCH_ENGINES = {
        "bing": "https://www.bing.com/search?q={q}",
        "baidu": "https://www.baidu.com/s?wd={q}",
    }

    _SEARCH_SELECTORS = {
        # [结果块, 标题链接, 摘要]
        "bing": ["li.b_algo", "h2 a", ".b_caption p"],
        "baidu": ["div.result, div.c-container", "h3 a", ".c-abstract"],
    }

    _JS_SEARCH = r"""
    (sel) => {
      const [itemSel, titleSel] = sel;
      const out = [];
      document.querySelectorAll(itemSel).forEach(el => {
        // 百度/必应的推广位：有的带 data-tuiguang，有的只在角落写一个"广告"
        if (el.hasAttribute('data-tuiguang') || el.hasAttribute('data-landingpage')) return;
        const adMark = Array.from(el.querySelectorAll('span,div,i,em'))
          .some(s => (s.innerText || '').trim() === '广告');
        if (adMark) return;
        const a = el.querySelector(titleSel);
        if (!a) return;
        const title = (a.innerText || '').trim();
        if (!title) return;
        const full = (el.innerText || '').replace(/\s+/g, ' ').trim();
        const snippet = full.slice(title.length).trim().slice(0, 250);
        out.push({ title: title, url: a.href || '', snippet: snippet });
      });
      return out;
    }
    """

    _JS_TEXT = r"""
    () => {
      const title = document.title || '';
      if (!document.body) return {title: title, text: ''};
      const d = document.body.cloneNode(true);
      d.querySelectorAll('script,style,noscript,nav,header,footer,aside,iframe,svg,form,button').forEach(e => e.remove());
      let t = d.innerText || '';
      t = t.replace(/\r/g, '').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n');
      return {title: title, text: t.trim()};
    }
    """

    async def _aux_page(self) -> Page:
        """搜索/抓取专用的后台页面（受限额保护，不会被当成多余标签关掉）。"""
        if self._context is None:
            raise RuntimeError("浏览器未运行")
        if self._aux_lock is None:
            self._aux_lock = asyncio.Lock()
        if self._aux is None or self._aux.is_closed():
            self._aux = await self._context.new_page()
        return self._aux

    async def search(self, keyword: str, num: int = 10, engine: str = "bing") -> dict:
        """搜一个关键词，返回 [{title, url, snippet}]。"""
        from urllib.parse import quote

        kw = (keyword or "").strip()
        if not kw:
            raise ValueError("缺少关键词")
        eng = (engine or "bing").lower()
        tmpl = self.SEARCH_ENGINES.get(eng) or self.SEARCH_ENGINES["bing"]
        sel = self._SEARCH_SELECTORS.get(eng) or self._SEARCH_SELECTORS["bing"]
        url = tmpl.format(q=quote(kw))

        if self._aux_lock is None:
            self._aux_lock = asyncio.Lock()
        async with self._aux_lock:
            page = await self._aux_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(sel[0], timeout=8000)
            except Exception:
                log.debug("搜索结果选择器等超时：%s", sel[0])
            items = await page.evaluate(self._JS_SEARCH, sel) or []

        results = [r for r in items if r.get("url")][: max(1, int(num))]
        for i, r in enumerate(results, 1):
            r["index"] = i
        return {
            "engine": eng,
            "keyword": kw,
            "count": len(results),
            "results": results,
        }

    async def fetch_text(self, url: str, max_chars: int = 20000) -> dict:
        """打开一个网址，把正文抽成纯文字。"""
        target = (url or "").strip()
        if not target:
            raise ValueError("缺少 url")
        if not target.startswith(("http://", "https://")):
            target = "https://" + target

        if self._aux_lock is None:
            self._aux_lock = asyncio.Lock()
        async with self._aux_lock:
            page = await self._aux_page()
            await page.goto(target, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            data = await page.evaluate(self._JS_TEXT) or {}
            final_url = page.url

        raw = (data.get("text") or "").strip()
        total = len(raw)
        limit = max(500, int(max_chars))
        text = raw if total <= limit else raw[:limit] + f"\n\n…（已截断，原文共 {total} 字）"
        return {
            "url": final_url,
            "title": data.get("title") or "",
            "chars": total,
            "text": text,
        }

    async def page_text(self, max_chars: int = 20000) -> dict:
        """读取"当前正在看的那一页"的正文。"""
        page = await self.live_page()
        data = await page.evaluate(self._JS_TEXT) or {}
        raw = (data.get("text") or "").strip()
        total = len(raw)
        limit = max(500, int(max_chars))
        text = raw if total <= limit else raw[:limit] + f"\n\n…（已截断，原文共 {total} 字）"
        return {
            "url": page.url,
            "title": data.get("title") or "",
            "chars": total,
            "text": text,
        }


# ----------------------------------------------------------------------
# 辅助：列出当前所有 chrome.exe 进程号
# ----------------------------------------------------------------------
def _list_browser_pids(image_name: str = "chrome.exe") -> set[int]:
    """列出指定进程名的所有 pid。

    ⚠️ 这里不能用 tasklist：程序是 pythonw 起的（没有控制台），
    tasklist 在没有控制台的父进程里会 rc=0 却什么都不返回 ——
    stdout 直接是 None，一调就把整个启动流程打挂（AttributeError）。

    改用 Toolhelp32 快照：纯 ctypes，不开子进程，也不依赖控制台。
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot is None or snapshot == 0 or snapshot == INVALID_HANDLE_VALUE:
            log.debug("进程快照拿不到")
            return set()

        target = image_name.lower()
        pids: set[int] = set()
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return set()
            while True:
                if entry.szExeFile.lower() == target:
                    pids.add(int(entry.th32ProcessID))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return pids
    except Exception:
        log.debug("枚举进程失败", exc_info=True)
        return set()
