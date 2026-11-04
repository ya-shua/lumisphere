"""微光域 LumiSphere —— 入口。

双击「启动.bat」或运行：
    .venv\\Scripts\\python.exe main.py
"""
from __future__ import annotations

import ctypes
import logging
import shutil
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

from core.browser import BrowserManager
from core.qq import QQNotifier
from core.server import ControlServer
from core.tray import build_tray, make_icon
from core.update import check as check_update
from core.version import __version__
from core.window import WindowController

ROOT = Path(__file__).resolve().parent


# ----------------------------------------------------------------------
# 基础
# ----------------------------------------------------------------------
def load_config() -> dict:
    path = ROOT / "config.yaml"
    if not path.exists():
        # 刚从 GitHub 克隆下来时只有 config.example.yaml
        example = ROOT / "config.example.yaml"
        if example.exists():
            shutil.copyfile(example, path)
            logging.getLogger("lumisphere").warning(
                "没有 config.yaml，已从 config.example.yaml 复制一份（QQ 推送默认关着）"
            )
        else:
            return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def setup_logging() -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_dir / "lumisphere.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    ]
    if sys.stdout and sys.stdout.isatty():
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )


def alert(title: str, message: str) -> None:
    """启动失败时给个看得懂的弹窗（双击运行时没有控制台）。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:
        pass


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    setup_logging()
    log = logging.getLogger("lumisphere")

    cfg = load_config()
    log.info("=" * 56)
    log.info("微光域 LumiSphere v%s 启动", __version__)

    manager = BrowserManager(cfg, ROOT)
    log.info("数据目录：%s", manager.profile_dir)

    # 登录二维码推送到 QQ
    notifier = QQNotifier(cfg.get("notify") or {})
    manager.notifier = notifier
    if notifier.ready:
        log.info("QQ 推送已启用 → %s %s", notifier.target_type, notifier.target_id)
    else:
        log.info("QQ 推送未启用（config.yaml 的 notify 段）")

    try:
        manager.start()
    except Exception as exc:  # noqa: BLE001
        log.exception("浏览器启动失败")
        alert(
            "微光域启动失败",
            f"{exc}\n\n"
            f"日志：{ROOT / 'logs' / 'lumisphere.log'}\n\n"
            "如果提示缺少 Chromium，请在本目录执行：\n"
            r".venv\Scripts\playwright.exe install chromium",
        )
        return 1

    window_ctrl = WindowController()
    # 窗口可能比进程晚一步出现，给最多 5 秒
    for _ in range(20):
        if window_ctrl.refresh(manager.pids):
            break
        time.sleep(0.25)
    log.info("已定位窗口：%s", window_ctrl.cached or "（暂未出现）")

    # 默认启动就藏起来：AI 在后台用，不挡屏幕。点托盘「显示浏览器」再叫出来。
    if (cfg.get("browser") or {}).get("start_hidden", True):
        hidden = 0
        for _ in range(12):
            hidden = window_ctrl.hide(manager.pids)
            if hidden:
                break
            time.sleep(0.3)
        if hidden:
            log.info(
                "已隐藏浏览器窗口（%d 个）—— 点托盘「显示浏览器」可以叫出来", hidden
            )
        else:
            log.info("窗口暂未出现，稍后可点托盘「隐藏浏览器」")

    server = ControlServer(cfg, manager, window_ctrl)
    server.start()

    icon = build_tray(
        manager,
        window_ctrl,
        on_quit=lambda: None,
        on_log=lambda msg: log.info(msg),
    )
    # 让 /quit 接口也能关掉托盘 → 主线程的 icon.run() 随之返回
    manager.request_quit = icon.stop

    def _on_disconnected() -> None:
        """用户把浏览器窗口关掉了：程序还在，把托盘状态改掉并提示一句。"""
        try:
            icon.icon = make_icon(False)
            icon.title = "微光域 · 浏览器已关闭（点我重新打开）"
            icon.update_menu()
        except Exception:
            log.debug("刷新托盘图标失败", exc_info=True)
        try:
            icon.notify("窗口已关闭，程序还在后台。点托盘图标可以重新打开。", "微光域")
        except Exception:
            log.debug("托盘通知失败", exc_info=True)

    manager.on_disconnected = _on_disconnected

    def _on_login_needed(info: dict) -> None:
        """需要扫码登录：二维码已经发到 QQ，这边再给个本地提示。"""
        try:
            icon.notify("有个页面要登录，二维码已发到你的 QQ。", "微光域")
        except Exception:
            log.debug("托盘通知失败", exc_info=True)
        log.info("已推送登录二维码：%s（%s）", (info.get("url") or "")[:100], info.get("reason"))

    manager.on_login_needed = _on_login_needed

    log.info("托盘已就绪，右键图标可操作")

    def _check_update_quietly() -> None:
        """开机后台问一句有没有新版，不耽误启动。"""
        try:
            info = check_update()
            if info.get("ok") and info.get("has_new"):
                log.info("发现新版本 %s（当前 %s）", info["latest"], info["current"])
                try:
                    icon.notify(
                        f"有新版本 {info['latest']}，右键托盘 →「检查更新」",
                        "微光域",
                    )
                except Exception:
                    log.debug("托盘通知失败", exc_info=True)
            elif info.get("ok"):
                log.info("已是最新版（%s）", info.get("current"))
        except Exception:
            log.debug("启动时检查更新失败", exc_info=True)

    threading.Thread(target=_check_update_quietly, daemon=True).start()

    try:
        icon.run()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C")
    finally:
        log.info("正在收尾……")
        server.stop()
        manager.shutdown()
        log.info("微光域已退出")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())