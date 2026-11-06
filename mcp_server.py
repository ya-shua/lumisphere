"""微光域 MCP 工具 —— 把她的浏览器接进 DSH，和刷视频、视觉模型并列。

域本身是个本地 HTTP 服务（127.0.0.1:8765）。这里只做一层转接，
不做任何判断：要不要开、看几帧、发给谁，都由调用它的 AI 决定。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP, Image

BASE = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 60

mcp = FastMCP("lumisphere")


def _req(path: str, payload: dict | None = None, timeout: int = DEFAULT_TIMEOUT):
    """请求微光域的本地接口。图片原样返回 bytes，其余返回 dict。"""
    url = BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "image" in ctype:
                return raw
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"微光域返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "连不上微光域（127.0.0.1:8765）。"
            "它大概没在跑——双击「启动.bat」，或看 logs/lumisphere.log。" + f"（{exc}）"
        ) from exc


# ---------------------------------------------------------------- 看 / 读


@mcp.tool()
def lumisphere_status() -> dict:
    """看微光域现在开着什么页面：url、标题、标签数、浏览器是否还连着。

    开机先调这个，确认浏览器活着，再干别的。
    """
    return _req("/status")


@mcp.tool()
def lumisphere_open(url: str) -> dict:
    """在微光域的浏览器里打开一个网址（复用同一个登录态）。

    会等到页面加载完；如果加载后落到了别的地址（比如跳登录页），
    返回里的 url 是实际落点。
    """
    return _req("/open", {"url": url}, timeout=90)


@mcp.tool()
def lumisphere_text(max_chars: int = 8000) -> dict:
    """读当前页面的正文（已去掉脚本和导航）。看长文、看视频简介用这个。"""
    return _req(f"/text?max_chars={int(max_chars)}")


@mcp.tool()
def lumisphere_search(keyword: str, num: int = 5) -> dict:
    """用域里那个浏览器去搜（带登录态，比裸抓更接近真人看到的）。

    keyword: 搜什么
    num: 要几条结果
    """
    return _req("/search", {"keyword": keyword, "num": int(num)}, timeout=90)


@mcp.tool()
def lumisphere_tabs() -> dict:
    """看当前开了哪些标签页——发现自己开了太多、想收一收的时候用。"""
    return _req("/tabs")


# ---------------------------------------------------------------- 看画面


@mcp.tool()
def lumisphere_shot():
    """截当前页面，整张图直接给你（不是路径，是画面本身）。

    看"这一页长什么样"、确认按钮位置、或者要发给人之前先看一眼。
    """
    img = _req("/screenshot")
    return Image(data=img, format="png")


@mcp.tool()
def lumisphere_video_probe() -> dict:
    """先探一下当前页有没有能播的视频：时长、分辨率、在不在播。"""
    return _req("/video/probe")


@mcp.tool()
def lumisphere_video_frames(count: int = 3, ratios: str = ""):
    """按时间比例从视频里抽帧，画面直接给你，一张一张看。

    这是"看视频"的核心：字幕告诉你它说什么，这个告诉你它拍的是什么。

    count: 抽几帧（1-6，默认 3，均匀分布）
    ratios: 想指定时间点就写比例，逗号分隔，比如 "0.1,0.5,0.9"；留空则按 count 均匀取
    """
    payload: dict = {"count": int(count)}
    if ratios.strip():
        try:
            payload["ratios"] = [float(x) for x in ratios.split(",") if x.strip()]
        except ValueError:
            raise RuntimeError("ratios 要写成逗号分隔的小数，比如 0.1,0.5,0.9") from None

    res = _req("/video/frames", payload, timeout=180)
    if not res.get("ok"):
        raise RuntimeError(res.get("reason") or "抽帧失败，这个页面上没找到视频")

    out: list[Image] = []
    for fr in res.get("frames") or []:
        try:
            with open(fr["path"], "rb") as fh:
                out.append(Image(data=fh.read(), format="jpeg"))
        except OSError:
            continue
    if not out:
        raise RuntimeError("帧抽出来了但读不到文件：" + str(res.get("dir")))
    return out


# ---------------------------------------------------------------- 登录


@mcp.tool()
def lumisphere_login_check() -> dict:
    """看看当前页是不是卡在登录页（只判断，不发送任何东西）。"""
    return _req("/login/detect")


@mcp.tool()
def lumisphere_login_notify(text: str = "") -> dict:
    """把当前页的二维码（或整页）发到她的 QQ，等人扫码。

    默认只发图。重发时建议带一句 text，比如"刚才那张过期了，这张是新的"，
    否则对方分不清哪张有效。二维码过期会自动点"刷新"再截。
    """
    return _req("/login/notify", {"text": text}, timeout=90)


@mcp.tool()
def lumisphere_login_wait(timeout: int = 120) -> dict:
    """阻塞等扫码完成。返回 logged_in 是 true 就可以接着干活。

    false 说明没扫成（多半二维码过期），重新 notify 一次即可。
    """
    t = max(5, min(300, int(timeout)))
    return _req("/login/wait", {"timeout": t}, timeout=t + 30)


# ---------------------------------------------------------------- 发给人


@mcp.tool()
def lumisphere_send_qq(text: str = "") -> dict:
    """把当前页面截图发到她的 QQ。text 留空就只发图。"""
    return _req("/shot/send", {"text": text}, timeout=90)


if __name__ == "__main__":
    mcp.run()