"""Win32 窗口控制：找到微光域浏览器的窗口并显示 / 隐藏。

Chromium / Edge 是多进程架构，主进程往往不持有窗口句柄 ——
窗口可能挂在某个子进程上。所以这里按「本次启动产生的一整批 pid」来找，
而不是只认一个主 pid。

全程走 Win32 API，不依赖 UI 自动化，所以哪怕窗口被藏到任务栏外、
或用户正在别的程序里打字，也不会抢焦点、不会误触。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

import win32con
import win32gui
import win32process

log = logging.getLogger(__name__)


def _normalize(pids: int | Iterable[int] | None) -> set[int]:
    if pids is None:
        return set()
    if isinstance(pids, int):
        return {pids}
    return {int(p) for p in pids}


def find_window_handles(pids: int | Iterable[int] | None) -> list[int]:
    """返回这些进程当前所有「可见且有标题」的顶层窗口句柄。"""
    targets = _normalize(pids)
    if not targets:
        return []

    handles: list[int] = []

    def _enum(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid not in targets:
                return True
            if win32gui.GetWindowText(hwnd):
                handles.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_enum, None)
    return handles


class WindowController:
    """记住浏览器窗口句柄。

    窗口一旦被隐藏，EnumWindows 就再也看不见它了，所以必须在隐藏前
    把句柄缓存下来，否则「显示」会失效。
    """

    def __init__(self) -> None:
        self._hwnds: list[int] = []

    # ---------- 对外 ----------
    def refresh(self, pids: int | Iterable[int] | None) -> list[int]:
        found = find_window_handles(pids)
        if found:
            self._hwnds = found
        return list(self._hwnds)

    def show(self, pids: int | Iterable[int] | None) -> int:
        handles = self.refresh(pids) or list(self._hwnds)
        shown = 0
        for hwnd in handles:
            try:
                if not win32gui.IsWindow(hwnd):
                    continue
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    # 前台权限被系统拒绝是正常的，不影响显示
                    pass
                shown += 1
            except Exception:
                log.debug("显示窗口失败 hwnd=%s", hwnd, exc_info=True)
        return shown

    def hide(self, pids: int | Iterable[int] | None) -> int:
        # 总是先刷新：Edge 启动后可能重建窗口，旧句柄会失效
        self.refresh(pids)
        if not self._hwnds:
            return 0
        hidden = 0
        for hwnd in list(self._hwnds):
            try:
                if not win32gui.IsWindow(hwnd):
                    continue
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                hidden += 1
            except Exception:
                log.debug("隐藏窗口失败 hwnd=%s", hwnd, exc_info=True)
        return hidden

    def is_visible(self, pids: int | Iterable[int] | None) -> bool:
        return bool(find_window_handles(pids))

    @property
    def cached(self) -> list[int]:
        return list(self._hwnds)