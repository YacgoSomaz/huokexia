"""一次性用真浏览器(Edge)铸抖音信任 cookie，供轻量 HTTP 复用以过风控验证墙。

背景：抖音对非浏览器会话（纯 requests/curl_cffi，哪怕带全套 header 或 TLS 指纹）
会返回约 6KB 的"验证码中间页"，roomId 与 m3u8 取流地址都抓不到。真浏览器会自动
跑完反爬 JS 挑战、生成 ttwid / s_v_web_id / odin_tt 等"信任 cookie"。

策略：用 playwright 驱动本机 Edge 有头开一次、过墙、把 cookie 铸出来缓存到文件；
全部房间的 HTTP 抓取复用同一套，浏览器只用这一下，风控面最小。cookie 过期或失效
时自动重铸。线程安全单例——15 个房间线程并发首取时只会触发一次浏览器。

注意：headless 会被识别导致挑战不放行，必须有头（会一闪而过弹个窗，属正常）。
"""

from __future__ import annotations

import json
import hashlib
import threading
import time

from . import config

_CACHE = config.COOKIE_CACHE
_TTL_SEC = 8 * 3600           # 缓存有效期，到点重铸
_MIN_REMINT_SEC = 60          # 强制重铸的最小间隔，避免并发线程同时开浏览器
_AUTO_RETRY_SEC = 30 * 60     # 自动铸造失败后至少半小时不再弹窗
from .fingerprint import USER_AGENT as _UA
# 必须导航到“真实房间页”才能跑完完整挑战、铸出 odin_tt 等过墙必需 cookie；
# 首页 live.douyin.com/ 铸出的 cookie 偏轻，抓房间页仍会被墙。
_LIVE_BASE = "https://live.douyin.com/"

_lock = threading.Lock()
_jar: dict[str, str] = {}
_minted_at: float = 0.0
_mint_rid: str | None = None  # 记住用哪个房间号铸，供 cookie 失效后自动重铸
_last_auto_attempt: float = 0.0


def is_authenticated(jar: dict[str, str] | None) -> bool:
    """Whether a cookie jar contains a reusable Douyin account session."""
    if not jar:
        return False
    if any(str(jar.get(key) or "").strip() for key in ("sessionid", "sessionid_ss", "sid_guard", "sid_tt")):
        return True
    return str(jar.get("passport_auth_mix_state") or "").strip().lower() in {"1", "true", "yes"}


def _session_fingerprint(jar: dict[str, str] | None) -> str:
    """Return a non-reversible identity for the account-session cookies."""
    if not jar:
        return ""
    parts = [
        f"{name}={str(jar.get(name) or '').strip()}"
        for name in ("sessionid", "sessionid_ss", "sid_guard", "sid_tt", "passport_auth_mix_state")
        if str(jar.get(name) or "").strip()
    ]
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _verified_session_matches(jar: dict[str, str] | None) -> bool:
    fingerprint = _session_fingerprint(jar)
    if not fingerprint:
        return False
    try:
        state = json.loads(config.DOUYIN_LOGIN_STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return str(state.get("session_fingerprint") or "") == fingerprint


def mark_login_verified(jar: dict[str, str] | None) -> bool:
    """Persist verification only after the user completes a visible login flow."""
    fingerprint = _session_fingerprint(jar)
    if not is_authenticated(jar) or not fingerprint:
        return False
    try:
        config.DOUYIN_LOGIN_STATE_JSON.write_text(
            json.dumps({"session_fingerprint": fingerprint, "verified_at": int(time.time())}),
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def shared_status() -> dict[str, object]:
    jar = cached_jar()
    return {
        "ok": bool(jar),
        "has_login": is_authenticated(jar) and _verified_session_matches(jar),
        "has_session_cookie": is_authenticated(jar),
        "cookie_count": len(jar),
        "minted_ts": int(_minted_at) if _minted_at else 0,
        "browser": "msedge",
    }


def store_shared_jar(jar: dict[str, str]) -> None:
    """Persist one authorized jar for dashboard, short video, and comment leads."""
    global _jar, _minted_at
    clean = {str(k): str(v) for k, v in (jar or {}).items() if k and v}
    if not clean:
        return
    with _lock:
        _jar = clean
        _minted_at = time.time()
        _save_cache(_jar, _minted_at)


def _load_cache() -> tuple[dict[str, str], float]:
    if not _CACHE.exists():
        return {}, 0.0
    try:
        d = json.loads(_CACHE.read_text(encoding="utf-8"))
        return dict(d.get("jar", {})), float(d.get("ts", 0))
    except (OSError, ValueError):
        return {}, 0.0


def _save_cache(jar: dict[str, str], ts: float) -> None:
    try:
        _CACHE.write_text(
            json.dumps({"jar": jar, "ts": ts}, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _content_safe(page) -> str:
    try:
        return page.content()
    except Exception:  # noqa: BLE001  挑战跳转途中 content() 会撞导航，忽略
        return ""


def _mint(rid: str | None) -> dict[str, str]:
    """有头开一次浏览器导航到房间页，过墙后返回 douyin.com 的 cookie 字典。失败返回 {}。"""
    import re

    from playwright.sync_api import sync_playwright

    url = _LIVE_BASE + rid if rid else _LIVE_BASE
    with sync_playwright() as p:
        browser = None
        import sys as _sys
        _channels = ({"channel": "msedge"}, {}) if _sys.platform == "win32" else ({},)
        for kw in _channels:
            try:
                browser = p.chromium.launch(headless=False, **kw)
                break
            except Exception:  # noqa: BLE001
                continue
        if browser is None:
            return {}
        try:
            ctx = browser.new_context(
                user_agent=_UA, viewport={"width": 1280, "height": 800}
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            jar: dict[str, str] = {}
            for _ in range(25):  # 等挑战跑完（roomId 出现 / 拿到 odin_tt），最多 25s
                page.wait_for_timeout(1000)
                jar = {
                    c["name"]: c["value"]
                    for c in ctx.cookies()
                    if c["domain"].endswith("douyin.com")
                }
                passed = "odin_tt" in jar or re.search(r'roomId..:..(\d+)', _content_safe(page))
                if "ttwid" in jar and passed:
                    break
            return jar
        finally:
            browser.close()


def get_jar(force: bool = False, mint_rid: str | None = None) -> dict[str, str]:
    """返回信任 cookie 字典；过期/缺失/force 时重铸。线程安全，串行铸造。

    mint_rid：铸造时导航到的房间号；第一个调用方（房间线程）传入自己的 live_id 即可，
    之后记住，cookie 失效自动重铸时复用同一房间。
    """
    global _jar, _minted_at, _mint_rid
    with _lock:
        if mint_rid:
            _mint_rid = mint_rid
        if not _jar:
            _jar, _minted_at = _load_cache()
        age = time.time() - _minted_at
        need = force or not _jar or age > _TTL_SEC
        # 并发线程在 cookie 失效后会同时 force；刚铸过就别重复开浏览器
        if force and _jar and age < _MIN_REMINT_SEC:
            need = False
        if need:
            new = _mint(_mint_rid)
            if new.get("ttwid"):
                _jar, _minted_at = new, time.time()
                _save_cache(_jar, _minted_at)
        return dict(_jar)


def cookie_header(force: bool = False) -> str:
    """拼成可直接塞进请求头 cookie 字段的字符串。"""
    return "; ".join(f"{k}={v}" for k, v in get_jar(force=force).items())


def cached_jar() -> dict[str, str]:
    """Return the cached trust cookie without ever opening a browser."""
    global _jar, _minted_at
    with _lock:
        if not _jar:
            _jar, _minted_at = _load_cache()
        return dict(_jar)


def cached_cookie_header() -> str:
    """Build a header from cached cookies only; safe for background workers."""
    return "; ".join(f"{k}={v}" for k, v in cached_jar().items())


def auto_refresh(mint_rid: str | None = None, *, force: bool = False) -> dict[str, object]:
    """Bounded unattended cookie self-healing.

    A healthy cached cookie causes no browser activity. Missing/expired cookies, or an
    explicit challenge-triggered ``force``, get one headed-browser attempt. If that
    attempt fails, all background callers share a long cooldown so the browser cannot
    repeatedly pop up. Manual ``remint`` deliberately bypasses this cooldown.
    """
    global _jar, _minted_at, _mint_rid, _last_auto_attempt
    with _lock:
        if mint_rid:
            _mint_rid = mint_rid
        if not _jar:
            _jar, _minted_at = _load_cache()
        now = time.time()
        age = now - _minted_at
        if not force and _jar and age <= _TTL_SEC:
            return {"ok": True, "attempted": False, "refreshed": False, "message": "信任 Cookie 可用"}
        if _last_auto_attempt and now - _last_auto_attempt < _AUTO_RETRY_SEC:
            return {
                "ok": bool(_jar),
                "attempted": False,
                "refreshed": False,
                "message": "自动验证冷却中，暂不重复打开浏览器",
            }
        _last_auto_attempt = now
        new = _mint(_mint_rid)
        if not new.get("ttwid"):
            return {
                "ok": bool(_jar),
                "attempted": True,
                "refreshed": False,
                "message": "自动验证未完成，已进入冷却",
            }
        _jar, _minted_at = new, time.time()
        _save_cache(_jar, _minted_at)
        return {"ok": True, "attempted": True, "refreshed": True, "message": "信任 Cookie 已自动更新"}


def remint(mint_rid: str | None = None) -> dict[str, object]:
    """User-triggered trust-cookie refresh; keeps the old cookie if minting fails."""
    global _jar, _minted_at, _mint_rid, _last_auto_attempt
    with _lock:
        if mint_rid:
            _mint_rid = mint_rid
        new = _mint(_mint_rid)
        if not new.get("ttwid"):
            return {"ok": False, "message": "未取得信任 Cookie，请完成浏览器验证后重试"}
        _jar, _minted_at = new, time.time()
        _last_auto_attempt = 0.0
        _save_cache(_jar, _minted_at)
        return {"ok": True, "message": "信任 Cookie 已更新", "minted_ts": int(_minted_at)}


def ttwid(force: bool = False) -> str:
    return get_jar(force=force).get("ttwid", "")
