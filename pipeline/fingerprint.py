"""集中管理浏览器指纹：UA、sec-ch-ua、sec-fetch-* 等安全头。

所有对抖音的 HTTP / WebSocket 请求统一从这里取头，保证 UA 版本、sec-ch-ua、
sec-fetch-* 三者一致，避免不同模块各写一份导致指纹矛盾被风控识别。
"""

from __future__ import annotations

import random
import sys

# ---- 版本号 ----
_CHROME_MAJOR = "140"
_EDGE_MAJOR = "140"
_FULL_VER = f"{_CHROME_MAJOR}.0.0.0"

# ---- 平台自适应 ----
_IS_MAC = sys.platform == "darwin"

if _IS_MAC:
    USER_AGENT = (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_FULL_VER} Safari/537.36 Edg/{_FULL_VER}"
    )
    _SEC_PLATFORM = '"macOS"'
else:
    USER_AGENT = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_FULL_VER} Safari/537.36 Edg/{_FULL_VER}"
    )
    _SEC_PLATFORM = '"Windows"'

SEC_CH_UA = (
    f'"Microsoft Edge";v="{_EDGE_MAJOR}", '
    f'"Chromium";v="{_CHROME_MAJOR}", '
    f'"Not-A.Brand";v="24"'
)

# ---- 房间页 GET 请求的完整头（模拟浏览器首次导航） ----
PAGE_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # requests does not reliably decode brotli/zstd in this packaged runtime.
    # If we advertise them, Douyin may return compressed bytes that look like
    # successful HTML but contain no parseable roomStore/roomId fields.
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": _SEC_PLATFORM,
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
}

# ---- API / XHR 请求头 ----
API_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": _SEC_PLATFORM,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-requested-with": "XMLHttpRequest",
}

# ---- WebSocket 握手头 ----
WS_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Origin": "https://live.douyin.com",
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": _SEC_PLATFORM,
    "sec-fetch-dest": "websocket",
    "sec-fetch-mode": "websocket",
    "sec-fetch-site": "cross-site",
    "sec-websocket-extensions": "permessage-deflate; client_max_window_bits",
}

# ---- WSS 服务器端点池（随机选取，分散流量） ----
_WSS_HOSTS = [
    "webcast3-ws-web-lq.douyin.com",
    "webcast5-ws-web-lq.douyin.com",
    "webcast100-ws-web-lq.douyin.com",
    "webcast3-ws-web-hl.douyin.com",
    "webcast5-ws-web-hl.douyin.com",
]


def pick_wss_host() -> str:
    """随机选取一个 WSS 端点，分散连接避免同一端点高频。"""
    return random.choice(_WSS_HOSTS)


# ---- WSS URL browser_version 参数（必须与 UA 一致） ----
if _IS_MAC:
    WSS_BROWSER_VERSION = (
        f"5.0%20(Macintosh;%20Intel%20Mac%20OS%20X%2010_15_7)%20AppleWebKit/537.36%20"
        f"(KHTML,%20like%20Gecko)%20Chrome/{_FULL_VER}%20Safari/537.36"
    )
else:
    WSS_BROWSER_VERSION = (
        f"5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20"
        f"(KHTML,%20like%20Gecko)%20Chrome/{_FULL_VER}%20Safari/537.36"
    )
