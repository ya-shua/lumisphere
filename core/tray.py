"""系统托盘：微光域在任务栏右下角的那颗小图标。

Windows 上托盘必须占住主线程（pystray 会自己跑消息循环），
所以浏览器的活儿全部交给 BrowserManager 的后台线程。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from .update import apply_update, check
from .version import __version__

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent

# MessageBoxW 的常用标志
MB_OK = 0x00
MB_ICONWARNING = 0x30
MB_ICONINFO = 0x40
MB_YESNO = 0x04
MB_ICONQUESTION = 0x20
IDYES = 6

MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000


def _msgbox(text: str, title: str = "微光域", flags: int = MB_ICONINFO) -> int:
    """弹一个 Windows 原生对话框。

    ⚠️ 只能在**后台线程**里调，不能在托盘的菜单回调里调 ——
    MessageBoxW 是模态的，会抢走当前线程的消息循环；
    而 pystray 的消息循环就在那个线程里，一抢就会「窗口关不掉、菜单点不动」。
    所有需要弹框的地方一律 threading.Thread(...).start() 出去。

    hwnd 传 0 没有 owner，窗口可能被别的窗口盖住，所以强制置顶 + 抢焦点。
    """
    flags |= MB_TOPMOST | MB_SETFOREGROUND
    try:
        from ctypes import windll

        return windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:  # 非 Windows 或不支持，退化成日志
        log.info("%s：%s", title, text)
        return 0


# 同一时间只允许一个更新流程在跑（防止连点弹出好几个框）
_update_lock = threading.Lock()


def make_icon(running: bool = True) -> Image.Image:
    """画托盘图标。

    Windows 的托盘会自动把图标缩到 16x16，所以：
      · 不透明深色底（半透明边缘缩放后会糊成脏块）
      · 一颗高饱和的实心圆 + 白色中心点，缩到 16px 也认得出
    """
    size = 64
    bg = (18, 22, 32, 255)                     # 不透明深底
    ring = (96, 168, 255, 255) if running else (150, 150, 150, 255)
    core = (255, 255, 255, 255)

    img = Image.new("RGBA", (size, size), bg)
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, size - 7, size - 7), fill=ring)          # 实心圆
    d.ellipse((22, 22, size - 23, size - 23), fill=core)      # 白色圆心
    return img


def build_tray(
    manager,
    window_ctrl,
    *,
    on_quit,
    on_log=None,
) -> pystray.Icon:
    """构造托盘图标与菜单。

    manager     : BrowserManager
    window_ctrl : WindowController
    on_quit     : 无参回调，点「完全退出」时调用
    on_log      : 可选，(msg) -> None
    """

    def _log(msg: str) -> None:
        log.info(msg)
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    def _guard(fn):
        """托盘回调里任何异常都不能把消息循环带崩。"""
        def wrapper(icon, item):
            try:
                fn(icon, item)
            except Exception:
                log.exception("托盘操作失败：%s", getattr(item, "text", "?"))
        return wrapper

    def _ensure_browser() -> bool:
        """浏览器被关掉了就重新拉起来，返回是否可用。"""
        if manager.is_connected:
            return True
        _log("浏览器不在运行，正在重新启动……")
        manager.relaunch()
        return manager.is_connected

    @_guard
    def on_open(icon, item):
        try:
            _ensure_browser()
        except Exception as exc:  # noqa: BLE001
            _log(f"启动浏览器失败：{exc}")
            return
        time.sleep(0.8)
        window_ctrl.refresh(manager.pids)
        n = window_ctrl.show(manager.pids)
        icon.icon = make_icon(True)
        icon.title = "微光域 · 运行中"
        _log(f"显示浏览器（{n} 个窗口）" if n else "浏览器已启动，窗口稍后出现")

    @_guard
    def on_hide(icon, item):
        n = window_ctrl.hide(manager.pids)
        _log(f"已隐藏浏览器窗口（{n} 个），后台继续运行" if n else "没有可隐藏的窗口")

    @_guard
    def on_restart(icon, item):
        _log("正在重启浏览器……")
        manager.relaunch()
        time.sleep(0.8)
        window_ctrl.refresh(manager.pids)
        window_ctrl.show(manager.pids)
        icon.icon = make_icon(True)
        icon.title = "微光域 · 运行中"
        _log("浏览器已重启")

    def _send_shot_worker(icon):
        """截图 + 发 QQ 可能要等几秒，放后台，别占着托盘线程。"""
        _log("正在截图并发送到 QQ……")
        try:
            r = manager.submit(manager.send_shot())
        except Exception as exc:  # noqa: BLE001
            log.exception("截图发送失败")
            _log(f"截图发送失败：{exc}")
            return

        if r.get("ok"):
            _log("已发送到 QQ（%d KB）" % (r.get("bytes", 0) // 1024))
            try:
                icon.notify("截图已发到 QQ", "微光域")
            except Exception:
                pass
        else:
            _log("没发出去：" + str(r.get("reason") or "未知原因"))

    @_guard
    def on_send_shot(icon, item):
        """把当前页面截图发到 QQ（需要登录时点这个最快）。"""
        threading.Thread(target=_send_shot_worker, args=(icon,), daemon=True).start()

    def _check_update_worker(icon):
        """整个更新流程都在后台线程里跑：查版本、下载、弹框，一个都不占托盘线程。"""
        if not _update_lock.acquire(blocking=False):
            _log("上一次更新还没结束，这次点击忽略了")
            return
        try:
            _log("正在检查更新……")
            info = check()
            if not info.get("ok"):
                reason = str(info.get("reason") or "未知原因")
                _log("检查更新失败：" + reason)
                _msgbox("检查更新失败：\n\n" + reason, flags=MB_ICONWARNING)
                return

            if not info.get("has_new"):
                _log(f"已是最新版（{info['current']}）")
                _msgbox(f"已经是最新版（{info['current']}）")
                return

            notes = (info.get("notes") or "（这次没有写更新说明）")[:800]
            ans = _msgbox(
                f"发现新版本 {info['latest']}（当前 {info['current']}）\n\n"
                f"{notes}\n\n"
                "要现在更新吗？\n"
                "只会覆盖代码，config.yaml 和 data/ 不会动。",
                flags=MB_YESNO | MB_ICONQUESTION,
            )
            if ans != IDYES:
                _log("用户取消了更新")
                return

            _log("正在下载更新……")
            result = apply_update(PROJECT_DIR, info.get("zip_url") or "")
            if result.get("ok"):
                _log(f"更新完成，覆盖了 {result.get('copied')} 个文件")
                _msgbox(
                    f"更新完成，覆盖了 {result.get('copied')} 个文件。\n\n"
                    "把微光域完全退出再打开，新版本就生效了。",
                )
            else:
                _log("更新失败：" + str(result.get("reason")))
                _msgbox("更新失败：\n\n" + str(result.get("reason")), flags=MB_ICONWARNING)
        except Exception:  # noqa: BLE001
            log.exception("检查更新出错")
            _log("检查更新出错，详情见 logs/lumisphere.log")
        finally:
            _update_lock.release()

    @_guard
    def on_check_update(icon, item):
        """问 GitHub 有没有新版本；有就一键覆盖代码。"""
        threading.Thread(target=_check_update_worker, args=(icon,), daemon=True).start()

    @_guard
    def on_open_data(icon, item):
        path = manager.profile_dir
        if path.exists():
            os.startfile(str(path))  # noqa: S606 - 打开自己的数据目录
            _log(f"打开数据目录：{path}")
        else:
            _log(f"数据目录不存在：{path}")

    @_guard
    def on_quit_clicked(icon, item):
        _log("正在退出……")
        on_quit()
        icon.stop()

    def _open_label(item):
        return "显示浏览器" if manager.is_connected else "启动浏览器"

    menu = pystray.Menu(
        pystray.MenuItem(_open_label, on_open, default=True),
        pystray.MenuItem("隐藏浏览器", on_hide),
        pystray.MenuItem("重启浏览器", on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("截图发到 QQ", on_send_shot),
        pystray.MenuItem("打开数据目录", on_open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"检查更新（v{__version__}）", on_check_update),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("完全退出", on_quit_clicked),
    )

    return pystray.Icon("lumisphere", make_icon(True), f"微光域 v{__version__} · 运行中", menu)